# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A small synthetic PostgreSQL 9.6 job for the routine repair seam tests (#1940).

``public.wal_pos(integer)`` calls a function PostgreSQL 10 renamed, so it fails its
check and needs repair; ``public.ok(integer)`` is carried; ``public.hidden(integer)``
also fails the check, but its body was opted out of in the collection (#1933);
``public.orphan(integer)`` names a table the collection lacks (a dependency reason no
edit of its text settles). Every literal in a body carries ``LITERAL_MARK``: the
collection redacts it on read, so it must never reach a model.
"""

from __future__ import annotations

import copy
from typing import Any

from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.routine_sources import RoutineSource
from src.storage.local_store import LocalArtifactStore
from src.tools.routines.migration import run_routine_migration_deterministic

DB, JOB = "shop", "job-1940"
LITERAL_MARK = "LiteralMark1940"
OPTED_OUT_MARK = "OptedOutBody1940"
WAL = "public.wal_pos(integer)"
HIDDEN = "public.hidden(integer)"

WAL_CREATE = (
    "CREATE OR REPLACE FUNCTION public.wal_pos(p integer)\n RETURNS text\n LANGUAGE sql\n"
    f"AS $function$SELECT pg_current_xlog_location()::text || '{LITERAL_MARK}'$function$\n"
)
OK_CREATE = (
    "CREATE OR REPLACE FUNCTION public.ok(p integer)\n RETURNS integer\n LANGUAGE sql\n"
    "AS $function$SELECT p$function$\n"
)
HIDDEN_CREATE = (
    "CREATE OR REPLACE FUNCTION public.hidden(p integer)\n RETURNS text\n LANGUAGE sql\n"
    f"AS $function$SELECT pg_current_xlog_location()::text || '{OPTED_OUT_MARK}'$function$\n"
)
ORPHAN_CREATE = (
    "CREATE OR REPLACE FUNCTION public.orphan(p integer)\n RETURNS integer\n LANGUAGE sql\n"
    "AS $function$SELECT count(*)::integer FROM public.missing_table$function$\n"
)
#: The redacted text the model is given for wal_pos.
WAL_REDACTED = WAL_CREATE.replace(f"'{LITERAL_MARK}'", "'?'")
#: A good repair of it: the renamed function's new name, nothing else.
WAL_REPAIRED = WAL_REDACTED.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")


def _function(name: str, create: str, *, status: str = "collected") -> dict[str, Any]:
    return {
        "procedure_id": name, "procedure_name": name, "schema_name": "public",
        "procedure_type": "FUNCTION", "specific_name": f"{name}_1", "language": "sql",
        "owner": "app", "security_definer": False, "body_status": status,
        "definition": create, "depends_on": [],
    }  # fmt: skip


def collector() -> dict[str, Any]:
    """The collection as written (literals in place: the store redacts them on read)."""
    return {
        "metadata": {"source_database": {"engine": "postgresql", "version": "9.6.2"}},
        "database_schema": {
            "tables": [{"table_id": "orders", "table_name": "orders", "schema_name": "public"}],
            "procedures": [
                _function("wal_pos", WAL_CREATE),
                _function("ok", OK_CREATE),
                _function("hidden", HIDDEN_CREATE),
                {
                    **_function("orphan", ORPHAN_CREATE),
                    "depends_on": [{"kind": "table", "name": "public.missing_table"}],
                },
            ],
        },
    }


def sources(col: dict[str, Any] | None = None) -> list[RoutineSource]:
    """The routine sources: the exact text of every routine, literals in place."""
    col = col or collector()
    return [
        RoutineSource(
            kind="function", routine_id=p["procedure_name"], schema_name="public",
            specific_name=p["specific_name"], create_statement=p["definition"],
            origin="pg_get_functiondef",
        )  # fmt: skip
        for p in col["database_schema"]["procedures"]
    ]


def migration(col: dict[str, Any] | None = None) -> RoutineMigrationContract:
    """The routine-migration artifact, checked on the exact text of every routine."""
    col = col or collector()
    built = run_routine_migration_deterministic(
        col, None, sources(col), job_id=JOB, database_name=DB, assignment_version=1
    )
    assert built is not None
    return built


def opted_out(col: dict[str, Any]) -> dict[str, Any]:
    """``col`` with hidden's body opted out of, its text still in the row (a defect a
    reader must not trust)."""
    out = copy.deepcopy(col)
    for p in out["database_schema"]["procedures"]:
        if p["procedure_name"] == "hidden":
            p["body_status"] = "opted_out"
    return out


def job(
    tmp_path,
    *,
    col: dict[str, Any] | None = None,
    hide: bool = True,
    built: RoutineMigrationContract | None = None,
    written: list[RoutineSource] | None = None,
) -> LocalArtifactStore:
    """A store with the job's collection, its routine sources (the exact texts) and its
    routine-migration artifact (v1, or ``built``). With ``hide`` the collection marks
    hidden's body opted out, though the artifact was checked on its text (as if the two
    disagreed)."""
    from src.agents.collector.routine_sources import write_routine_sources

    store = LocalArtifactStore(base_dir=str(tmp_path))
    col = col or collector()
    store.write_json(f"{DB}/{JOB}/collector/output.json", opted_out(col) if hide else col)
    write_routine_sources(store, DB, JOB, col, written if written is not None else sources(col))
    store.write_json(
        f"{DB}/{JOB}/routines/v1/routine_migration.json",
        (built or migration(col)).model_dump(mode="json"),
    )
    return store


# --------------------------------------------------------------------------- MySQL

MYSQL_BODY = "BEGIN\n  DECLARE rank INT DEFAULT 1;\n  SELECT CONCAT(p, rank, 'salt') INTO r;\nEND"
MYSQL_EXACT = (
    "CREATE DEFINER=`app`@`%` PROCEDURE `hashpw`(IN p varchar(64), OUT r varchar(64))\n"
    + MYSQL_BODY
)
MYSQL_ID = "shop.hashpw"


def mysql_collector() -> dict[str, Any]:
    """A MySQL 5.7 procedure whose variable is a word MySQL 8.0 reserves."""
    proc = {
        "procedure_id": MYSQL_ID, "procedure_name": "hashpw", "schema_name": "shop",
        "procedure_type": "PROCEDURE", "specific_name": "hashpw", "language": "SQL",
        "owner": "app@%", "security_definer": True, "body_status": "collected",
        "body_exact": True, "definition": MYSQL_BODY, "sql_mode": "STRICT_TRANS_TABLES",
        "parameters": [
            {"parameter_name": "p", "data_type": "varchar(64)", "parameter_mode": "IN"},
            {"parameter_name": "r", "data_type": "varchar(64)", "parameter_mode": "OUT"},
        ],
    }  # fmt: skip
    return {
        "metadata": {"source_database": {"engine": "mysql", "version": "5.7.40"}},
        "database_schema": {
            "tables": [{"table_id": "shop.users", "table_name": "users", "schema_name": "shop"}],
            "procedures": [proc],
        },
    }


def mysql_migration() -> RoutineMigrationContract:
    source = RoutineSource(
        kind="procedure", routine_id="hashpw", schema_name="shop", specific_name="hashpw",
        create_statement=MYSQL_EXACT, origin="show_create",
    )  # fmt: skip
    built = run_routine_migration_deterministic(
        mysql_collector(), None, [source], job_id=JOB, database_name=DB, assignment_version=1
    )
    assert built is not None
    return built
