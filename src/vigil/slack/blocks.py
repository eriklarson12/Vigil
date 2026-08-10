"""Block Kit builders (spec §9), including the deterministic fallback brief.

THE BRIEF ALWAYS POSTS: build_brief handles every degraded combination —
missing commit analysis, empty retrieval, absent impact, and a failed
brief-composition LLM call (brief_text=None -> deterministic wording).
"""

from typing import Any

SEV_STYLE = {
    "SEV1": ("\U0001f534", "#E01E5A"),  # red circle
    "SEV2": ("\U0001f7e0", "#F2A33C"),  # orange circle
    "SEV3": ("\U0001f7e1", "#ECB22E"),  # yellow circle
    "SEV4": ("⚪", "#CCCCCC"),      # white circle
}


def confidence_bar(confidence: float) -> str:
    filled = round(confidence * 5)
    return "▓" * filled + "░" * (5 - filled) + f" {round(confidence * 100)}%"


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _cause_text(
    commit_analysis: dict[str, Any] | None,
    commit_scores: list[dict[str, Any]],
    errors: dict[str, str],
    repo: str | None,
    confidence_floor: float,
) -> str:
    for node in ("fetch_commits", "rank_commits_llm", "load_context"):
        if commit_analysis is None and node in errors:
            return f"*Likely cause:* commit analysis unavailable ({errors[node]})"
    if commit_analysis is None:
        return "*Likely cause:* commit analysis unavailable"
    sha = commit_analysis.get("likely_culprit_sha")
    if not sha:
        reason = commit_analysis.get("no_culprit_reason") or "no commit in the lookback window matches"
        return f"*Likely cause:* no likely commit identified — {reason}"
    verdict = next((v for v in commit_analysis["verdicts"] if v["sha"] == sha), None)
    conf = verdict["confidence"] if verdict else 0.0
    qualifier = "Likely culprit" if conf >= 0.65 else "Possible culprit"
    link = f"<https://{repo}/commit/{sha}|`{sha[:10]}`>" if repo else f"`{sha[:10]}`"
    lines = [f"*{qualifier}:* {link}  {confidence_bar(conf)}"]
    if verdict:
        lines.append(f"_{verdict['rationale']}_")
        lines.append(f"*Suggested action:* {verdict['suggested_action'].replace('_', ' ')}")
    return "\n".join(lines)


def build_brief(
    *,
    incident: dict[str, Any],
    alert: dict[str, Any],
    impact: dict[str, Any] | None,
    commit_analysis: dict[str, Any] | None,
    commit_scores: list[dict[str, Any]],
    runbook_chunks: list[dict[str, Any]],
    brief_text: dict[str, Any] | None,
    errors: dict[str, str],
    repo: str | None,
    dashboard_url: str,
    confidence_floor: float = 0.4,
) -> dict[str, Any]:
    labels = alert.get("labels", {})
    severity = (impact or {}).get("severity", "SEV4")
    emoji, color = SEV_STYLE.get(severity, SEV_STYLE["SEV4"])
    service = labels.get("service", "unknown")
    alertname = labels.get("alertname", "alert")

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {severity} · {service} · {alertname}"},
        }
    ]

    # Impact
    if brief_text and brief_text.get("impact_narrative"):
        impact_line = brief_text["impact_narrative"]
    elif impact and impact.get("est_requests_affected"):
        impact_line = (
            f"~{impact['est_requests_affected']:,} requests affected "
            f"({impact['baseline_rpm']}/min × {impact['minutes_open']} min open)."
        )
    else:
        impact_line = "Impact figures unavailable."
    blast = (impact or {}).get("blast_radius") or []
    if blast:
        impact_line += f"\n*Downstream user-facing:* {', '.join(blast)}"
    if (impact or {}).get("unknown_service"):
        impact_line += "\n:warning: unknown service — add it to services.yaml"
    blocks.append(_section(f"*Impact*\n{impact_line}"))

    # Cause
    cause = _cause_text(commit_analysis, commit_scores, errors, repo, confidence_floor)
    if brief_text and brief_text.get("cause_summary"):
        cause += f"\n{brief_text['cause_summary']}"
    blocks.append(_section(cause))

    # Runbook
    citation = (brief_text or {}).get("runbook_citation")
    if citation:
        blocks.append(
            _section(
                f"*Runbook:* `{citation['slug']}` → {citation['heading_path']}\n_{citation['why']}_"
            )
        )
    elif runbook_chunks:
        top = runbook_chunks[0]
        blocks.append(
            _section(f"*Runbook (top match):* `{top['runbook_slug']}` → {top['heading_path']}")
        )
    else:
        blocks.append(_section("*Runbook:* no matching runbook found"))

    # Next steps (LLM only — the deterministic fallback omits them)
    if brief_text and brief_text.get("next_steps"):
        steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(brief_text["next_steps"]))
        blocks.append(_section(f"*Next steps*\n{steps}"))

    if errors:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "degraded: " + ", ".join(sorted(errors))}
                ],
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"incident `{incident['id']}` · started {alert.get('starts_at')}",
                }
            ],
        }
    )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Mark resolved"},
                    "action_id": "resolve_incident",
                    "value": str(incident["id"]),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Dashboard"},
                    "url": f"{dashboard_url}/incidents/{incident['id']}",
                },
            ],
        }
    )

    return {
        "text": f"{emoji} {severity} · {service} · {alertname}",
        "attachments": [{"color": color, "blocks": blocks}],
    }


def build_postmortem_message(markdown: str, incident_id: str) -> dict[str, Any]:
    # Slack mrkdwn is not full markdown; keep the structure simple.
    text = markdown if len(markdown) <= 11000 else markdown[:11000] + "\n_[truncated]_"
    return {
        "text": f"Postmortem for incident {incident_id}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "\U0001f4dd Postmortem"}},
            _section(text),
        ],
    }
