# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
RoutineMigrationContract Pydantic Models
Version: 1.0
Last Modified: 2026-11-09
Description: How each collected routine migrates to its Aurora target (#1934)

Written at ``<db>/<job>/routines/v<N>/routine_migration.json``, keyed on the assignment
version it read, by the deterministic routine-migration phase
(``src.tools.routines.migration``, ADR-034). One row per PostgreSQL or MySQL function,
procedure, trigger and event the collection lists, with:

- its identity: ``kind`` and ``routine_id`` (unique together; a PostgreSQL function's id
  names its argument types, so overloads differ), its schema and name, and the
  ``specific_name`` that finds its exact ``CREATE`` among the job's routine sources;
- its ``signature`` (argument types) and ``returns``, identifiers and types only;
- its ``dependencies`` (and ``tables_leaving_aurora``: the tables it names that the
  end-state assignment moves off Aurora, information only, since step 1 mirrors every
  table), its ``deploy_order`` (1-based, every routine after the routines
  it names) and the ``cycle`` it is part of, if any;
- its ``status``: ``as_is``, ``rewritten`` or ``needs_attention`` (``model_repaired`` is
  reserved for the model repair of #1940 and is never written yet), with ``reasons``
  (failed rule ids, then fixed reason codes);
- the rewrites applied, by rule id, and the check results that did not pass, as checked
  before the rewrites (``rules_passed`` counts the rest; a rewrite's rule says which
  check it resolves).

No row holds a routine's text, a diff of it or a hash of it: the exact text, literals
included, stays only in the job's routine sources (ADR-034 Decision 4), which the
artifact names by ``(kind, schema_name, specific_name)``. ``source_text`` says whether that text was there
and exact; a routine is carried only from exact text.

Version History:
- 1.0 (2026-11-09, #1934): First version.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

RoutineKind = Literal["function", "procedure", "trigger", "event"]
MigrationStatus = Literal["as_is", "rewritten", "model_repaired", "needs_attention"]
SourceText = Literal["exact", "not_exact", "not_collected"]
#: The fixed reason codes a row's ``reasons`` may hold after its failed rule ids.
REASON_CODES: tuple[str, ...] = (
    "source_text_not_collected",
    "source_text_not_exact",
    "depends_on_table_not_collected",
    "depends_on_routine_not_collected",
    "dependency_cycle",
    "depends_on_needs_attention",
)
_STATUSES: tuple[MigrationStatus, ...] = ("as_is", "rewritten", "model_repaired", "needs_attention")


class RoutineRef(BaseModel):
    """A routine of this artifact, by its kind and id."""

    kind: RoutineKind = Field(..., description="The routine's kind")
    routine_id: str = Field(..., min_length=1, description="The routine's routine_id")


class RoutineDependencyEntry(BaseModel):
    """A table or routine the routine names, as the checks read it (lower case)."""

    kind: Literal["table", "routine"] = Field(..., description="What it names")
    name: str = Field(
        ..., min_length=1, description="schema.name (the routine's own schema when unqualified)"
    )
    routine_kind: RoutineKind | None = Field(
        None, description="A routine's kind when the text tells it, else None"
    )
    found: bool = Field(
        ...,
        description=(
            "A table: the collection has it (step 1 mirrors the whole database onto "
            "Aurora, whatever the end-state assignment says). A routine: the collection "
            "lists it"
        ),
    )
    routines: list[RoutineRef] = Field(
        default_factory=list, description="The routines of this artifact it resolves to"
    )


class TableLeavingAurora(BaseModel):
    """A table the routine names that the end-state assignment moves off Aurora. Step 1
    still mirrors it onto Aurora, so it never changes the routine's status: it says the
    routine has to change before the table's own move."""

    table: str = Field(..., min_length=1, description="schema.name, as the collection names it")
    engines: list[str] = Field(
        default_factory=list, description="The engines the assignment serves it from, sorted"
    )
    source_of_truth: str | None = Field(
        None, description="The engine its writes land on, when a query writes it"
    )


class RoutineCheckEntry(BaseModel):
    """A check rule that did not pass: its id, outcome and what it found (never text)."""

    rule_id: str = Field(..., min_length=1, description="The routine check rule's id")
    outcome: Literal["fail", "unknown"] = Field(
        ..., description="fail, or unknown (it could not be checked; never a pass)"
    )
    findings: list[str] = Field(
        default_factory=list, description="What failed or why it is unknown, never routine text"
    )


class RoutineRewriteEntry(BaseModel):
    """A rewrite applied to the routine's header, by its rule (the change itself is not
    kept: a deploy applies the rule to the exact text again)."""

    rule_id: str = Field(..., min_length=1, description="The routine rewrite rule's id")
    rewrite: Literal["definer", "owner", "search_path"] = Field(
        ..., description="What the rewrite changes"
    )


class RoutineMigration(BaseModel):
    """How one routine migrates."""

    routine_id: str = Field(
        ...,
        min_length=1,
        description=(
            "schema.name(argument types) for a PostgreSQL function or procedure, "
            "schema.table.trigger for a PostgreSQL trigger, schema.name otherwise"
        ),
    )
    kind: RoutineKind = Field(..., description="function, procedure, trigger or event")
    schema_name: str | None = Field(None, description="The routine's schema or database")
    name: str = Field(..., min_length=1, description="The routine's own name")
    specific_name: str | None = Field(
        None,
        description=(
            "The routine sources' specific_name for it (a trigger's 'table.trigger'); with "
            "kind and schema_name it finds the exact CREATE. None when the collection has none"
        ),
    )
    table: str | None = Field(None, description="A trigger's table, as the collection names it")
    signature: str | None = Field(
        None,
        description=(
            "Its argument types, in order (PostgreSQL's identity arguments: no OUT argument, "
            "no default value); None for a trigger or event, or when no text was read"
        ),
    )
    returns: str | None = Field(None, description="A function's return type, when known")
    source_text: SourceText = Field(
        ...,
        description=(
            "exact: the routine sources hold its CREATE as the server prints it; not_exact: "
            "a MySQL rebuild around a literal; not_collected: no routine sources entry"
        ),
    )
    body_status: Literal["collected", "not_readable", "opted_out", "not_supported"] | None = Field(
        None, description="The collection's body_status for it"
    )
    status: MigrationStatus = Field(
        ...,
        description=(
            "as_is: carried unchanged; rewritten: carried with its header rewrites; "
            "needs_attention: not carried until a reason is settled; model_repaired: "
            "reserved, never written yet. Only a failed check (or a reason code) changes "
            "it: a check whose outcome is unknown never does, so as_is or rewritten does "
            "not mean every check passed (see checks)"
        ),
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Why it needs attention: failed rule ids, then fixed reason codes",
    )
    blocked_by: list[RoutineRef] = Field(
        default_factory=list,
        description=(
            "The routines it names that need attention (depends_on_needs_attention); "
            "follow theirs for the routines further down"
        ),
    )
    deploy_order: int = Field(
        ..., ge=1, description="Its place in the create order, from 1: after what it names"
    )
    cycle: int | None = Field(
        None, ge=0, description="The index into cycles of the dependency cycle it is part of"
    )
    dependencies: list[RoutineDependencyEntry] = Field(
        default_factory=list, description="The tables and routines it names"
    )
    dependencies_complete: bool = Field(
        ..., description="False when the source does not record every object it names"
    )
    tables_leaving_aurora: list[TableLeavingAurora] = Field(
        default_factory=list,
        description="The tables it names that the end-state assignment moves off Aurora",
    )
    rewrites: list[RoutineRewriteEntry] = Field(
        default_factory=list, description="The rewrites applied, in rule id order"
    )
    notes: list[Literal["runs_as_target_user"]] = Field(
        default_factory=list,
        description="runs_as_target_user: it now runs with the target user's privileges",
    )
    checks: list[RoutineCheckEntry] = Field(
        default_factory=list,
        description=(
            "The check rules that failed or were unknown, as checked before the rewrites; "
            "an unknown outcome (it could not be checked) is never a pass and never "
            "changes the status"
        ),
    )
    rules_passed: int = Field(..., ge=0, description="How many check rules passed")

    @model_validator(mode="after")
    def _status_fits_reasons(self) -> RoutineMigration:
        if self.status == "needs_attention" and not self.reasons:
            raise ValueError(f"{self.kind} {self.routine_id}: needs_attention without a reason")
        if self.blocked_by and "depends_on_needs_attention" not in self.reasons:
            raise ValueError(f"{self.kind} {self.routine_id}: blocked_by without its reason")
        if self.status in ("as_is", "rewritten") and self.reasons:
            raise ValueError(f"{self.kind} {self.routine_id}: {self.status} with reasons")
        if self.status in ("as_is", "rewritten") and self.source_text != "exact":
            raise ValueError(f"{self.kind} {self.routine_id}: {self.status} without exact text")
        if self.status == "rewritten" and not self.rewrites:
            raise ValueError(f"{self.kind} {self.routine_id}: rewritten with no rewrite")
        return self


class RoutineRuleEntry(BaseModel):
    """A rule the rows name, as the catalog states it, so the artifact reads on its own."""

    rule_id: str = Field(..., min_length=1, description="The rule's id")
    rule: Literal["check", "rewrite"] = Field(..., description="A check rule or a rewrite rule")
    impact: (
        Literal["blocks_move", "needs_rewrite", "behaves_differently", "informational"] | None
    ) = Field(None, description="A check rule's impact")
    reason: str = Field(..., min_length=1, description="The catalog's reason")
    fix_hint: str | None = Field(None, description="A check rule's fix hint")


class RoutineMigrationContract(BaseModel):
    """How a job's routines migrate, for one assignment version."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    job_id: str = Field(..., min_length=1, description="The job")
    database_name: str = Field(..., min_length=1, description="The job's database name")
    assignment_version: int = Field(
        ..., ge=1, description="The assignment version read (routines/v<N>)"
    )
    source_engine: Literal["postgresql", "mysql"] = Field(
        ..., description="The routines' engine family (MariaDB reads as MySQL)"
    )
    source_version: str | None = Field(None, description="The source server's version")
    target_engine: Literal["aurora_postgresql", "aurora_mysql"] = Field(
        ..., description="The same-family Aurora engine the routines move to (ADR-034)"
    )
    target_line: str | None = Field(
        None, description="The Aurora version line checked against; None for every line"
    )
    target_user: str = Field(
        ..., min_length=1, description="The user the header rewrites name as owner or DEFINER"
    )
    target_user_origin: Literal["default"] = Field(
        "default",
        description="default: Aurora's default master user name; a deploy names its own",
    )
    routine_sources: bool = Field(
        ...,
        description="Whether the job's routine sources (exact CREATE statements) hold any routine",
    )
    total: int = Field(..., ge=0, description="Routines in the artifact")
    counts: dict[MigrationStatus, int] = Field(
        ..., description="Routines per status, every status listed"
    )
    cycles: list[list[RoutineRef]] = Field(
        default_factory=list, description="Each dependency cycle's routines, sorted"
    )
    rules: list[RoutineRuleEntry] = Field(
        default_factory=list, description="Every rule the rows name, by id"
    )
    routines: list[RoutineMigration] = Field(
        default_factory=list, description="One row per routine, in deploy order"
    )

    @model_validator(mode="after")
    def _consistent(self) -> RoutineMigrationContract:
        if self.total != len(self.routines):
            raise ValueError("total does not match the routines")
        counted = {s: 0 for s in _STATUSES}
        seen: set[tuple[str, str]] = set()
        for row in self.routines:
            counted[row.status] += 1
            key = (row.kind, row.routine_id)
            if key in seen:
                raise ValueError(f"two rows for {row.kind} {row.routine_id}")
            seen.add(key)
            if row.cycle is not None and row.cycle >= len(self.cycles):
                raise ValueError(f"{row.kind} {row.routine_id}: cycle {row.cycle} is not listed")
        if dict(self.counts) != counted:
            raise ValueError("counts do not match the routines' statuses")
        if [r.deploy_order for r in self.routines] != list(range(1, len(self.routines) + 1)):
            raise ValueError("routines are not in deploy order 1..total")
        for members in self.cycles:
            for ref in members:
                if (ref.kind, ref.routine_id) not in seen:
                    raise ValueError(f"cycle names {ref.kind} {ref.routine_id}, not a row")
        return self
