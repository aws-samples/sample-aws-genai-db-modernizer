# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
VerifyResult — Contract Model

Step 1's optional, independent comparison of the source and Aurora (#1998), written at
``<db>/<job>/data-movement/step-<k>/verify.json`` by ``scripts/run_dms_step.py verify``.
The modernizer never connects to the customer's databases: the customer runs the two
read-only scripts ``verify-scripts`` generated (``verify-source.sql``, ``verify-target.sql``),
once or twice each, and this result reads their output files, strictly, as untrusted input
(a file for another contract, side or engine, cut short, or naming a table the scripts do
not ask for is refused whole, never guessed).

Per moved table: ``match`` (the same row count and checksum, in the latest run),
``changing`` (they differ and the source or Aurora changed between the customer's two
runs), ``differ`` (they differ and neither side changed, or there was one run only),
``not_compared`` (a script counted rows whose hash came out NULL: never ``match``) or
``missing`` (a script printed no line for the table on a side). No row holds a value, a
checksum or a line of the customer's files: only the contract's table ids, the row counts
and the results. Verify is optional and never changes the gate (``gate_effect`` is
``none``).

Version History:
- 1.0 (2026-11-09, #1998): First version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

VerifyStatus = Literal["match", "changing", "differ", "not_compared", "missing"]
#: Best to worst: the result's ``status`` is its worst table's.
STATUS_ORDER: tuple[str, ...] = ("match", "changing", "differ", "not_compared", "missing")


class VerifyTable(BaseModel):
    """One moved table's comparison, by the contract's table id."""

    table_id: str = Field(..., min_length=1, description="The contract rule's source table")
    status: VerifyStatus = Field(
        ..., description="match, changing, differ, not_compared or missing"
    )
    dms_validated: bool = Field(
        ..., description="Whether DMS validation can check the table too (it has a key)"
    )
    source_rows: int | None = Field(
        None, ge=0, description="The source's row count in the latest run; None when missing"
    )
    target_rows: int | None = Field(
        None, ge=0, description="Aurora's row count in the latest run; None when missing"
    )
    missing_on: list[Literal["source", "target"]] = Field(
        default_factory=list, description="The sides whose output has no line for the table"
    )
    unhashed_on: list[Literal["source", "target"]] = Field(
        default_factory=list,
        description="The sides whose line counts rows with a NULL hash (not compared)",
    )

    @model_validator(mode="after")
    def _consistent(self) -> VerifyTable:
        if (self.status == "missing") != bool(self.missing_on):
            raise ValueError(f"{self.table_id}: missing exactly when a side has no line for it")
        if (self.status == "not_compared") != bool(self.unhashed_on):
            raise ValueError(f"{self.table_id}: not_compared exactly when a side has NULL hashes")
        return self


class VerifyNotChecked(BaseModel):
    """A moved table the scripts leave out, with a fixed reason code."""

    table_id: str = Field(..., min_length=1, description="The contract rule's source table")
    reason: Literal["unsafe_name"] = Field(
        ..., description="unsafe_name: a table or column name holds a control character"
    )


class VerifyResult(BaseModel):
    """Step 1's verify result, read from the customer's output files."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    job_id: str = Field(..., min_length=1, description="The job")
    database_name: str = Field(..., min_length=1, description="The job's database name")
    step: int = Field(1, ge=1, description="The migration step")
    source_engine: Literal["postgresql", "mysql"] = Field(..., description="The source family")
    target_engine: Literal["aurora_postgresql", "aurora_mysql"] = Field(
        ..., description="The Aurora engine"
    )
    contract_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the step contract the scripts were generated from",
    )
    checked_at: datetime = Field(..., description="When the output files were read")
    second_run: bool = Field(..., description="Whether the customer ran each script twice")
    gate_effect: Literal["none"] = Field(
        "none", description="Verify is optional: it never changes the gate"
    )
    status: VerifyStatus = Field(..., description="The worst table's status")
    tables: list[VerifyTable] = Field(..., min_length=1, description="Sorted by table id")
    not_checked: list[VerifyNotChecked] = Field(
        default_factory=list, description="Moved tables the scripts leave out, by table id"
    )

    @model_validator(mode="after")
    def _consistent(self) -> VerifyResult:
        worst = max((t.status for t in self.tables), key=STATUS_ORDER.index)
        if self.status != worst:
            raise ValueError(f"status {self.status} disagrees with the tables ({worst})")
        ids = [t.table_id for t in self.tables]
        if ids != sorted(set(ids)):
            raise ValueError("tables must be sorted by table id, each once")
        if not self.second_run and any(t.status == "changing" for t in self.tables):
            raise ValueError("changing needs a second run: the source changed between two runs")
        return self
