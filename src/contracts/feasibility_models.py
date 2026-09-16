"""Feasibility-review finding contract (ADR-029 Layer C).

The post-gate feasibility reviewer emits structured findings describing why a
customer routing will not work. Findings are severity-tagged: ``blocking`` ones
loop the assignment-review gate (the customer must fix the routing or explicitly
accept the risk), ``advisory`` ones inform. Accepted blocking findings are
recorded on the assignment artifact so an approved-with-known-risk routing is
self-describing.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FindingKind(str, Enum):
    """What kind of infeasibility the finding describes."""

    READ_WRITE_SPLIT = "read_write_split"
    CO_DEPENDENCY_SPLIT = "co_dependency_split"


class FindingSeverity(str, Enum):
    """``blocking`` loops the gate; ``advisory`` informs and proceeds."""

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class FeasibilityFinding(BaseModel):
    """One structured feasibility finding about a customer routing."""

    kind: FindingKind = Field(..., description="The category of infeasibility")
    severity: FindingSeverity = Field(..., description="blocking or advisory")
    table: str | None = Field(
        None, description="Table involved (read/write split); None for query-group findings"
    )
    engines: list[str] = Field(
        default_factory=list, description="Engines involved in the split, sorted"
    )
    query_ids: list[str] = Field(
        default_factory=list, description="Query IDs involved in the finding, sorted"
    )
    message: str = Field(..., description="Human-facing explanation and remediation")

    model_config = ConfigDict(extra="forbid")
