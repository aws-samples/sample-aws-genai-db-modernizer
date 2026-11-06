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
- ``ownership``: a ``SECURITY DEFINER`` routine is not owned by a superuser;
- the body checks (#1951), on the statements of a function's or procedure's body as
  PostgreSQL's parser reads them (each SQL statement and expression of a PL/pgSQL body, a
  quoted SQL body, an SQL-standard body; the text an ``EXECUTE`` runs is not read):
  ``function_call`` (a listed function is called), ``catalog_column`` (a listed catalog
  column is read), ``public_schema_create`` (an object is created in the public schema),
  ``setting`` (a listed setting is set, reset, shown or read), ``type_use`` (a listed type
  is named) and ``maintenance_search_path`` (a function with no ``SET search_path`` names a
  table without its schema). A body that does not parse is ``unknown``, never a pass, and
  so is a check that finds nothing in a body that runs dynamic SQL (``EXECUTE``).

A rule with a ``[version_change]`` (#1951) runs only when the move crosses that version:
the source's line (``source_version``) is older than the rule's line and the target line
has it (:func:`~src.shared.catalog.routine_checks.version_change_scope`). On a same-major
target it is left out of the verdict; when the source's version is unknown its outcome is
``unknown``.

The checks on metadata run whether or not the statement parses, so an unparseable
routine still gets every verdict the collection allows. Text holding a NUL, or one the
parser cannot encode (a lone surrogate), is unparseable; no parser error escapes.

The verdict never holds the routine's text: the parser's error message quotes the
statement (literals included), so only the failing stage and the character position are
kept, and the reasons and fix hints are the catalog's own sentences (the exact routine
text stays apart from reports, logs and model inputs, epic #1925).

The target line is the one the gap check's move lands on for the source's installed
extensions (:func:`routine_target_line`), or, when the customer picks it, the newest
listed Aurora PostgreSQL line (``target_choice="newest"``, #1951).
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
    version_change_scope,
)
from src.tools.analysis.relational_gaps import TargetLineChoice, extension_landing_line
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

_CHECK_ORDER: tuple[RoutineCheck, ...] = (
    "parse",
    "language",
    "extensions",
    "ownership",
    "function_call",
    "catalog_column",
    "public_schema_create",
    "setting",
    "type_use",
    "maintenance_search_path",
)
# The checks that read the body's statements (#1951).
_BODY_CHECKS: frozenset[RoutineCheck] = frozenset(_CHECK_ORDER[4:])
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
    target_choice: TargetLineChoice = "same_major",
) -> str | None:
    """The version line the source's routines are checked against: the one the gap
    check's move lands on for the source's installed extensions (#1078,
    :func:`~src.tools.analysis.relational_gaps.extension_landing_line`), or the newest
    listed line when the customer picks ``target_choice="newest"`` (#1951)."""
    return extension_landing_line(
        installed_extensions,
        SOURCE_ENGINE,
        source_version,
        target_engine,
        extension_lines=extension_lines,
        target_choice=target_choice,
    )


# --------------------------------------------------------------------------- the body (#1951)

# libpg_query's raw parse modes of a PL/pgSQL expression (``PLpgSQL_expr.parseMode``):
# 0 a whole SQL statement, 2 an expression, 3-5 an assignment ``target := expression``.
_STATEMENT_MODE = 0
_EXPRESSION_MODE = 2
_ASSIGN_MODES = frozenset({3, 4, 5})
_ASSIGN = re.compile(r":=|=")
# The PL/pgSQL statements that run SQL built at run time: EXECUTE, FOR ... IN EXECUTE,
# RETURN QUERY EXECUTE, OPEN ... FOR EXECUTE (the last two carry a ``dynquery``).
_DYNAMIC_STATEMENTS = frozenset({"PLpgSQL_stmt_dynexecute", "PLpgSQL_stmt_dynfors"})
# The settings functions whose first argument names a setting.
_SETTING_FUNCTIONS = frozenset({"current_setting", "set_config"})


@dataclass(frozen=True)
class _BodyRead:
    """What the body checks read from a function's or procedure's ``CREATE``:

    - ``trees``: the parse tree of each body statement and expression;
    - ``header``: the whole statement's parse tree (its parameters, return type and
      ``SET`` clauses);
    - ``declared_types``: the types a PL/pgSQL body declares its variables with, lower
      case, without schema;
    - ``dynamic``: the body runs SQL built at run time (``EXECUTE``), which no check reads;
    - ``sets_search_path``: the statement has its own ``SET search_path`` clause.
    """

    trees: tuple[Any, ...]
    header: Any
    declared_types: frozenset[str]
    dynamic: bool
    sets_search_path: bool


def _nodes(tree: Any) -> Iterable[tuple[str, Any]]:
    """Every ``(node type, node)`` pair in a parse tree, iteratively (no recursion)."""
    stack = [tree]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, value in item.items():
                if isinstance(value, (Mapping, list)):
                    yield key, value
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)


def _type_name(text: str) -> str:
    """A declared type's own name: lower case, without schema, quotes or modifiers."""
    name = text.split("(")[0].split("[")[0].strip().rsplit(".", 1)[-1]
    return name.strip('"').strip().lower()


def _plpgsql(create_sql: str) -> tuple[list[str], set[str], bool] | None:
    """The SQL texts of a PL/pgSQL body (each statement as written, each expression as
    ``SELECT <expression>``), the types it declares, and whether it runs dynamic SQL;
    None when the body does not parse."""
    try:
        tree = pgparse.parse_pgsql(create_sql)
    except Exception:  # noqa: BLE001 -- parse_routine already judged the text
        return None
    out: list[str] = []
    types: set[str] = set()
    dynamic = False
    for key, node in _nodes(tree):
        if not isinstance(node, Mapping):
            continue
        if key in _DYNAMIC_STATEMENTS or node.get("dynquery") is not None:
            dynamic = True
        if key == "PLpgSQL_type" and isinstance(node.get("typname"), str):
            types.add(_type_name(node["typname"]))
        if key != "PLpgSQL_expr":
            continue
        query, mode = node.get("query"), node.get("parseMode")
        if not isinstance(query, str):
            continue
        if mode == _STATEMENT_MODE:
            out.append(query)
        elif mode == _EXPRESSION_MODE:
            out.append("SELECT " + query)
        elif mode in _ASSIGN_MODES and (match := _ASSIGN.search(query)) is not None:
            out.append("SELECT " + query[match.end() :])
    return out, types, dynamic


def _read_body(create_sql: str, kind: PostgresKind) -> _BodyRead | None:
    """The :class:`_BodyRead` of a function's or procedure's body, or None when the body
    was not read (the statement does not parse, or its language is neither SQL nor
    PL/pgSQL). A piece that does not parse on its own is left out."""
    if kind == "trigger" or parse_routine(create_sql, kind) is not None:
        return None
    try:
        header = pgparse.parse(create_sql)
        stmt = header[0]["stmt"]["CreateFunctionStmt"]
    except Exception:  # noqa: BLE001 -- parse_routine read it; anything else is unread
        return None
    language, body = _function_options(stmt)
    head = postgresql_header(create_sql, kind)
    sets_path = head is not None and head.sets_search_path
    if stmt.get("sql_body") is not None:
        return _BodyRead((stmt["sql_body"],), header, frozenset(), False, sets_path)
    types: set[str] = set()
    dynamic = False
    if language == "plpgsql":
        read = _plpgsql(create_sql)
        if read is None:
            return None
        texts, types, dynamic = read
    elif language == "sql":
        texts = [body] if body is not None else []
    else:
        return None
    trees = tuple(tree for text in texts if (tree := _parse_piece(text)) is not None)
    return _BodyRead(trees, header, frozenset(types), dynamic, sets_path)


def _parse_piece(text: str) -> Any:
    """One body statement's parse tree; None for a piece the parser does not read alone."""
    try:
        return pgparse.parse(text)
    except Exception:  # noqa: BLE001 -- any parser failure leaves the piece out
        return None


def _names(node: Mapping[str, Any], key: str) -> list[str]:
    return [n.lower() for n in (_string(item) for item in node.get(key) or ()) if n]


def _calls(trees: Iterable[Any]) -> Iterable[tuple[list[str], Mapping[str, Any]]]:
    """Each function call's name parts (lower case) and node."""
    for tree in trees:
        for key, node in _nodes(tree):
            if key == "FuncCall" and isinstance(node, Mapping):
                yield _names(node, "funcname"), node


def _builtin(names: list[str]) -> str | None:
    """A call's function name when it is unqualified or in pg_catalog, else None."""
    return names[-1] if names and names[:-1] in ([], ["pg_catalog"]) else None


def _function_call_findings(rule: RoutineCheckRule, read: _BodyRead) -> list[str]:
    listed = set(rule.match.called_functions)
    return sorted(
        {f"{name}()" for names, _ in _calls(read.trees) if (name := _builtin(names)) in listed}
    )


# The schemas a catalog relation is read from: unqualified (pg_catalog is on every
# search_path), pg_catalog, or information_schema.
_CATALOG_SCHEMAS = frozenset({"", "pg_catalog", "information_schema"})


def _catalog_relation(node: Mapping[str, Any]) -> str | None:
    schema = str(node.get("schemaname") or "").lower()
    name = str(node.get("relname") or "").lower()
    return name if name and schema in _CATALOG_SCHEMAS else None


def _catalog_column_findings(rule: RoutineCheckRule, read: _BodyRead) -> list[str]:
    """A listed ``relation.column`` that one statement reads: the statement names the
    relation and a column of that name. The pairing is per statement, so a column read
    later through a record variable is not seen, and a user table's column of the same
    name in a statement that also reads the catalog relation is counted."""
    wanted = [tuple(c.split(".")) for c in rule.match.catalog_columns]
    out: set[str] = set()
    for tree in read.trees:
        for statement in tree if isinstance(tree, list) else [tree]:
            relations: set[str] = set()
            columns: set[str] = set()
            for key, node in _nodes(statement):
                if key == "RangeVar" and isinstance(node, Mapping):
                    if (relation := _catalog_relation(node)) is not None:
                        relations.add(relation)
                elif key == "ColumnRef" and isinstance(node, Mapping):
                    fields = _names(node, "fields")
                    if fields:
                        columns.add(fields[-1])
            out.update(f"{r}.{c}" for r, c in wanted if r in relations and c in columns)
    return sorted(out)


# The statements that create an object in a schema: the node key of its name, and the
# object kind the finding names. A relation's name is a RangeVar; a function's, type's
# or domain's is a list of name parts.
_CREATES_RELATION = {"CreateStmt": ("relation", "TABLE"), "ViewStmt": ("view", "VIEW")}
_CREATES_RELATION |= {"CreateSeqStmt": ("sequence", "SEQUENCE")}
_CREATES_NAMED = {
    "CreateFunctionStmt": ("funcname", "FUNCTION"),
    "CreateEnumStmt": ("typeName", "TYPE"),
    "CreateDomainStmt": ("domainname", "DOMAIN"),
}


def _public_create_findings(read: _BodyRead) -> list[str]:
    """Objects the body creates in the public schema: named ``public``, or unqualified
    when the routine has no ``SET search_path`` of its own (the default path's first
    existing schema is public). A temporary table is never one."""
    out: set[str] = set()

    def public(schema: str | None) -> bool:
        return (schema or "").lower() == "public" or (not schema and not read.sets_search_path)

    for tree in read.trees:
        for key, node in _nodes(tree):
            if not isinstance(node, Mapping):
                continue
            relation: Any = None
            kind = "TABLE"
            if key in _CREATES_RELATION:
                field, kind = _CREATES_RELATION[key]
                relation = node.get(field)
            elif key == "CreateTableAsStmt":
                relation = (node.get("into") or {}).get("rel")
            elif key == "CompositeTypeStmt":
                relation, kind = node.get("typevar"), "TYPE"
            elif key in _CREATES_NAMED:
                field, kind = _CREATES_NAMED[key]
                names = _names(node, field)
                if names and public(names[-2] if len(names) > 1 else None):
                    out.add(f"{kind} created in public")
                continue
            if not isinstance(relation, Mapping) or relation.get("relpersistence") == "t":
                continue
            if public(relation.get("schemaname")):
                out.add(f"{kind} created in public")
    return sorted(out)


def _unqualified_findings(read: _BodyRead) -> list[str]:
    """A function with no ``SET search_path`` whose body names a table without its schema
    (a CTE's name, and a ``pg_``-prefixed catalog relation, which still resolves through
    pg_catalog, are not tables here)."""
    if read.sets_search_path:
        return []
    for tree in read.trees:
        ctes = {
            str(node.get("ctename") or "").lower()
            for key, node in _nodes(tree)
            if key == "CommonTableExpr" and isinstance(node, Mapping)
        }
        for key, node in _nodes(tree):
            if key == "RangeVar" and isinstance(node, Mapping) and not node.get("schemaname"):
                name = str(node.get("relname") or "").lower()
                if name not in ctes and not name.startswith("pg_"):
                    return ["unqualified table, no SET search_path"]
    return []


def _setting_findings(rule: RoutineCheckRule, read: _BodyRead) -> list[str]:
    """Listed settings the routine sets, resets or shows (a body ``SET``/``RESET``/``SHOW``,
    or the function's own ``SET`` clause) or reads or sets through ``current_setting()`` /
    ``set_config()`` with the name as a string literal."""
    listed = set(rule.match.removed_settings)
    found: set[str] = set()
    for tree in (*read.trees, read.header):
        for key, node in _nodes(tree):
            if not isinstance(node, Mapping):
                continue
            if key in ("VariableSetStmt", "VariableShowStmt"):
                name = str(node.get("name") or "").lower()
                if name in listed:
                    found.add(f"{'SHOW' if key == 'VariableShowStmt' else 'SET'} {name}")
    for names, node in _calls(read.trees):
        if _builtin(names) not in _SETTING_FUNCTIONS:
            continue
        args = node.get("args") or []
        first = args[0].get("A_Const") if args and isinstance(args[0], Mapping) else None
        value = ((first or {}).get("sval") or {}).get("sval")
        if isinstance(value, str) and value.lower() in listed:
            found.add(f"{names[-1]}({value.lower()})")
    return sorted(found)


_TYPE_KEYS = frozenset({"typeName", "argType", "returnType", "TypeName"})


def _type_findings(rule: RoutineCheckRule, read: _BodyRead) -> list[str]:
    """Listed types the routine names: its parameters and return type, a cast or column
    type in its body, or a PL/pgSQL variable's declared type."""
    listed = set(rule.match.removed_types)
    found = {t for t in read.declared_types if t in listed}
    for tree in (*read.trees, read.header):
        for key, node in _nodes(tree):
            if key in _TYPE_KEYS and isinstance(node, Mapping):
                names = _names(node, "names")
                if names and names[-1] in listed:
                    found.add(names[-1])
    return sorted(found)


def _unknown(rule: RoutineCheckRule, why: str) -> RoutineCheckResult:
    return RoutineCheckResult(
        rule_id=rule.id,
        check=rule.match.check,
        outcome="unknown",
        impact=rule.impact,
        reason=rule.reason,
        fix_hint=rule.fix_hint,
        findings=(why,),
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
    source_version: str | None = None,
    rules: Iterable[RoutineCheckRule] | None = None,
    extension_lines: Mapping[str, AuroraExtensionLines] | None = None,
) -> RoutineVerdict:
    """The verdict for ``routine`` on ``target_engine``'s ``target_line`` (``"16"``), from
    a source at ``source_version`` (the collection's server version, ``"16.4"``; None
    when unknown). A ``[version_change]`` rule runs only when the move crosses its version
    (see the module docstring).

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
    # #1951: a version-change rule runs only when the move crosses its version.
    scopes = {
        r.id: version_change_scope(r, SOURCE_ENGINE, source_version, (target_line,))
        for r in applicable
    }
    applicable = [r for r in applicable if scopes[r.id] != "not_a_version_change"]
    body = (
        _read_body(routine.create_sql, routine.kind)
        if any(r.match.check in _BODY_CHECKS for r in applicable)
        else None
    )
    # Parsed once: the parse rule reads the failure, the language rule the statement's
    # declared language (None when it did not parse that far).
    failure, declared = _parse(routine.create_sql, routine.kind)
    parse_failure: ParseFailure | None = None
    results: list[RoutineCheckResult] = []
    for rule in applicable:
        match = rule.match
        if scopes[rule.id] == "source_unknown":
            results.append(_unknown(rule, "source version unknown"))
        elif match.check in _BODY_CHECKS:
            results.append(_body_result(rule, body))
        elif match.check == "parse":
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


def _body_result(rule: RoutineCheckRule, read: _BodyRead | None) -> RoutineCheckResult:
    """A body check's result: ``unknown`` when the body was not read, or when it found
    nothing but the body runs dynamic SQL no check reads (#2016 review)."""
    if read is None:
        return _unknown(rule, "body not read")
    check = rule.match.check
    if check == "function_call":
        found = _function_call_findings(rule, read)
    elif check == "catalog_column":
        found = _catalog_column_findings(rule, read)
    elif check == "public_schema_create":
        found = _public_create_findings(read)
    elif check == "setting":
        found = _setting_findings(rule, read)
    elif check == "type_use":
        found = _type_findings(rule, read)
    else:
        found = _unqualified_findings(read)
    if not found and read.dynamic:
        return _unknown(rule, "dynamic SQL not read")
    return _result(rule, bool(found), found)


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
