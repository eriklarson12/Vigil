"""The three generation prompts, verbatim from tasks/todo.md §12.
Change them there and here together."""

import json
from typing import Any

SHORT_SHA = 10  # the sha width the model is shown, and so the width it answers with

COMMIT_RANKING_SYSTEM = """You are the commit-analysis engine of Vigil, an automated incident responder.
Given a production alert and a set of candidate commits (each pre-scored by
deterministic heuristics), rank the commits by how plausibly they caused the
alert. Base every judgment ONLY on the evidence provided: the alert's labels
and annotations, and each commit's message, file list, and diff. Cite specific
files or diff lines in each rationale. If NO commit plausibly explains this
alert, set likely_culprit_sha to null and explain why in no_culprit_reason —
an honest "none" is far more valuable than a forced guess. Do not invent
commits or reference shas not listed below."""

BRIEF_SYSTEM = """You write the on-call Slack brief for Vigil, an automated incident responder.
You are given: the alert, deterministic impact figures (severity, estimated
affected requests, downstream services), the commit analysis (may be absent),
and up to 4 runbook excerpts retrieved by search. Your job:
1. impact_narrative: ONE sentence stating the user impact, using ONLY the
   numbers provided (do not invent metrics).
2. cause_summary: 1-2 sentences on the likely cause. If commit analysis is
   absent or found no culprit, say so plainly and suggest where to look based
   on the alert.
3. runbook_citation: pick the ONE most relevant excerpt for THIS alert and
   return its slug and heading_path, with one line on why it applies. If none
   of the excerpts is relevant, return null — do not force a citation.
4. next_steps: up to 3 concrete actions for the on-call engineer, ordered.
Tone: calm, specific, zero filler. Use only the provided information."""

POSTMORTEM_SYSTEM = """You write blameless postmortems for Vigil. You are given the complete incident
record: alert details, the analysis timeline, commit verdicts, the runbook
used, impact figures computed from actual duration, and how it was resolved.
Write a postmortem following the provided schema. Rules:
- Blameless: describe systems and processes, never fault individuals. Name no
  authors; refer to changes by commit sha.
- The timeline must contain only events present in the provided record.
- Impact must use the provided figures verbatim.
- Root cause: commit analysis confidence >= 0.65 -> state it as the probable
  root cause with its rationale; lower or absent -> state the root cause as
  undetermined and list hypotheses from the available evidence.
- Action items: 2-5, each concrete and assignable, derived from what the
  record shows went wrong or slowly."""

DIFF_LINE_CAP = 120


def _truncate_patch(patch: str) -> str:
    lines = patch.splitlines()
    if len(lines) <= DIFF_LINE_CAP:
        return patch
    return "\n".join(lines[:DIFF_LINE_CAP]) + "\n[diff truncated at 120 lines]"


def build_commit_ranking_prompt(
    alert: dict[str, Any], service: dict[str, Any] | None, candidates: list[dict[str, Any]]
) -> tuple[str, str]:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    parts = [
        "## Alert",
        f"name: {labels.get('alertname')}",
        f"service: {labels.get('service')}"
        + (f" — tier {service.get('tier')}, user_facing={service.get('user_facing')}" if service else ""),
        f"severity label: {labels.get('severity')}",
        f"scenario: {labels.get('scenario', 'n/a')}",
        f"started: {alert.get('starts_at')}",
        f"summary: {annotations.get('summary', '')}",
        f"description: {annotations.get('description', '')}",
        "",
        "## Candidate commits (pre-scored by heuristics, highest first)",
    ]
    for c in candidates:
        commit = c["commit"]
        parts += [
            f"### Commit {commit['sha'][:SHORT_SHA]} — heuristic score {c['score']:.2f}",
            f"author: {commit['author']}   committed: {commit['committed_at']}",
            f"message: {commit['message']}",
            "files: "
            + ", ".join(f"{f['path']} (+{f['additions']}/-{f['deletions']})" for f in commit["files"]),
            "diff (may be truncated):",
            "```diff",
            _truncate_patch(commit.get("patch", "")),
            "```",
            "",
        ]
    parts.append("Rank all candidates and identify the likely culprit, or return null if none is plausible.")
    return COMMIT_RANKING_SYSTEM, "\n".join(parts)


def build_brief_prompt(
    alert: dict[str, Any],
    impact: dict[str, Any] | None,
    commit_analysis: dict[str, Any] | None,
    commit_error: str | None,
    runbook_chunks: list[dict[str, Any]],
) -> tuple[str, str]:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    if commit_analysis is None:
        commit_block = f"UNAVAILABLE: {commit_error or 'commit analysis did not run'}"
    elif commit_analysis.get("likely_culprit_sha") is None:
        commit_block = f"NO PLAUSIBLE COMMIT: {commit_analysis.get('no_culprit_reason', 'none identified')}"
    else:
        commit_block = json.dumps(commit_analysis, indent=2, default=str)

    impact_block = (
        f"severity: {impact['severity']} · est. affected: {impact.get('est_requests_affected')}"
        f" requests ({impact.get('baseline_rpm')}/min × {impact.get('minutes_open')} min)\n"
        f"downstream user-facing services: {', '.join(impact.get('blast_radius', [])) or 'none'}"
        if impact
        else "UNAVAILABLE"
    )

    chunk_lines = []
    for i, chunk in enumerate(runbook_chunks):
        chunk_lines += [
            f"[{i}] slug={chunk['runbook_slug']} heading={chunk['heading_path']}",
            chunk["content"],
            "",
        ]
    user = "\n".join(
        [
            "## Alert",
            f"{labels.get('alertname')} on {labels.get('service')} — {annotations.get('summary', '')}",
            f"scenario: {labels.get('scenario', 'n/a')}",
            f"started: {alert.get('starts_at')}",
            "",
            "## Deterministic impact (computed, do not alter)",
            impact_block,
            "",
            "## Commit analysis",
            commit_block,
            "",
            "## Runbook excerpts (retrieved by search — may or may not be relevant)",
            *(chunk_lines or ["(none retrieved)"]),
        ]
    )
    return BRIEF_SYSTEM, user


def build_postmortem_prompt(record: dict[str, Any]) -> tuple[str, str]:
    return POSTMORTEM_SYSTEM, json.dumps(record, indent=2, default=str)
