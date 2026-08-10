"""GitHub commit fetching (spec §6.1). Two modes:

- live:    GitHub REST API (unauthenticated 60 req/h → detail-fetch only top 15
           by detail-free features; with GITHUB_TOKEN fetch all).
- fixture: replay tests/fixtures/github/{scenario}.json — commit timestamps are
           stored as hours_before_alert and materialized relative to starts_at,
           so time-decay behaves identically no matter when the demo runs.
"""

import json
import pathlib
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from vigil.commits.scoring import f_msg_base, f_time
from vigil.config import Settings

log = structlog.get_logger()

MAX_COMMITS = 50
DETAIL_CAP_UNAUTHENTICATED = 15


def _fixture_path(settings: Settings, scenario: str) -> pathlib.Path:
    return pathlib.Path(settings.github_fixtures_dir) / f"{scenario}.json"


def load_fixture_commits(settings: Settings, scenario: str, starts_at: datetime) -> list[dict[str, Any]]:
    path = _fixture_path(settings, scenario)
    if not path.exists():
        path = _fixture_path(settings, "default")
        if not path.exists():
            return []
    data = json.loads(path.read_text(encoding="utf-8"))
    commits = []
    for c in data["commits"]:
        commits.append(
            {
                "sha": c["sha"],
                "message": c["message"],
                "author": c.get("author", "dev"),
                "committed_at": starts_at - timedelta(hours=c["hours_before_alert"]),
                "files": c.get("files", []),
                "patch": c.get("patch", ""),
            }
        )
    return commits


async def _fetch_live(repo: str, settings: Settings, starts_at: datetime) -> list[dict[str, Any]]:
    owner_repo = repo.removeprefix("github.com/")
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    since = (starts_at - timedelta(hours=settings.commit_lookback_hours)).isoformat()
    async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=10.0) as gh:
        resp = await gh.get(
            f"/repos/{owner_repo}/commits",
            params={"since": since, "until": starts_at.isoformat(), "per_page": MAX_COMMITS},
        )
        resp.raise_for_status()
        listing = resp.json()[:MAX_COMMITS]

        skeletons = [
            {
                "sha": item["sha"],
                "message": item["commit"]["message"],
                "author": (item["commit"]["author"] or {}).get("name", "unknown"),
                "committed_at": datetime.fromisoformat(
                    item["commit"]["committer"]["date"].replace("Z", "+00:00")
                ),
                "files": [],
                "patch": "",
            }
            for item in listing
        ]
        # Unauthenticated rate limit is 60/h: only detail-fetch the most promising
        # by the features we can compute without details (time + message).
        if not settings.github_token and len(skeletons) > DETAIL_CAP_UNAUTHENTICATED:
            skeletons.sort(
                key=lambda c: 0.30 * f_time(c["committed_at"], starts_at) + 0.10 * f_msg_base(c["message"]),
                reverse=True,
            )
            skeletons = skeletons[:DETAIL_CAP_UNAUTHENTICATED]
        for c in skeletons:
            detail = await gh.get(f"/repos/{owner_repo}/commits/{c['sha']}")
            detail.raise_for_status()
            body = detail.json()
            c["files"] = [
                {
                    "path": f["filename"],
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                }
                for f in body.get("files", [])
            ]
            c["patch"] = "\n".join(
                f"--- {f['filename']}\n{f.get('patch', '')}" for f in body.get("files", [])
            )
    return skeletons


async def fetch_candidates(
    *, repo: str, settings: Settings, starts_at: datetime, scenario_hint: str | None
) -> list[dict[str, Any]]:
    if settings.github_mode == "fixture":
        return load_fixture_commits(settings, scenario_hint or "default", starts_at)
    return await _fetch_live(repo, settings, starts_at)
