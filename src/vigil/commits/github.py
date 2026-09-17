"""GitHub commit and deployment fetching (spec §6.1, roadmap R6). Two modes:

- live:    GitHub REST API (unauthenticated 60 req/h → detail-fetch only top 15
           by detail-free features; with GITHUB_TOKEN fetch all).
- fixture: replay tests/fixtures/github/{scenario}.json — commit timestamps are
           stored as hours_before_alert and deployments as minutes_before_alert,
           both materialized relative to starts_at, so time-decay and f_deploy
           behave identically no matter when the demo runs.

This module never touches the database. The caller (the fetch_commits graph
node) owns both the deploy_events write and the degradation contract.
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
MAX_DEPLOYMENTS = 100
DETAIL_CAP_UNAUTHENTICATED = 15


def _fixture_path(settings: Settings, scenario: str) -> pathlib.Path:
    return pathlib.Path(settings.github_fixtures_dir) / f"{scenario}.json"


def _load_fixture(settings: Settings, scenario: str) -> dict[str, Any] | None:
    path = _fixture_path(settings, scenario)
    if not path.exists():
        path = _fixture_path(settings, "default")
        if not path.exists():
            return None
    return json.loads(path.read_text(encoding="utf-8"))


def _client(settings: Settings) -> httpx.AsyncClient:
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=10.0)


def _gh_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_fixture_commits(settings: Settings, scenario: str, starts_at: datetime) -> list[dict[str, Any]]:
    data = _load_fixture(settings, scenario)
    if not data:
        return []
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
    since = (starts_at - timedelta(hours=settings.commit_lookback_hours)).isoformat()
    async with _client(settings) as gh:
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
                "committed_at": _gh_time(item["commit"]["committer"]["date"]),
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


def parse_deployments(
    items: list[dict[str, Any]], starts_at: datetime, lookback_hours: int
) -> list[dict[str, Any]]:
    """Map a GET /deployments payload to [{sha, finished_at}], newest first.

    `finished_at` comes from `updated_at`, falling back to `created_at`. `created_at`
    is when the deployment *record* was created, i.e. deploy start, so using it would
    over-age every deploy by its own duration and push a genuine 25-minute deploy out
    of f_deploy's 45-minute 1.0 tier — wrong direction, for free. The exact finish
    lives in GET /deployments/{id}/statuses, but that costs one request per deployment
    on the same 60/h unauthenticated budget `_fetch_live` already rations for commit
    details, which drive f_path + f_risk + f_size (0.25 + 0.15 + 0.10) against
    f_deploy's 0.10. `updated_at` is already in the list payload and its error is far
    below the feature's 45/240-minute resolution.

    Deliberately not filtered: `task` (a deploy:migrations task is still a deploy
    signal) and deployment state (a failed deploy right before an alert is a
    correlation, not noise — and reading state needs the statuses call above).
    """
    window_start = starts_at - timedelta(hours=lookback_hours)
    best: dict[tuple[str, datetime], dict[str, Any]] = {}
    for item in items:
        sha = item.get("sha")
        stamp = item.get("updated_at") or item.get("created_at")
        if not sha or not stamp:
            continue
        finished_at = _gh_time(stamp)
        if not window_start <= finished_at <= starts_at:
            continue
        best[(sha, finished_at)] = {"sha": sha, "finished_at": finished_at}
    return sorted(best.values(), key=lambda d: d["finished_at"], reverse=True)


def load_fixture_deployments(
    settings: Settings, scenario: str, starts_at: datetime
) -> list[dict[str, Any]]:
    """Replay the fixture's optional `deployments` array.

    A fixture with no `deployments` key yields [] — that absence is what keeps
    ambiguous_latency, cert_expiry, and memory_leak at f_deploy = 0.
    """
    data = _load_fixture(settings, scenario)
    if not data:
        return []
    return [
        {
            "sha": d["sha"],
            "finished_at": starts_at - timedelta(minutes=d["minutes_before_alert"]),
        }
        for d in data.get("deployments", [])
    ]


async def _fetch_live_deployments(
    repo: str, settings: Settings, starts_at: datetime
) -> list[dict[str, Any]]:
    owner_repo = repo.removeprefix("github.com/")
    # Unlike /commits, this endpoint takes no since/until — results come back newest
    # first, so one page of 100 covers any realistic lookback and parse_deployments
    # applies the window client-side. No pagination loop, deliberately.
    params: dict[str, Any] = {"per_page": MAX_DEPLOYMENTS}
    if settings.github_deploy_environment:
        params["environment"] = settings.github_deploy_environment
    async with _client(settings) as gh:
        resp = await gh.get(f"/repos/{owner_repo}/deployments", params=params)
        resp.raise_for_status()
        return parse_deployments(resp.json(), starts_at, settings.commit_lookback_hours)


async def fetch_deployments(
    *, repo: str, settings: Settings, starts_at: datetime, scenario_hint: str | None
) -> list[dict[str, Any]]:
    if settings.github_mode == "fixture":
        return load_fixture_deployments(settings, scenario_hint or "default", starts_at)
    return await _fetch_live_deployments(repo, settings, starts_at)
