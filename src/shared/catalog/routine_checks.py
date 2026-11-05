# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine check rules (#1935, #1936): what stops a source routine loading or running
on the target Aurora engine.

A routine (a function, procedure, trigger or event) is checked one at a time, from its
``CREATE`` statement and the metadata the collection gives with it, before anything
moves (epic #1925). One ``src/catalog/routine_checks/<rule_id>.toml`` per rule says, in
one place, which source and target engines and routine kinds it applies to, what it
checks, how much it matters (``impact``, the gap rules' vocabulary), why, how to fix it,
and the public documentation it rests on. This module loads them into strict Pydantic
models (an unknown, missing or mistyped field fails at load time); the checkers
(:mod:`src.tools.routines.postgresql_checks`, :mod:`src.tools.routines.mysql_checks`)
only read them, so no rule lives in code.

Rule fields:

- ``id``: equals the file name.
- ``feature``: the short name of what the rule checks (one line).
- ``source_engines``: the collector source engines it applies to.
- ``targets``: the Aurora engines it applies to.
- ``kinds``: the routine kinds it applies to (:data:`ROUTINE_KINDS`); a ``language`` or
  ``ownership`` rule lists only ``function`` and ``procedure``.
- ``impact``: one of the gap rules' impacts (:data:`~src.shared.catalog.gaps.GAP_IMPACTS`).
- ``reason``: why a routine that fails the rule does not load or run as it did (one
  sentence).
- ``fix_hint``: what to change so it passes (one sentence).
- ``docs``: the public documentation the rule rests on, by name.
- ``[match]``: exactly one check (see :class:`RoutineCheckMatch`).
"""

from __future__ import annotations

import re
import tomllib
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.gaps import (
    AURORA_TARGETS,
    SOURCE_ENGINES,
    AuroraExtensionLines,
    GapImpact,
    aurora_extension_lines,
)

__all__ = [
    "ROUTINE_CHECKS_DIR",
    "ROUTINE_KINDS",
    "RoutineCheck",
    "RoutineCheckMatch",
    "RoutineCheckRule",
    "RoutineKind",
    "load_routine_check_rules",
    "routine_check_rules",
]

ROUTINE_CHECKS_DIR = Path(__file__).resolve().parents[2] / "catalog" / "routine_checks"

RoutineKind = Literal["function", "procedure", "trigger", "event"]
ROUTINE_KINDS: tuple[RoutineKind, ...] = ("function", "procedure", "trigger", "event")
# What a rule checks, one per group of [match] keys:
# - every engine: the routine's CREATE statement parses as its kind (``parse``);
# - PostgreSQL: its language (``language``), the extensions it uses (``extensions``), its
#   owner and security mode (``ownership``);
# - MySQL: its DECLARE order (``declare_order``), its handlers (``handler``), its
#   DEFINER (``definer``), the statements in its body (``statement``) and its stored
#   sql_mode (``sql_mode``).
RoutineCheck = Literal[
    "parse",
    "language",
    "extensions",
    "ownership",
    "declare_order",
    "handler",
    "definer",
    "statement",
    "sql_mode",
]

# The checks that read a function's or procedure's own pg_proc metadata (language,
# owner, security mode): no such rule applies to a trigger or an event (#1961 review).
_OWN_METADATA_CHECKS: frozenset[RoutineCheck] = frozenset({"language", "ownership"})
_OWN_METADATA_KINDS: frozenset[RoutineKind] = frozenset({"function", "procedure"})

_ONE_LINE = r"^[^\n]+$"
_LANGUAGE = re.compile(r"^[a-z][a-z0-9_]*$")
_WORDS = re.compile(r"^[A-Z][A-Z0-9_]*( [A-Z][A-Z0-9_]*)*$")
_VARIABLE = re.compile(r"^[a-z][a-z0-9_]*$")


class RoutineCheckMatch(BaseModel):
    """What a rule checks: the keys of exactly one check below."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # parse: the CREATE statement is one statement of the routine's kind that the source
    # engine's parser reads, body included (PostgreSQL: PL/pgSQL and SQL bodies; MySQL:
    # the header and the compound statements of the body).
    parses: bool = False
    # language (PostgreSQL): languages that pass on every target line (pg_language.lanname,
    # lower case).
    languages: tuple[str, ...] = ()
    # Trusted languages that pass only where the target line offers them as an
    # extension (aurora_postgresql_extensions.toml). Any other language fails.
    trusted_extension_languages: tuple[str, ...] = ()
    # extensions (PostgreSQL): ``offered_on_target_line``: every extension the routine
    # uses is in the target line's extension list (by pg_extension name; versions are the
    # gap check's).
    extensions: Literal["offered_on_target_line"] | None = None
    # ownership (PostgreSQL): a SECURITY DEFINER routine whose owner is a superuser on the
    # source fails.
    security_definer_owned_by_superuser: bool = False
    # declare_order (MySQL): every DECLARE opens its BEGIN ... END block, variables and
    # conditions first, then cursors, then handlers.
    declare_order: bool = False
    # handler (MySQL): every handler is CONTINUE or EXIT and names valid, declared,
    # distinct conditions.
    valid_handlers: bool = False
    # definer (MySQL): a DEFINER naming a user other than the target user (a CURRENT_USER
    # form passes) fails, for a routine that runs as its definer: a kind listed here
    # always does; a procedure or function does unless it is SQL SECURITY INVOKER.
    definer_not_target_user: bool = False
    always_definer_kinds: tuple[RoutineKind, ...] = ()
    # statement (MySQL): a body statement that starts with one of ``statement_starts`` or
    # contains one of ``statement_contains`` (keywords, upper case, outside quotes), or
    # that SETs a variable in one of ``set_scopes`` or one of ``set_variables``, fails.
    statement_starts: tuple[str, ...] = ()
    statement_contains: tuple[str, ...] = ()
    set_scopes: tuple[str, ...] = ()
    set_variables: tuple[str, ...] = ()
    # sql_mode (MySQL): the routine's stored sql_mode holds one of ``sql_modes``, or (with
    # ``unknown_sql_modes``) a mode outside ``known_sql_modes`` and every other sql_mode
    # rule's ``sql_modes``.
    sql_modes: tuple[str, ...] = ()
    unknown_sql_modes: bool = False
    known_sql_modes: tuple[str, ...] = ()

    @property
    def check(self) -> RoutineCheck:
        return self._checks()[0]

    def _checks(self) -> list[RoutineCheck]:
        found: list[RoutineCheck] = []
        if self.parses:
            found.append("parse")
        if self.languages:
            found.append("language")
        if self.extensions is not None:
            found.append("extensions")
        if self.security_definer_owned_by_superuser:
            found.append("ownership")
        if self.declare_order:
            found.append("declare_order")
        if self.valid_handlers:
            found.append("handler")
        if self.definer_not_target_user:
            found.append("definer")
        if self.statement_starts or self.statement_contains or self.set_scopes:
            found.append("statement")
        elif self.set_variables:
            found.append("statement")
        if self.sql_modes or self.unknown_sql_modes:
            found.append("sql_mode")
        return found

    @model_validator(mode="after")
    def _exactly_one_check(self) -> RoutineCheckMatch:
        if len(self._checks()) != 1:
            raise ValueError(
                "[match] needs exactly one of parses, languages, extensions, "
                "security_definer_owned_by_superuser, declare_order, valid_handlers, "
                "definer_not_target_user, a statement list, sql_modes or unknown_sql_modes"
            )
        if self.trusted_extension_languages and not self.languages:
            raise ValueError("trusted_extension_languages goes with languages")
        if self.always_definer_kinds and not self.definer_not_target_user:
            raise ValueError("always_definer_kinds goes with definer_not_target_user")
        if self.known_sql_modes and not self.unknown_sql_modes:
            raise ValueError("known_sql_modes goes with unknown_sql_modes")
        names = (*self.languages, *self.trusted_extension_languages)
        for name in names:
            if not _LANGUAGE.match(name):
                raise ValueError(f"language {name!r}: a pg_language name is lower case")
        lists = {
            "languages": names,
            "statement_starts": self.statement_starts,
            "statement_contains": self.statement_contains,
            "set_scopes": self.set_scopes,
            "set_variables": self.set_variables,
            "sql_modes": (*self.sql_modes, *self.known_sql_modes),
        }
        for key, values in lists.items():
            if len(set(values)) != len(values):
                raise ValueError(f"a value in {key} is listed twice")
        keywords = (
            *self.statement_starts,
            *self.statement_contains,
            *self.set_scopes,
            *self.sql_modes,
            *self.known_sql_modes,
        )
        for word in keywords:
            if not _WORDS.match(word):
                raise ValueError(f"{word!r}: keywords and modes are upper case, single-spaced")
        for variable in self.set_variables:
            if not _VARIABLE.match(variable):
                raise ValueError(f"{variable!r}: a system variable name is lower case")
        return self


class RoutineCheckRule(BaseModel):
    """One routine check rule (one ``src/catalog/routine_checks/<id>.toml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    feature: str = Field(min_length=1, pattern=_ONE_LINE)
    source_engines: tuple[str, ...] = Field(min_length=1)
    targets: tuple[str, ...] = Field(min_length=1)
    kinds: tuple[RoutineKind, ...] = Field(min_length=1)
    impact: GapImpact
    reason: str = Field(min_length=1, pattern=_ONE_LINE)
    fix_hint: str = Field(min_length=1, pattern=_ONE_LINE)
    docs: str = Field(min_length=1, pattern=_ONE_LINE)
    match: RoutineCheckMatch

    @model_validator(mode="after")
    def _known_values(self) -> RoutineCheckRule:
        if unknown := sorted(set(self.source_engines) - SOURCE_ENGINES):
            raise ValueError(f"unknown source engines {unknown}")
        if unknown := sorted(set(self.targets) - AURORA_TARGETS):
            raise ValueError(f"unknown targets {unknown}")
        for field in ("source_engines", "targets", "kinds"):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError(f"{field} lists a value twice")
        if self.match.check in _OWN_METADATA_CHECKS:
            for kind in self.kinds:
                if kind not in _OWN_METADATA_KINDS:
                    raise ValueError(
                        f"a {self.match.check} rule cannot apply to a {kind}: only a function "
                        "or procedure has a language, owner and security mode of its own"
                    )
        return self


def load_routine_check_rules(
    directory: Path = ROUTINE_CHECKS_DIR,
    extension_lines: dict[str, AuroraExtensionLines] | None = None,
) -> tuple[RoutineCheckRule, ...]:
    """Every routine check rule file in ``directory``, validated, sorted by id.

    A rule that reads the target line's extension list (``extensions``, or
    ``trusted_extension_languages``) must target only engines with a list for each of
    its source engines (``extension_lines``, the #1078 lists).
    """
    extension_lines = aurora_extension_lines() if extension_lines is None else extension_lines
    rules: list[RoutineCheckRule] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                rule = RoutineCheckRule.model_validate(tomllib.load(fh))
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if rule.id != path.stem:
            raise CatalogError(f"{path.name}: id {rule.id!r} does not match the file name")
        if rule.match.extensions or rule.match.trusted_extension_languages:
            for target in rule.targets:
                listed = extension_lines.get(target)
                stray = sorted(
                    set(rule.source_engines) - set(listed.source_engines if listed else ())
                )
                if stray:
                    raise CatalogError(
                        f"{path.name}: a rule that reads the extension list needs one; "
                        f"{target} has none for {stray}"
                    )
        rules.append(rule)
    if not rules:
        raise CatalogError(f"no routine check rule files under {directory}")
    return tuple(rules)


@cache
def routine_check_rules() -> tuple[RoutineCheckRule, ...]:
    """The loaded routine check rules (read once per process), sorted by id."""
    return load_routine_check_rules(extension_lines=aurora_extension_lines())
