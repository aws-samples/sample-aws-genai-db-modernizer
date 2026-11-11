# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A collection written before #1652 lists routines by name only (#2124).

Its routine rows have no ``body_status``: no code, and the functions and aggregates an
extension owns among them (``CREATE EXTENSION`` creates those on Aurora; today's
collectors leave them out by ``pg_depend``'s extension membership). The routine phase
reads none of them, so they are never routines to carry or to migrate yourself; a row a
current collection lists, whatever its language, is still read and checked.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.routine_sources import RoutineSource
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.routines.migration import (
    listed_by_name_only,
    name_only_routine_count,
    run_routine_migration_deterministic,
)

REPO = Path(__file__).resolve().parents[4]


def _discourse_collector(tmp_path: Path) -> dict[str, Any]:
    """The bundled discourse collection (pre-#1652), ingested as run_collect.py does."""
    with zipfile.ZipFile(REPO / "docs/examples/discourse/discourse.zip") as z:
        z.extractall(tmp_path)
    raw = json.loads((tmp_path / "discourse-collection.json").read_text())
    parsed = parse_offline_collection(raw, database_name="discourse")
    return build_offline_collector_output(parsed, job_id="j", database_name="discourse", s3_key="u")


def _run(collector: dict[str, Any], sources: list[RoutineSource] | None = None) -> Any:
    return run_routine_migration_deterministic(
        collector, None, sources, job_id="j", database_name="db", assignment_version=1
    )


def test_the_discourse_names_are_no_routine_to_migrate(tmp_path: Path) -> None:
    collector = _discourse_collector(tmp_path)
    schema = collector["database_schema"]
    assert len(schema["procedures"]) == 266 and len(schema["triggers"]) == 3
    # Its 240 C and internal functions (hstore, citext, pg_trgm, unaccent, vector) and
    # 6 aggregates are an extension's; none of the rows says which, and none has code.
    assert name_only_routine_count(collector) == 269
    assert _run(collector) is None


def _row(name: str, language: str, **extra: Any) -> dict[str, Any]:
    return {
        "procedure_id": name, "procedure_name": name, "schema_name": "public",
        "procedure_type": "FUNCTION", "specific_name": f"{name}_1", "language": language,
        "owner": "app", "security_definer": False, "depends_on": [], **extra,
    }  # fmt: skip


def _collector(procedures: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> dict:
    return {
        "metadata": {"source_database": {"engine": "postgresql", "version": "16.2"}},
        "database_schema": {
            "tables": [{"table_id": "orders", "table_name": "orders", "schema_name": "public"}],
            "procedures": procedures,
            "triggers": triggers,
        },
    }


CREATE = (
    "CREATE OR REPLACE FUNCTION public.{name}()\n RETURNS integer\n LANGUAGE {lang}\n"
    "AS $function${body}$function$\n"
)


def test_only_the_rows_a_current_collection_lists_are_read() -> None:
    old = [_row("akeys", "C"), _row("avg", "INTERNAL"), _row("legacy_fn", "PLPGSQL")]
    old_trigger = {"trigger_name": "tg_old", "table_id": "orders", "definition": "EXECUTE x()"}
    current = [
        _row("app_fn", "sql", body_status="collected", owner="postgres"),
        # A current collection never lists an extension's function: a C function it
        # lists is the customer's, read and checked like any other.
        _row("own_c_fn", "c", body_status="collected"),
        _row("off_fn", "plpgsql", body_status="opted_out"),
    ]
    sources = [
        RoutineSource(
            kind="function", routine_id=name, schema_name="public", specific_name=f"{name}_1",
            create_statement=CREATE.format(name=name, lang=lang, body=body),
            origin="pg_get_functiondef", exact=True,
        )  # fmt: skip
        for name, lang, body in (("app_fn", "sql", "SELECT 1"), ("own_c_fn", "c", "x"))
    ]
    collector = _collector(old + current, [old_trigger])
    assert [listed_by_name_only(r) for r in old + current] == [True] * 3 + [False] * 3
    assert name_only_routine_count(collector) == 4
    artifact = _run(collector, sources)
    rows = {r.routine_id: r for r in artifact.routines}
    assert set(rows) == {"public.app_fn", "public.own_c_fn", "public.off_fn"}
    assert rows["public.app_fn"].status == "as_is"
    assert rows["public.own_c_fn"].reasons == ["postgresql-routine-language"]
    assert rows["public.off_fn"].reasons == ["source_text_not_collected"]
    assert artifact.total == 3


def test_a_mysql_collection_before_1652_is_not_read_either() -> None:
    """An old MySQL collection kept the body (``definition``) but no status: no exact
    CREATE, so it is not read either, as the PostgreSQL one."""
    collector = {
        "metadata": {"source_database": {"engine": "mysql", "version": "8.0.36"}},
        "database_schema": {
            "tables": [{"table_id": "orders", "table_name": "orders"}],
            "procedures": [
                {"procedure_id": "p", "procedure_name": "p", "procedure_type": "PROCEDURE",
                 "definition": "BEGIN END", "sql_mode": ""}  # fmt: skip
            ],
        },
    }
    assert name_only_routine_count(collector) == 1
    assert _run(collector) is None


def test_the_run_log_counts_the_rows_it_leaves_out(tmp_path: Path, monkeypatch) -> None:
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("db/j/collector/output.json", _collector([_row("akeys", "C")], []))
    logged: list[str] = []
    monkeypatch.setattr(run_assessment, "_log", lambda _phase, text: logged.append(text))
    monkeypatch.setattr(run_assessment, "_start_phase", lambda _phase: None)
    monkeypatch.setattr(
        "src.tools.routines.migration.ensure_routine_migration",
        lambda *_a: (1, None, None, False),
    )
    assert run_assessment.phase_routine_migration(store, "j", "db")["status"] == "skipped"
    assert logged[0].startswith("1 routine is listed by name only (a collection")
    assert "collect again with the current script" in logged[0]
