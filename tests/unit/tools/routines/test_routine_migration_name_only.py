# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A routine row with neither a body status nor code is a name only (#2124).

A collection written before #1652 lists routines so, the functions and aggregates an
extension owns among them (``CREATE EXTENSION`` creates those on Aurora; today's
collectors leave them out by ``pg_depend``'s extension membership). The routine phase
reads none of them, so they are never routines to carry or to migrate yourself. A row
with code is read, status or not: a DDL upload's and an ``assess()`` collection's
routines still get their verdict, the HIGH to-do included.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.routine_sources import RoutineSource
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.routines.migration import run_routine_migration_deterministic

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
    # 6 aggregates are an extension's; none of the 266 rows says which, and none has
    # code. Its 3 triggers carry their action statement, so they are read.
    artifact = _run(collector)
    assert sorted(r.kind for r in artifact.routines) == ["trigger"] * 3
    assert artifact.total == 3
    from src.tools.routines.migration import name_only_routine_count

    assert name_only_routine_count(collector) == 266


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
    old_trigger = {"trigger_name": "tg_old", "table_id": "orders"}
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
    artifact = _run(collector, sources)
    rows = {r.routine_id: r for r in artifact.routines}
    assert set(rows) == {"public.app_fn", "public.own_c_fn", "public.off_fn"}
    assert rows["public.app_fn"].status == "as_is"
    assert rows["public.own_c_fn"].reasons == ["postgresql-routine-language"]
    assert rows["public.off_fn"].reasons == ["source_text_not_collected"]
    assert artifact.total == 3
    from src.tools.routines.migration import listed_by_name_only, name_only_routine_count

    assert [listed_by_name_only(r) for r in old + current] == [True] * 3 + [False] * 3
    assert name_only_routine_count(collector) == 4


def _mysql_collector(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "metadata": {"source_database": {"engine": "mysql", "version": "8.0.36"}},
        "database_schema": {"tables": [{"table_id": "orders", "table_name": "orders"}], **schema},
    }


DDL = """
CREATE PROCEDURE shop.purge_orders()
BEGIN
  DELETE FROM orders WHERE id < 0;
END;
CREATE TRIGGER shop.tg_orders BEFORE INSERT ON orders FOR EACH ROW SET NEW.id = NEW.id;
"""


def test_a_ddl_upload_keeps_its_routines_and_the_to_do() -> None:
    """A DDL upload's rows carry their code and no body status (``parse_ddl_routines``):
    each is read, and with no routine sources each is the HIGH to-do, as on main."""
    from src.tools.database.ddl_parser import parse_ddl_routines
    from src.tools.routines.migration import listed_by_name_only

    raw = parse_ddl_routines(DDL, "mysql")
    assert raw["procedures"] and raw["triggers"]
    assert all("body_status" not in r for r in raw["procedures"] + raw["triggers"])
    procedures = [
        {"procedure_id": r["routine_name"], "procedure_name": r["routine_name"],
         "schema_name": r.get("schema_name"), "procedure_type": r["routine_type"],
         "definition": r["definition"]}
        for r in raw["procedures"]
    ]  # fmt: skip
    triggers = [
        {"trigger_id": r["trigger_name"], "trigger_name": r["trigger_name"],
         "schema_name": r.get("schema_name"), "table_id": r["table_name"],
         "definition": r["definition"]}
        for r in raw["triggers"]
    ]  # fmt: skip
    assert not any(listed_by_name_only(r) for r in procedures + triggers)
    artifact = _run(_mysql_collector({"procedures": procedures, "triggers": triggers}))
    assert {(r.kind, r.status) for r in artifact.routines} == {
        ("procedure", "needs_attention"),
        ("trigger", "needs_attention"),
    }
    assert all("source_text_not_collected" in r.reasons for r in artifact.routines)


def test_an_assess_collection_with_code_and_no_status_keeps_its_routines() -> None:
    """``body_status`` is optional in the collector contract an ``assess()`` caller
    gives: a routine with code is read; one with neither is a name only."""
    from src.tools.routines.migration import name_only_routine_count

    collector = _mysql_collector({
        "procedures": [
            {"procedure_id": "p", "procedure_name": "p", "procedure_type": "PROCEDURE",
             "definition": "BEGIN END", "sql_mode": ""},
            {"procedure_id": "q", "procedure_name": "q", "procedure_type": "PROCEDURE"},
        ],
    })  # fmt: skip
    artifact = _run(collector)
    assert [(r.kind, r.routine_id) for r in artifact.routines] == [("procedure", "p")]
    assert artifact.routines[0].status == "needs_attention"
    assert name_only_routine_count(collector) == 1


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
    assert logged[0].startswith("1 routine is listed by name only, with no code")
    assert "run the current script" in logged[0] and "a DDL upload" in logged[0]
