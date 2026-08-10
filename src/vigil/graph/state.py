"""Graph state schemas (spec §13). Values must be checkpoint-serializable:
datetimes travel as ISO strings, models as .model_dump() dicts."""

import operator
from typing import Annotated, Any, TypedDict


class TriageState(TypedDict, total=False):
    incident_id: str
    alert_id: str
    alert: dict[str, Any]            # {labels, annotations, starts_at: iso}
    service: dict[str, Any] | None   # services.yaml entry (+ name)
    commits: list[dict[str, Any]]    # raw candidates, committed_at as iso
    commit_scores: list[dict[str, Any]]
    commit_analysis: dict[str, Any] | None
    runbook_chunks: list[dict[str, Any]]
    impact: dict[str, Any] | None
    brief_text: dict[str, Any] | None
    slack_ts: str | None
    # merged across parallel branches:
    errors: Annotated[dict[str, str], operator.or_]
    llm_calls_used: Annotated[int, operator.add]


class PostmortemState(TypedDict, total=False):
    incident_id: str
    record: dict[str, Any]           # gather_timeline output
    postmortem: dict[str, Any] | None
    markdown: str | None
    errors: Annotated[dict[str, str], operator.or_]
