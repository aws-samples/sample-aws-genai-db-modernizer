# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
ViewCheckResult Pydantic Models
Version: 1.0
Last Modified: 2026-11-15
Description: What the customer's run of step 1's view check script shows (#2082)

Written at ``<db>/<job>/data-movement/step-<k>/views-check.json`` by
``scripts/run_dms_step.py views-check``, from the output file of the read-only check
script step 1 generates (``views-check.sql``). The modernizer never connects to the
customer's databases: the customer runs the views deploy script and then the check
script, and this result reads the check's output, validated strictly against the step
contract's views section (an unknown view, a malformed line or a file for another
contract is refused, never guessed).

Each carried view is confirmed when it exists on the target as a view with the source's
columns: the same names in the same order and, where the collection gave a type, the
same type. A view step 1 does not carry is listed with the contract's reasons; it is a
named exception of the gate, never a failure of this result.

No row holds a view's text or a line of the customer's file: only the contract's view
ids and column names, fixed problem codes and counts.

Version History:
- 1.0 (2026-11-15, #2082): First version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ViewProblem = Literal["missing", "not_a_view", "columns_differ", "column_types_differ"]


class ViewMismatch(BaseModel):
    """A carried view the check output does not confirm, by the contract's id."""

    view_id: str = Field(..., min_length=1, description="The contract row's view_id")
    problem: ViewProblem = Field(
        ...,
        description=(
            "missing: not on the target; not_a_view: a relation of that name that is not a "
            "view; columns_differ: other column names or another order; column_types_differ: "
            "a column of another type"
        ),
    )
    columns: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "The source columns (the contract's names) that differ, and each column the "
            "target has past the source's last by its position, up to 20"
        ),
    )


class ViewNotCarried(BaseModel):
    """A view step 1 does not carry, with the contract's reasons."""

    view_id: str = Field(..., min_length=1, description="The contract row's view_id")
    reasons: list[str] = Field(..., min_length=1, description="Reason codes")


class ViewCheckResult(BaseModel):
    """Step 1's view check, read from the customer's output file."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    job_id: str = Field(..., min_length=1, description="The job")
    database_name: str = Field(..., min_length=1, description="The job's database name")
    step: int = Field(1, ge=1, description="The migration step")
    target_engine: Literal["aurora_postgresql", "aurora_mysql"] = Field(
        ..., description="The Aurora engine the views were created on"
    )
    contract_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the step contract the check script was generated from",
    )
    checked_at: datetime = Field(..., description="When the output file was read")
    status: Literal["pass", "fail"] = Field(
        ..., description="pass: every carried view confirmed; fail: a mismatch"
    )
    carried: int = Field(..., ge=0, description="Views the deploy script creates")
    confirmed: int = Field(..., ge=0, description="Carried views the check confirms")
    rows_read: int = Field(..., ge=0, description="View lines in the output file")
    mismatches: list[ViewMismatch] = Field(
        default_factory=list, description="Carried views not confirmed, by view id"
    )
    not_carried: list[ViewNotCarried] = Field(
        default_factory=list, description="Views step 1 does not carry, by view id"
    )

    @model_validator(mode="after")
    def _consistent(self) -> ViewCheckResult:
        if self.confirmed + len(self.mismatches) != self.carried:
            raise ValueError("each carried view is confirmed or a mismatch")
        if self.status != ("fail" if self.mismatches else "pass"):
            raise ValueError("status disagrees with the mismatches")
        if len({m.view_id for m in self.mismatches}) != len(self.mismatches):
            raise ValueError("a view is named twice among the mismatches")
        return self
