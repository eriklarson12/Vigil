"""Backtest the commit scorer on real reverts (roadmap R18).

A `Revert "..."` commit whose body says `This reverts commit <sha>` is a free label: the
commit it reverts caused a problem someone noticed. Each such revert becomes a case. The
anchor is the revert's commit time, the candidates are the commits in the lookback window
before it, and the label is the reverted sha. `score_commits` ranks the candidates with no
service catalog (every file matches, so `f_path` is 1 for all) and no deploys.

    git clone --bare --single-branch --no-tags --shallow-since=2024-01-01 <url> go.git
    uv run python scripts/backtest_reverts.py --repo go.git --repo react.git ... \
        --until 2026-11-25 --out tests/fixtures/eval/backtest.json

Reads local git history only. It builds no Settings, so it touches no database, model or
Slack, and needs no safe-run.sh.
"""

import argparse
import bisect
import json
import pathlib
import re
import statistics
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vigil.commits.github import MAX_COMMITS  # noqa: E402
from vigil.commits.scoring import REVERT_RE, REVERTED_SHA_RE, score_commits  # noqa: E402
from vigil.config import Settings  # noqa: E402

LOOKBACK_HOURS: int = Settings.model_fields["commit_lookback_hours"].default
METHODS = ("scorer", "most_recent", "largest_diff")
RECORD_SEP = "\x1e"


def _git(repo: pathlib.Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin, capture_output=True, text=True, check=True,
    ).stdout


def load_history(repo: pathlib.Path, until: datetime | None) -> list[dict[str, Any]]:
    """Every non-merge commit on HEAD, newest first, without the shallow boundary.

    A shallow boundary commit has no parent here, so its diff is the whole tree.
    """
    shallow_file = pathlib.Path(_git(repo, "rev-parse", "--git-path", "shallow").strip())
    if not shallow_file.is_absolute():
        shallow_file = repo / shallow_file
    boundary = set(shallow_file.read_text().split()) if shallow_file.exists() else set()
    args = ["log", "--no-merges", f"--format={RECORD_SEP}%H%x00%ct%x00%B", "HEAD"]
    if until:
        args.insert(2, f"--until={int(until.timestamp())}")
    commits = []
    for record in _git(repo, *args).split(RECORD_SEP)[1:]:
        sha, ct, message = record.split("\x00", 2)
        if sha in boundary:
            continue
        commits.append({
            "sha": sha,
            "committed_at": datetime.fromtimestamp(int(ct), UTC),
            "message": message.strip(),
        })
    return commits


# How a revert names what it reverts. `git revert` writes the sha; GitHub's revert button
# writes the PR number, and squash merges end their subject with it; Gerrit (Go) writes
# the CL number, which the reverted commit carries in its Reviewed-on trailer.
REVERTED_PR_RE = re.compile(r"\bReverts [\w.-]+/[\w.-]+#(\d+)")
REVERTED_CL_RE = re.compile(r"\bThis reverts CL (\d+)", re.IGNORECASE)
SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
REVIEWED_ON_RE = re.compile(r"^Reviewed-on: \S+/\+/(\d+)\s*$", re.MULTILINE)


def _is_revert(commit: dict[str, Any]) -> bool:
    return bool(REVERT_RE.search(commit["message"]))


def _reference(message: str) -> tuple[str, str] | None:
    for kind, pattern in (("sha", REVERTED_SHA_RE), ("pr", REVERTED_PR_RE), ("cl", REVERTED_CL_RE)):
        if m := pattern.search(message):
            return kind, m.group(1).lower()
    return None


def _unique(index: dict[str, list[str]], key: str) -> str | None:
    hits = index.get(key, [])
    return hits[0] if len(hits) == 1 else None


def _resolver(history: list[dict[str, Any]]):
    """Map a revert's reference to the one commit it names, or None."""
    sorted_shas = sorted(c["sha"] for c in history)
    by_pr: dict[str, list[str]] = {}
    by_cl: dict[str, list[str]] = {}
    for c in history:
        if m := SQUASH_PR_RE.search(c["message"].split("\n", 1)[0]):
            by_pr.setdefault(m.group(1), []).append(c["sha"])
        if m := REVIEWED_ON_RE.search(c["message"]):
            by_cl.setdefault(m.group(1), []).append(c["sha"])

    def resolve(ref: tuple[str, str]) -> str | None:
        kind, value = ref
        if kind == "pr":
            return _unique(by_pr, value)
        if kind == "cl":
            return _unique(by_cl, value)
        i = bisect.bisect_left(sorted_shas, value)
        hits = [s for s in sorted_shas[i : i + 2] if s.startswith(value)]
        return hits[0] if len(hits) == 1 else None

    return resolve


def extract_cases(
    history: list[dict[str, Any]],
    lookback_hours: int = LOOKBACK_HOURS,
    cap: int = MAX_COMMITS,
) -> tuple[list[dict[str, Any]], Counter]:
    """Cases from `history`, and a count of every revert skipped and why.

    The revert itself, and any other commit reverting the same label, is removed from the
    candidates: REVERT_BONUS would hand the scorer the answer.
    """
    lookback = timedelta(hours=lookback_hours)
    history = sorted(history, key=lambda c: c["committed_at"], reverse=True)
    neg_ts = [-c["committed_at"].timestamp() for c in history]
    by_sha = {c["sha"]: c for c in history}
    history_start = min((c["committed_at"] for c in history), default=None)
    resolve = _resolver(history)
    reverts = {c["sha"]: _reference(c["message"]) for c in history if _is_revert(c)}
    target = {sha: resolve(ref) if ref else None for sha, ref in reverts.items()}
    cases: list[dict[str, Any]] = []
    skipped: Counter = Counter()

    for revert in history:
        if revert["sha"] not in reverts:
            continue
        if reverts[revert["sha"]] is None:
            skipped["no_reference"] += 1
            continue
        anchor = revert["committed_at"]
        if anchor - lookback < history_start:
            skipped["window_before_history"] += 1
            continue
        label = target[revert["sha"]]
        if label is None:
            skipped["label_not_on_branch"] += 1  # a merge, another branch, or ambiguous
            continue
        if _is_revert(by_sha[label]):
            skipped["label_is_a_revert"] += 1  # a revert of a revert re-lands, not an incident
            continue
        if by_sha[label]["committed_at"] > anchor:
            skipped["label_after_anchor"] += 1
            continue
        if by_sha[label]["committed_at"] < anchor - lookback:
            skipped["label_outside_window"] += 1
            continue

        newest = bisect.bisect_left(neg_ts, -anchor.timestamp())
        oldest = bisect.bisect_right(neg_ts, -(anchor - lookback).timestamp())
        window = [
            c["sha"] for c in history[newest:oldest]
            if c["sha"] != revert["sha"] and target.get(c["sha"]) != label
        ][:cap]
        if label not in window:
            skipped["label_beyond_cap"] += 1
            continue
        if len(window) < 2:
            skipped["single_candidate"] += 1
            continue
        cases.append({
            "revert": revert["sha"], "label": label,
            "anchor": anchor.isoformat(), "candidates": window,
        })
    return cases, skipped


def load_files(repo: pathlib.Path, shas: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Per-commit `git --numstat` against the first parent, in the shape the scorer reads."""
    out = _git(
        repo, "log", "--no-walk=unsorted", "--stdin", "--no-renames", "--numstat",
        f"--format={RECORD_SEP}%H", stdin="\n".join(sorted(shas)) + "\n",
    )
    files: dict[str, list[dict[str, Any]]] = {}
    for record in out.split(RECORD_SEP)[1:]:
        sha, *lines = record.strip().split("\n")
        rows = []
        for line in lines:
            if not line.strip():
                continue
            added, deleted, path = line.split("\t", 2)
            rows.append({
                "path": path,
                "additions": int(added) if added.isdigit() else 0,
                "deletions": int(deleted) if deleted.isdigit() else 0,
            })
        files[sha] = rows
    return files


def rank_case(
    case: dict[str, Any], by_sha: dict[str, dict], files: dict[str, list[dict]]
) -> dict[str, int]:
    """The label's 1-based rank under the scorer and each baseline."""
    commits = [dict(by_sha[s], files=files.get(s, [])) for s in case["candidates"]]
    scored = score_commits(
        commits, service="", path_globs=["**"], shared_globs=[], deploys=[],
        starts_at=datetime.fromisoformat(case["anchor"]),
    )
    size = {c["sha"]: sum(f["additions"] + f["deletions"] for f in c["files"]) for c in commits}
    orders = {
        "scorer": [s["sha"] for s in scored],
        "most_recent": case["candidates"],
        "largest_diff": sorted(case["candidates"], key=lambda s: -size[s]),
    }
    return {m: order.index(case["label"]) + 1 for m, order in orders.items()}


def _rate(n: int, total: int) -> dict[str, Any]:
    return {"n": n, "rate": round(n / total, 4) if total else None}


def summarize(ranked: list[dict[str, Any]]) -> dict[str, Any]:
    """top-1, top-3 and MRR per method, plus the expected values of a random pick."""
    total = len(ranked)
    out: dict[str, Any] = {"cases": total}
    for m in METHODS:
        ranks = [r["ranks"][m] for r in ranked]
        out[m] = {
            "top1": _rate(sum(r == 1 for r in ranks), total),
            "top3": _rate(sum(r <= 3 for r in ranks), total),
            "mrr": round(sum(1 / r for r in ranks) / total, 4) if total else None,
        }
    sizes = [r["n"] for r in ranked]
    out["random_expected"] = {
        "top1": round(sum(1 / n for n in sizes) / total, 4) if total else None,
        "top3": round(sum(min(3, n) / n for n in sizes) / total, 4) if total else None,
        "mrr": round(sum(sum(1 / k for k in range(1, n + 1)) / n for n in sizes) / total, 4)
        if total else None,
    }
    out["median_candidates"] = statistics.median(sizes) if sizes else None
    return out


def macro(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Each metric averaged over repos, so the busiest repo does not decide the result."""
    out: dict[str, Any] = {"repos": len(summaries)}
    for m in (*METHODS, "random_expected"):
        out[m] = {}
        for k in ("top1", "top3", "mrr"):
            vals = [s[m][k]["rate"] if isinstance(s[m][k], dict) else s[m][k] for s in summaries]
            out[m][k] = round(statistics.mean(vals), 4)
    return out


def run_repo(repo: pathlib.Path, until: datetime | None) -> dict[str, Any]:
    history = load_history(repo, until)
    cases, skipped = extract_cases(history)
    by_sha = {c["sha"]: c for c in history}
    files = load_files(repo, {s for case in cases for s in case["candidates"]})
    ranked = [
        {"revert": c["revert"], "label": c["label"], "n": len(c["candidates"]),
         "ranks": rank_case(c, by_sha, files)}
        for c in cases
    ]
    labelled = len(cases) + skipped["label_outside_window"] + skipped["label_beyond_cap"]
    return {
        "head": _git(repo, "rev-parse", "HEAD").strip(),
        "history_start": min(c["committed_at"] for c in history).isoformat(),
        "reverts": len(cases) + sum(skipped.values()),
        "skipped": dict(sorted(skipped.items())),
        # Of reverts whose label is a usable commit, how many the window could not reach.
        "out_of_window_miss": _rate(
            skipped["label_outside_window"] + skipped["label_beyond_cap"], labelled
        ),
        "summary": summarize(ranked),
        "cases": ranked,
    }


def _print(report: dict[str, Any]) -> None:
    def row(name: str, s: dict[str, Any]) -> str:
        cells = [f"{s[m]['top1']['rate']:.2f}/{s[m]['top3']['rate']:.2f}/{s[m]['mrr']:.2f}"
                 for m in METHODS]
        r = s["random_expected"]
        cells.append(f"{r['top1']:.2f}/{r['top3']:.2f}/{r['mrr']:.2f}")
        return f"  {name:10} {s['cases']:5}  " + "  ".join(f"{c:16}" for c in cells)

    print("top1/top3/MRR    cases  " + "  ".join(f"{m:16}" for m in (*METHODS, "random")))
    for name, r in report["repos"].items():
        print(row(name, r["summary"]) + f"  out-of-window {r['out_of_window_miss']['rate']}")
    print(row("pooled", report["pooled"]))
    mac = report["macro"]
    cells = [f"{mac[m]['top1']:.2f}/{mac[m]['top3']:.2f}/{mac[m]['mrr']:.2f}"
             for m in (*METHODS, "random_expected")]
    print(f"  {'per-repo':10} {'mean':>5}  " + "  ".join(f"{c:16}" for c in cells))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", action="append", required=True, type=pathlib.Path)
    ap.add_argument("--until", type=datetime.fromisoformat,
                    help="ignore commits after this date, so a re-run reproduces")
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--cases", action="store_true", help="keep per-case ranks in --out")
    args = ap.parse_args()
    until = args.until.replace(tzinfo=args.until.tzinfo or UTC) if args.until else None

    repos = {p.name.removesuffix(".git"): run_repo(p, until) for p in args.repo}
    pooled = summarize([c for r in repos.values() for c in r["cases"]])
    if not args.cases:
        for r in repos.values():
            del r["cases"]
    report = {
        "generated_by": "scripts/backtest_reverts.py",
        "until": until.isoformat() if until else None,
        "lookback_hours": LOOKBACK_HOURS,
        "max_commits": MAX_COMMITS,
        "pooled": pooled,
        "macro": macro([r["summary"] for r in repos.values()]),
        "repos": repos,
    }
    _print(report)
    if args.out:
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
