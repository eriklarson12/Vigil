"""Live-model ranking eval (roadmap R17). Manual; never run in CI.

CI replays canned responses, so it cannot say what the model adds over the heuristics.
This builds the production ranking prompt for every scenario, sends it to the live model
`--runs` times, and reports heuristic-only top-1 against heuristic+model top-1, the
false-culprit rate on the scenarios with no single culprit, and agreement across runs.

    ./scripts/safe-run.sh --llm-live uv run python scripts/eval_ranking.py
    ./scripts/safe-run.sh --llm-live uv run python scripts/eval_ranking.py --record-fixtures
    ./scripts/safe-run.sh --llm-live uv run python scripts/eval_ranking.py --fill --record-fixtures

safe-run pins the local database, which holds the daily budget counter. Each call spends
one unit of it and one request of the real Gemini free-tier quota, which production shares.
The free tier allows 20 requests a day, so a full run of 30 calls spans two Pacific days:
the first run records what it can, and `--fill` re-runs only the errored or missing calls,
keeping the rest. A fill refuses when the prompt, model, or confidence floor has changed
since the kept runs, because the report would then mix two experiments.

The pipeline is mirrored, not imported as a graph: same scorer, same floor and top-k, same
prompt builder, same sha guards. A scenario whose candidates all score under the floor
gets no call, exactly as in production, and is reported as gated.

The fallback model is pinned to the primary for the run, so every number is attributable
to one model. A 429 still retries once after the client's backoff.
"""

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vigil.commits.github import load_fixture_commits  # noqa: E402
from vigil.commits.schemas import CommitAnalysis  # noqa: E402
from vigil.commits.scoring import score_commits  # noqa: E402
from vigil.config import get_settings  # noqa: E402
from vigil.db.pool import apply_migrations, create_pool, open_pool_with_retry  # noqa: E402
from vigil.graph.triage import apply_sha_guards  # noqa: E402
from vigil.impact.catalog import ServiceCatalog  # noqa: E402
from vigil.llm.budget import BudgetExhausted  # noqa: E402
from vigil.llm.client import GeminiClient, LLMUnavailable  # noqa: E402
from vigil.llm.prompts import SCENARIO_LABEL, SHORT_SHA, build_commit_ranking_prompt  # noqa: E402

SCENARIOS_DIR = ROOT / "simulator" / "scenarios"
GITHUB_DIR = ROOT / "tests" / "fixtures" / "github"
LLM_DIR = ROOT / "tests" / "fixtures" / "llm"
OUT = ROOT / "tests" / "fixtures" / "eval" / "ranking.json"

# Matches tests/conftest.py, so the commit times the model sees are the ones CI scores.
ALERT_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
# The day Google's free-tier quota counts, and so the day a run is dated by.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")


def _short(sha: str | None) -> str:
    return sha[:SHORT_SHA] if sha else "-"


def _case(name: str, settings, catalog: ServiceCatalog) -> dict[str, Any]:
    """Everything triage would hold just before `rank_commits_llm`."""
    scenario = json.loads((SCENARIOS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    planted = json.loads((GITHUB_DIR / f"{name}.json").read_text(encoding="utf-8"))["culprit"]
    labels = {k: v for k, v in scenario["alert"]["labels"].items() if k != SCENARIO_LABEL}
    alert = {**scenario["alert"], "labels": labels, "starts_at": ALERT_TIME.isoformat()}
    service = catalog.get(labels["service"])

    commits = load_fixture_commits(settings, name, ALERT_TIME)
    deploys = [
        {
            "service": d["service"],
            "commit_shas": d["commit_shas"],
            "finished_at": ALERT_TIME - timedelta(minutes=d["minutes_before_alert"]),
        }
        for d in scenario.get("deploys", [])
    ]
    scores = score_commits(
        commits,
        service=service["name"],
        path_globs=service.get("path_globs", []),
        shared_globs=catalog.shared_globs,
        deploys=deploys,
        starts_at=ALERT_TIME,
    )
    top = [s for s in scores if s["score"] >= settings.commit_score_floor][: settings.commit_top_k]
    by_sha = {c["sha"]: dict(c, committed_at=c["committed_at"].isoformat()) for c in commits}
    candidates = [{"score": s["score"], "commit": by_sha[s["sha"]]} for s in top]
    return {
        "name": name,
        "planted": planted,
        "alert": alert,
        "service": service,
        "top": top,
        "candidates": candidates,
        "n_commits": len(commits),
    }


def _expand(analysis: CommitAnalysis, top: list[dict[str, Any]]) -> tuple[CommitAnalysis, int]:
    """The raw answer with short shas widened to the candidates' full ones.

    Unlike `apply_sha_guards` this keeps the model's culprit even under the confidence
    floor, which is what a fixture must hold for the guard to be exercised at replay.
    Returns the count of verdicts naming no candidate, which are dropped.
    """
    by_prefix = {s["sha"][:SHORT_SHA]: s["sha"] for s in top}
    kept = [
        v.model_copy(update={"sha": by_prefix[v.sha[:SHORT_SHA]]})
        for v in analysis.verdicts
        if v.sha[:SHORT_SHA] in by_prefix
    ]
    culprit = by_prefix.get((analysis.likely_culprit_sha or "")[:SHORT_SHA])
    expanded = CommitAnalysis(
        verdicts=kept, likely_culprit_sha=culprit, no_culprit_reason=analysis.no_culprit_reason
    )
    return expanded, len(analysis.verdicts) - len(kept)


class _Pacer:
    """Spaces every call after the first by `pause` seconds (free-tier RPM)."""

    def __init__(self, pause: float):
        self.pause = pause
        self.first = True

    async def wait(self) -> None:
        if not self.first:
            await asyncio.sleep(self.pause)
        self.first = False


def _prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n\n{user}".encode()).hexdigest()[:16]


async def _call(case, client, settings, system: str, user: str) -> dict[str, Any]:
    date = datetime.now(QUOTA_TZ).date().isoformat()
    try:
        raw = await client.generate_structured(system, user, CommitAnalysis, "commit_ranking")
    except (LLMUnavailable, BudgetExhausted) as exc:
        return {"date": date, "error": f"{type(exc).__name__}: {exc}"[:300]}
    guarded = apply_sha_guards(raw, case["top"], settings.confidence_floor)
    expanded, invented = _expand(raw, case["top"])
    named = expanded.likely_culprit_sha
    return {
        "date": date,
        "culprit": guarded.likely_culprit_sha,
        "model_named": named,
        "model_confidence": max(
            (v.confidence for v in expanded.verdicts if v.sha == named), default=None
        ),
        "invented_verdicts": invented,
        "analysis": expanded.model_dump(),
    }


def _pending(case, runs: int, prior: dict[str, Any] | None) -> int:
    if not case["top"]:
        return 0
    kept = [x for x in (prior or {}).get("runs", [])[:runs] if "error" not in x]
    return runs - len(kept)


async def _run_case(
    case, client, settings, runs: int, pacer: _Pacer, prior: dict[str, Any] | None
) -> dict[str, Any]:
    """Fill every errored or missing run; a run that succeeded in `prior` is kept as is."""
    heuristic = case["top"][0]["sha"] if case["top"] else None
    result: dict[str, Any] = {
        "planted_culprit": case["planted"],
        "commits_in_window": case["n_commits"],
        "candidates_shown": len(case["top"]),
        "heuristic_top1": heuristic,
        "heuristic_top1_score": round(case["top"][0]["score"], 4) if case["top"] else None,
        "gated": not case["top"],
        "prompt_sha256": None,
        "runs": [],
    }
    if not case["top"]:
        return result

    system, user = build_commit_ranking_prompt(case["alert"], case["service"], case["candidates"])
    result["prompt_sha256"] = _prompt_hash(system, user)
    slots: list[dict[str, Any] | None] = list((prior or {}).get("runs", []))[:runs]
    if prior and slots and prior.get("prompt_sha256") != result["prompt_sha256"]:
        raise SystemExit(
            f"{case['name']}: the ranking prompt changed since the kept runs. A fill would mix"
            " two prompts in one report; run without --fill."
        )
    slots += [None] * (runs - len(slots))
    for i, run in enumerate(slots):
        if run is not None and "error" not in run:
            continue
        await pacer.wait()
        slots[i] = await _call(case, client, settings, system, user)
    result["runs"] = slots
    return result


def _summarize(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    positives = {n: r for n, r in results.items() if r["planted_culprit"]}
    negatives = {n: r for n, r in results.items() if not r["planted_culprit"]}

    def ok_runs(r):
        return [x for x in r["runs"] if "error" not in x]

    model_hits = model_runs = 0
    for r in positives.values():
        for x in ok_runs(r):
            model_runs += 1
            model_hits += x["culprit"] == r["planted_culprit"]
    neg_false = neg_runs = 0
    for r in negatives.values():
        if r["gated"]:
            continue
        for x in ok_runs(r):
            neg_runs += 1
            neg_false += x["culprit"] is not None

    called = {n: r for n, r in results.items() if not r["gated"]}
    unanimous = sum(
        1 for r in called.values()
        if len(ok_runs(r)) == len(r["runs"]) and len({x["culprit"] for x in r["runs"]}) == 1
    )
    return {
        "positive_scenarios": len(positives),
        "heuristic_top1_correct": sum(
            r["heuristic_top1"] == r["planted_culprit"] for r in positives.values()
        ),
        "model_top1_correct_runs": model_hits,
        "model_top1_runs": model_runs,
        "negative_scenarios": len(negatives),
        "negative_gated": sum(r["gated"] for r in negatives.values()),
        "heuristic_false_culprits": sum(r["heuristic_top1"] is not None for r in negatives.values()),
        "model_false_culprit_runs": neg_false,
        "model_negative_runs": neg_runs,
        "scenarios_called": len(called),
        "scenarios_unanimous": unanimous,
        "errored_runs": sum(len(r["runs"]) - len(ok_runs(r)) for r in results.values()),
        "invented_verdicts": sum(x.get("invented_verdicts", 0) for r in results.values() for x in r["runs"]),
    }


def _print_report(model: str, results: dict[str, dict[str, Any]], summary: dict[str, Any]) -> None:
    print(f"\nranking eval: {model}, {len(results)} scenarios\n")
    print(f"  {'scenario':22} {'planted':11} {'heuristic':11} runs (culprit after guards, model conf)")
    for name, r in results.items():
        runs = "gated: no candidate above the floor, no call" if r["gated"] else "  ".join(
            "error" if "error" in x else
            f"{_short(x['culprit'])}{'*' if x['culprit'] == r['planted_culprit'] else ''}"
            f"({x['model_confidence'] if x['model_confidence'] is not None else '-'})"
            for x in r["runs"]
        )
        print(f"  {name:22} {_short(r['planted_culprit']):11} {_short(r['heuristic_top1']):11} {runs}")
    s = summary
    print(
        f"\n  heuristic-only top-1        {s['heuristic_top1_correct']}/{s['positive_scenarios']} scenarios"
        f"\n  heuristic+model top-1       {s['model_top1_correct_runs']}/{s['model_top1_runs']} runs"
        f"\n  false culprit, heuristic    {s['heuristic_false_culprits']}/{s['negative_scenarios']}"
        " negative scenarios"
        f"\n  false culprit, model        {s['model_false_culprit_runs']}/{s['model_negative_runs']} runs"
        f" ({s['negative_gated']} negative scenario(s) gated before the call)"
        f"\n  unanimous across runs       {s['scenarios_unanimous']}/{s['scenarios_called']} scenarios called"
        f"\n  errored runs {s['errored_runs']}, invented verdicts {s['invented_verdicts']}"
    )


def _record_fixtures(results: dict[str, dict[str, Any]]) -> None:
    """Overwrite each called scenario's ranking fixture with its first successful run."""
    for name, r in results.items():
        run = next((x for x in r["runs"] if "error" not in x), None)
        if run is None:
            if not r["gated"]:
                print(f"  {name}: no successful run, fixture left as it was")
            continue
        path = LLM_DIR / f"commit_ranking.{name}.json"
        path.write_text(json.dumps(run["analysis"], indent=2) + "\n", encoding="utf-8")
        print(f"  wrote {path.relative_to(ROOT)}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--pause", type=float, default=7.0, help="seconds between calls (free-tier RPM)")
    parser.add_argument("--scenario", action="append", help="limit to one scenario; repeatable")
    parser.add_argument(
        "--record-fixtures", action="store_true", help="re-record commit_ranking fixtures from run 1"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the candidates and call count, call nothing"
    )
    parser.add_argument(
        "--fill", action="store_true", help="keep the successful runs in the report; redo the rest"
    )
    args = parser.parse_args()

    settings = get_settings()
    if not args.dry_run and (settings.llm_mode != "gemini" or not settings.gemini_api_key):
        raise SystemExit("needs the live model: run through ./scripts/safe-run.sh --llm-live")
    settings = settings.model_copy(update={"gemini_fallback_model": settings.gemini_model})

    names = sorted(p.stem for p in GITHUB_DIR.glob("*.json"))
    if args.scenario:
        unknown = set(args.scenario) - set(names)
        if unknown:
            raise SystemExit(f"unknown scenario(s): {sorted(unknown)}")
        names = sorted(args.scenario)

    prior: dict[str, Any] = {}
    if args.fill:
        if not OUT.exists():
            raise SystemExit(f"--fill needs an earlier report at {OUT.relative_to(ROOT)}")
        prior = json.loads(OUT.read_text(encoding="utf-8"))
        for key, now in (("model", settings.gemini_model), ("confidence_floor", settings.confidence_floor)):
            if prior.get(key) != now:
                raise SystemExit(f"--fill: {key} is {now!r}, the kept runs used {prior.get(key)!r}")
    kept = prior.get("scenarios", {})

    catalog = ServiceCatalog.load(settings.services_file)
    cases = [_case(n, settings, catalog) for n in names]
    calls = sum(_pending(c, args.runs, kept.get(c["name"])) for c in cases)
    print(f"{settings.gemini_model}: {len(cases)} scenarios, {calls} calls, {args.pause}s apart")
    if args.dry_run:
        for c in cases:
            shown = ", ".join(f"{_short(s['sha'])} {s['score']:.2f}" for s in c["top"]) or "gated"
            print(f"  {c['name']:22} planted {_short(c['planted']):11} shown: {shown}")
        return 0

    results = dict(kept)
    pool = create_pool(settings.database_url)
    await open_pool_with_retry(pool)
    try:
        await apply_migrations(pool)
        client = GeminiClient(settings, pool)
        pacer = _Pacer(args.pause)
        for case in cases:
            results[case["name"]] = await _run_case(
                case, client, settings, args.runs, pacer, kept.get(case["name"])
            )
            print(f"  {case['name']}: done")
    finally:
        await pool.close()
    results = dict(sorted(results.items()))

    summary = _summarize(results)
    _print_report(settings.gemini_model, results, summary)

    if args.scenario and not args.fill:
        print("\n--scenario run: nothing written")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "model": settings.gemini_model,
        "dates": sorted({x["date"] for r in results.values() for x in r["runs"]}),
        "runs_per_scenario": args.runs,
        "temperature": 0.2,
        "commit_score_floor": settings.commit_score_floor,
        "commit_top_k": settings.commit_top_k,
        "confidence_floor": settings.confidence_floor,
        "summary": summary,
        "scenarios": results,
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    if args.record_fixtures:
        _record_fixtures(results)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
