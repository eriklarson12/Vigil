"""Postmortem graph (spec §10):
START → gather_timeline → generate_postmortem_llm → post_postmortem → END

gather_timeline is pure Postgres reads; one LLM call total. On LLM failure no
row is written — the incident stays 'resolved' and the resume tick retries.
"""

from typing import Any

import structlog
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row

from vigil.graph.deps import Deps
from vigil.graph.nodes_util import degrading
from vigil.graph.state import PostmortemState
from vigil.llm.prompts import build_postmortem_prompt
from vigil.llm.schemas import Postmortem

log = structlog.get_logger()


def render_markdown(pm: Postmortem, incident: dict[str, Any]) -> str:
    lines = [
        f"# Postmortem: {incident.get('title', 'incident')}",
        "",
        f"**Severity:** {incident.get('severity')} · **Service:** {incident.get('service')}"
        f" · **Resolved via:** {incident.get('resolution_source')}",
        "",
        "## Summary",
        pm.summary,
        "",
        "## Timeline",
        "| Time | Event |",
        "|---|---|",
        *[f"| {t.time} | {t.event} |" for t in pm.timeline],
        "",
        "## Root cause",
        pm.root_cause,
        "",
        "## Contributing factors",
        *([f"- {f}" for f in pm.contributing_factors] or ["- none identified"]),
        "",
        "## Impact",
        pm.impact,
        "",
        "## Resolution",
        pm.resolution,
        "",
        "## What went well",
        *([f"- {w}" for w in pm.went_well] or ["- n/a"]),
        "",
        "## What went poorly",
        *([f"- {w}" for w in pm.went_poorly] or ["- n/a"]),
        "",
        "## Action items",
        *[f"- **{a.priority}** {a.description} _(owner: {a.owner})_" for a in pm.action_items],
    ]
    return "\n".join(lines)


def build_postmortem_graph(deps: Deps, checkpointer: Any = None):
    @degrading("gather_timeline", retries=2, backoff=0.5)
    async def gather_timeline(state: PostmortemState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        async with deps.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
                incident = await cur.fetchone()
                await cur.execute(
                    "SELECT event_type, payload, created_at FROM incident_events"
                    " WHERE incident_id = %s ORDER BY created_at",
                    (incident_id,),
                )
                events = await cur.fetchall()
                await cur.execute(
                    "SELECT sha, message, author, committed_at, heuristic_score, feature_scores,"
                    " llm_rank, llm_confidence, llm_rationale FROM commit_candidates"
                    " WHERE incident_id = %s ORDER BY heuristic_score DESC",
                    (incident_id,),
                )
                candidates = await cur.fetchall()
                await cur.execute(
                    "SELECT alert_name, labels, annotations, starts_at, ends_at FROM alerts"
                    " WHERE incident_id = %s ORDER BY starts_at",
                    (incident_id,),
                )
                alerts = await cur.fetchall()

        if not incident:
            raise RuntimeError(f"incident {incident_id} not found")

        service = deps.catalog.get(incident["service"])
        first_start = min((a["starts_at"] for a in alerts), default=incident["created_at"])
        resolved_at = incident["resolved_at"]
        duration_min = (
            max(1, int((resolved_at - first_start).total_seconds() // 60)) if resolved_at else None
        )
        brief_event = next((e for e in events if e["event_type"] == "brief_posted"), None)
        record = {
            "incident": {k: str(v) if v is not None else None for k, v in incident.items()},
            "alerts": [
                {
                    "alert_name": a["alert_name"],
                    "labels": a["labels"],
                    "annotations": a["annotations"],
                    "starts_at": str(a["starts_at"]),
                    "ends_at": str(a["ends_at"]) if a["ends_at"] else None,
                }
                for a in alerts
            ],
            "timeline_events": [
                {"time": str(e["created_at"]), "event_type": e["event_type"], "payload": e["payload"]}
                for e in events
            ],
            "commit_candidates": [
                {k: (str(v) if v is not None else None) for k, v in c.items()} for c in candidates
            ],
            "durations": {
                "started_at": str(first_start),
                "brief_posted_at": str(brief_event["created_at"]) if brief_event else None,
                "resolved_at": str(resolved_at) if resolved_at else None,
                "minutes_open": duration_min,
                "detection_to_brief_seconds": (
                    int((brief_event["created_at"] - first_start).total_seconds())
                    if brief_event
                    else None
                ),
            },
            "actual_impact": {
                "baseline_rpm": (service or {}).get("baseline_rpm"),
                "est_requests_affected": (
                    (service or {}).get("baseline_rpm", 0) * duration_min
                    if service and service.get("baseline_rpm") and duration_min
                    else None
                ),
            },
            "owner": (service or {}).get("owner", "on-call"),
        }
        return {"record": record, "errors": {}}

    @degrading("generate_postmortem_llm", retries=1, backoff=10.0)
    async def generate_postmortem_llm(state: PostmortemState) -> dict[str, Any]:
        record = state.get("record")
        if not record:
            raise RuntimeError("gather_timeline produced no record")
        system, user = build_postmortem_prompt(record)
        pm = await deps.llm.generate_structured(system, user, Postmortem, "postmortem")
        incident = record["incident"]
        return {"postmortem": pm.model_dump(), "markdown": render_markdown(pm, incident)}

    @degrading("post_postmortem", retries=2, backoff=1.0)
    async def post_postmortem(state: PostmortemState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        markdown = state.get("markdown")
        if not markdown:
            # LLM failed — leave the incident 'resolved'; the resume tick retries.
            log.warning("postmortem_skipped_no_markdown", incident_id=incident_id)
            return {}
        async with deps.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO postmortems (incident_id, markdown, model_used) VALUES (%s, %s, %s)"
                    " ON CONFLICT (incident_id) DO NOTHING RETURNING id",
                    (incident_id, markdown, deps.settings.gemini_model),
                )
                inserted = await cur.fetchone()

                if inserted:  # exactly-once posting: only the inserting run posts to Slack
                    cur = await conn.execute(
                        "SELECT slack_message_ts FROM incidents WHERE id = %s", (incident_id,)
                    )
                    row = await cur.fetchone()
                    try:
                        from vigil.slack.blocks import build_postmortem_message

                        payload = build_postmortem_message(markdown, incident_id)
                        await deps.slack.post_thread(incident_id, (row and row[0]) or "", payload)
                    except Exception as exc:  # noqa: BLE001
                        # Slack failing must never block the status transition below —
                        # the postmortem is written and correct either way.
                        log.warning("postmortem_slack_post_failed", incident_id=incident_id, error=str(exc))

                await conn.execute(
                    "UPDATE incidents SET status = 'postmortem_done' WHERE id = %s AND status = 'resolved'",
                    (incident_id,),
                )
        return {}

    graph = StateGraph(PostmortemState)
    graph.add_node("gather_timeline", gather_timeline)
    graph.add_node("generate_postmortem_llm", generate_postmortem_llm)
    graph.add_node("post_postmortem", post_postmortem)
    graph.add_edge(START, "gather_timeline")
    graph.add_edge("gather_timeline", "generate_postmortem_llm")
    graph.add_edge("generate_postmortem_llm", "post_postmortem")
    graph.add_edge("post_postmortem", END)
    return graph.compile(checkpointer=checkpointer)
