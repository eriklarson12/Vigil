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

import httpx
import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from vigil.config import Settings
from vigil.graph.deps import Deps
from vigil.impact.catalog import ServiceCatalog
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


def _write_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {settings.github_write_token}",
        },
        timeout=10.0,
    )


async def _get(gh: httpx.AsyncClient, path: str, **params: Any) -> Any:
    resp = await gh.get(path, params=params or None)
    resp.raise_for_status()
    return resp.json()


async def _post(gh: httpx.AsyncClient, path: str, body: dict[str, Any]) -> Any:
    resp = await gh.post(path, json=body)
    resp.raise_for_status()
    return resp.json()


async def _tree_at(gh: httpx.AsyncClient, base: str, commit_sha: str) -> tuple[str, dict[str, dict]]:
    """(tree sha, {path: entry}) for every blob at `commit_sha`."""
    tree_sha = (await _get(gh, f"{base}/git/commits/{commit_sha}"))["tree"]["sha"]
    tree = await _get(gh, f"{base}/git/trees/{tree_sha}", recursive="1")
    if tree.get("truncated"):
        raise RevertRefused("repository tree too large to diff through the API")
    return tree_sha, {e["path"]: e for e in tree["tree"] if e["type"] == "blob"}


def revert_entries(
    files: list[dict[str, Any]], head: dict[str, dict], parent: dict[str, dict]
) -> list[dict[str, Any]]:
    """Tree entries that put every file the culprit touched back to its parent-side state.

    Refuses when head no longer matches what the culprit left behind: restoring the
    parent-side blob there would silently undo every later commit to that file.
    """

    def delete(path: str) -> dict[str, Any]:
        return {"path": path, "mode": "100644", "type": "blob", "sha": None}

    def restore(path: str) -> dict[str, Any]:
        entry = parent[path]
        return {"path": path, "mode": entry["mode"], "type": "blob", "sha": entry["sha"]}

    entries: list[dict[str, Any]] = []
    for f in files:
        path, status = f["filename"], f["status"]
        head_sha = (head.get(path) or {}).get("sha")
        if status == "removed":
            if path in head:
                raise RevertRefused(f"conflict: {path} was re-added after the culprit")
            entries.append(restore(path))
            continue
        if head_sha != f["sha"]:
            raise RevertRefused(f"conflict: {path} changed after the culprit")
        if status == "added":
            entries.append(delete(path))
        elif status == "renamed":
            old = f["previous_filename"]
            if old in head:
                raise RevertRefused(f"conflict: {old} was re-created after the culprit")
            entries += [delete(path), restore(old)]
        else:
            entries.append(restore(path))
    return entries


async def open_live_pr(
    gh: httpx.AsyncClient, *, owner_repo: str, sha: str, pr: dict[str, Any], default_branch: str
) -> str:
    base = f"/repos/{owner_repo}"
    branch = pr["branch"]

    existing = await gh.get(f"{base}/git/ref/heads/{branch}")
    if existing.status_code == 200:
        # A previous run got as far as the branch. Resume: never build a second one.
        owner = owner_repo.split("/")[0]
        pulls = await _get(gh, f"{base}/pulls", head=f"{owner}:{branch}", state="open")
        if pulls:
            return pulls[0]["html_url"]
    elif existing.status_code == 404:
        culprit = await _get(gh, f"{base}/commits/{sha}")
        if len(culprit["parents"]) != 1:
            raise RevertRefused("culprit is a merge commit; revert it by hand")
        head_sha = (await _get(gh, f"{base}/git/ref/heads/{default_branch}"))["object"]["sha"]
        head_tree_sha, head_tree = await _tree_at(gh, base, head_sha)
        _, parent_tree = await _tree_at(gh, base, culprit["parents"][0]["sha"])
        entries = revert_entries(culprit["files"], head_tree, parent_tree)

        tree = await _post(gh, f"{base}/git/trees", {"base_tree": head_tree_sha, "tree": entries})
        commit = await _post(
            gh,
            f"{base}/git/commits",
            {
                "message": f"{pr['title']}\n\nThis reverts commit {sha}.",
                "tree": tree["sha"],
                "parents": [head_sha],
            },
        )
        await _post(gh, f"{base}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    else:
        existing.raise_for_status()

    opened = await _post(
        gh,
        f"{base}/pulls",
        {"title": pr["title"], "head": branch, "base": default_branch, "body": pr["body"]},
    )
    return opened["html_url"]


async def _open_pr(deps: Deps, ctx: dict[str, Any], pr: dict[str, Any]) -> str:
    settings = deps.settings
    if settings.rollback_mode == "mock":
        return f"mock://{pr['repo']}/tree/{pr['branch']}"
    if not settings.github_write_token:
        raise RevertRefused("GITHUB_WRITE_TOKEN is not set")
    async with _write_client(settings) as gh:
        return await open_live_pr(
            gh,
            owner_repo=ServiceCatalog.normalize_repo(pr["repo"]),
            sha=ctx["sha"],
            pr=pr,
            default_branch=settings.demo_repo_default_branch,
        )


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
