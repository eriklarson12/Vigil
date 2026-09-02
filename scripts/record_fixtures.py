"""Record the demo repo's real shas back into every fixture that names a commit (roadmap R2).

`build_demo_repo.py` turns the invented 10-character shas into real 40-character ones. This
propagates that rename everywhere it has to land, which is wider than it looks:

- `tests/fixtures/github/*.json`  — re-read from the repo, so the shas are the repo's truth.
- `tests/fixtures/llm/*.json`     — recorded LLM responses name commits in `verdicts[].sha`,
                                    `likely_culprit_sha`, and in prose.
- `simulator/scenarios/*.json`    — `deploys[].commit_shas`, which drive `f_deploy`.

Getting this wrong fails *silently*. `rank_commits_llm` drops verdicts whose sha is not among
the scored candidates (the spec §6.3 anti-hallucination guard), so a half-applied rename
produces an empty verdict list and `culprit=None` — indistinguishable from an honest "no
culprit found". Hence the round-trip guard below: nothing is written unless every declared
culprit still scores rank 1 above the LLM gate floor.

    # before pushing: read the built repo straight off disk
    uv run python scripts/record_fixtures.py --from-local /tmp/vigil-demo-shop --dry-run

    # after pushing: read it back through the real GitHub client
    GITHUB_TOKEN=<read-only PAT> uv run python scripts/record_fixtures.py \
        --repo eriklarson12/vigil-demo-shop
"""

import argparse
import asyncio
import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

GITHUB_DIR = ROOT / "tests" / "fixtures" / "github"
LLM_DIR = ROOT / "tests" / "fixtures" / "llm"
SCENARIOS_DIR = ROOT / "simulator" / "scenarios"

LLM_GATE_FLOOR = 0.15  # spec §6.3: below this the candidate never reaches the ranking call
SHORT = 10  # what blocks.py renders, and the width of the shas being replaced


# --------------------------------------------------------------------------- sources


def _from_local(repo: pathlib.Path, anchor: datetime, lookback: int) -> list[dict[str, Any]]:
    """Same window and shape as `_fetch_live`, read straight from a local clone.

    Lets the whole round-trip be verified before the repo is pushed anywhere.
    """
    since = (anchor - timedelta(hours=lookback)).isoformat()
    listing = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%H", f"--since={since}", f"--until={anchor.isoformat()}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()

    commits = []
    for sha in listing:
        # NUL-delimit through the end of the body: a commit message has its own blank line
        # between subject and body, so splitting on that would drop everything after it —
        # including the `This reverts commit <sha>` line the revert bonus is parsed from.
        meta = subprocess.run(
            ["git", "-C", str(repo), "show", "--format=%an%x00%cI%x00%B%x00", "--numstat", sha],
            capture_output=True, text=True, check=True,
        ).stdout
        author, committed, message, rest = meta.split("\x00", 3)
        files = []
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit():
                files.append({"path": parts[2], "additions": int(parts[0]), "deletions": int(parts[1])})
        patch = subprocess.run(
            ["git", "-C", str(repo), "show", "--format=", "-U3", sha],
            capture_output=True, text=True, check=True,
        ).stdout
        commits.append(
            {
                "sha": sha,
                "message": message.strip(),
                "author": author,
                "committed_at": datetime.fromisoformat(committed),
                "files": files,
                "patch": patch,
            }
        )
    return commits


async def _from_github(repo: str, anchor: datetime) -> list[dict[str, Any]]:
    from vigil.commits.github import _fetch_live
    from vigil.config import get_settings

    settings = get_settings()
    if not settings.github_token:
        raise SystemExit(
            "GITHUB_TOKEN is required: recording is 11 listings plus 46 detail calls, "
            "against an unauthenticated ceiling of 60/hour."
        )
    return await _fetch_live(repo, settings, anchor)


# --------------------------------------------------------------------------- rewriting


def _fixture_commits(
    scenario: str, fetched: list[dict[str, Any]], shas: dict[str, str], anchor: datetime
) -> list[dict[str, Any]]:
    """Rebuild the fixture's commit list in its original order, with real shas."""
    original = json.loads((GITHUB_DIR / f"{scenario}.json").read_text(encoding="utf-8"))["commits"]
    by_sha = {c["sha"]: c for c in fetched}

    out = []
    for commit in original:
        real = by_sha[shas[commit["sha"]]]
        hours = (anchor - real["committed_at"]).total_seconds() / 3600.0
        out.append(
            {
                "sha": real["sha"],
                "message": real["message"],
                "author": real["author"],
                "hours_before_alert": round(hours, 4),
                "files": real["files"],
                "patch": real["patch"],
            }
        )
    return out


def _substitute(value: Any, full: dict[str, str], key: str | None = None) -> Any:
    """Apply the rename. Sha-valued keys get all 40 characters; prose gets 10.

    The full width matters: `v.sha in valid` is exact set membership, so a truncated sha in
    a verdict is dropped as a hallucination rather than flagged as a mistake.
    """
    if isinstance(value, dict):
        return {k: _substitute(v, full, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, full, key) for v in value]
    if isinstance(value, str):
        if key in ("sha", "likely_culprit_sha", "culprit") or key == "commit_shas":
            return full.get(value, value)
        for old, new in full.items():
            value = value.replace(old, new[:SHORT])
        return value
    return value


# --------------------------------------------------------------------------- guard


def _round_trip(fixtures: dict[str, dict], scenarios: dict[str, dict]) -> list[str]:
    """Re-score the rewritten fixtures. Returns a list of failures, empty when clean."""
    from vigil.commits.scoring import score_commits
    from vigil.impact.catalog import ServiceCatalog

    catalog = ServiceCatalog.load(ROOT / "services.yaml")
    alert_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    failures = []
    for scenario, fixture in fixtures.items():
        culprit = fixture.get("culprit")
        if not culprit:
            continue
        service = scenarios[scenario]["alert"]["labels"]["service"]
        commits = [
            {**c, "committed_at": alert_time - timedelta(hours=c["hours_before_alert"])}
            for c in fixture["commits"]
        ]
        deploys = [
            {
                "service": d["service"],
                "commit_shas": d["commit_shas"],
                "finished_at": alert_time - timedelta(minutes=d["minutes_before_alert"]),
            }
            for d in scenarios[scenario].get("deploys", [])
        ]
        cfg = catalog.get(service)
        scores = score_commits(
            commits, service=service, path_globs=cfg["path_globs"],
            shared_globs=catalog.shared_globs, deploys=deploys, starts_at=alert_time,
        )
        if scores[0]["sha"] != culprit:
            failures.append(f"{scenario}: culprit {culprit[:SHORT]} fell to rank "
                            f"{next(i for i, s in enumerate(scores, 1) if s['sha'] == culprit)}")
        elif scores[0]["score"] < LLM_GATE_FLOOR:
            failures.append(f"{scenario}: culprit scores {scores[0]['score']:.3f}, below the "
                            f"{LLM_GATE_FLOOR} gate floor")
    return failures


# --------------------------------------------------------------------------- main


def record(manifest: dict, fetch, dry_run: bool) -> int:
    full: dict[str, str] = {}
    for entry in manifest.values():
        full.update(entry["shas"])

    fixtures: dict[str, dict] = {}
    for scenario, entry in manifest.items():
        anchor = datetime.fromisoformat(entry["anchor"])
        fetched = fetch(anchor)
        expected = set(entry["shas"].values())
        missing = expected - {c["sha"] for c in fetched}
        if missing:
            raise SystemExit(
                f"{scenario}: {len(missing)} manifest commit(s) absent from the fetch window "
                f"[{anchor - timedelta(hours=48)}, {anchor}] — rebuild and re-record together."
            )
        fixtures[scenario] = {
            "culprit": entry["culprit"],
            "commits": _fixture_commits(scenario, fetched, entry["shas"], anchor),
        }

    scenarios = {
        p.stem: _substitute(json.loads(p.read_text(encoding="utf-8")), full)
        for p in sorted(SCENARIOS_DIR.glob("*.json"))
    }
    llm = {
        p.name: _substitute(json.loads(p.read_text(encoding="utf-8")), full)
        for p in sorted(LLM_DIR.glob("*.json"))
    }

    failures = _round_trip(fixtures, scenarios)
    if failures:
        print("round-trip guard failed — nothing written:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(f"round-trip clean: every declared culprit still ranks 1 above {LLM_GATE_FLOOR}")
    if dry_run:
        print(f"dry run — would rewrite {len(fixtures)} commit fixtures, {len(llm)} LLM "
              f"fixtures, {len(scenarios)} scenarios")
        return 0

    for scenario, fixture in fixtures.items():
        (GITHUB_DIR / f"{scenario}.json").write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    for name, data in llm.items():
        (LLM_DIR / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for scenario, data in scenarios.items():
        (SCENARIOS_DIR / f"{scenario}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"rewrote {len(fixtures)} commit fixtures, {len(llm)} LLM fixtures, "
          f"{len(scenarios)} scenarios")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="owner/name of the pushed demo repo, read via the GitHub API")
    source.add_argument("--from-local", type=pathlib.Path, help="path to the built repo, read via git")
    parser.add_argument("--manifest", type=pathlib.Path, default=ROOT / "build" / "manifest.json")
    parser.add_argument("--dry-run", action="store_true", help="run the guard, write nothing")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))

    if args.repo:
        def fetch(anchor):
            return asyncio.run(_from_github(args.repo, anchor))
    else:
        from vigil.config import get_settings

        lookback = get_settings().commit_lookback_hours

        def fetch(anchor):
            return _from_local(args.from_local, anchor, lookback)

    return record(manifest, fetch, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
