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
- ``[version_change]`` (optional, #1952): the rule checks a change a newer major version
  made (:class:`VersionChange`), so it runs only when the source's version line is older
  than that version and the target line has it (:func:`version_change_scope`); it never
  runs on a same-major target.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.gaps import (
    AURORA_TARGETS,
    SOURCE_ENGINES,
    AuroraExtensionLines,
    AuroraVersionLines,
    GapImpact,
    aurora_extension_lines,
    aurora_version_lines,
    parse_version_line,
)

__all__ = [
    "ROUTINE_CHECKS_DIR",
    "SET_SCOPES",
    "ROUTINE_KINDS",
    "RoutineCheck",
    "RoutineCheckMatch",
    "RoutineCheckRule",
    "RoutineKind",
    "VersionChange",
    "VersionChangeScope",
    "load_routine_check_rules",
    "routine_check_rules",
    "version_change_scope",
]

ROUTINE_CHECKS_DIR = Path(__file__).resolve().parents[2] / "catalog" / "routine_checks"

RoutineKind = Literal["function", "procedure", "trigger", "event"]
ROUTINE_KINDS: tuple[RoutineKind, ...] = ("function", "procedure", "trigger", "event")
# What a rule checks, one per group of [match] keys:
# - every engine: the routine's CREATE statement parses as its kind (``parse``);
# - PostgreSQL: its language (``language``), the extensions it uses (``extensions``), its
#   owner and security mode (``ownership``), and in its body (#1951) the functions it calls
#   (``function_call``), the catalog columns it reads (``catalog_column``), the objects it
#   creates in the public schema (``public_schema_create``), the settings it sets or reads
#   (``setting``), the types it names (``type_use``) and the tables it names with no
#   search_path of its own (``maintenance_search_path``);
# - MySQL: its DECLARE order (``declare_order``), its handlers (``handler``), its
#   DEFINER (``definer``), the statements in its body (``statement``), its stored
#   sql_mode (``sql_mode``), the dynamic SQL it prepares (``dynamic_sql``), the words it
#   uses as identifiers (``reserved_word``, #1952) and its GROUP BY clauses (``group_by``);
# - every engine: the tables and routines it names move in the same step
#   (``dependencies``, #1937). The dependency check needs the set of objects the step
#   moves, so it runs in :mod:`src.tools.routines.dependencies`, not in the checkers.
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
    "dynamic_sql",
    "reserved_word",
    "group_by",
    "function_call",
    "catalog_column",
    "public_schema_create",
    "setting",
    "type_use",
    "maintenance_search_path",
    "dependencies",
]

# The checks that read a function's or procedure's own pg_proc metadata (language,
# owner, security mode): no such rule applies to a trigger or an event (#1961 review).
_OWN_METADATA_CHECKS: frozenset[RoutineCheck] = frozenset({"language", "ownership"})
_OWN_METADATA_KINDS: frozenset[RoutineKind] = frozenset({"function", "procedure"})
# The checks that read a PostgreSQL function's or procedure's body (#1951): a PostgreSQL
# trigger's CREATE TRIGGER has none (its body is its trigger function's).
_POSTGRESQL_BODY_CHECKS: frozenset[RoutineCheck] = frozenset(
    {
        "function_call",
        "catalog_column",
        "public_schema_create",
        "setting",
        "type_use",
        "maintenance_search_path",
    }
)
# The source engines whose checker implements each engine-specific check; ``parse`` is
# every engine's. A rule naming a check for another engine fails at load time.
_POSTGRESQL_FAMILY = frozenset({"postgresql"})
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
_CHECK_ENGINES: dict[RoutineCheck, frozenset[str]] = {
    "language": _POSTGRESQL_FAMILY,
    "extensions": _POSTGRESQL_FAMILY,
    "ownership": _POSTGRESQL_FAMILY,
    "declare_order": _MYSQL_FAMILY,
    "handler": _MYSQL_FAMILY,
    "definer": _MYSQL_FAMILY,
    "statement": _MYSQL_FAMILY,
    "sql_mode": _MYSQL_FAMILY,
    "dynamic_sql": _MYSQL_FAMILY,
    "reserved_word": _MYSQL_FAMILY,
    "group_by": _MYSQL_FAMILY,
    "function_call": _POSTGRESQL_FAMILY,
    "catalog_column": _POSTGRESQL_FAMILY,
    "public_schema_create": _POSTGRESQL_FAMILY,
    "maintenance_search_path": _POSTGRESQL_FAMILY,
    "setting": _POSTGRESQL_FAMILY,
    "type_use": _POSTGRESQL_FAMILY,
}
# Only MySQL has events (CREATE EVENT).
_EVENT_ENGINES = _MYSQL_FAMILY
# The SET scopes the MySQL checker reads: ``SET GLOBAL x``, ``SET @@persist.x``, ...
SET_SCOPES = frozenset({"GLOBAL", "PERSIST", "PERSIST_ONLY", "SESSION", "LOCAL"})

_ONE_LINE = r"^[^\n]+$"
_LANGUAGE = re.compile(r"^[a-z][a-z0-9_]*$")
_WORDS = re.compile(r"^[A-Z][A-Z0-9_]*( [A-Z][A-Z0-9_]*)*$")
_VARIABLE = re.compile(r"^[a-z][a-z0-9_]*$")
_CATALOG_COLUMN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_CONTEXT = re.compile(r"^(?:[A-Z][A-Z0-9_]*|\()(?: (?:[A-Z][A-Z0-9_]*|\())*$")


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
    # contains one of ``statement_contains`` (keywords, upper case, outside quotes) where
    # none of ``statement_exceptions`` starts at the same word, that SETs a variable in one
    # of ``set_scopes`` (of :data:`SET_SCOPES`) or one of ``set_variables``, that SETs or
    # reads (``@@name``, ``@@session.name``) one of ``system_variables``, or that calls one
    # of ``function_calls``, fails.
    statement_starts: tuple[str, ...] = ()
    statement_contains: tuple[str, ...] = ()
    statement_exceptions: tuple[str, ...] = ()
    set_scopes: tuple[str, ...] = ()
    set_variables: tuple[str, ...] = ()
    system_variables: tuple[str, ...] = ()
    function_calls: tuple[str, ...] = ()
    # sql_mode (MySQL): the routine's stored sql_mode holds one of ``sql_modes``, or (with
    # ``unknown_sql_modes``) a mode outside ``known_sql_modes`` and every other sql_mode
    # rule's ``sql_modes``.
    sql_modes: tuple[str, ...] = ()
    unknown_sql_modes: bool = False
    known_sql_modes: tuple[str, ...] = ()
    # dynamic_sql (MySQL): a PREPARE whose statement text is not a string literal the
    # checker can read fails, since the statement checks cannot see what it runs.
    dynamic_sql: bool = False
    # reserved_word (MySQL, #1952): one of ``reserved_words`` (upper case) written as an
    # unquoted identifier anywhere after the routine's name (a parameter, a variable, a
    # label, a column or table name), including the literal text a PREPARE runs, fails.
    # A word after a ``.`` (a qualified name's later part) or naming an ``@`` user or ``@@``
    # system variable is not one, nor is a word inside one of ``reserved_word_contexts``
    # (keyword sequences where the word is the keyword, such as ``EACH ROW``).
    reserved_words: tuple[str, ...] = ()
    reserved_word_contexts: tuple[str, ...] = ()
    # group_by (MySQL, #1952): ``without_order_by``: a SELECT statement (a cursor's
    # included) whose own GROUP BY has no ORDER BY after it at the same level, so its rows
    # came out sorted only because the source sorted GROUP BY results, or a GROUP BY at any
    # level with a LIMIT and no ORDER BY at its level (the LIMIT kept the first sorted rows);
    # ``with_direction``: a GROUP BY that sorts its columns (``ASC`` or ``DESC``) at any
    # level.
    group_by: Literal["without_order_by", "with_direction"] | None = None
    # The PostgreSQL body checks (#1951) read the statements of a function's or procedure's
    # body with PostgreSQL's parser (a PL/pgSQL body's SQL statements and expressions, a
    # quoted SQL body, an SQL-standard body); a body that is not read, or one that finds
    # nothing but runs dynamic SQL (EXECUTE), is ``unknown``.
    # function_call: a call to one of ``called_functions`` (lower case, unqualified or
    # in pg_catalog) fails.
    called_functions: tuple[str, ...] = ()
    # catalog_column: a statement that reads one of ``catalog_columns``
    # (``relation.column``, lower case: the relation unqualified or in pg_catalog, and a
    # column of that name) fails.
    catalog_columns: tuple[str, ...] = ()
    # public_schema_create: a statement that creates a table, view, materialized view,
    # sequence (not a temporary one), function, type or domain in the public schema, by name,
    # or unqualified when the routine has no SET search_path of its own, fails.
    creates_in_public_schema: bool = False
    # setting: a SET, RESET or SHOW of one of ``removed_settings`` (lower case) in the body
    # or the function's own SET clause, or a current_setting() / set_config() of it by a
    # string literal, fails.
    removed_settings: tuple[str, ...] = ()
    # type_use: one of ``removed_types`` (lower case) as a parameter, return, cast, column or
    # declared variable type fails.
    removed_types: tuple[str, ...] = ()
    # maintenance_search_path: a function with no SET search_path of its own whose body
    # names a table without its schema fails.
    unqualified_without_search_path: bool = False
    # dependencies (every engine, #1937): a table or routine the routine names that is not
    # in the set of objects moved in the same step fails.
    dependencies_moved_together: bool = False

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
        statement = (
            self.statement_starts,
            self.statement_contains,
            self.set_scopes,
            self.set_variables,
            self.system_variables,
            self.function_calls,
        )
        if any(statement):
            found.append("statement")
        if self.sql_modes or self.unknown_sql_modes:
            found.append("sql_mode")
        if self.dynamic_sql:
            found.append("dynamic_sql")
        if self.reserved_words:
            found.append("reserved_word")
        if self.group_by is not None:
            found.append("group_by")
        if self.called_functions:
            found.append("function_call")
        if self.catalog_columns:
            found.append("catalog_column")
        if self.creates_in_public_schema:
            found.append("public_schema_create")
        if self.removed_settings:
            found.append("setting")
        if self.removed_types:
            found.append("type_use")
        if self.unqualified_without_search_path:
            found.append("maintenance_search_path")
        if self.dependencies_moved_together:
            found.append("dependencies")
        return found

    @model_validator(mode="after")
    def _exactly_one_check(self) -> RoutineCheckMatch:
        if len(self._checks()) != 1:
            raise ValueError(
                "[match] needs exactly one of parses, languages, extensions, "
                "security_definer_owned_by_superuser, declare_order, valid_handlers, "
                "definer_not_target_user, a statement list, sql_modes, unknown_sql_modes, "
                "dynamic_sql, reserved_words, group_by, called_functions, catalog_columns, "
                "creates_in_public_schema, removed_settings, removed_types, "
                "unqualified_without_search_path or "
                "dependencies_moved_together"
            )
        if self.trusted_extension_languages and not self.languages:
            raise ValueError("trusted_extension_languages goes with languages")
        if self.always_definer_kinds and not self.definer_not_target_user:
            raise ValueError("always_definer_kinds goes with definer_not_target_user")
        if self.known_sql_modes and not self.unknown_sql_modes:
            raise ValueError("known_sql_modes goes with unknown_sql_modes")
        if self.statement_exceptions and not self.statement_contains:
            raise ValueError("statement_exceptions goes with statement_contains")
        if self.reserved_word_contexts and not self.reserved_words:
            raise ValueError("reserved_word_contexts goes with reserved_words")
        if unknown := sorted(set(self.set_scopes) - SET_SCOPES):
            raise ValueError(f"set_scopes {unknown}: the scopes are {sorted(SET_SCOPES)}")
        names = (*self.languages, *self.trusted_extension_languages)
        for name in names:
            if not _LANGUAGE.match(name):
                raise ValueError(f"language {name!r}: a pg_language name is lower case")
        lists = {
            "languages": names,
            "statement_starts": self.statement_starts,
            "statement_contains": self.statement_contains,
            "statement_exceptions": self.statement_exceptions,
            "function_calls": self.function_calls,
            "set_scopes": self.set_scopes,
            "set_variables": self.set_variables,
            "system_variables": self.system_variables,
            "sql_modes": (*self.sql_modes, *self.known_sql_modes),
            "reserved_words": self.reserved_words,
            "reserved_word_contexts": self.reserved_word_contexts,
            "called_functions": self.called_functions,
            "catalog_columns": self.catalog_columns,
            "removed_settings": self.removed_settings,
            "removed_types": self.removed_types,
        }
        for key, values in lists.items():
            if len(set(values)) != len(values):
                raise ValueError(f"a value in {key} is listed twice")
        keywords = (
            *self.statement_starts,
            *self.statement_contains,
            *self.statement_exceptions,
            *self.function_calls,
            *self.set_scopes,
            *self.sql_modes,
            *self.known_sql_modes,
            *self.reserved_words,
        )
        for word in keywords:
            if not _WORDS.match(word):
                raise ValueError(f"{word!r}: keywords and modes are upper case, single-spaced")
        for name in (*self.called_functions, *self.removed_settings, *self.removed_types):
            if not _VARIABLE.match(name):
                raise ValueError(f"{name!r}: a function, setting or type name is lower case")
        for column in self.catalog_columns:
            if not _CATALOG_COLUMN.match(column):
                raise ValueError(f"{column!r}: a catalog column is relation.column, lower case")
        for variable in (*self.set_variables, *self.system_variables):
            if not _VARIABLE.match(variable):
                raise ValueError(f"{variable!r}: a system variable name is lower case")
        for context in self.reserved_word_contexts:
            if not _CONTEXT.match(context):
                raise ValueError(
                    f"{context!r}: a context is upper-case words and ( , single-spaced"
                )
            if not set(context.split()) & set(self.reserved_words):
                raise ValueError(f"{context!r}: a context holds one of the reserved words")
        return self


class VersionChange(BaseModel):
    """The major version that made the change a rule checks (``[version_change]``, #1952).

    ``line``: that version's line as ``aurora_versions.toml`` writes lines (``"8.0"``,
    ``"16"``). The rule runs only when the source's line is older than ``line`` and the
    target line is ``line`` or newer (:func:`version_change_scope`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    line: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?$")


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
    version_change: VersionChange | None = None

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
        engines = _CHECK_ENGINES.get(self.match.check)
        if engines is not None and (stray := sorted(set(self.source_engines) - engines)):
            raise ValueError(
                f"a {self.match.check} rule applies only to {sorted(engines)}, not {stray}"
            )
        if self.match.check in _OWN_METADATA_CHECKS:
            for kind in self.kinds:
                if kind not in _OWN_METADATA_KINDS:
                    raise ValueError(
                        f"a {self.match.check} rule cannot apply to a {kind}: only a function "
                        "or procedure has a language, owner and security mode of its own"
                    )
        if self.match.check in _POSTGRESQL_BODY_CHECKS:
            for kind in self.kinds:
                if kind not in _OWN_METADATA_KINDS:
                    raise ValueError(
                        f"a {self.match.check} rule cannot apply to a {kind}: it reads a "
                        "function's or procedure's body"
                    )
        if self.match.check == "maintenance_search_path" and self.kinds != ("function",):
            raise ValueError("a maintenance_search_path rule applies to functions only")
        if "event" in self.kinds and (stray := sorted(set(self.source_engines) - _EVENT_ENGINES)):
            raise ValueError(f"only MySQL has events; kinds lists event for {stray}")
        if self.version_change is not None:
            if self.match.check in {"parse", "dependencies"}:
                raise ValueError(f"a {self.match.check} rule applies to every version")
            for engine in self.source_engines:
                if parse_version_line(engine, self.version_change.line) is None:
                    raise ValueError(
                        f"version_change line {self.version_change.line!r} is not a "
                        f"{engine} version line"
                    )
        return self


VersionChangeScope = Literal["runs", "not_a_version_change", "source_unknown", "target_unknown"]


def _line(engine: str, text: str) -> tuple[int, ...]:
    parsed = parse_version_line(engine, text)
    if parsed is None:  # the loader checks every line
        raise ValueError(f"{text!r} is not a {engine} version line")
    return parsed


def version_change_scope(
    rule: RoutineCheckRule,
    source_engine: str,
    source_version: str | None,
    target_lines: Sequence[str],
) -> VersionChangeScope | None:
    """Whether a ``[version_change]`` rule runs for a ``source_engine`` source at
    ``source_version`` moving to one of ``target_lines`` (the chosen line, or every line
    the target may be), #1952. None for a rule with no ``[version_change]``: it always runs.

    - ``runs``: the source's line is older than the rule's line, and every target line has
      it (is that line or newer).
    - ``not_a_version_change``: the source's line already has the change, or no target
      line has it: the rule does not apply, so it is left out of the verdict. A same-major
      target is always this.
    - ``source_unknown``: the source's version has no line, so whether the move crosses the
      change is unknown.
    - ``target_unknown``: the source's line is older, but only some of ``target_lines``
      have the change (no single line was chosen).
    """
    if rule.version_change is None:
        return None
    changed = _line(source_engine, rule.version_change.line)
    targets = [_line(source_engine, t) for t in target_lines]
    if not targets or all(t < changed for t in targets):
        return "not_a_version_change"
    source = parse_version_line(source_engine, source_version)
    if source is None:
        return "source_unknown"
    if source >= changed:
        return "not_a_version_change"
    return "runs" if all(t >= changed for t in targets) else "target_unknown"


def _check_version_change(
    path: Path, rule: RoutineCheckRule, versions: Mapping[str, AuroraVersionLines]
) -> None:
    """A ``[version_change]`` rule's line must be one some target line has: each target
    lists lines for each source engine, and its newest is the rule's line or newer."""
    if rule.version_change is None:
        return
    for target in rule.targets:
        listed = versions.get(target)
        for engine in rule.source_engines:
            if listed is None or engine not in listed.source_engines:
                raise CatalogError(
                    f"{path.name}: a version_change rule needs version lines; "
                    f"{target} has none for {engine}"
                )
            newest = max(_line(engine, line) for line in listed.lines)
            if _line(engine, rule.version_change.line) > newest:
                raise CatalogError(
                    f"{path.name}: version_change line {rule.version_change.line!r} is newer "
                    f"than every {target} line ({', '.join(listed.lines)})"
                )


def load_routine_check_rules(
    directory: Path = ROUTINE_CHECKS_DIR,
    extension_lines: dict[str, AuroraExtensionLines] | None = None,
    versions: dict[str, AuroraVersionLines] | None = None,
) -> tuple[RoutineCheckRule, ...]:
    """Every routine check rule file in ``directory``, validated, sorted by id.

    A rule that reads the target line's extension list (``extensions``, or
    ``trusted_extension_languages``) must target only engines with a list for each of
    its source engines (``extension_lines``, the #1078 lists). A ``[version_change]``
    rule's line must be one a target line has (``versions``, ``aurora_versions.toml``).
    """
    extension_lines = aurora_extension_lines() if extension_lines is None else extension_lines
    versions = aurora_version_lines() if versions is None else versions
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
        _check_version_change(path, rule, versions)
        rules.append(rule)
    if not rules:
        raise CatalogError(f"no routine check rule files under {directory}")
    return tuple(rules)


@cache
def routine_check_rules() -> tuple[RoutineCheckRule, ...]:
    """The loaded routine check rules (read once per process), sorted by id."""
    return load_routine_check_rules(
        extension_lines=aurora_extension_lines(), versions=aurora_version_lines()
    )
