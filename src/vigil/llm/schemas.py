"""Structured-output schemas for the brief and postmortem calls (spec §10, §12)."""

from typing import Literal

from pydantic import BaseModel, Field


class RunbookCitation(BaseModel):
    slug: str
    heading_path: str
    why: str = Field(max_length=200)


class BriefContent(BaseModel):
    impact_narrative: str = Field(max_length=300)
    cause_summary: str = Field(max_length=500)
    runbook_citation: RunbookCitation | None = None
    next_steps: list[str] = Field(max_length=3)


class TimelineEntry(BaseModel):
    time: str
    event: str


class ActionItem(BaseModel):
    description: str
    owner: str
    priority: Literal["P0", "P1", "P2"]


class Postmortem(BaseModel):
    summary: str
    timeline: list[TimelineEntry]
    root_cause: str
    contributing_factors: list[str]
    impact: str
    resolution: str
    went_well: list[str]
    went_poorly: list[str]
    action_items: list[ActionItem] = Field(min_length=2, max_length=5)
