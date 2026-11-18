# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
RoutineCheckResult Pydantic Models
Version: 1.1
Last Modified: 2026-11-11
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
disabled. A MySQL trigger or event is left to the triggers-enable script and must be absent.
A routine step 1 cannot carry fails the result as a mismatch does. ``proof`` is
``static``: the check confirms what exists and runs nothing; ``label`` is the routine
test's state in the shared words (#2429).

No row holds a routine's text, a hash of it or a line of the customer's file: only the
contract's routine ids, fixed problem codes and counts.

Version History:
- 1.0 (2026-11-09, #1947): First version.
- 1.1 (2026-11-11, #2113): Added the problem code ``present_before_triggers_enable``,
  written in place of ``present_before_cutover``, which a result saved before #2113
  still holds and is read as the new code.
- 1.2 (2026-11-18, #2429): ``label`` takes the routine test's state in the shared words,
  ``checked, not tested yet`` or ``tested on a test Aurora``; a result saved with
  ``checked, not load-tested`` is read as ``checked, not tested yet``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RoutineProblem = Literal[
    "missing",
    "signature_mismatch",
    "trigger_enabled",
    "present_before_triggers_enable",
    "present_before_cutover",  # legacy alias (#2113): read from saved files, never written
]
#: Problem codes a result saved before #2113 holds, read as today's code.
LEGACY_PROBLEMS = {"present_before_cutover": "present_before_triggers_enable"}  # legacy alias
#: The one label per state of the routine test (#2429), for every deliverable, step 1
#: line and stored result: before a test on a test Aurora on the job's routines file as it
#: is now, and after one. ``src.shared.routine_facts`` reads them from here.
NOT_TESTED_LABEL: Literal["checked, not tested yet"] = "checked, not tested yet"
TESTED_LABEL: Literal["tested on a test Aurora"] = "tested on a test Aurora"
#: The labels an artifact saved before #2429 holds -> today's (the one copy of the map).
LEGACY_TEST_LABELS: dict[str, str] = {
    "checked, not load-tested": NOT_TESTED_LABEL,
    "load-tested on a test Aurora": TESTED_LABEL,
}
#: What a check result says until a test ran (#1945; the words since #2429).
STATIC_LABEL = NOT_TESTED_LABEL


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
            "replication; present_before_triggers_enable: a MySQL trigger or event the "
            "triggers-enable script creates is already there (a result saved before #2113 "
            "names it by its old code, read as this one)"
        ),
    )

    @field_validator("problem", mode="before")
    @classmethod
    def _today_code(cls, value: object) -> object:
        """A code a result saved before #2113 holds, as today's."""
        return LEGACY_PROBLEMS.get(value, value) if isinstance(value, str) else value


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
        default="1.2", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
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
        "static",
        description=(
            "static: this check confirms what exists on Aurora and runs nothing; whether a "
            "test on a test Aurora ran is the label"
        ),
    )
    label: Literal[
        "checked, not tested yet", "tested on a test Aurora", "checked, not load-tested"
    ] = Field(
        NOT_TESTED_LABEL,
        description=(
            "The routine test's state in the words every deliverable shows (#2429): "
            "checked, not tested yet, or tested on a test Aurora on the job's routines "
            "file as it is now; a result saved before #2429 holds 'checked, not "
            "load-tested', read as 'checked, not tested yet'"
        ),
    )
    status: Literal["pass", "fail"] = Field(
        ..., description="pass: every carried routine confirmed and none not carried"
    )
    carried: int = Field(..., ge=0, description="Routines the deploy script carries")
    confirmed: int = Field(..., ge=0, description="Carried routines the check confirms")
    deferred: int = Field(
        ..., ge=0, description="MySQL triggers and events left to the triggers-enable script"
    )
    rows_read: int = Field(..., ge=0, description="Routine lines in the output file")
    mismatches: list[RoutineMismatch] = Field(
        default_factory=list, description="Routines not confirmed, by routine id"
    )
    not_carried: list[RoutineNotCarried] = Field(
        default_factory=list, description="Routines step 1 does not carry, by routine id"
    )

    @field_validator("label", mode="before")
    @classmethod
    def _todays_label(cls, value: object) -> object:
        """A label a result saved before #2429 holds, read as today's."""
        return LEGACY_TEST_LABELS.get(value, value) if isinstance(value, str) else value

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
