"""
PE Review Contract — structured feedback from the PE reviewer agent.

The PE reviewer evaluates a DynamoDB schema design output and either
approves it or returns specific, actionable change requests. The designer
agent uses this feedback to revise its output.

Version History:
- 1.0 (2026-03-24): Initial version
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class ReviewVerdict(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    # Keep old aliases for backward compat with existing PE skill prompts
    CHANGES_NEEDED = "CHANGES_REQUESTED"


class ChangeCategory(str, Enum):
    """Categories of PE feedback — ordered by impact."""

    TABLE_BOUNDARY = "table_boundary"
    KEY_DESIGN = "key_design"
    GSI_NECESSITY = "gsi_necessity"
    DENORMALIZATION = "denormalization"
    JOIN_TYPE = "join_type"
    ACCESS_PATTERN = "access_pattern"
    COST_CONCERN = "cost_concern"
    MISSING_TRADE_OFF = "missing_trade_off"
    OVER_ENGINEERING = "over_engineering"
    SCOPE_CHALLENGE = "scope_challenge"
    ETL_LINEAGE = "etl_lineage"


class Severity(str, Enum):
    """How critical the change is."""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class ChangeRequest(BaseModel):
    """A single actionable change request from the PE reviewer."""

    category: ChangeCategory
    severity: Severity
    target: str = Field(
        ...,
        description="What to change — table name, pattern_id, attribute name, "
        "or GSI name. Be specific.",
    )
    current_state: str = Field(
        ...,
        description="What the designer did — brief factual description.",
    )
    requested_change: str = Field(
        ...,
        description="What the PE wants instead — specific and actionable.",
    )
    rationale: str = Field(
        ...,
        description="Why this change matters — cost, complexity, correctness, "
        "or operational impact.",
    )


class PEReviewResult(BaseModel):
    """Structured output from the PE reviewer agent."""

    verdict: ReviewVerdict
    summary: str = Field(
        ...,
        description="One-paragraph overall assessment of the design quality.",
    )
    change_requests: list[ChangeRequest] = Field(
        default_factory=list,
        description="Specific changes needed. Empty when verdict=approved.",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="What the designer got right — reinforces good patterns.",
    )
    pe_notes: list[str] = Field(
        default_factory=list,
        description="Advisory notes that don't block approval but should be "
        "documented as trade-offs or migration notes.",
    )

    @model_validator(mode="after")
    def validate_changes_match_verdict(self) -> PEReviewResult:
        blockers = [cr for cr in self.change_requests if cr.severity == Severity.BLOCKER]
        if self.verdict == ReviewVerdict.APPROVED and blockers:
            raise ValueError("Cannot approve with blocker-severity change requests.")
        if self.verdict == ReviewVerdict.CHANGES_REQUESTED and not self.change_requests:
            raise ValueError("Must provide change_requests when verdict=CHANGES_REQUESTED.")
        return self
