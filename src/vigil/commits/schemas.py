"""Structured-output schemas for commit analysis (spec §6.3)."""

from typing import Literal

from pydantic import BaseModel, Field


class CommitVerdict(BaseModel):
    sha: str
    rank: int  # 1 = most likely
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=600)  # must cite specific diff lines
    suggested_action: Literal["revert", "rollback_deploy", "config_fix", "investigate"]


class CommitAnalysis(BaseModel):
    verdicts: list[CommitVerdict]
    likely_culprit_sha: str | None = None
    no_culprit_reason: str | None = None
