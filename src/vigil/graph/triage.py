"""Triage graph (spec §13.2):

START → load_context ─┬→ fetch_commits → score_commits → rank_commits_llm ─┐
                      ├→ retrieve_runbooks ────────────────────────────────┤
                      └→ estimate_impact ──────────────────────────────────┤
                                                     compose_brief (join) ←┘
                                                     → post_slack → finalize → END
"""

from datetime import datetime
from typing import Any

import structlog
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from psycopg.types.json import Json

from vigil.commits.github import fetch_candidates
from vigil.commits.schemas import CommitAnalysis
from vigil.commits.scoring import score_commits
from vigil.graph.deps import Deps
from vigil.graph.nodes_util import degrading
from vigil.graph.state import TriageState
from vigil.impact.severity import classify
from vigil.ingest.queue import add_event
from vigil.llm.client import LLMUnavailable
from vigil.llm.prompts import build_brief_prompt, build_commit_ranking_prompt
from vigil.llm.schemas import BriefContent
from vigil.rag.retrieve import hybrid_search
from vigil.slack.blocks import build_brief

log = structlog.get_logger()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def build_triage_graph(deps: Deps, checkpointer: Any = None):
    settings = deps.settings

    @degrading("load_context", retries=2, backoff=0.5)
    async def load_context(state: TriageState) -> dict[str, Any]:
        service_name = state["alert"].get("labels", {}).get("service")
        service = deps.catalog.get(service_name)
        async with deps.pool.connection() as conn:  # also warms Neon on cold start
            await conn.execute("SELECT 1")
        return {"service": service, "errors": {}, "llm_calls_used": 0}

    @degrading("fetch_commits", retries=2, backoff=2.0)
    async def fetch_commits(state: TriageState) -> dict[str, Any]:
        service = state.get("service")
        if not service or not service.get("repo"):
            return {"commits": [], "errors": {"fetch_commits": "unknown service or no repo configured"}}
        starts_at = _parse(state["alert"]["starts_at"])
        commits = await fetch_candidates(
            repo=service["repo"],
            settings=settings,
            starts_at=starts_at,
            scenario_hint=state["alert"].get("labels", {}).get("scenario"),
        )
        for c in commits:
            c["committed_at"] = _iso(c["committed_at"])
        return {"commits": commits}

    @degrading("score_commits")
    async def score_commits_node(state: TriageState) -> dict[str, Any]:
        service = state.get("service")
        commits = state.get("commits") or []
        if not service or not commits:
            return {"commit_scores": []}
        async with deps.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT service, commit_shas, finished_at FROM deploy_events WHERE service = %s",
                    (service["name"],),
                )
                deploys = await cur.fetchall()
        parsed = [dict(c, committed_at=_parse(c["committed_at"])) for c in commits]
        scores = score_commits(
            parsed,
            service=service["name"],
            path_globs=service.get("path_globs", []),
            shared_globs=deps.catalog.shared_globs,
            deploys=deploys,
            starts_at=_parse(state["alert"]["starts_at"]),
        )
        return {"commit_scores": scores}

    @degrading("rank_commits_llm", retries=1, backoff=10.0)
    async def rank_commits_llm(state: TriageState) -> dict[str, Any]:
        scores = state.get("commit_scores") or []
        by_sha = {c["sha"]: c for c in state.get("commits") or []}
        top = [s for s in scores if s["score"] >= settings.commit_score_floor][: settings.commit_top_k]
        if not top:
            # Skip the LLM entirely — quota saved, honest "none" (spec §6.3).
            return {
                "commit_analysis": CommitAnalysis(
                    verdicts=[],
                    likely_culprit_sha=None,
                    no_culprit_reason=(
                        f"No commit in the last {settings.commit_lookback_hours}h scored above "
                        f"{settings.commit_score_floor} for this service"
                    ),
                ).model_dump()
            }
        candidates = [{"score": s["score"], "commit": by_sha[s["sha"]]} for s in top]
        system, user = build_commit_ranking_prompt(state["alert"], state.get("service"), candidates)
        analysis = await deps.llm.generate_structured(system, user, CommitAnalysis, "commit_ranking")

        # Anti-hallucination guards (spec §6.3): drop invented shas, apply floor.
        valid = {s["sha"] for s in top}
        verdicts = [v for v in analysis.verdicts if v.sha in valid]
        culprit = analysis.likely_culprit_sha if analysis.likely_culprit_sha in valid else None
        if culprit:
            best = max((v.confidence for v in verdicts if v.sha == culprit), default=0.0)
            if best < settings.confidence_floor:
                culprit = None
        result = CommitAnalysis(
            verdicts=verdicts,
            likely_culprit_sha=culprit,
            no_culprit_reason=analysis.no_culprit_reason
            if culprit is None
            else None,
        )
        return {"commit_analysis": result.model_dump(), "llm_calls_used": 1}

    @degrading("retrieve_runbooks", retries=2, backoff=1.0)
    async def retrieve_runbooks(state: TriageState) -> dict[str, Any]:
        chunks = await hybrid_search(deps.pool, deps.embedder, state["alert"])
        return {"runbook_chunks": chunks}

    @degrading("estimate_impact")
    async def estimate_impact(state: TriageState) -> dict[str, Any]:
        labels = state["alert"].get("labels", {})
        impact = classify(
            catalog=deps.catalog,
            service_name=labels.get("service"),
            alert_severity_label=labels.get("severity"),
            starts_at=_parse(state["alert"]["starts_at"]),
        )
        return {"impact": impact}

    @degrading("compose_brief", retries=1, backoff=10.0)
    async def compose_brief(state: TriageState) -> dict[str, Any]:
        system, user = build_brief_prompt(
            state["alert"],
            state.get("impact"),
            state.get("commit_analysis"),
            state.get("errors", {}).get("rank_commits_llm") or state.get("errors", {}).get("fetch_commits"),
            state.get("runbook_chunks") or [],
        )
        try:
            brief = await deps.llm.generate_structured(system, user, BriefContent, "brief_composition")
            return {"brief_text": brief.model_dump(), "llm_calls_used": 1}
        except LLMUnavailable as exc:
            # Deterministic fallback brief takes over in post_slack (ADR-007).
            return {"brief_text": None, "errors": {"compose_brief": str(exc)[:300]}}

    @degrading("post_slack", retries=3, backoff=2.0)
    async def post_slack(state: TriageState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        async with deps.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT slack_message_ts FROM incidents WHERE id = %s", (incident_id,)
            )
            row = await cur.fetchone()
        if row and row[0]:
            return {"slack_ts": row[0]}  # resumed run already posted — exactly-once
        payload = build_brief(
            incident={"id": incident_id},
            alert=state["alert"],
            impact=state.get("impact"),
            commit_analysis=state.get("commit_analysis"),
            commit_scores=state.get("commit_scores") or [],
            runbook_chunks=state.get("runbook_chunks") or [],
            brief_text=state.get("brief_text"),
            errors=state.get("errors", {}),
            repo=(state.get("service") or {}).get("repo"),
            dashboard_url=settings.dashboard_url,
            confidence_floor=settings.confidence_floor,
        )
        ts = await deps.slack.post_brief(incident_id, payload)
        async with deps.pool.connection() as conn:
            await conn.execute(
                "UPDATE incidents SET slack_message_ts = %s, severity = %s WHERE id = %s",
                (ts, (state.get("impact") or {}).get("severity", "SEV4"), incident_id),
            )
        return {"slack_ts": ts}

    @degrading("persist_commit_candidates", retries=2, backoff=1.0)
    async def persist_commit_candidates(state: TriageState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        by_sha = {c["sha"]: c for c in state.get("commits") or []}
        analysis = state.get("commit_analysis") or {}
        verdict_by_sha = {v["sha"]: v for v in analysis.get("verdicts", [])}
        async with deps.pool.connection() as conn:
            for s in state.get("commit_scores") or []:
                commit = by_sha.get(s["sha"], {})
                verdict = verdict_by_sha.get(s["sha"])
                await conn.execute(
                    """
                    INSERT INTO commit_candidates
                        (incident_id, sha, message, author, committed_at, files,
                        heuristic_score, feature_scores, llm_rank, llm_confidence, llm_rationale)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (incident_id, sha) DO UPDATE SET
                        heuristic_score = EXCLUDED.heuristic_score,
                        feature_scores = EXCLUDED.feature_scores,
                        llm_rank = EXCLUDED.llm_rank,
                        llm_confidence = EXCLUDED.llm_confidence,
                        llm_rationale = EXCLUDED.llm_rationale
                    """,
                    (
                        incident_id,
                        s["sha"],
                        commit.get("message"),
                        commit.get("author"),
                        commit.get("committed_at"),
                        Json(commit.get("files", [])),
                        s["score"],
                        Json(s["feature_scores"]),
                        verdict["rank"] if verdict else None,
                        verdict["confidence"] if verdict else None,
                        verdict["rationale"] if verdict else None,
                    ),
                )
        return {}


    @degrading("finalize", retries=2, backoff=1.0)
    async def finalize(state: TriageState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        analysis = state.get("commit_analysis") or {}
        await add_event(
            deps.pool,
            incident_id,
            "triage_finalized",
            {
                "scored": len(state.get("commit_scores") or []),
                "culprit": analysis.get("likely_culprit_sha"),
                "chunks": len(state.get("runbook_chunks") or []),
                "severity": (state.get("impact") or {}).get("severity"),
                "llm_calls": state.get("llm_calls_used", 0),
                "errors": state.get("errors", {}),
            },
        )
        return {}

    graph = StateGraph(TriageState)
    graph.add_node("load_context", load_context)
    graph.add_node("fetch_commits", fetch_commits)
    graph.add_node("score_commits", score_commits_node)
    graph.add_node("rank_commits_llm", rank_commits_llm)
    graph.add_node("retrieve_runbooks", retrieve_runbooks)
    graph.add_node("estimate_impact", estimate_impact)
    graph.add_node("persist_commit_candidates", persist_commit_candidates)
    graph.add_node("compose_brief", compose_brief)
    graph.add_node("post_slack", post_slack)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "fetch_commits")
    graph.add_edge("load_context", "retrieve_runbooks")
    graph.add_edge("load_context", "estimate_impact")
    graph.add_edge("fetch_commits", "score_commits")
    graph.add_edge("score_commits", "rank_commits_llm")
    # list-form edge = barrier join: compose_brief waits for ALL three branches
    # (separate add_edge calls would re-trigger it per branch at different supersteps)
    graph.add_edge(["rank_commits_llm", "retrieve_runbooks", "estimate_impact"], "persist_commit_candidates")
    graph.add_edge("persist_commit_candidates", "compose_brief")
    graph.add_edge("compose_brief", "post_slack")
    graph.add_edge("post_slack", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
