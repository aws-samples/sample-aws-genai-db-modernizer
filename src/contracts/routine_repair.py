# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
RoutineRepair Pydantic Models
Version: 1.0
Last Modified: 2026-11-16
Description: The model seam for repairing routines the deterministic path can't carry (#1940)

Two shapes, one per side of the seam (ADR-034 Decision 2):

- :class:`RoutineRepairResponse`: a model's answer, in every LLM mode. Claude Code writes
  it to ``routines/v<N>/repair/llm_response.json``; the Bedrock call asks for it as
  Strands structured output. Both go through
  ``src.agents.routine_repair.handler.apply_routine_repair_llm_output``. Per routine it
  holds the repaired ``CREATE`` and a short change note, nothing else: ``extra="forbid"``,
  so an answer that tries to set a status (``proved``, say) is refused whole.
- :class:`RoutineRepairContract`: what the seam made of that answer, written at
  ``<db>/<job>/routines/v<N>/routine_repair.json`` for the routine-migration artifact of
  the same assignment version. Each routine the deterministic path could not carry and
  that may go to a model has a row, with its state:

  - ``repaired_pending_proof``: the candidate kept the routine's name, signature and
    schemas and adds no object. It is **not carried**: only the proof (the side-by-side
    comparison, #1944, and the test-cluster proof, #1945) can accept it, and the repair
    loop (#1941) runs it. No state here says ``proved``;
  - ``refused``: the candidate broke one of those rules (``refusal`` says which);
  - ``no_answer``: the model gave none for it (or its call failed);
  - ``not_repaired``: no repair ran (``--llm-mode none``): it stays not carried, with the
    routine-migration artifact's reasons.

A routine whose body the customer opted out of collecting (#1933) never has a row: its
body is never sent to a model.

**Text.** The model works on the collection's redacted text (literals replaced, #1625),
never on the exact text of the routine sources (ADR-034 Decision 4). So
``repaired_create`` is redacted text too: the repair loop (#1941) carries the change onto
the exact text only after the proof. No row holds the exact text, and no deliverable
prints ``repaired_create``; ``change_note`` is the model's own words, untrusted, which a
renderer escapes like any other model text.

Version History:
- 1.0 (2026-11-16, #1940): First version.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RoutineKind = Literal["function", "procedure", "trigger", "event"]
RepairState = Literal["repaired_pending_proof", "refused", "no_answer", "not_repaired"]
#: Why a candidate was refused: the rules every repaired routine keeps (#1940).
RefusalCode = Literal[
    "not_one_statement",
    "renamed",
    "screen_refused",
    "signature_changed",
    "changes_privileges",
    "literals_changed",
    "adds_objects",
    "adds_statements",
    "adds_calls",
    "names_other_schema",
    "unchanged",
    "duplicate_answer",
]
REPAIR_STATES: tuple[RepairState, ...] = (
    "repaired_pending_proof",
    "refused",
    "no_answer",
    "not_repaired",
)
#: The longest change note a model may write: one or two plain sentences.
CHANGE_NOTE_MAX = 300
#: The longest repaired CREATE a model may return.
CREATE_MAX = 200_000


class RoutineRepairAnswer(BaseModel):
    """One repaired routine, as the model returns it."""

    model_config = ConfigDict(extra="forbid")

    kind: RoutineKind = Field(..., description="The routine's kind, copied from the request")
    routine_id: str = Field(
        ..., min_length=1, max_length=1000, description="The routine's id, copied from the request"
    )
    repaired_create: str = Field(
        ...,
        min_length=1,
        max_length=CREATE_MAX,
        description=(
            "The whole repaired CREATE statement, one statement, with the request's redacted "
            "literals ('?') left as they are"
        ),
    )
    change_note: str = Field(
        ...,
        min_length=1,
        max_length=CHANGE_NOTE_MAX,
        description="What changed and why, in one or two plain sentences, with no code",
    )

    @field_validator("change_note")
    @classmethod
    def _one_line(cls, v: str) -> str:
        """One line of words: whitespace runs collapsed, never blank."""
        note = " ".join(v.split())
        if not note:
            raise ValueError("change_note is blank")
        return note

    @field_validator("repaired_create")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("repaired_create is blank")
        return v


class RoutineRepairResponse(BaseModel):
    """A model's answer to the routine repair request (every LLM mode)."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    repairs: list[RoutineRepairAnswer] = Field(
        default_factory=list,
        description="One entry per routine repaired; leave a routine out to not repair it",
    )


def parse_routine_repair_response(raw: object) -> RoutineRepairResponse:
    """Validate a raw answer (a dict, or a built response) against the contract; raises
    ``pydantic.ValidationError`` when it is off the contract."""
    if isinstance(raw, RoutineRepairResponse):
        return raw
    return RoutineRepairResponse.model_validate(raw)


class RoutineRepairRow(BaseModel):
    """What the seam made of one routine that may be repaired."""

    kind: RoutineKind = Field(..., description="The routine's kind")
    routine_id: str = Field(..., min_length=1, description="Its routine-migration routine_id")
    state: RepairState = Field(..., description="See the module docstring; never proved")
    reasons: list[str] = Field(
        default_factory=list,
        description=(
            "Why it needed an intervention: the routine-migration artifact's failed rule ids "
            "and reasons, and target_error when the target refused it"
        ),
    )
    refusal: RefusalCode | None = Field(None, description="Why a candidate was refused")
    refusal_detail: str | None = Field(
        None, max_length=300, description="The refusal in words, never routine text"
    )
    repaired_create: str | None = Field(
        None,
        max_length=CREATE_MAX,
        description="repaired_pending_proof: the candidate, redacted text, waiting for the proof",
    )
    change_note: str | None = Field(
        None,
        max_length=CHANGE_NOTE_MAX,
        description="repaired_pending_proof: what changed, the model's own words (untrusted)",
    )

    @model_validator(mode="after")
    def _state_fits(self) -> RoutineRepairRow:
        pending = self.state == "repaired_pending_proof"
        if pending and not (self.repaired_create and self.change_note):
            raise ValueError(f"{self.kind} {self.routine_id}: repaired with no candidate or note")
        if not pending and (self.repaired_create is not None or self.change_note is not None):
            raise ValueError(f"{self.kind} {self.routine_id}: {self.state} keeps a candidate")
        if (self.state == "refused") != (self.refusal is not None):
            raise ValueError(f"{self.kind} {self.routine_id}: refusal does not fit {self.state}")
        return self


class RoutineRepairContract(BaseModel):
    """The routine repair seam's result for one assignment version (#1940)."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR)"
    )
    job_id: str = Field(..., min_length=1, description="The job")
    database_name: str = Field(..., min_length=1, description="The job's database name")
    assignment_version: int = Field(
        ..., ge=1, description="The routine-migration artifact's version (routines/v<N>)"
    )
    llm_mode: Literal["bedrock", "external", "none"] = Field(
        ..., description="Who was asked: Bedrock, Claude Code, or nobody"
    )
    source_engine: Literal["postgresql", "mysql"] = Field(..., description="The source family")
    target_engine: Literal["aurora_postgresql", "aurora_mysql"] = Field(
        ..., description="The same-family Aurora engine"
    )
    target_line: str | None = Field(None, description="The Aurora version line checked against")
    skipped_reason: str | None = Field(
        None, description="Why no repair ran (llm_mode none, or the model call failed)"
    )
    ignored_answers: int = Field(
        0, ge=0, description="Answers for a routine the request did not list, dropped"
    )
    counts: dict[RepairState, int] = Field(..., description="Routines per state, every state")
    routines: list[RoutineRepairRow] = Field(
        default_factory=list, description="One row per routine that may be repaired"
    )

    @model_validator(mode="after")
    def _consistent(self) -> RoutineRepairContract:
        counted = dict.fromkeys(REPAIR_STATES, 0)
        seen: set[tuple[str, str]] = set()
        for row in self.routines:
            counted[row.state] += 1
            key = (row.kind, row.routine_id)
            if key in seen:
                raise ValueError(f"two rows for {row.kind} {row.routine_id}")
            seen.add(key)
        if dict(self.counts) != counted:
            raise ValueError("counts do not match the routines' states")
        if self.llm_mode == "none" and counted["repaired_pending_proof"]:
            raise ValueError("llm_mode none repairs nothing")
        return self
