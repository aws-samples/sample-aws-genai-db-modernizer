# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Collected views, view sources and step contracts for step 1's view tests (#2082)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.collector.routine_sources import write_routine_sources
from src.contracts.routine_sources import RoutineSession, RoutineSource
from src.contracts.transformation_contract import StepContract
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.step1_views import build_step_views
from src.tools.data_movement.view_scripts import write_view_scripts
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract

SHA = "b" * 64
#: A literal only the routine sources and the views deploy script may ever hold.
SENTINEL = "v1ew-s3cr3t-2082"
SOURCE = {"aurora_postgresql": "postgresql", "aurora_mysql": "mysql"}


def view(
    name: str,
    *,
    schema: str | None = None,
    engine: str = "aurora_postgresql",
    tables: list[str] | None = None,
    views: list[str] | None = None,
    columns: list[tuple[str, str | None]] | None = None,
    status: str | None = "collected",
    lineage: str = "resolved",
    definition: str = "SELECT 1",
    **extra: Any,
) -> dict[str, Any]:
    """A collector output view row, as the collector writes it after #2082."""
    own = schema or ("shop" if engine == "aurora_mysql" else "public")
    return {
        "view_id": name, "view_name": name, "schema_name": own, "definition": definition,
        "referenced_tables": tables if tables is not None else [f"{own}.orders"],
        "referenced_views": views or [], "lineage_status": lineage,
        "columns": [{"name": n, "data_type": t} for n, t in (columns or [("id", "bigint")])],
        "body_status": status, **extra,
    }  # fmt: skip


def schema(views: list[dict[str, Any]], procedures: list[dict[str, Any]] | None = None):
    return {"views": views, "procedures": procedures or []}


def contract_with_views(
    views: list[dict[str, Any]],
    engine: str = "aurora_postgresql",
    *,
    procedures: list[dict[str, Any]] | None = None,
    contract: StepContract | None = None,
) -> StepContract:
    base = contract or make_contract(engine)
    section = build_step_views(schema(views, procedures), base, source_engine=SOURCE[engine])
    return base.model_copy(update={"views_section": section})


def pg_view_source(name: str, text: str, *, schema: str = "public",
                   search_path: list[str] | None = None) -> RoutineSource:  # fmt: skip
    return RoutineSource(kind="view", routine_id=name, schema_name=schema, specific_name=name,
                         create_statement=text, origin="pg_get_viewdef",
                         session=RoutineSession(search_path=search_path or ["public"]))  # fmt: skip


def mysql_view_source(name: str, text: str, *, schema: str = "shop",
                      collation: str | None = "utf8mb4_0900_ai_ci") -> RoutineSource:  # fmt: skip
    return RoutineSource(kind="view", routine_id=name, schema_name=schema, specific_name=name,
                         create_statement=text, origin="information_schema",
                         session=RoutineSession(character_set_client="utf8mb4",
                                                collation_connection=collation))  # fmt: skip


def job(tmp_path: Path, sources: list[RoutineSource] | None, engine: str = "postgresql"):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    if sources is not None:
        collector = {"metadata": {"source_database": {"engine": engine, "version": "16.2"}}}
        write_routine_sources(store, "shop", "job1", collector, sources)
    return store


def write(tmp_path: Path, store, contract: StepContract, **kw):
    return write_view_scripts(store, "shop", "job1", contract, SHA, tmp_path / "step", **kw)
