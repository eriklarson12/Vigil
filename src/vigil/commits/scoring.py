"""Deterministic commit pre-scoring — Vigil's core differentiator (spec §6.2).

Pure functions only: no I/O, no clock reads. Runs BEFORE any LLM call so the
LLM only ever ranks a handful of pre-vetted candidates.

If you change weights or feature rules here, update tasks/todo.md §6.2 and the
golden tests in tests/unit/test_scoring.py in the same change.
"""

import math
import re
from datetime import datetime
from typing import Any

WEIGHTS = {
    "f_time": 0.30,
    "f_path": 0.25,
    "f_risk": 0.15,
    "f_size": 0.10,
    "f_msg": 0.10,
    "f_deploy": 0.10,
}
RELEVANCE_GATE = 0.3  # multiplier when f_path == 0 and f_deploy == 0
TIME_CONSTANT_HOURS = 6.0

RISK_CATEGORIES: list[tuple[float, list[str]]] = [
    (0.35, ["requirements*.txt", "poetry.lock", "uv.lock", "package*.json", "go.mod", "Gemfile.lock"]),
    (0.35, ["**/migrations/**", "*.sql"]),
    (0.30, ["*.yaml", "*.yml", "*.toml", "*.ini", "*.env*", "config/**/*.json"]),
    (0.30, ["Dockerfile*", "k8s/**", "helm/**", "*.tf"]),
    (0.25, ["*flag*", "*toggle*"]),
]

MSG_SIGNALS: list[tuple[float, str]] = [
    (0.5, r"hotfix|urgent|quick.?fix"),
    (0.3, r"\bfix\b|\bpatch\b"),
    (0.3, r"flag|toggle|enable|disable"),
    (0.3, r"\bbump\b|\bupgrade\b"),
    (0.4, r"\bWIP\b|\btemp\b|\bhack\b"),
    (0.15, r"refactor"),
]
REVERT_RE = re.compile(r'^Revert "', re.IGNORECASE)
REVERTED_SHA_RE = re.compile(r"reverts commit ([0-9a-f]{7,40})", re.IGNORECASE)
REVERT_BONUS = 0.6


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Convert a `**`-aware glob to a full-path regex (fnmatch can't do `**`)."""
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            if glob[i : i + 3] == "**/":
                out.append(r"(?:.*/)?")
                i += 3
                continue
            if glob[i : i + 2] == "**":
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        elif c == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_any(path: str, globs: list[str]) -> bool:
    # A bare-filename glob (no slash) matches the basename anywhere in the tree.
    basename = path.rsplit("/", 1)[-1]
    for g in globs:
        target = path if "/" in g else basename
        if glob_to_regex(g).match(target):
            return True
    return False


def f_time(committed_at: datetime, starts_at: datetime) -> float:
    delta_h = max(0.0, (starts_at - committed_at).total_seconds() / 3600.0)
    return math.exp(-delta_h / TIME_CONSTANT_HOURS)


def f_path(files: list[dict[str, Any]], path_globs: list[str], shared_globs: list[str]) -> float:
    if not files:
        return 0.0
    total = len(files)
    matched = sum(1 for f in files if _matches_any(f["path"], path_globs))
    shared = sum(1 for f in files if _matches_any(f["path"], shared_globs))
    return max(matched / total, 0.5 * shared / total)


def f_risk(files: list[dict[str, Any]]) -> float:
    score = 0.0
    for points, globs in RISK_CATEGORIES:
        if any(_matches_any(f["path"], globs) for f in files):
            score += points
    return min(1.0, score)


def f_size(files: list[dict[str, Any]]) -> float:
    changed = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    return min(1.0, math.log10(changed + 1) / 3.0)


def f_msg_base(message: str) -> float:
    score = sum(points for points, pattern in MSG_SIGNALS if re.search(pattern, message, re.IGNORECASE))
    return min(1.0, score)


def f_deploy(sha: str, service: str, deploys: list[dict[str, Any]], starts_at: datetime) -> float:
    best = 0.0
    for d in deploys:
        if d["service"] != service or sha not in d["commit_shas"]:
            continue
        delta_min = (starts_at - d["finished_at"]).total_seconds() / 60.0
        if 0 <= delta_min <= 45:
            best = max(best, 1.0)
        elif 0 <= delta_min <= 240:
            best = max(best, 0.5)
    return best


def score_commits(
    commits: list[dict[str, Any]],
    *,
    service: str,
    path_globs: list[str],
    shared_globs: list[str],
    deploys: list[dict[str, Any]],
    starts_at: datetime,
) -> list[dict[str, Any]]:
    """Return [{sha, score, feature_scores}], sorted by score desc.

    Revert special-case (spec §6.2): a `Revert "..."` commit contributes no
    message signal itself, and the commit it reverts gets +0.6 message bonus.
    """
    revert_bonus: dict[str, float] = {}
    for c in commits:
        msg = c.get("message", "")
        if REVERT_RE.search(msg):
            m = REVERTED_SHA_RE.search(msg)
            if m:
                revert_bonus[m.group(1)] = REVERT_BONUS

    results = []
    for c in commits:
        files = c.get("files", [])
        msg = c.get("message", "")
        is_revert = bool(REVERT_RE.search(msg))
        msg_score = 0.0 if is_revert else f_msg_base(msg)
        for prefix, bonus in revert_bonus.items():
            if c["sha"].startswith(prefix) or prefix.startswith(c["sha"]):
                msg_score = min(1.0, msg_score + bonus)
        features = {
            "f_time": f_time(c["committed_at"], starts_at),
            "f_path": f_path(files, path_globs, shared_globs),
            "f_risk": f_risk(files),
            "f_size": f_size(files),
            "f_msg": msg_score,
            "f_deploy": f_deploy(c["sha"], service, deploys, starts_at),
        }
        raw = sum(WEIGHTS[k] * v for k, v in features.items())
        gated = features["f_path"] == 0 and features["f_deploy"] == 0
        score = raw * RELEVANCE_GATE if gated else raw
        results.append(
            {
                "sha": c["sha"],
                "score": round(score, 6),
                "feature_scores": {k: round(v, 6) for k, v in features.items()},
                "gated": gated,
            }
        )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results
