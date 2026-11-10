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
- 1.1 (2026-11-07, #1918): Added optional ``Rule.moves_rows`` (default true): false when
  step 1 creates the table but DMS does not move its rows (a tsvector column), with a
  ``not_moved`` warning finding.
- 1.2 (2026-11-08, #2042): Added optional ``Rule.key_columns``: the table's key (the
  primary key, else the smallest non-partial, expression-free unique index on NOT NULL
  columns), and the ``key_not_validatable`` warning finding for a key on a column type
  DMS cannot validate on; ``no_key`` now also covers a table whose only unique index
  allows NULLs.
- 1.3 (2026-11-09, #1947): Added optional ``StepContract.routines`` (``StepRoutines``,
  ``StepRoutine``): the routines step 1 carries in its deploy script, in order, and why
  each other one is not carried; left out of the dump when None.
- 1.4 (2026-11-10, #1948): Added optional ``StepRoutine.source_state``, ``fires`` and
  ``action_order`` (a PostgreSQL trigger's source state; a MySQL trigger's timing, event
  and ACTION_ORDER), which the cutover script reads; left out of the dump when None.
- 1.5 (2026-11-10, #2107): Rule.moves_rows description no longer says when to rebuild
  (no cutover timing)
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
# The finding code of a rule whose table the step creates but does not move (#1918).
NOT_MOVED = "not_moved"
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
    # Left out of the dump when empty, so a keyless rule keeps the bytes it had before (#2042).
    key_columns: list[str] = Field(
        default_factory=list,
        exclude_if=lambda cols: not cols,
        description=(
            "The table's key (src/tools/data_movement/validation_key.py): the primary key, "
            "else the smallest non-partial, expression-free unique index whose columns are all "
            "NOT NULL; empty when it has none, or in a contract written before the field "
            "existed (read primary_key then)"
        ),
    )
    fields: list[FieldRule] = Field(default_factory=list)
    status: RuleStatus = "valid"
    findings: list[Finding] = Field(default_factory=list)
    # Left out of the dump when true, so a contract with no such rule keeps the bytes (and the
    # SHA-256 a plan pins) it had before the field existed.
    moves_rows: bool = Field(
        True,
        exclude_if=lambda moves: moves is True,
        description=(
            "False when the step creates the target table but does not move its rows: a "
            "column of a type the mover does not migrate (DMS and tsvector, #1918); a "
            "not_moved warning finding says why and what to rebuild"
        ),
    )

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

    @model_validator(mode="after")
    def _not_moved_says_why(self) -> "Rule":
        if not self.moves_rows and not any(f.code == NOT_MOVED for f in self.findings):
            raise ValueError(
                f"{self.source_table}: a rule that moves no rows needs a {NOT_MOVED} finding"
            )
        return self


RoutineCarry = Literal["carried", "not_carried", "deferred_to_cutover"]
RoutineEnabledAs = Literal["enabled", "disabled", "absent"]


class StepRoutine(BaseModel):
    """One routine of the step's source, and whether step 1 carries it (#1947).

    Identifiers and reason codes only, never a routine's text: the exact ``CREATE`` stays
    in the job's routine sources, and only the deploy script step 1 generates holds it."""

    routine_id: str = Field(..., min_length=1, description="The routine-migration row's id")
    kind: Literal["function", "procedure", "trigger", "event"] = Field(
        ..., description="What the routine is"
    )
    schema_name: str | None = Field(None, description="The routine's schema or database")
    name: str = Field(..., min_length=1, description="The routine's own name")
    specific_name: str | None = Field(
        None, description="With kind and schema_name, finds its exact CREATE in the routine sources"
    )
    table: str | None = Field(None, description="A trigger's table, as the collection names it")
    signature: str | None = Field(
        None, description="Its argument types, as the routine-migration row has them"
    )
    returns: str | None = Field(None, description="A function's return type, when known")
    carry: RoutineCarry = Field(
        ...,
        description=(
            "carried: in the deploy script; not_carried: blocks cutover until a reason is "
            "settled; deferred_to_cutover: a MySQL trigger or event, created by the cutover "
            "script, since MySQL cannot create one disabled"
        ),
    )
    order: int | None = Field(
        None, ge=1, description="Its place in the deploy script, from 1; set only when carried"
    )
    migration_status: Literal["as_is", "rewritten"] | None = Field(
        None, description="The routine-migration status of a carried routine"
    )
    enabled_as: RoutineEnabledAs | None = Field(
        None,
        description=(
            "The state step 1 leaves it in: enabled, disabled (a PostgreSQL trigger) or "
            "absent (deferred to cutover); None when not carried"
        ),
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Why it is not carried: rule ids and reason codes, never text",
    )
    # #1948: what the cutover script needs of a trigger, from the collection; each is left
    # out of the dump when None, so a section written before them keeps its bytes.
    source_state: Literal["O", "A", "R", "D", "enabled", "disabled"] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description=(
            "PostgreSQL trigger: its state on the source, which the cutover script sets: as "
            "pg_trigger.tgenabled spells it (O origin, A always, R replica, D disabled) when the "
            "collection gives the letter; enabled or disabled when it records only that (an "
            "always or replica-only trigger is then not told apart); None when not reported"
        ),
    )
    fires: str | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="MySQL trigger: its timing and event, such as BEFORE INSERT",
    )
    action_order: int | None = Field(
        None,
        ge=1,
        exclude_if=lambda value: value is None,
        description=(
            "MySQL trigger: its place among its table's triggers of one timing and event "
            "(information_schema ACTION_ORDER), the order the cutover script creates them in"
        ),
    )

    @model_validator(mode="after")
    def _carry_fits(self) -> "StepRoutine":
        carried = self.carry == "carried"
        if carried != (self.order is not None) or carried != (self.migration_status is not None):
            raise ValueError(
                f"{self.kind} {self.routine_id}: order and status are set when carried"
            )
        if self.carry == "not_carried" and not self.reasons:
            raise ValueError(f"{self.kind} {self.routine_id}: not carried without a reason")
        if self.carry != "not_carried" and self.reasons:
            raise ValueError(f"{self.kind} {self.routine_id}: {self.carry} with reasons")
        expected = {"carried": ("enabled", "disabled"), "deferred_to_cutover": ("absent",)}
        if self.enabled_as not in expected.get(self.carry, (None,)):
            raise ValueError(f"{self.kind} {self.routine_id}: enabled_as {self.enabled_as}")
        return self


class StepRoutines(BaseModel):
    """The routines step 1 carries, in deploy order, and why each other one is not (#1947).

    Step 1 generates the deploy script from it; the customer runs that script after the
    full load, and the read-only check script after it; Prove reads the check's output."""

    source: Literal["routine_migration", "none"] = Field(
        ...,
        description=(
            "routine_migration: read from routines/v<N>/routine_migration.json; none: the "
            "collection lists no routine this pipeline carries"
        ),
    )
    routine_migration_version: int | None = Field(
        None, ge=1, description="The routine-migration artifact read (routines/v<N>)"
    )
    total: int = Field(0, ge=0, description="Routines in the section")
    carried: int = Field(0, ge=0, description="Routines in the deploy script")
    not_carried: int = Field(0, ge=0, description="Routines that block cutover")
    deferred: int = Field(0, ge=0, description="Routines the cutover script creates")
    routines: list[StepRoutine] = Field(
        default_factory=list, description="Carried in deploy order, then the rest by id"
    )

    @model_validator(mode="after")
    def _counts(self) -> "StepRoutines":
        counted = {"carried": 0, "not_carried": 0, "deferred_to_cutover": 0}
        for row in self.routines:
            counted[row.carry] += 1
        if (self.total, self.carried, self.not_carried, self.deferred) != (
            len(self.routines),
            counted["carried"],
            counted["not_carried"],
            counted["deferred_to_cutover"],
        ):
            raise ValueError("the routines section's counts disagree with its rows")
        orders = [r.order for r in self.routines if r.order is not None]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("carried routines are not in deploy order 1..carried")
        if (self.source == "none") != (self.routine_migration_version is None) or (
            self.source == "none" and self.routines
        ):
            raise ValueError("source none has no rows and no routine-migration version")
        return self


class StepContract(BaseModel):
    """The transformation contract of one migration step."""

    contract_version: str = Field(
        default="1.5", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
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
    # Left out of the dump when None, so a contract written before #1947 keeps its bytes
    # (and the SHA-256 a plan pins).
    routines: StepRoutines | None = Field(
        None,
        exclude_if=lambda section: section is None,
        description=(
            "Step 1: the routines it carries in its deploy script, in order, and why each "
            "other one is not carried; None in a contract written before the section existed"
        ),
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
