"""Feasibility-review finding contract (ADR-029 Layer C).

The post-gate feasibility reviewer emits structured findings describing why a
customer routing will not work. Findings are severity-tagged: ``blocking`` ones
loop the assignment-review gate (the customer must fix the routing or explicitly
accept the risk), ``advisory`` ones inform. Accepted blocking findings are
recorded on the assignment artifact so an approved-with-known-risk routing is
self-describing.

Version History:
- 1.0 (2026-10-16, #975): The version the value-set snapshot
  (``scripts/contract_value_snapshot.py``) tracks for ``FindingKind`` and
  ``FindingSeverity``. No schema change.
- 1.1 (2026-10-17, #976): ``FeasibilityFinding``: Unknown fields are accepted and kept
  (``extra="allow"``, was ``"forbid"``): a file written by a later MINOR loads in
  older code (ADR-008 Decision 5, #976).
- 1.2 (2026-10-18, #992): Added ``FindingKind.WRITE_SPLIT``: a table whose writes
  sit on more than one engine, which breaks its one source of truth (ADR-031
  Amendment 9). Blocking.
- 1.3 (2026-10-20, #1151): Added ``FindingKind.STRANDED_COUNT``: a key-scoped count left
  on an engine its waiver no longer covers, because a reroute held a customer's pin
  (ADR-031 Decision 2 Amendment 12). Blocking.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FindingKind(str, Enum):
    """What kind of infeasibility the finding describes."""

    READ_WRITE_SPLIT = "read_write_split"
    # A table whose writes (or locking reads) sit on more than one engine: it no
    # longer has one source of truth (ADR-031 Amendment 9, #992). Blocking.
    WRITE_SPLIT = "write_split"
    CO_DEPENDENCY_SPLIT = "co_dependency_split"
    # A co-dependent JOIN group co-located on ONE engine that has no server-side
    # complex-join support (e.g. the whole group pinned to DynamoDB). Not a split,
    # but the joins cannot run natively — they need a denormalized design or
    # application-side joining. Advisory (the schema designer denormalizes).
    CO_DEPENDENCY_ON_NON_JOIN_ENGINE = "co_dependency_on_non_join_engine"
    # A key-scoped COUNT on an engine that serves it only while its table's writes are
    # there (ADR-031 Decision 2 Amendment 12), whose table's writes are elsewhere: a
    # count the customer pinned, which a reroute held rather than move (#1151). Blocking.
    STRANDED_COUNT = "stranded_count"


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
    recommended_pattern: str | None = Field(
        None,
        description=(
            "For an advisory read/write split, the recommended replication/consistency "
            "pattern (e.g. CDC, zero-ETL, CQRS, cache-aside, Saga) that makes the split "
            "work. None when not applicable."
        ),
    )

    model_config = ConfigDict(extra="allow")
