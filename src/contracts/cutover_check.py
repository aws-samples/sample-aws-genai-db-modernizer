# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
CutoverCheckResult Pydantic Models
Version: 1.0
Last Modified: 2026-11-10
Description: What the customer's run of the cutover routine check script shows (#1948)

Written at ``<db>/<job>/data-movement/step-<k>/cutover-check.json`` by
``scripts/run_dms_step.py cutover-check``, from the output file of the read-only check
script ``run_dms_step.py cutover-scripts`` generates (``cutover-check.sql``). Cutover is
the customer's: they run the cutover script and then the check, and the modernizer never
connects to their databases, never runs either script and never signals the cutover. The
output is read strictly against the step contract's routines section (an unknown routine,
a malformed line or a file for another contract is refused, never guessed).

On Aurora PostgreSQL each trigger step 1 carried (created disabled) is confirmed when it
exists in its source state (``tgenabled`` O, A, R or D: the cutover script enables it the
same way, or leaves a disabled one disabled). On Aurora MySQL each trigger and event step
1 left to cutover (``deferred_to_cutover``) is confirmed when it exists, and an event when
its status is the one its source ``CREATE EVENT`` gives (``ENABLE``, ``DISABLE`` or
``DISABLE ON REPLICA``); when an event is created enabled, the cluster's
``event_scheduler`` must be ON. A trigger or event step 1 cannot carry, or one the cutover
script leaves out for a reason the contract shows (its source state or its firing order
unknown), fails the result, named with its reasons.

No row holds a routine's text, a hash of it or a line of the customer's file: only the
contract's routine ids, fixed problem codes and counts.

Version History:
- 1.0 (2026-11-10, #1948): First version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.contracts.routine_check import RoutineNotCarried

CutoverProblem = Literal["missing", "trigger_state_differs", "event_status_differs"]
CutoverLeftOutReason = Literal["trigger_state_unknown", "trigger_order_unknown"]


class CutoverMismatch(BaseModel):
    """A trigger or event the cutover check output does not confirm, by the contract's id."""

    routine_id: str = Field(..., min_length=1, description="The contract row's routine_id")
    kind: Literal["trigger", "event"] = Field(..., description="The routine's kind")
    problem: CutoverProblem = Field(
        ...,
        description=(
            "missing: not on the target; trigger_state_differs: a PostgreSQL trigger in "
            "another state than on the source; event_status_differs: a MySQL event whose "
            "status is not the one its source CREATE EVENT gives"
        ),
    )


class CutoverLeftOut(BaseModel):
    """A trigger the cutover script leaves out for a reason the step contract shows."""

    routine_id: str = Field(..., min_length=1, description="The contract row's routine_id")
    kind: Literal["trigger", "event"] = Field(..., description="The routine's kind")
    reason: CutoverLeftOutReason = Field(
        ...,
        description=(
            "trigger_state_unknown: the collection does not report the PostgreSQL trigger's "
            "state; trigger_order_unknown: one of two or more MySQL triggers on one table, "
            "timing and event whose firing order the collection does not give"
        ),
    )


class CutoverCheckResult(BaseModel):
    """The cutover routine check, read from the customer's output file."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    job_id: str = Field(..., min_length=1, description="The job")
    database_name: str = Field(..., min_length=1, description="The job's database name")
    step: int = Field(1, ge=1, description="The migration step")
    target_engine: Literal["aurora_postgresql", "aurora_mysql"] = Field(
        ..., description="The Aurora engine the cutover script ran on"
    )
    contract_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the step contract the check script was generated from",
    )
    checked_at: datetime = Field(..., description="When the output file was read")
    status: Literal["pass", "fail"] = Field(
        ..., description="pass: every expected trigger and event confirmed, none not carried"
    )
    expected: int = Field(
        ...,
        ge=0,
        description=(
            "Routines the cutover acts on: carried PostgreSQL triggers to enable, or MySQL "
            "triggers and events to create"
        ),
    )
    confirmed: int = Field(..., ge=0, description="Expected routines the check confirms")
    rows_read: int = Field(..., ge=0, description="Routine lines in the output file")
    mismatches: list[CutoverMismatch] = Field(
        default_factory=list, description="Routines not confirmed, by routine id"
    )
    not_carried: list[RoutineNotCarried] = Field(
        default_factory=list,
        description="Triggers and events step 1 does not carry, by routine id",
    )
    left_out: list[CutoverLeftOut] = Field(
        default_factory=list,
        description="Triggers the cutover script leaves out for a reason the contract shows",
    )
    events_scheduled: int = Field(
        0, ge=0, description="Expected MySQL events created enabled: they need the scheduler ON"
    )
    event_scheduler: Literal["ON", "OFF", "DISABLED"] | None = Field(
        None,
        description="MySQL: @@GLOBAL.event_scheduler as the check read it; None on PostgreSQL",
    )

    @property
    def scheduler_off(self) -> bool:
        """Events are created enabled and the scheduler that runs them is not ON."""
        return self.events_scheduled > 0 and self.event_scheduler != "ON"

    @model_validator(mode="after")
    def _consistent(self) -> CutoverCheckResult:
        if self.confirmed + len(self.mismatches) != self.expected:
            raise ValueError("confirmed and mismatched routines do not add up to the expected")
        failed = self.mismatches or self.not_carried or self.left_out or self.scheduler_off
        if self.status != ("fail" if failed else "pass"):
            raise ValueError(
                "status disagrees with the mismatches, not-carried and left-out routines and "
                "the event scheduler"
            )
        if len({(m.kind, m.routine_id) for m in self.mismatches}) != len(self.mismatches):
            raise ValueError("a routine is named twice among the mismatches")
        return self
