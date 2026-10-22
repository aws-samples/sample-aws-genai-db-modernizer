"""The schema design trace is a versioned contract (ADR-008 Decision 1, #1258).

``schema-<engine>/[v<N>/]design_trace.json`` records how a schema was designed:
the designer and PE review calls, the PE reviewer's routing flags, token usage and,
for DynamoDB (designed in groups), each group's trace and the merge's mechanical
notes. Other phases read it back: the post-schema router reads the routing flags
from it (``routing_flags_from_trace``) and synthesis reads it for the report. It was
a plain dict, so a renamed field dropped the flags without any guard noticing.

Three shapes, one body (:class:`DesignTraceRun`):

- a single design's trace (Bedrock mode, one pass, or the external PE review
  recorded on a design): the body alone;
- one DynamoDB group's trace (``design_trace_group_<G>.json``,
  :class:`DesignTraceGroup`): the body, versioned on its own;
- the merged trace (:class:`DesignTrace`): the body plus ``total_groups``,
  ``groups`` (each group's body, whose version is the merged file's) and
  ``merge_reconciliation``. ``--merge`` writes it from the group files.

Every field of the body is optional, as a trace holds whatever the run recorded: a
deterministic run has no token usage, a design with no PE review has no routing
flags. An absent field is never written as null. Every model keeps unknown keys
(``extra="allow"``): an older or newer trace loads (ADR-008 Decision 5), and a
round trip through the model loses no key a designer added without declaring it.
A trace saved before this contract has no ``contract_version`` and still loads.

Version History:
- 1.0 (2026-10-21, #1258): Initial version, the keys the trace already had
  (``trace``, ``routing_flags``, ``token_usage``, ``delta_merge``, ``pe_review``;
  ``total_groups``, ``groups`` and ``merge_reconciliation`` on the merged trace)
  plus ``contract_version``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.contracts.decision_trace_base import KEEP_UNKNOWN, is_none


class DesignTraceEntry(BaseModel):
    """One model call in the run: a designer pass, a PE review, or a failed review."""

    model_config = KEEP_UNKNOWN

    type: str = Field(description="designer, pe_review or pe_error")
    # The numbers below are recorded after a paid model call and some come from the
    # model's own answer, so a type quirk must not throw the trace away: they are
    # kept as given (``Any``), and the writer's tests pin what the code records.
    iteration: Any
    elapsed_seconds: Any = Field(default=None, exclude_if=is_none)
    # A designer pass: the counts of what it produced (tables, indexes, patterns, ...).
    output_summary: dict[str, Any] | None = Field(default=None, exclude_if=is_none)
    # A PE review: its verdict, how many changes it asked for, and a clipped summary.
    verdict: str | None = Field(default=None, exclude_if=is_none)
    change_requests: Any = Field(default=None, exclude_if=is_none)
    summary: str | None = Field(default=None, exclude_if=is_none)
    # A PE review that failed.
    error: str | None = Field(default=None, exclude_if=is_none)


class DesignTraceReview(BaseModel):
    """A PE review run outside the designer loop (``--llm-mode external``, #1059).

    ``source`` says where it came from; the rest is the engine's own review answer
    (verdict, change requests, notes, flags) or, for a failed review, ``error``.
    """

    model_config = KEEP_UNKNOWN

    source: str
    error: str | None = Field(default=None, exclude_if=is_none)


class DeltaMergeTrace(BaseModel):
    """How an Aurora designer's delta was merged into its draft."""

    model_config = KEEP_UNKNOWN

    summary: dict[str, Any]
    errors: list[str]
    warnings: list[str]


class DesignTraceRun(BaseModel):
    """What one design run recorded: the body every shape of the trace shares."""

    model_config = KEEP_UNKNOWN

    trace: list[DesignTraceEntry] | None = Field(default=None, exclude_if=is_none)
    # The PE reviewer's flags of the accepted design, kept as written: the router
    # checks each entry on its own (``parse_model_flags``), so one bad entry never
    # drops the rest.
    routing_flags: list[dict[str, Any]] | None = Field(default=None, exclude_if=is_none)
    # Input, output and cache tokens and calls over the run's model calls, as the model
    # client reported them: values are kept as given.
    token_usage: dict[str, Any] | None = Field(default=None, exclude_if=is_none)
    delta_merge: DeltaMergeTrace | None = Field(default=None, exclude_if=is_none)
    pe_review: DesignTraceReview | None = Field(default=None, exclude_if=is_none)
    # What the writer dropped from a model's answer because it did not fit the contract
    # (``<path>: <reason>``, no values); absent when nothing was dropped.
    dropped_fields: list[str] | None = Field(default=None, exclude_if=is_none)


class DesignTraceGroup(DesignTraceRun):
    """``schema-dynamodb/v<N>/design_trace_group_<G>.json``: one group's design trace."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )


class DesignTrace(DesignTraceRun):
    """``schema-<engine>/[v<N>/]design_trace.json``: how the schema was designed."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    # The merged trace of a grouped design (DynamoDB).
    total_groups: int | None = Field(default=None, exclude_if=is_none)
    groups: list[DesignTraceRun] | None = Field(default=None, exclude_if=is_none)
    # The merge's mechanical reconciliation notes; an empty list when there are none.
    merge_reconciliation: list[str] | None = Field(default=None, exclude_if=is_none)
