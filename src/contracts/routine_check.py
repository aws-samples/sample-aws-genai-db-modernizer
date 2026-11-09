# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
RoutineCheckResult Pydantic Models
Version: 1.0
Last Modified: 2026-11-09
Description: What the customer's run of step 1's routine check script shows (#1947)

Written at ``<db>/<job>/data-movement/step-<k>/routines-check.json`` by
``scripts/run_dms_step.py routines-check``, from the output file of the read-only check
script step 1 generates (``routines-check.sql``). The modernizer never connects to the
customer's databases: the customer runs the deploy script and then the check script, and
this result reads the check's output, validated strictly against the step contract's
routines section (an unknown routine, a malformed line or a file for another contract
is refused, never guessed).

Each carried routine is confirmed when it exists on the target (its ``CREATE`` ran with
the body checked: ``check_function_bodies`` on PostgreSQL, the whole body parsed on
MySQL, so it compiled), with its source signature, and, for a PostgreSQL trigger,
disabled. A MySQL trigger or event is deferred to the cutover script and must be absent.
A routine step 1 cannot carry fails the result as a mismatch does. ``proof`` is
``static``: checked, not load-tested, until the load-and-run proof (#1945) exists.

No row holds a routine's text, a hash of it or a line of the customer's file: only the
contract's routine ids, fixed problem codes and counts.

Version History:
- 1.0 (2026-11-09, #1947): First version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

RoutineProblem = Literal[
    "missing", "signature_mismatch", "trigger_enabled", "present_before_cutover"
]
#: What the label of a routine check says while no load-and-run proof exists (#1945).
STATIC_LABEL: Literal["checked, not load-tested"] = "checked, not load-tested"


class RoutineMismatch(BaseModel):
    """A routine the check output does not confirm, by the contract's id."""

    routine_id: str = Field(..., min_length=1, description="The contract row's routine_id")
    kind: Literal["function", "procedure", "trigger", "event"] = Field(
        ..., description="The routine's kind"
    )
    problem: RoutineProblem = Field(
        ...,
        description=(
            "missing: not on the target; signature_mismatch: on the target with other "
            "argument types; trigger_enabled: a PostgreSQL trigger left enabled during "
            "replication; present_before_cutover: a MySQL trigger or event created before "
            "the cutover script"
        ),
    )


class RoutineNotCarried(BaseModel):
    """A routine step 1 does not carry, with the contract's reasons."""

    routine_id: str = Field(..., min_length=1, description="The contract row's routine_id")
    kind: Literal["function", "procedure", "trigger", "event"] = Field(
        ..., description="The routine's kind"
    )
    reasons: list[str] = Field(..., min_length=1, description="Rule ids and reason codes")


class RoutineCheckResult(BaseModel):
    """Step 1's routine check, read from the customer's output file."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    job_id: str = Field(..., min_length=1, description="The job")
    database_name: str = Field(..., min_length=1, description="The job's database name")
    step: int = Field(1, ge=1, description="The migration step")
    target_engine: Literal["aurora_postgresql", "aurora_mysql"] = Field(
        ..., description="The Aurora engine the routines were deployed to"
    )
    contract_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the step contract the check script was generated from",
    )
    checked_at: datetime = Field(..., description="When the output file was read")
    proof: Literal["static"] = Field(
        "static", description="static: checked, not load-tested (no load-and-run proof yet)"
    )
    label: Literal["checked, not load-tested"] = Field(
        STATIC_LABEL, description="The words every deliverable shows for this proof"
    )
    status: Literal["pass", "fail"] = Field(
        ..., description="pass: every carried routine confirmed and none not carried"
    )
    carried: int = Field(..., ge=0, description="Routines the deploy script carries")
    confirmed: int = Field(..., ge=0, description="Carried routines the check confirms")
    deferred: int = Field(..., ge=0, description="MySQL triggers and events left to cutover")
    rows_read: int = Field(..., ge=0, description="Routine lines in the output file")
    mismatches: list[RoutineMismatch] = Field(
        default_factory=list, description="Routines not confirmed, by routine id"
    )
    not_carried: list[RoutineNotCarried] = Field(
        default_factory=list, description="Routines step 1 does not carry, by routine id"
    )

    @model_validator(mode="after")
    def _consistent(self) -> RoutineCheckResult:
        failed = {(m.kind, m.routine_id) for m in self.mismatches}
        if self.confirmed > self.carried:
            raise ValueError("more routines confirmed than carried")
        if self.status != ("fail" if self.mismatches or self.not_carried else "pass"):
            raise ValueError("status disagrees with the mismatches and not-carried routines")
        if len(failed) != len(self.mismatches):
            raise ValueError("a routine is named twice among the mismatches")
        return self
