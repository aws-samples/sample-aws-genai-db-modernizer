# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The verdict a routine check returns, shared by every source engine (#1935, #1936).

Each engine's checker (:mod:`src.tools.routines.postgresql_checks`,
:mod:`src.tools.routines.mysql_checks`) parses one routine's ``CREATE`` statement, runs
every rule of ``src/catalog/routine_checks/`` (:mod:`src.shared.catalog.routine_checks`)
that applies to its source engine, target engine and kind, and returns one
:class:`RoutineVerdict`:

- ``checks``: one :class:`RoutineCheckResult` per applicable rule, passed or failed, with
  the rule's impact, reason and fix hint. ``findings`` names what failed (a language, an
  extension, a statement form, a mode), never routine text.
- ``parsed``: whether the statement parsed, None when no parse rule ran.
- ``parse_failure``: where the statement did not parse, by stage and position and a fixed
  word from the checker, never the parser's message (it quotes the statement, literals
  included).
- ``dependencies``: the tables and routines the routine names, for the dependency check
  (#1921); ``dependencies_complete`` is False when some statement could not be read for
  them (a checker that records none leaves both at their defaults).
- ``needs_attention`` (and ``status``): some rule failed with an impact other than
  ``informational``. A rule whose outcome is ``unknown`` (it could not be checked, such as
  the dependency check on an incomplete dependency list, #1937) never blocks, and is listed
  in ``unknown_rules`` so it is never read as a pass.

The exact routine text keeps its literals and stays apart from reports, logs and model
inputs (epic #1925): nothing here holds it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from src.shared.catalog.gaps import GapImpact
from src.shared.catalog.routine_checks import RoutineCheck, RoutineKind

__all__ = [
    "ParseFailure",
    "RoutineCheckResult",
    "RoutineDependency",
    "RoutineVerdict",
]

# No validation error message repeats a model's input (#1961 review).
_HIDDEN = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ParseFailure(BaseModel):
    """Why a routine's ``CREATE`` statement did not parse, without its text.

    - ``stage``: ``statement`` (the parser rejected the statement or its header), ``kind``
      (it parsed, but is not exactly one statement of the routine's kind) or ``body``
      (its body did not parse).
    - ``position``: the 1-based character the parser stopped at, when it says.
    - ``detail``: a fixed word from the checker naming what failed (``end label``,
      ``unterminated quote``), when it has one.
    """

    model_config = _HIDDEN

    stage: Literal["statement", "kind", "body"]
    position: int | None = None
    detail: str | None = None


class RoutineCheckResult(BaseModel):
    """One rule's result for one routine. ``findings`` names what failed (the
    language, the extensions the target line lacks, the statement forms), or why an
    ``unknown`` outcome could not be checked, never routine text."""

    model_config = _HIDDEN

    rule_id: str
    check: RoutineCheck
    outcome: Literal["pass", "fail", "unknown"]
    impact: GapImpact
    reason: str
    fix_hint: str
    findings: tuple[str, ...] = ()


class RoutineDependency(BaseModel):
    """A table or routine the routine names, as written (lower case). ``routine_kind`` says
    which kind of routine it is when the text tells (a ``CALL`` names a procedure, a call
    in an expression a function, ``FOLLOWS`` a trigger, #1937); None when it does not."""

    model_config = _HIDDEN

    kind: Literal["table", "routine"]
    name: str = Field(min_length=1)
    schema_name: str | None = None
    routine_kind: RoutineKind | None = None


class RoutineVerdict(BaseModel):
    """A routine's verdict against one target Aurora engine (and version line)."""

    model_config = _HIDDEN

    routine_id: str
    kind: RoutineKind
    target_engine: str
    target_line: str | None
    parse_failure: ParseFailure | None
    checks: tuple[RoutineCheckResult, ...]
    dependencies: tuple[RoutineDependency, ...] = ()
    dependencies_complete: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def parsed(self) -> bool | None:
        """Whether the statement parsed (no ``parse_failure``); None when no parse rule
        ran, so nothing was parsed (#1961 review)."""
        if not any(c.check == "parse" for c in self.checks):
            return None
        return self.parse_failure is None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_rules(self) -> tuple[str, ...]:
        return tuple(c.rule_id for c in self.checks if c.outcome == "fail")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unknown_rules(self) -> tuple[str, ...]:
        """The rules that could not be checked (outcome ``unknown``): never a pass."""
        return tuple(c.rule_id for c in self.checks if c.outcome == "unknown")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_attention(self) -> bool:
        return any(c.outcome == "fail" and c.impact != "informational" for c in self.checks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> Literal["as_is", "needs_attention"]:
        return "needs_attention" if self.needs_attention else "as_is"
