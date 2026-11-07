# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parse and check one PostgreSQL routine against Aurora PostgreSQL (#1935), no model.

The input is a routine's ``CREATE`` statement plus the metadata the collection gives
with it (:class:`PostgresRoutine`); the output is a verdict (:class:`RoutineVerdict`):
whether PostgreSQL's own parser reads the statement, and one result per applicable rule
of ``src/catalog/routine_checks/`` (:mod:`src.shared.catalog.routine_checks`), each with
its rule id, outcome, reason and fix hint. Every rule is data; this module only applies
them, one routine at a time, with no I/O beyond reading the catalog:

- ``parse``: the statement is one ``CREATE FUNCTION``, ``CREATE PROCEDURE`` or
  ``CREATE TRIGGER`` (the routine's kind) that libpg_query, PostgreSQL's parser, reads;
  a PL/pgSQL body is parsed by its PL/pgSQL parser and a quoted SQL body as SQL;
- ``language``: the metadata's language and the statement's ``LANGUAGE`` agree (when the
  statement parses that far), and the language is one every line runs, or a trusted
  language the target line offers as an extension;
- ``extensions``: every extension the routine uses is offered on the target line
  (``src/catalog/aurora_postgresql_extensions.toml``, #1078);
- ``ownership``: a ``SECURITY DEFINER`` routine is not owned by a superuser.

The checks on metadata run whether or not the statement parses, so an unparseable
routine still gets every verdict the collection allows. Text holding a NUL, or one the
parser cannot encode (a lone surrogate), is unparseable; no parser error escapes.

The verdict never holds the routine's text: the parser's error message quotes the
statement (literals included), so only the failing stage and the character position are
kept, and the reasons and fix hints are the catalog's own sentences (the exact routine
text stays apart from reports, logs and model inputs, epic #1925).

The target line is the one the gap check's move lands on for the source's installed
extensions (:func:`routine_target_line`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import pgparse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from src.shared.catalog.gaps import AuroraExtensionLines, aurora_extension_lines
from src.shared.catalog.routine_checks import (
    RoutineCheck,
    RoutineCheckRule,
    routine_check_rules,
)
from src.tools.analysis.relational_gaps import extension_landing_line
from src.tools.routines.verdict import (
    ParseFailure,
    RoutineCheckResult,
    RoutineDependency,
    RoutineVerdict,
)

__all__ = [
    "SOURCE_ENGINE",
    "PostgresHeader",
    "PostgresKind",
    "ParseFailure",
    "PostgresRoutine",
    "RoutineCheckResult",
    "RoutineVerdict",
    "check_postgresql_routine",
    "parse_routine",
    "postgresql_header",
    "routine_target_line",
    "trigger_references",
]

# The collector source engine these routines come from (the rules' source_engines).
SOURCE_ENGINE = "postgresql"
# The routine kinds PostgreSQL has (no events: RoutineKind's "event" is MySQL's).
PostgresKind = Literal["function", "procedure", "trigger"]

_CHECK_ORDER: tuple[RoutineCheck, ...] = ("parse", "language", "extensions", "ownership")
# A language name the verdict may repeat (pg_language.lanname is a lower-case identifier
# of at most 63 bytes); a declared language that is not one is named by a placeholder.
_LANGUAGE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_NOT_A_LANGUAGE_NAME = "(not a language name)"
# A model's config: no error message or detail repeats the input (the routine's text).
_HIDDEN = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
_STATEMENT_KINDS: dict[PostgresKind, str] = {
    "function": "CreateFunctionStmt",
    "procedure": "CreateFunctionStmt",
    "trigger": "CreateTrigStmt",
}


class PostgresRoutine(BaseModel):
    """One PostgreSQL routine, as the routine collection (#1652, #1920) gives it.

    - ``routine_id``: the routine's name with its schema and argument types
      (``public.add_tax(numeric)``), or its trigger's ``schema.table.trigger``.
    - ``kind``: ``function`` (a trigger function too: one that ``RETURNS trigger``),
      ``procedure`` (PostgreSQL 11 and later) or ``trigger`` (the ``CREATE TRIGGER``).
    - ``create_sql``: ``pg_get_functiondef(oid)`` for a function or procedure,
      ``pg_get_triggerdef(oid)`` for a trigger.
    - ``language``: ``pg_language.lanname`` (``plpgsql``, ``sql``, ``c``, ...); a
      trigger has none.
    - ``owner``: the owning role (``pg_roles.rolname``); ``owner_is_superuser`` its
      ``rolsuper``, required for a function or procedure.
    - ``security_definer``: ``pg_proc.prosecdef``.
    - ``extensions``: the extensions the routine depends on (``pg_depend`` to
      ``pg_extension``), by name.

    A validation error never holds the input: neither its message nor
    ``ValidationError.errors()`` repeats ``create_sql`` (epic #1925).
    """

    model_config = _HIDDEN

    routine_id: str = Field(min_length=1)
    kind: PostgresKind
    create_sql: str
    language: str | None = None
    owner: str | None = None
    owner_is_superuser: bool | None = None
    security_definer: bool = False
    extensions: tuple[str, ...] = ()

    @field_validator("language")
    @classmethod
    def _lower_language(cls, value: str | None) -> str | None:
        text = (value or "").strip().lower()
        return text or None

    @field_validator("extensions")
    @classmethod
    def _lower_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({name.strip().lower() for name in value if name.strip()}))

    def _kind_problem(self) -> str | None:
        if self.kind == "trigger":
            if self.language is not None or self.security_definer:
                return "a trigger has no language and no SECURITY DEFINER"
        elif self.language is None:
            return f"a {self.kind} needs its language"
        elif self.owner_is_superuser is None:
            return f"a {self.kind} needs owner_is_superuser"
        return None

    @model_validator(mode="wrap")
    @classmethod
    def _without_input(
        cls, data: Any, handler: Callable[[Any], PostgresRoutine]
    ) -> PostgresRoutine:
        """Validate, then check the metadata fits the kind; every error is rebuilt
        with no input and no context, outside the handler's exception, so nothing
        reachable from it (``errors()``, ``__context__``) holds the routine's text."""
        details: list[InitErrorDetails] = []
        try:
            routine = handler(data)
        except ValidationError as exc:
            details = [
                _hidden_detail(e["type"], e["msg"], e["loc"])
                for e in exc.errors(include_url=False, include_input=False, include_context=False)
            ]
        else:
            if (problem := routine._kind_problem()) is None:
                return routine
            details = [_hidden_detail("value_error", problem, ())]
        raise ValidationError.from_exception_data(cls.__name__, details, hide_input=True)


def _hidden_detail(kind: str, message: str, loc: tuple[int | str, ...]) -> InitErrorDetails:
    """One validation error with its message only (braces would be read as a template)."""
    text = message.replace("{", "(").replace("}", ")")
    return {"type": PydanticCustomError(kind, text), "loc": loc, "input": None}


def _string(node: Any) -> str | None:
    if isinstance(node, Mapping) and isinstance(node.get("String"), Mapping):
        value = node["String"].get("sval")
        return value if isinstance(value, str) else None
    return None


def _function_options(stmt: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """A CREATE FUNCTION's declared language and its quoted body (the first ``AS``
    string; a C function's second one is its link symbol), each None when absent."""
    language = body = None
    for option in stmt.get("options") or ():
        elem = option.get("DefElem") if isinstance(option, Mapping) else None
        if not isinstance(elem, Mapping):
            continue
        arg = elem.get("arg")
        if elem.get("defname") == "language":
            language = (_string(arg) or "").lower() or None
        elif elem.get("defname") == "as" and isinstance(arg, Mapping):
            items = (arg.get("List") or {}).get("items") or ()
            body = _string(items[0]) if items else None
    return language, body


def _parse(create_sql: str, kind: PostgresKind) -> tuple[ParseFailure | None, str | None]:
    """Where ``create_sql`` failed to parse (None when it parsed) and the language its
    statement declares (None for a trigger, or when it is not known)."""
    if "\x00" in create_sql:
        # PostgreSQL text holds no NUL, and the C parser would stop reading at it.
        return ParseFailure(stage="statement", position=create_sql.index("\x00") + 1), None
    try:
        statements = pgparse.parse(create_sql)
    except UnicodeError:
        # Text the parser cannot encode (a lone surrogate); the error quotes the text.
        return ParseFailure(stage="statement"), None
    except pgparse.PGQueryError as exc:
        position = getattr(exc, "position", None)
        return (
            ParseFailure(
                stage="statement",
                position=position if isinstance(position, int) and position > 0 else None,
            ),
            None,
        )
    if len(statements) != 1:
        return ParseFailure(stage="kind"), None
    stmt_node = statements[0].get("stmt") or {}
    stmt = stmt_node.get(_STATEMENT_KINDS[kind]) if isinstance(stmt_node, Mapping) else None
    if not isinstance(stmt, Mapping):
        return ParseFailure(stage="kind"), None
    if kind == "trigger":
        return None, None
    if bool(stmt.get("is_procedure")) != (kind == "procedure"):
        return ParseFailure(stage="kind"), None
    language, body = _function_options(stmt)
    try:
        if language == "plpgsql":
            pgparse.parse_pgsql(create_sql)
        elif language == "sql" and body is not None and stmt.get("sql_body") is None:
            pgparse.parse(body)
    except (pgparse.PGQueryError, UnicodeError):
        return ParseFailure(stage="body"), language
    return None, language


def parse_routine(create_sql: str, kind: PostgresKind) -> ParseFailure | None:
    """None when ``create_sql`` is one statement of ``kind`` that PostgreSQL's parser
    reads, body included; else where it failed. The parser's message is dropped: it
    quotes the statement. Text holding a NUL, or one the parser cannot encode, does not
    parse; no exception leaves this function."""
    return _parse(create_sql, kind)[0]


def _language_findings(
    match: Any, line_extensions: Mapping[str, str], metadata: str, declared: str | None
) -> tuple[str, ...]:
    """What fails the language rule: the metadata's language and the statement's declared
    one must agree, and each must run on the target line. Empty when it passes."""

    def runs(language: str) -> bool:
        return language in match.languages or (
            language in match.trusted_extension_languages and language in line_extensions
        )

    def named(language: str) -> str:
        return language if _LANGUAGE_NAME.match(language) else _NOT_A_LANGUAGE_NAME

    if declared is not None and declared != metadata:
        return (f"metadata: {named(metadata)}", f"statement: {named(declared)}")
    return () if runs(metadata) else (named(metadata),)


def routine_target_line(
    source_version: str | None,
    installed_extensions: Mapping[str, str | None] | None,
    target_engine: str,
    *,
    extension_lines: Mapping[str, AuroraExtensionLines] | None = None,
) -> str | None:
    """The version line the source's routines are checked against: the one the gap
    check's move lands on for the source's installed extensions (#1078,
    :func:`~src.tools.analysis.relational_gaps.extension_landing_line`)."""
    return extension_landing_line(
        installed_extensions,
        SOURCE_ENGINE,
        source_version,
        target_engine,
        extension_lines=extension_lines,
    )


def _result(
    rule: RoutineCheckRule, failed: bool, findings: Iterable[str] = ()
) -> RoutineCheckResult:
    return RoutineCheckResult(
        rule_id=rule.id,
        check=rule.match.check,
        outcome="fail" if failed else "pass",
        impact=rule.impact,
        reason=rule.reason,
        fix_hint=rule.fix_hint,
        findings=tuple(findings) if failed else (),
    )


def check_postgresql_routine(
    routine: PostgresRoutine,
    *,
    target_engine: str,
    target_line: str,
    rules: Iterable[RoutineCheckRule] | None = None,
    extension_lines: Mapping[str, AuroraExtensionLines] | None = None,
) -> RoutineVerdict:
    """The verdict for ``routine`` on ``target_engine``'s ``target_line`` (``"16"``).

    Every rule for a PostgreSQL source, ``target_engine`` and the routine's kind runs,
    in check order (parse, language, extensions, ownership); the dependency check is
    :func:`src.tools.routines.dependencies.check_dependencies`'s. ``rules`` and
    ``extension_lines`` default to the catalog's. A ``target_line`` the extension list
    does not name is a ValueError.
    """
    lists = aurora_extension_lines() if extension_lines is None else extension_lines
    offered = lists.get(target_engine)
    if offered is None or SOURCE_ENGINE not in offered.source_engines:
        raise ValueError(f"{target_engine!r} has no extension list for {SOURCE_ENGINE}")
    line = offered.lines.get(target_line)
    if line is None:
        raise ValueError(
            f"{target_engine} line {target_line!r} is not listed ({sorted(offered.lines)})"
        )
    applicable = sorted(
        (
            r
            for r in (routine_check_rules() if rules is None else rules)
            if SOURCE_ENGINE in r.source_engines
            and target_engine in r.targets
            and routine.kind in r.kinds
            # The dependency check needs the step's moved objects (dependencies.py).
            and r.match.check in _CHECK_ORDER
        ),
        key=lambda r: (_CHECK_ORDER.index(r.match.check), r.id),
    )
    # Parsed once: the parse rule reads the failure, the language rule the statement's
    # declared language (None when it did not parse that far).
    failure, declared = _parse(routine.create_sql, routine.kind)
    parse_failure: ParseFailure | None = None
    results: list[RoutineCheckResult] = []
    for rule in applicable:
        match = rule.match
        if match.check == "parse":
            parse_failure = failure
            results.append(_result(rule, failure is not None))
        elif match.check == "language":
            found = _language_findings(match, line.extensions, routine.language or "", declared)
            results.append(_result(rule, bool(found), found))
        elif match.check == "extensions":
            missing = [name for name in routine.extensions if name not in line.extensions]
            results.append(_result(rule, bool(missing), missing))
        else:
            results.append(
                _result(rule, routine.security_definer and bool(routine.owner_is_superuser))
            )
    return RoutineVerdict(
        routine_id=routine.routine_id,
        kind=routine.kind,
        target_engine=target_engine,
        target_line=target_line,
        parse_failure=parse_failure,
        checks=tuple(results),
    )


# --------------------------------------------------------------------------- the header, for rewrites


@dataclass(frozen=True)
class PostgresHeader:
    """What the rewrites (#1937) read from a function's or procedure's ``CREATE``, from
    PostgreSQL's own parse tree (never a pattern over the text):

    - ``security_definer``: the statement says ``SECURITY DEFINER``;
    - ``sets_search_path``: it already has a ``SET search_path`` clause (any form);
    - ``insert_at``: the character offset a new clause goes in at: the start of its ``AS``
      clause, or of its first option when the body is an SQL-standard one
      (``RETURN ...``, ``BEGIN ATOMIC ...``); None when it has neither;
    - ``body``: the quoted body (``AS '...'``) as the parser reads it, None for an
      SQL-standard body. The text keeps its literals: it is never recorded.
    """

    security_definer: bool
    sets_search_path: bool
    insert_at: int | None
    body: str | None


def _char_offset(text: str, byte_offset: int) -> int:
    """The character offset of a parse-tree location (libpg_query counts UTF-8 bytes)."""
    return len(text.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def _function_statement(create_sql: str, kind: PostgresKind) -> Mapping[str, Any] | None:
    """The one ``CreateFunctionStmt`` (or ``CreateTrigStmt``) of a statement that parses as
    ``kind``, body included; None otherwise."""
    if parse_routine(create_sql, kind) is not None:
        return None
    stmt_node = pgparse.parse(create_sql)[0].get("stmt") or {}
    stmt = stmt_node.get(_STATEMENT_KINDS[kind])
    return stmt if isinstance(stmt, Mapping) else None


def created_schemas(create_sql: str, kind: PostgresKind) -> tuple[str | None, ...] | None:
    """The schema of each name the statement creates or binds, as PostgreSQL's parser
    reads it (a ``U&"..."`` name decoded, unquoted names folded): a function's or
    procedure's own name; a trigger's table and its function. None for an unqualified
    name; the result is None when the statement does not parse as ``kind`` (#1944)."""
    stmt = _function_statement(create_sql, kind)
    if stmt is None:
        return None

    def schema(parts: Any) -> str | None:
        names = [_string(p) for p in parts or []]
        return names[-2] if len(names) >= 2 else None

    if kind == "trigger":
        relation = stmt.get("relation") or {}
        return (relation.get("schemaname"), schema(stmt.get("funcname")))
    return (schema(stmt.get("funcname")),)


def postgresql_header(create_sql: str, kind: PostgresKind) -> PostgresHeader | None:
    """The :class:`PostgresHeader` of a function's or procedure's ``CREATE``; None for a
    trigger, or a statement that does not parse as ``kind``. Never raises."""
    if kind == "trigger":
        return None
    try:
        stmt = _function_statement(create_sql, kind)
    except Exception:  # noqa: BLE001 -- parse_routine read it; anything else is no header
        return None
    if stmt is None:
        return None
    security = sets_path = False
    as_at: int | None = None
    first: int | None = None
    body: str | None = None
    for option in stmt.get("options") or ():
        elem = option.get("DefElem") if isinstance(option, Mapping) else None
        if not isinstance(elem, Mapping):
            continue
        location = elem.get("location")
        if isinstance(location, int) and location >= 0:
            first = location if first is None else min(first, location)
        name, arg = elem.get("defname"), elem.get("arg")
        if name == "security" and isinstance(arg, Mapping):
            security = bool((arg.get("Boolean") or {}).get("boolval"))
        elif name == "set" and isinstance(arg, Mapping):
            var = arg.get("VariableSetStmt") or {}
            sets_path = sets_path or str(var.get("name", "")).lower() == "search_path"
        elif name == "as" and isinstance(location, int) and location >= 0:
            as_at = location
            body = _function_options(stmt)[1]
    if as_at is None and stmt.get("sql_body") is None:
        first = None
    at = as_at if as_at is not None else first
    return PostgresHeader(
        security_definer=security,
        sets_search_path=sets_path,
        insert_at=_char_offset(create_sql, at) if at is not None else None,
        body=body,
    )


def trigger_references(create_sql: str) -> tuple[RoutineDependency, ...] | None:
    """The table a ``CREATE TRIGGER`` is on and the function it runs, from the parse tree
    (lower case, schema None when unqualified); None when it does not parse as one
    trigger. Never raises."""
    try:
        stmt = _function_statement(create_sql, "trigger")
    except Exception:  # noqa: BLE001 -- parse_routine read it; anything else is no answer
        return None
    if stmt is None:
        return None
    relation = stmt.get("relation") or {}
    names = [n for n in (_string(item) for item in stmt.get("funcname") or ()) if n]
    out: list[RoutineDependency] = []
    if relation.get("relname"):
        out.append(
            RoutineDependency(
                kind="table",
                name=str(relation["relname"]).lower(),
                schema_name=(str(relation.get("schemaname") or "").lower() or None),
            )
        )
    if names:
        out.append(
            RoutineDependency(
                kind="routine",
                name=names[-1].lower(),
                schema_name=names[-2].lower() if len(names) > 1 else None,
                routine_kind="function",
            )
        )
    return tuple(sorted(out, key=lambda d: (d.kind, d.schema_name or "", d.name)))
