# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine-migration phase on its own (#1934): what a table the assignment moves
elsewhere does to a routine, the order of the input, the contract's guards, old jobs,
and the cost at 100K routines. The run through ``run_assessment.py`` is in
``tests/unit/scripts/test_run_assessment_routine_migration.py``."""

from __future__ import annotations

import random
from typing import Any

import pytest
from pydantic import ValidationError

from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.routine_sources import RoutineSource
from src.storage.local_store import LocalArtifactStore
from src.tools.routines.migration import (
    latest_routine_migration,
    read_routine_migration,
    routine_migration_path,
    run_routine_migration_deterministic,
)
from tests.unit.tools.sql._timing import assert_cost_ratio

_DEPENDENCY_RULE = "postgresql-routine-dependency-not-moved"


def _function(
    i: int, *, depends_on: list[dict[str, str]] | None = None, status: str = "collected"
) -> dict[str, Any]:
    return {
        "procedure_id": f"f{i}", "procedure_name": f"f{i}", "schema_name": "public",
        "procedure_type": "FUNCTION", "specific_name": f"f{i}_{16384 + i}", "language": "sql",
        "owner": "app", "security_definer": False, "body_status": status,
        "definition": _create(i) if status == "collected" else None,
        "depends_on": depends_on or [],
    }  # fmt: skip


def _create(i: int) -> str:
    return (
        f"CREATE OR REPLACE FUNCTION public.f{i}(p integer)\n RETURNS integer\n LANGUAGE sql\n"
        "AS $function$SELECT p$function$\n"
    )


def _collector(procedures: list[dict[str, Any]], *, engine: str = "postgresql") -> dict[str, Any]:
    return {
        "metadata": {"source_database": {"engine": engine, "version": "16.2"}},
        "database_schema": {
            "tables": [{"table_id": "orders", "table_name": "orders", "schema_name": "public"}],
            "procedures": procedures,
        },
    }


def _sources(procedures: list[dict[str, Any]]) -> list[RoutineSource]:
    return [
        RoutineSource(
            kind="function", routine_id=p["procedure_name"], schema_name="public",
            specific_name=p["specific_name"], create_statement=p["definition"],
            origin="pg_get_functiondef",
        )  # fmt: skip
        for p in procedures
        if p["definition"]
    ]


def _run(collector, assignment=None, sources=None) -> RoutineMigrationContract:
    artifact = run_routine_migration_deterministic(
        collector, assignment, sources, job_id="j1", database_name="shop", assignment_version=1
    )
    assert artifact is not None
    return artifact


def _orders(engines: list[str], source_of_truth: str | None) -> dict[str, Any]:
    return {
        "table_assignments": [
            {"table_id": "orders", "primary_engine": engines[0], "engines": engines,
             "query_count": 1, "source_of_truth": source_of_truth}
        ]
    }  # fmt: skip


@pytest.mark.parametrize(
    ("assignment", "leaving"),
    [
        (None, None),  # no query touches it
        (_orders(["aurora_postgresql"], "aurora_postgresql"), None),
        (_orders(["aurora_postgresql", "dynamodb"], "dynamodb"), None),  # an Aurora replica
        (_orders(["dynamodb"], "dynamodb"), (["dynamodb"], "dynamodb")),
        (_orders(["opensearch", "dynamodb"], None), (["dynamodb", "opensearch"], None)),
    ],
)
def test_a_table_leaving_aurora_is_recorded_and_never_changes_the_status(assignment, leaving):
    # Step 1 mirrors every table onto Aurora, whatever the end state says.
    procedures = [_function(1, depends_on=[{"kind": "table", "name": "public.orders"}])]
    (row,) = _run(_collector(procedures), assignment, _sources(procedures)).routines
    (dependency,) = row.dependencies
    assert (dependency.name, dependency.found) == ("public.orders", True)
    assert row.status == "rewritten" and row.reasons == []
    if leaving is None:
        assert row.tables_leaving_aurora == []
    else:
        (table,) = row.tables_leaving_aurora
        assert (table.table, table.engines, table.source_of_truth) == ("public.orders", *leaving)


def test_a_table_the_collection_lacks_needs_attention():
    procedures = [_function(1, depends_on=[{"kind": "table", "name": "public.archive"}])]
    (row,) = _run(_collector(procedures), None, _sources(procedures)).routines
    assert row.status == "needs_attention"
    assert row.reasons == [_DEPENDENCY_RULE, "depends_on_table_not_collected"]
    (failed,) = [c for c in row.checks if c.rule_id == _DEPENDENCY_RULE]
    assert failed.outcome == "fail" and failed.findings == ["table public.archive"]


def _calls(i: int, *callees: int, **kw: Any) -> dict[str, Any]:
    deps = [{"kind": "function", "name": f"public.f{c}(integer)"} for c in callees]
    return _function(i, depends_on=deps, **kw)


def test_a_routine_naming_one_that_needs_attention_needs_attention_transitively():
    # f3 -> f2 -> f1 (no text); f4 -> f0 (fine); f5 -> f3.
    procedures = [
        _function(0), _calls(1, status="not_readable"), _calls(2, 1), _calls(3, 2),
        _calls(4, 0), _calls(5, 3),
    ]  # fmt: skip
    rows = {r.name: r for r in _run(_collector(procedures), None, _sources(procedures)).routines}
    assert rows["f1"].reasons == ["source_text_not_collected"] and rows["f1"].blocked_by == []
    # f1 has no text, so no signature in its id.
    blockers = {"f2": "public.f1", "f3": "public.f2(integer)", "f5": "public.f3(integer)"}
    for name, blocker in blockers.items():
        assert rows[name].status == "needs_attention"
        assert rows[name].reasons == ["depends_on_needs_attention"]
        assert [b.routine_id for b in rows[name].blocked_by] == [blocker]
    assert rows["f0"].status == rows["f4"].status == "rewritten"
    assert rows["f4"].blocked_by == []


def test_a_routine_naming_a_cycle_needs_attention_and_names_the_member():
    # f1 <-> f2 is a cycle; f3 -> f1; f4 -> f3.
    procedures = [_calls(1, 2), _calls(2, 1), _calls(3, 1), _calls(4, 3)]
    artifact = _run(_collector(procedures), None, _sources(procedures))
    rows = {r.name: r for r in artifact.routines}
    assert [[m.routine_id for m in c] for c in artifact.cycles] == [
        ["public.f1(integer)", "public.f2(integer)"]
    ]
    assert rows["f1"].reasons == ["dependency_cycle", "depends_on_needs_attention"]
    assert [b.routine_id for b in rows["f1"].blocked_by] == ["public.f2(integer)"]
    assert [b.routine_id for b in rows["f3"].blocked_by] == ["public.f1(integer)"]
    assert [b.routine_id for b in rows["f4"].blocked_by] == ["public.f3(integer)"]
    assert rows["f4"].reasons == ["depends_on_needs_attention"]


def test_the_same_input_in_any_order_gives_the_same_artifact():
    procedures = [
        _function(i, depends_on=[{"kind": "function", "name": f"public.f{(i + 1) % 6}(integer)"}])
        for i in range(6)
    ] + [_function(9, status="not_readable")]
    first = _run(_collector(procedures), None, _sources(procedures)).model_dump(mode="json")
    shuffled = list(procedures)
    random.Random(1934).shuffle(shuffled)
    sources = _sources(procedures)
    random.Random(7).shuffle(sources)
    assert _run(_collector(shuffled), None, sources).model_dump(mode="json") == first
    assert first["cycles"] and len(first["cycles"][0]) == 6


def test_a_source_engine_routines_are_not_carried_from_writes_nothing():
    assert (
        run_routine_migration_deterministic(
            _collector([_function(1)], engine="sqlserver"), None, None,
            job_id="j1", database_name="shop", assignment_version=1,
        )  # fmt: skip
        is None
    )
    assert (
        run_routine_migration_deterministic(
            _collector([]), None, None, job_id="j1", database_name="shop", assignment_version=1
        )
        is None
    )


def test_two_routines_with_no_signature_still_get_distinct_ids():
    # A collection from before #1652: no specific name, no text, one name twice.
    old = [{"procedure_id": "f", "procedure_name": "f", "schema_name": "public",
            "procedure_type": "FUNCTION"}] * 2  # fmt: skip
    rows = _run(_collector(old)).routines
    assert [r.routine_id for r in rows] == ["public.f", "public.f[2]"]
    assert all(r.reasons == ["source_text_not_collected"] for r in rows)


# -- the contract ---------------------------------------------------------------------


def _schema_fields(schema: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for definition in [schema, *schema.get("$defs", {}).values()]:
        names |= set(definition.get("properties", {}))
    return names


def test_no_field_of_the_contract_can_hold_routine_text_or_its_hash():
    fields = _schema_fields(RoutineMigrationContract.model_json_schema())
    for word in ("create", "statement", "body_text", "definition", "sha", "hash", "diff", "sql"):
        assert not [f for f in fields if word in f], word


def test_the_contract_refuses_rows_that_disagree():
    good = _run(_collector([_function(1)]), None, _sources([_function(1)])).model_dump(mode="json")
    RoutineMigrationContract.model_validate(good)
    for change in (
        lambda d: d["counts"].update(as_is=5),
        lambda d: d["routines"][0].update(status="needs_attention"),  # with no reason
        lambda d: d["routines"][0].update(status="as_is", reasons=["dependency_cycle"]),
        lambda d: d["routines"].append(dict(d["routines"][0], deploy_order=2)),  # twice
        lambda d: d["routines"][0].update(deploy_order=2),
        lambda d: d["routines"][0].update(source_text="not_exact"),  # rewritten, no exact text
    ):
        broken = RoutineMigrationContract.model_validate(good).model_dump(mode="json")
        change(broken)
        with pytest.raises(ValidationError):
            RoutineMigrationContract.model_validate(broken)


def test_a_job_saved_without_the_artifact_still_loads(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("shop/j1/assignment/v1/assignment.json", {"query_assignments": []})
    assert read_routine_migration(store, "shop", "j1", 1) is None
    artifact = _run(_collector([_function(1)]), None, _sources([_function(1)]))
    store.write_json(routine_migration_path("shop", "j1", 2), artifact.model_dump(mode="json"))
    assert read_routine_migration(store, "shop", "j1", 1) is None
    assert read_routine_migration(store, "shop", "j1", 2) == artifact


# -- scale ----------------------------------------------------------------------------


def _library(count: int) -> dict[str, Any]:
    procedures = [
        _function(i, status="opted_out",
                  depends_on=[{"kind": "table", "name": "public.orders"}]
                  + ([{"kind": "function", "name": f"public.f{i - 1}(integer)"}] if i else []))
        for i in range(count)
    ]  # fmt: skip
    return _collector(procedures)


def test_a_hundred_thousand_routines_cost_about_ten_times_ten_thousand():
    # Relative (AGENTS.md checklist 8): a same-process reference at a tenth of the size;
    # x25 sits well under the x100 a quadratic phase would take.
    reference, large = _library(10_000), _library(100_000)
    artifact = _run(large)
    assert artifact.total == 100_000 and artifact.cycles == []
    assert [r.routine_id for r in artifact.routines[:2]] == ["public.f0", "public.f1"]
    assert_cost_ratio(lambda: _run(large), lambda: _run(reference), 25, max_rounds=3)


def test_checked_routines_cost_linear_in_their_number():
    small = [_function(i) for i in range(400)]
    large = [_function(i) for i in range(2_000)]
    small_in, large_in = (_collector(small), _sources(small)), (_collector(large), _sources(large))
    assert _run(large_in[0], None, large_in[1]).counts["rewritten"] == 2_000
    assert_cost_ratio(
        lambda: _run(large_in[0], None, large_in[1]),
        lambda: _run(small_in[0], None, small_in[1]),
        12,
        max_rounds=4,
    )


def test_the_newest_version_at_or_below_the_one_asked_for_is_read(tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    assert latest_routine_migration(store, "shop", "j1", 3) is None
    for version in (1, 3):
        artifact = run_routine_migration_deterministic(
            _collector([_function(1)]), None, _sources([_function(1)]),
            job_id="j1", database_name="shop", assignment_version=version,
        )  # fmt: skip
        assert artifact is not None
        store.write_json(
            routine_migration_path("shop", "j1", version), artifact.model_dump(mode="json")
        )
    found = latest_routine_migration(store, "shop", "j1", 2)
    assert found is not None and found[0] == 1 and found[1].assignment_version == 1
    for asked, used in ((1, 1), (3, 3), (5, 3)):
        found = latest_routine_migration(store, "shop", "j1", asked)
        assert found is not None and found[0] == used


def test_peak_memory_grows_linearly_with_the_routines():
    # Relative, like the timing check: the peak at 50K against a 5K reference (x10 is
    # linear, x100 quadratic), and against the artifact's own size. Built once, a row
    # peaks at about 11 times its JSON; building each row three times (#1934 review)
    # peaked at about 17 times, so x14 catches a return to it.
    import gc
    import tracemalloc

    def peak(collector: dict[str, Any]) -> tuple[int, int]:
        gc.collect()
        tracemalloc.start()
        try:
            artifact = _run(collector)
            measured = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        return measured, len(artifact.model_dump_json())

    reference, _ = peak(_library(5_000))
    measured, json_bytes = peak(_library(50_000))
    assert measured < 15 * reference, (measured, reference)
    assert measured < 14 * json_bytes, (measured, json_bytes)
