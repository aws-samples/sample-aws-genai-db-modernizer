# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The statements SQL Server runs on its own system catalogs, left out of the workload
(#1894).

A SQL Server collection records every cached statement that ran often enough, and
SQL Server's tooling, telemetry and the collection script itself read the catalog views
and DMVs and call the catalog procedures. Those statements read the catalog, never the
application's data, so routing must not score them. The collector leaves them out with
the AWS DMS statements (``migration_tool_statements.split_migration_tool_statements``
calls :func:`catalog_statement_match`) and records each one.

Two rules, for a SQL Server source only, each read with the T-SQL dialect. Both fail
closed: a statement is left out only when the rule accounts for every row source in it.

``sqlserver_catalog``
    A query whose every row source (``facade.row_sources``: each ``FROM`` item, comma
    join, ``JOIN``, ``APPLY`` and subquery) is one of:

    - a system catalog view or DMV: a ``sys`` or ``INFORMATION_SCHEMA`` schema in any
      database (``sys.objects``, ``master.sys.databases``), as
      ``table_resolution.is_engine_system_object`` reads a SQL Server name, or one of
      the ``msdb`` database's own ``dbo`` tables (:data:`_MSDB_TABLE_PREFIXES`:
      ``msdb.dbo.sysjobs``, ``msdb.dbo.backupset``);
    - a ``sys`` table-valued function (``CROSS APPLY sys.dm_exec_sql_text(...)``) or a
      built-in row source that reads no table (``OPENJSON``, ``STRING_SPLIT``);
    - a CTE the statement defines.

    It must read at least one catalog view and write nothing (no ``INSERT``,
    ``UPDATE``, ``DELETE``, ``MERGE``, ``SELECT ... INTO``, ``OUTPUT ... INTO``, DDL or
    ``EXEC``), advance no sequence (``NEXT VALUE FOR``), and call no function qualified
    with a schema other than ``sys``. Anything else keeps the statement: an application
    table, function, procedure or view, a temp table or table variable, a four-part
    linked-server name or a ``..`` name, ``OPENQUERY``, ``OPENROWSET`` and
    ``OPENDATASOURCE``, and a name that only resembles a catalog's (``sysobjects``
    unqualified, ``dbo.sysjobs``, ``msdb.dbo.my_jobs``).
``sqlserver_system_procedure``
    One ``EXEC`` of a system catalog procedure (:data:`CATALOG_PROCEDURES`: ``sp_help``,
    ``sp_columns``, ``sp_tables``, ...), unqualified or qualified only by ``sys`` or
    ``master``, with plain arguments, and nothing after it. ``dbo.sp_help`` is an
    application procedure. The procedures that run the application's own statements
    (``sp_executesql``, ``sp_prepexec``, the ``sp_cursor*`` family) are not on the list.

A statement the collection links to an application routine (``parent_routine_ids``) is
part of that routine's code and is kept. A statement the parser cannot read is kept;
only then is it read once more without the invisible format characters (zero-width
spaces) some tools write into their text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from src.shared.excluded_statements import SQLSERVER_CATALOG, SQLSERVER_SYSTEM_PROCEDURE

# The system catalog procedures: each reads metadata or server state and runs no
# statement of the caller's. From SQL Server's documented catalog and system procedures.
CATALOG_PROCEDURES = frozenset(
    {
        "sp_column_privileges", "sp_columns", "sp_columns_100", "sp_databases",
        "sp_datatype_info", "sp_datatype_info_100", "sp_depends",
        "sp_describe_first_result_set", "sp_describe_undeclared_parameters", "sp_fkeys",
        "sp_help", "sp_helpconstraint", "sp_helpdb", "sp_helpfile", "sp_helpfilegroup",
        "sp_helpindex", "sp_helplanguage", "sp_helplogins", "sp_helprole",
        "sp_helprolemember", "sp_helprotect", "sp_helpserver", "sp_helpsort",
        "sp_helpsrvrole", "sp_helpstats", "sp_helptext", "sp_helptrigger", "sp_helpuser",
        "sp_lock", "sp_monitor", "sp_pkeys", "sp_server_info", "sp_special_columns",
        "sp_special_columns_100", "sp_spaceused", "sp_sproc_columns",
        "sp_sproc_columns_100", "sp_statistics", "sp_statistics_100",
        "sp_stored_procedures", "sp_table_privileges", "sp_tables", "sp_who", "sp_who2",
    }
)  # fmt: skip
# The msdb system database's own dbo tables, by the prefixes SQL Server names them with:
# the Agent (sysjobs, sysschedules, sysmail_*), backup and restore history, log shipping.
_MSDB_TABLE_PREFIXES = ("sys", "backup", "restore", "log_shipping_")
# A word every catalog name the rule reads holds (``sys``, ``information_schema``,
# ``msdb``); a statement with none of them outside its comments and literals is not parsed.
_CATALOG_WORDS = ("sys", "information_schema", "msdb")

# One argument of a procedure call, on the code text (literals are ``?``): ``N?``, ``?``,
# ``@v``, a number, NULL, DEFAULT or a bare name, optionally ``@name =`` and OUTPUT.
_ARG = r"(?:@\w+\s*=\s*)?(?:N?\?|@\w+|[-+]?\d+(?:\.\d+)?|\w+)(?:\s+OUT(?:PUT)?)?"
# The procedure unqualified, or as sys.p, master..p, master.sys.p.
_PROCEDURE_RE = re.compile(
    rf"\A\s*EXEC(?:UTE)?\s+(?:master\s*\.\s*(?:sys\s*)?\.\s*|sys\s*\.\s*)?"
    rf"(?P<proc>sp_\w+)(?:\s+{_ARG}(?:\s*,\s*{_ARG})*)?\s*;?\s*\Z",
    re.IGNORECASE,
)
_EXEC_RE = re.compile(r"(?<![\w@$#])EXEC(?:UTE)?\b", re.IGNORECASE)
_BRACKETS_RE = re.compile(r"[\[\]]")


def _without_format_characters(text: str) -> str:
    """``text`` without Unicode format characters (zero-width spaces and joiners)."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def _code(text: str) -> str:
    """The text as SQL Server runs it, brackets dropped: comments a space, literals ``?``."""
    from src.shared.sql_spans import code_text

    return _BRACKETS_RE.sub("", code_text(text, "sqlserver"))


def _procedure(code: str) -> str | None:
    m = _PROCEDURE_RE.match(code)
    if m and m.group("proc").lower() in CATALOG_PROCEDURES:
        return m.group("proc").lower()
    return None


def _is_catalog_table(parts: tuple[str, ...], own_database: str) -> bool:
    """A table source's parts (``[[database.]schema.]name``) name a catalog view."""
    from src.agents.referee.table_resolution import SourceScope, is_engine_system_object

    if len(parts) == 3 and parts[0] == "msdb" and own_database != "msdb":
        if parts[1] == "dbo" and parts[2].startswith(_MSDB_TABLE_PREFIXES):
            return True
    if len(parts) not in (2, 3):
        return False
    # T-SQL reads a two-part name as schema.table, so the database part never stands for
    # the schema: ``sys.objects`` is the catalog view in a database named sys too, and
    # ``sys.dbo.Orders`` is that database's table.
    return is_engine_system_object(".".join(parts[-2:]), SourceScope(None, "sqlserver"))


def _is_catalog_function(parts: tuple[str, ...]) -> bool:
    """A function source is a ``sys`` one, or a built-in row source that reads no table
    (``regex_facts.TSQL_BUILTIN_ROW_SOURCES``: ``OPENJSON``, ``STRING_SPLIT``, ...)."""
    from src.tools.sql.regex_facts import TSQL_BUILTIN_ROW_SOURCES

    if len(parts) == 1:
        return parts[0] in TSQL_BUILTIN_ROW_SOURCES
    return "sys" in parts[:-1]


def _catalog_tables(text: str, database_name: str | None) -> tuple[str, ...] | None:
    """The catalog views a statement reads when the rule accounts for every row source
    in it (see the module docstring); () when it does not; None when the parser cannot
    read the text."""
    from src.tools.sql import facade

    code = _code(text)
    lowered = code.lower()
    if not any(word in lowered for word in _CATALOG_WORDS):  # no catalog name: no parse
        return ()
    read = facade.row_sources(text, "sqlserver")
    if read is None:
        return None
    if read.writes or read.next_value or _EXEC_RE.search(code):
        return ()
    own = (database_name or "").strip().lower()
    catalog: set[str] = set()
    for source in read.sources:
        if source.kind == "table" and _is_catalog_table(source.parts, own):
            catalog.add(".".join(source.parts))
        elif source.kind == "function" and _is_catalog_function(source.parts):
            continue
        elif source.kind != "cte":
            return ()
    calls = facade.sql_tokens(text, "sqlserver").calls
    if any("." in c and "SYS" not in c.split(".")[:-1] for c in calls):
        return ()
    return tuple(sorted(catalog))


def catalog_statement_match(
    row: Mapping[str, Any], text: str, source_engine: str, database_name: str | None = None
) -> tuple[str, tuple[str, ...]] | None:
    """The rule and the catalog objects that make ``text`` (``row``'s statement) one
    SQL Server ran on its own catalogs, or None. Only a ``sqlserver`` source has any."""
    if (source_engine or "").strip().lower() != "sqlserver" or not text.strip():
        return None
    if row.get("parent_routine_ids"):
        return None
    procedure = _procedure(_code(text))
    if procedure:
        return SQLSERVER_SYSTEM_PROCEDURE, (procedure,)
    tables = _catalog_tables(text, database_name)
    if tables is None:  # unreadable: one retry without format characters, else kept
        cleaned = _without_format_characters(text)
        tables = _catalog_tables(cleaned, database_name) if cleaned != text else None
    return (SQLSERVER_CATALOG, tables) if tables else None
