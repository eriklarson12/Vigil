"""Postmortem action items become GitHub issues (roadmap R9, ADR-013).

Filing runs after the postmortem graph, not inside it: once an incident is
`postmortem_done` the graph never runs again, so a node that died mid-way would
never be retried. `Runner.run_postmortem` files right after the graph, and the
resume tick sweeps anything left pending.

Idempotency has two layers. `postmortems.issue_urls` records each URL as soon as
it exists, and an index with a URL is never filed again. A crash between GitHub
creating an issue and Postgres recording it is caught by the marker in every
issue body: a live run adopts a marked issue instead of filing a second.

No LLM call: every word comes from the stored action items.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog
from psycopg.rows import dict_row

from vigil.commits.github_write import get_json, post_json, write_client
from vigil.graph.deps import Deps
from vigil.impact.catalog import ServiceCatalog
from vigil.ingest.queue import add_event

log = structlog.get_logger()

LABEL = "vigil-action-item"
MAX_ATTEMPTS = 3
_MARKER_RE = re.compile(r"<!-- vigil:[0-9a-f-]+:\d+ -->")
_MAX_PAGES = 10


def marker(incident_id: str, index: int) -> str:
    return f"<!-- vigil:{incident_id}:{index} -->"


def issue_title(incident_id: str, item: dict[str, Any]) -> str:
    return f"[Vigil {str(incident_id)[:8]}] {item['description']}"[:256]


def issue_body(incident_id: str, index: int, item: dict[str, Any], dashboard_url: str) -> str:
    return "\n".join(
        [
            f"**Priority:** {item['priority']} · **Owner:** {item['owner']}",
            "",
            item["description"],
            "",
            f"Incident: {dashboard_url}/incidents/{incident_id}",
            "",
            "Filed from the postmortem by Vigil.",
            "",
            marker(incident_id, index),
        ]
    )


def mock_url(repo: str, incident_id: str, index: int) -> str:
    return f"mock://{repo}/issues/{str(incident_id)[:8]}-{index}"


async def _ensure_label(gh: httpx.AsyncClient, base: str) -> None:
    resp = await gh.post(
        f"{base}/labels",
        json={"name": LABEL, "color": "d93f0b", "description": "Action item from a Vigil postmortem"},
    )
    if resp.status_code != 422:  # 422: the label already exists
        resp.raise_for_status()


async def _marked_issues(gh: httpx.AsyncClient, base: str) -> dict[str, str]:
    """{marker: html_url} for every labeled issue, open or closed."""
    found: dict[str, str] = {}
    for page in range(1, _MAX_PAGES + 1):
        issues = await get_json(
            gh, f"{base}/issues", labels=LABEL, state="all", per_page="100", page=str(page)
        )
        for issue in issues:
            for m in _MARKER_RE.findall(issue.get("body") or ""):
                found.setdefault(m, issue["html_url"])
        if len(issues) < 100:
            break
    return found


async def file_live_issues(
    gh: httpx.AsyncClient,
    *,
    owner_repo: str,
    issues: dict[int, dict[str, Any]],
    on_filed: Callable[[int, str], Awaitable[None]],
) -> None:
    """File each issue in `issues` ({index: {title, body, marker}}) unless a marked one exists.

    `on_filed` runs after every issue, so URLs filed before an error are kept.
    """
    base = f"/repos/{owner_repo}"
    await _ensure_label(gh, base)
    existing = await _marked_issues(gh, base)
    for index, issue in issues.items():
        url = existing.get(issue["marker"])
        if url is None:
            created = await post_json(
                gh,
                f"{base}/issues",
                {"title": issue["title"], "body": issue["body"], "labels": [LABEL]},
            )
            url = created["html_url"]
        await on_filed(index, url)


async def file_action_items(deps: Deps, incident_id: str) -> None:
    """File every action item of this incident's postmortem that has no issue URL yet."""
    settings = deps.settings
    async with deps.pool.connection() as conn:
        async with conn.transaction():
            # The row lock keeps a second in-process run (a double resolve) from filing the
            # same items; SKIP LOCKED makes that second run a no-op.
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                """
                SELECT p.action_items, p.issue_urls, i.service
                FROM postmortems p JOIN incidents i ON i.id = p.incident_id
                WHERE p.incident_id = %s AND p.action_items IS NOT NULL
                  AND p.issue_attempts < %s
                FOR UPDATE OF p SKIP LOCKED
                """,
                (incident_id, MAX_ATTEMPTS),
            )
            row = await cur.fetchone()
            if not row:
                return
            pending = {
                i: item
                for i, item in enumerate(row["action_items"])
                if str(i) not in row["issue_urls"]
            }
            if not pending:
                return

            repo = settings.issues_repo or (deps.catalog.get(row["service"]) or {}).get("repo", "")
            issues = {
                i: {
                    "title": issue_title(incident_id, item),
                    "body": issue_body(incident_id, i, item, settings.dashboard_url),
                    "marker": marker(incident_id, i),
                }
                for i, item in pending.items()
            }

            async def on_filed(index: int, url: str) -> None:
                await conn.execute(
                    "UPDATE postmortems SET issue_urls = issue_urls || jsonb_build_object(%s::text, %s::text)"
                    " WHERE incident_id = %s",
                    (str(index), url, incident_id),
                )
                await add_event(
                    deps.pool,
                    incident_id,
                    "issue_filed",
                    {"index": index, "url": url, "repo": repo, "title": issues[index]["title"]},
                )

            error: str | None = None
            try:
                if not repo:
                    raise RuntimeError(f"no repo configured for service {row['service']}")
                if settings.issues_mode == "mock":
                    for i in issues:
                        await on_filed(i, mock_url(repo, incident_id, i))
                elif not settings.github_write_token:
                    raise RuntimeError("GITHUB_WRITE_TOKEN is not set")
                else:
                    async with write_client(settings) as gh:
                        await file_live_issues(
                            gh,
                            owner_repo=ServiceCatalog.normalize_repo(repo),
                            issues=issues,
                            on_filed=on_filed,
                        )
            except Exception as exc:  # noqa: BLE001 — a failure must count an attempt, never raise
                error = str(exc)[:300]

            if error:
                await conn.execute(
                    "UPDATE postmortems SET issue_attempts = issue_attempts + 1 WHERE incident_id = %s",
                    (incident_id,),
                )
                await add_event(deps.pool, incident_id, "issues_file_failed", {"error": error})
    log.info("action_items_filed", incident_id=incident_id, pending=len(pending), error=error)


# Postmortems with unfiled items, old enough that no in-process run still owns them.
PENDING_SQL = """
    SELECT p.incident_id FROM postmortems p
    WHERE p.action_items IS NOT NULL
      AND p.issue_attempts < %(max_attempts)s
      AND p.created_at < now() - make_interval(mins => %(stale_minutes)s)
      AND (SELECT count(*) FROM jsonb_object_keys(p.issue_urls)) < jsonb_array_length(p.action_items)
"""
