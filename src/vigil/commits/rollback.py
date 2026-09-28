"""Revert PR with a human approval gate (roadmap R5, ADR-012).

The Slack button click is the approval; Vigil opens a PR and never merges.
No LLM call: every word of the PR comes from rows already in Postgres.

State lives on `incidents.revert_pr_state`: NULL -> requested -> proposed | failed.
`claim_revert` is the only way into `requested`, and it is one conditional UPDATE,
so a double click or a Slack retry is a no-op. `failed` may be claimed again.

ROLLBACK_MODE=mock records the would-be PR as an event. ROLLBACK_MODE=live uses
GITHUB_WRITE_TOKEN, never the read-only GITHUB_TOKEN.
"""

from typing import Any

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from vigil.graph.deps import Deps
from vigil.ingest.queue import add_event
from vigil.slack.blocks import build_revert_result_message, revert_gate

log = structlog.get_logger()


class RevertRefused(Exception):
    """A revert Vigil will not propose. The message is shown in Slack."""


async def load_revert_context(pool: AsyncConnectionPool, incident_id: str) -> dict[str, Any] | None:
    """The rank-1 candidate, if it passes the gate. Re-checked here on every click:
    the Slack payload only names the incident, it never vouches for the verdict."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT i.id, i.service, c.sha, c.message, c.llm_confidence,
                   c.llm_rationale, c.llm_suggested_action
            FROM incidents i
            JOIN commit_candidates c ON c.incident_id = i.id AND c.llm_rank = 1
            WHERE i.id = %s
            """,
            (incident_id,),
        )
        row = await cur.fetchone()
    if not row or not revert_gate(row["llm_confidence"], row["llm_suggested_action"]):
        return None
    return row


async def claim_revert(pool: AsyncConnectionPool, incident_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE incidents SET revert_pr_state = 'requested'
            WHERE id = %s AND (revert_pr_state IS NULL OR revert_pr_state = 'failed')
            RETURNING id
            """,
            (incident_id,),
        )
        return await cur.fetchone() is not None


def branch_name(sha: str, incident_id: str) -> str:
    # Deterministic, so a retried run finds its own branch instead of making a second one.
    return f"vigil/revert-{sha[:7]}-{incident_id}"


def build_pr_payload(ctx: dict[str, Any], repo: str, dashboard_url: str) -> dict[str, Any]:
    sha = ctx["sha"]
    subject = (ctx["message"] or "").splitlines()[0] if ctx["message"] else sha[:10]
    confidence = round(float(ctx["llm_confidence"]) * 100)
    body = "\n".join(
        [
            f"Reverts {sha}.",
            "",
            f"Vigil named this commit the likely cause of incident `{ctx['id']}` "
            f"({ctx['service']}) at {confidence}% confidence.",
            "",
            f"> {ctx['llm_rationale'] or 'No rationale recorded.'}",
            "",
            f"Incident: {dashboard_url}/incidents/{ctx['id']}",
            "",
            "Proposed from the Slack brief by a human click. Vigil never merges; review before you do.",
        ]
    )
    return {
        "repo": repo,
        "branch": branch_name(sha, str(ctx["id"])),
        "title": f'Revert "{subject}"',
        "body": body,
    }


async def _open_pr(deps: Deps, ctx: dict[str, Any], pr: dict[str, Any]) -> str:
    if deps.settings.rollback_mode == "mock":
        return f"mock://{pr['repo']}/tree/{pr['branch']}"
    raise RevertRefused("live rollback mode is not implemented yet")


async def propose_revert(deps: Deps, incident_id: str, response_url: str | None) -> None:
    """Run a claimed revert to `proposed` or `failed`, then answer the click."""
    ctx = await load_revert_context(deps.pool, incident_id)
    pr_url: str | None = None
    error: str | None = None
    sha = ctx["sha"] if ctx else ""
    try:
        if ctx is None:
            raise RevertRefused("the culprit verdict no longer passes the revert gate")
        repo = (deps.catalog.get(ctx["service"]) or {}).get("repo")
        if not repo:
            raise RevertRefused(f"no repo configured for service {ctx['service']}")
        pr = build_pr_payload(ctx, repo, deps.settings.dashboard_url)
        pr_url = await _open_pr(deps, ctx, pr)
    except Exception as exc:  # noqa: BLE001 — every failure must land in `failed`, never stay `requested`
        error = str(exc)[:300]

    async with deps.pool.connection() as conn:
        await conn.execute(
            "UPDATE incidents SET revert_pr_state = %s, revert_pr_url = %s WHERE id = %s",
            ("proposed" if pr_url else "failed", pr_url, incident_id),
        )
    if pr_url:
        await add_event(deps.pool, incident_id, "revert_pr_proposed", {"url": pr_url, "pr": pr})
    else:
        await add_event(deps.pool, incident_id, "revert_pr_failed", {"sha": sha, "error": error})
    log.info("revert_pr_done", incident_id=incident_id, proposed=bool(pr_url), error=error)
    await deps.slack.post_response(
        incident_id, response_url, build_revert_result_message(sha, pr_url, error)
    )
