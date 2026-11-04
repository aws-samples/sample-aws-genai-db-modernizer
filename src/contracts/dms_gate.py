# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DMS Step Gate — Contract Model

The evidence a customer decides a step's cutover on (#1800), written at
``<db>/<job>/data-movement/step-<k>/gate.json`` by ``src.tools.data_movement.dms_gate``
from read-only DMS, CloudWatch and CloudFormation calls: load, lag and parity checks,
each table's DMS statistics, and the named exceptions (tables DMS could not validate,
keyless tables whose LOB values can be lost, unsupported fields, skipped rules). The
decision is the customer's (``cutover_decision``), stated by the fixed message
``cutover_message_id`` names (``src.shared.cutover_message``). Later steps deploy only
on ``pass`` for the same ``contract_sha256``.

Version History:
- 1.0 (2026-11-03, #1800): Initial version.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

GateStatus = Literal["pass", "pending", "fail"]
NOT_VALIDATED = "not validated: no primary or unique key"
_ORDER = ("load", "lag", "parity")
_STATUS_ORDER = ("pass", "pending", "fail")  # best to worst


class GateCheck(BaseModel):
    """One of the three checks the gate runs."""

    id: Literal["load", "lag", "parity"] = Field(
        ..., description="Which check: load, lag or parity"
    )
    status: GateStatus = Field(..., description="pass, pending (not yet decidable) or fail")
    summary: str = Field(..., min_length=1, description="One plain line a customer can read")
    evidence: dict[str, str] = Field(
        default_factory=dict, description="Named values the status rests on"
    )
    reasons: list[str] = Field(
        default_factory=list, description="Why the check is not pass, one per finding"
    )


class GateTable(BaseModel):
    """One contract table as DMS reports it."""

    table: str = Field(..., description="Source table id (schema.table)")
    rows_loaded: int | None = Field(
        None, ge=0, description="DMS FullLoadRows; None if not reported"
    )
    table_state: str | None = Field(None, description="DMS table state, as reported")
    validation_state: str | None = Field(None, description="DMS validation state, as reported")
    validation_failed_records: int = Field(0, ge=0, description="DMS ValidationFailedRecords")
    validation_suspended_records: int = Field(0, ge=0, description="DMS ValidationSuspendedRecords")
    status: Literal["pass", "pending", "fail", "exception"] = Field(
        ..., description="The table's verdict; exception means a named exception covers it"
    )
    reason: str | None = Field(None, description="Why the table is not pass")


_FIXED_REASON_KINDS = ("table", "view")


class GateException(BaseModel):
    """A table, view, field or rule the gate names because it cannot vouch for it.

    ``table`` and ``view`` are tables and views DMS does not validate, with the fixed
    ``NOT_VALIDATED`` reason. ``keyless_lob`` is a keyless table with LOB columns (DMS
    supports LOB data types only on tables with a primary key, so values can be lost).
    ``unsupported_field`` is a moved column no operation can express; ``skipped_rule`` is
    a contract table the step does not move because an error finding blocks it.
    """

    table: str = Field(..., description="Source table id (schema.table)")
    kind: Literal["table", "view", "keyless_lob", "unsupported_field", "skipped_rule"] = Field(
        ..., description="What is being named"
    )
    column: str | None = Field(None, description="The column, for unsupported_field only")
    reason: str = Field(
        NOT_VALIDATED, min_length=1, description="Plain reason; fixed for table and view"
    )
    rows_loaded: int | None = Field(
        None, ge=0, description="DMS FullLoadRows; None for a view or when not reported"
    )

    @model_validator(mode="after")
    def _consistent(self) -> "GateException":
        if self.kind in _FIXED_REASON_KINDS and self.reason != NOT_VALIDATED:
            raise ValueError(f"reason for a {self.kind} must be {NOT_VALIDATED!r}")
        if (self.kind == "unsupported_field") != (self.column is not None):
            raise ValueError("column is set for unsupported_field and only for it")
        return self


class DmsGate(BaseModel):
    """Step 1's gate result (#1800)."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version"
    )
    job_id: str = Field(..., description="The assessment job")
    database_name: str = Field(..., description="The source database")
    step: int = Field(1, ge=1, description="The migration step the gate covers")
    region: str = Field(..., min_length=1, description="AWS Region the checks read")
    stack_name: str = Field(..., description="The step's CloudFormation stack")
    task_arn: str = Field(..., description="The DMS replication task read")
    checked_at: datetime = Field(..., description="When the checks ran")
    window_minutes: int = Field(
        ..., ge=1, le=1440, description="Minutes of CloudWatch lag history judged"
    )
    max_lag_seconds: int = Field(
        ..., ge=0, description="Largest lag, in seconds, the lag check accepts"
    )
    contract_sha256: str = Field(
        ..., pattern=r"^[0-9a-f]{64}$", description="SHA-256 of the contract file the stack runs"
    )
    status: GateStatus = Field(..., description="The worst of the three checks")
    cutover_decision: Literal["customer"] = Field(
        "customer", description="Who decides the cutover: always the customer"
    )
    cutover_message_id: str = Field(
        ..., min_length=1, description="Id of the fixed cutover message"
    )
    checks: list[GateCheck] = Field(..., description="load, lag and parity, in that order")
    tables: list[GateTable] = Field(default_factory=list, description="Sorted by table")
    exceptions: list[GateException] = Field(default_factory=list, description="Sorted by table")

    @model_validator(mode="after")
    def _consistent(self) -> "DmsGate":
        if tuple(c.id for c in self.checks) != _ORDER:
            raise ValueError(f"checks must be {_ORDER}, in order")
        worst = max((c.status for c in self.checks), key=_STATUS_ORDER.index)
        if self.status != worst:
            raise ValueError(f"status {self.status} disagrees with the checks ({worst})")
        return self
