# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Transformation Contract — Contract Model

One file per migration step (ADR-033 Decision 1, #1762):
``<db>/<job>/transformation-contract/v<N>/step-<k>.json``, ``v<N>`` the assignment
version and ``k`` the step number. Each rule states, for one source table, the target
table and, per column, the target column, its type and the lineage operation that
builds it. The dry run and the data movement layer read only these files; a step
pins the hash of every step whose output it reads (``depends_on``).

Version History:
- 1.0 (2026-11-03, #1762): Initial version: step, wave and its shape, target,
  depends_on, status with its reason, rule and unsupported counts, views, and rules
  with their fields, status and findings.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .lineage_ops import (
    TABLE_ID_PATTERN,
    CopyOp,
    LineageOp,
    TransformOp,
    UnsupportedOp,
    reads_own_row,
)

InputSchema = Literal["collector", "step-1"]
StepStatus = Literal["valid", "has_blocked_rules", "no_relational_anchor", "staged_move_pending"]
RuleStatus = Literal["valid", "blocked"]
Severity = Literal["error", "warning"]
TableClass = Literal["stays", "leaves", "replicated_out", "unread"]


class Finding(BaseModel):
    """One validation or shape-check result about a rule."""

    code: str = Field(..., min_length=1, description="Stable id, e.g. unknown_column, no_key")
    severity: Severity = Field(..., description="An error blocks its rule; a warning never does")
    message: str = Field(..., min_length=1, description="One sentence for the report")
    table_id: str | None = Field(None, description="The source table, when it is about one")
    column: str | None = Field(None, description="The source column, when it is about one")


class DependsOn(BaseModel):
    """A step whose output this step reads, pinned by the hash of its contract file.

    The pin is SHA-256 over ``canonical_json(contract)``
    (``src/tools/transformation_contract/hashing.py``). Writers write exactly those
    bytes, never a re-indented dump, so the file's own SHA-256 equals the pin."""

    step: int = Field(..., ge=1)
    contract_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the pinned step's file: the canonical_json bytes, as written",
    )


class TargetSpec(BaseModel):
    """The engine the step writes."""

    engine: str = Field(..., min_length=1, description="Engine id, e.g. aurora_mysql")


class FieldRule(BaseModel):
    """One target column and the operation that builds it."""

    source_column: str = Field(..., min_length=1)
    target_column: str = Field(..., min_length=1)
    target_type: str = Field(..., min_length=1, description="The target column's declared type")
    op: LineageOp

    @model_validator(mode="after")
    def _source_column_is_the_op_column(self) -> "FieldRule":
        if (
            isinstance(self.op, CopyOp | TransformOp)
            and self.source_column != self.op.column.column
        ):
            raise ValueError(
                f"field {self.target_column}: source_column {self.source_column} "
                f"is not the op's column {self.op.column.column}"
            )
        return self


class Rule(BaseModel):
    """The rows of one source table into one target table."""

    input_schema: InputSchema = Field(
        ...,
        description="Which relational schema the input rows have: the collector's, or step 1's output",
    )
    source_table: str = Field(
        ..., pattern=TABLE_ID_PATTERN, description="Collector table id (schema.table)"
    )
    target_table: str = Field(..., min_length=1, description="Target table name")
    table_class: TableClass | None = Field(
        None, description="Wave 1 table class, when known (#1129)"
    )
    primary_key: list[str] = Field(default_factory=list, description="Key columns; empty if none")
    fields: list[FieldRule] = Field(default_factory=list)
    status: RuleStatus = "valid"
    findings: list[Finding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _own_row(self) -> "Rule":
        for f in self.fields:
            if not reads_own_row(f.op, self.source_table):
                raise ValueError(
                    f"field {f.target_column}: copy and transform read the rule's own row "
                    f"({self.source_table})"
                )
        return self

    @model_validator(mode="after")
    def _status_agrees_with_findings(self) -> "Rule":
        has_error = any(f.severity == "error" for f in self.findings)
        if (self.status == "blocked") != has_error:
            raise ValueError(
                f"rule status {self.status} disagrees with its findings: "
                "a rule is blocked exactly when it has an error finding"
            )
        return self


class StepContract(BaseModel):
    """The transformation contract of one migration step."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    job_id: str = Field(..., description="The assessment job this contract belongs to")
    assignment_version: int = Field(
        ..., ge=1, description="The assignment version the contract was built from"
    )
    step: int = Field(..., ge=1, description="The migration step number, from 1")
    wave: int | None = Field(None, ge=1, description="The migration wave this step comes from")
    wave_shape: str | None = Field(None, description="Wave 1's homogeneity marker, for step 1")
    target: TargetSpec = Field(..., description="The target engine of the step")
    depends_on: list[DependsOn] = Field(
        default_factory=list,
        description="Earlier steps whose output this step reads, with their hashes",
    )
    status: StepStatus = Field(..., description="Whether the step has rules to run")
    status_reason: str | None = Field(None, description="Why a step has no rules")
    blocked_rule_count: int = Field(0, ge=0, description="Rules blocked by an error finding")
    unsupported_count: int = Field(0, ge=0, description="Fields no operation can express")
    views: list[str] = Field(
        default_factory=list, description="Views: no rows, recreated from the schema"
    )
    rules: list[Rule] = Field(
        default_factory=list, description="One rule per target table the step builds"
    )

    @model_validator(mode="after")
    def _counts_and_status(self) -> "StepContract":
        blocked = sum(1 for r in self.rules if r.status == "blocked")
        unsupported = sum(
            1 for r in self.rules for f in r.fields if isinstance(f.op, UnsupportedOp)
        )
        for d in self.depends_on:
            if d.step >= self.step:
                raise ValueError(f"depends_on step {d.step} is not earlier than step {self.step}")
        if blocked != self.blocked_rule_count:
            raise ValueError(
                f"blocked_rule_count is {self.blocked_rule_count}, the rules have {blocked}"
            )
        if unsupported != self.unsupported_count:
            raise ValueError(
                f"unsupported_count is {self.unsupported_count}, the fields have {unsupported}"
            )
        if self.status in ("no_relational_anchor", "staged_move_pending"):
            if self.rules or not self.status_reason:
                raise ValueError(
                    f"status {self.status}: a step with no rules lists none and states a status_reason"
                )
        elif (self.status == "has_blocked_rules") != (blocked > 0):
            raise ValueError(f"status {self.status} disagrees with {blocked} blocked rules")
        return self
