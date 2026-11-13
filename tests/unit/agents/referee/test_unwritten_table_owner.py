# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A table nothing writes stays with the relational step (#2166).

ADR-031 Amendment 9 and ADR-033 Amendment 1: a table's owner is its one writer
(``write_groups.sources_of_truth``); a table no statement writes, or whose writes have no
single engine, stays with the relational step's engine, and the engines that read it read
a copy. With no relational step the source database keeps it. Never the engine that reads
it most (its ``primary_engine``, a summary label). The waves, the wave plan's owners
(``wave_table_owners``) and the owner lines take it from one helper,
``table_writer.table_writer``; a cache or search engine never owns a table.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.migration_wave_tables import _durable_owner
from src.agents.referee.migration_waves import build_migration_waves, wave_table_owners
from src.agents.referee.table_assignments import derive_table_assignments
from src.contracts.assignment_models import QueryAssignment
from src.shared.engine_names import SOURCE_DATABASE
from src.shared.report_facts import table_owner_line, table_owner_reason
from src.tools.data_movement.plan import _owner_of

T = "db.t"
OTHER = "db.other"


def _qa(qid: str, engine: str, tables: list[str]) -> QueryAssignment:
    return QueryAssignment(
        query_id=qid,
        assigned_engine=engine,
        confidence=80,
        source_tables=tables,
        assignment_reason="test",
    )


def _plan(
    qas: list[QueryAssignment],
    write_groups: dict[str, list[str]],
    engine_scope: list[str] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]] | None]:
    """The wave plan's owners and the waves, through the real resolver and wave builder."""
    tas, _ = derive_table_assignments(
        qas, retained_engine="aurora_mysql", write_groups=write_groups
    )
    counts: dict[str, int] = {}
    for q in qas:
        counts[q.assigned_engine] = counts.get(q.assigned_engine, 0) + 1
    ranking = [
        {"target": e, "assigned_queries": n, "workload_percent": 100.0 * n / len(qas)}
        for e, n in sorted(counts.items())
    ]
    args: dict[str, Any] = {
        "ranking": ranking,
        "table_assignments": [t.model_dump() for t in tas],
        "query_assignments": [q.model_dump() for q in qas],
        "source_engine": "mysql",
        "known_tables": {T, OTHER},
        "engine_scope": engine_scope,
    }
    waves = build_migration_waves(**args, co_dependency_groups=[], cache_overlay=None)
    return wave_table_owners(**args), waves


def _listed(waves: list[dict[str, Any]] | None, engine: str) -> dict[str, str]:
    """``{table: owner}`` from the owner lines of every wave ``engine`` is in."""
    return {
        str(o["table"]): str(o["owner"])
        for w in waves or []
        if engine in (w.get("engines") or [])
        for o in w.get("table_owners") or []
    }


def test_a_table_only_dynamodb_reads_stays_with_the_relational_step() -> None:
    owners, waves = _plan(
        [_qa("r1", "dynamodb", [T]), _qa("r2", "dynamodb", [T]), _qa("w1", "dynamodb", [OTHER])],
        {OTHER: ["w1"]},
    )
    assert owners == {T: "aurora_mysql", OTHER: "dynamodb"}
    # DynamoDB's wave reads a copy of it, kept current from Aurora MySQL.
    assert _listed(waves, "dynamodb") == {T: "aurora_mysql"}


def test_a_table_several_engines_read_stays_with_the_relational_step() -> None:
    owners, waves = _plan(
        [
            _qa("r1", "dynamodb", [T]),
            _qa("r2", "dynamodb", [T]),
            _qa("r3", "documentdb", [T]),
            _qa("w1", "dynamodb", [OTHER]),
        ],
        {OTHER: ["w1"]},
    )
    assert owners[T] == "aurora_mysql"
    assert _listed(waves, "dynamodb")[T] == "aurora_mysql"
    assert _listed(waves, "documentdb")[T] == "aurora_mysql"


def test_a_read_tie_between_two_engines_goes_to_neither() -> None:
    """One read on DynamoDB and one on DocumentDB: no tie-break between readers, since
    no reader owns a table nothing writes."""
    owners, _ = _plan(
        [_qa("r1", "dynamodb", [T]), _qa("r2", "documentdb", [T])],
        {},
    )
    assert owners == {T: "aurora_mysql"}


def test_split_writes_stay_with_the_relational_step() -> None:
    """Writes on DynamoDB and DocumentDB (a version saved before one writer per table)
    have no single writer, so the relational step keeps the table."""
    owners, _ = _plan(
        [
            _qa("w1", "dynamodb", [T]),
            _qa("w2", "documentdb", [T]),
            *(_qa(f"r{i}", "dynamodb", [T]) for i in range(3)),
        ],
        {T: ["w1", "w2"]},
    )
    assert owners == {T: "aurora_mysql"}


def test_with_no_relational_step_the_source_database_keeps_it() -> None:
    """An engine scope with no Aurora engine has no relational step (#1329)."""
    owners, waves = _plan(
        [_qa("r1", "dynamodb", [T]), _qa("w1", "dynamodb", [OTHER])],
        {OTHER: ["w1"]},
        engine_scope=["dynamodb"],
    )
    assert owners == {T: SOURCE_DATABASE, OTHER: "dynamodb"}
    assert _listed(waves, "dynamodb") == {T: SOURCE_DATABASE}


@pytest.mark.passes_on_main
def test_a_table_one_engine_writes_is_owned_by_that_writer() -> None:
    """Unchanged: written on DynamoDB, owned there although Aurora MySQL reads it more."""
    owners, waves = _plan(
        [_qa("w1", "dynamodb", [T]), *(_qa(f"r{i}", "aurora_mysql", [T]) for i in range(3))],
        {T: ["w1"]},
    )
    assert owners == {T: "dynamodb"}
    assert T not in _listed(waves, "dynamodb")


@pytest.mark.passes_on_main
def test_an_empty_workload_has_no_owners_and_no_waves() -> None:
    assert (
        wave_table_owners(
            ranking=[], table_assignments=[], query_assignments=[], source_engine="mysql"
        )
        == {}
    )
    assert (
        build_migration_waves(
            ranking=[],
            table_assignments=[],
            query_assignments=[],
            co_dependency_groups=[],
            cache_overlay=None,
            source_engine="mysql",
        )
        is None
    )


def test_a_writer_that_cannot_own_falls_to_the_relational_step() -> None:
    """A hand-built or legacy row naming a search engine its writer: never the owner, and
    never the table's main reader either."""
    row = {
        "table_id": T,
        "source_of_truth": "opensearch",
        "primary_engine": "dynamodb",
        "engines": ["dynamodb", "opensearch"],
    }
    assert _durable_owner(row, "aurora_mysql") == "aurora_mysql"
    assert _durable_owner({**row, "source_of_truth": None}, None) == SOURCE_DATABASE


@pytest.mark.parametrize("non_owner", ["elasticache", "opensearch"])
def test_a_relational_step_that_cannot_own_is_skipped(non_owner: str) -> None:
    """A retained engine that is a cache or search engine never owns a table either:
    with no other relational step the source database keeps it."""
    assert _durable_owner({"table_id": T}, non_owner) == SOURCE_DATABASE
    written = {"table_id": T, "source_of_truth": "dynamodb"}
    assert _durable_owner(written, non_owner) == "dynamodb"


def test_the_owner_line_with_no_wave_owner_follows_the_same_rule() -> None:
    """``assess()`` before synthesis states each line with no wave plan: a table nothing
    writes is the relational step's, whichever engine reads it most."""
    line = table_owner_line({}, {"dynamodb": 3, "aurora_mysql": 1}, [], (), None, "aurora_mysql")
    assert (line["owner"], line["owner_basis"]) == ("aurora_mysql", "retained")
    assert line["also_read_by"] == ["dynamodb"]
    reason = table_owner_reason(line, {}, {"dynamodb": 3, "aurora_mysql": 1}, "aurora_mysql")
    assert reason == (
        "No statement writes it, so it stays on Aurora MySQL, the plan's relational engine; "
        "DynamoDB reads a copy kept current from Aurora MySQL via CDC."
    )
    none = table_owner_line({}, {"dynamodb": 2}, [], (), None, None)
    assert (none["owner"], none["owner_basis"]) == (SOURCE_DATABASE, "retained")
    assert table_owner_reason(none, {}, {"dynamodb": 2}, None) == (
        "No statement writes it and no relational engine is in scope, so it stays on the "
        "source database; DynamoDB reads a copy kept current from the source database via CDC."
    )


def test_the_owner_line_states_the_wave_owner_of_an_unwritten_table() -> None:
    line = table_owner_line({}, {"dynamodb": 3}, [], (), "aurora_mysql", "aurora_mysql")
    assert (line["owner"], line["owner_basis"]) == ("aurora_mysql", "retained")
    split = table_owner_line(
        {"dynamodb": 2, "documentdb": 1},
        {"dynamodb": 5, "documentdb": 1},
        [],
        (),
        "aurora_mysql",
        "aurora_mysql",
    )
    assert (split["owner"], split["owner_basis"]) == ("aurora_mysql", "split_writes")
    assert table_owner_reason(
        split, {"dynamodb": 2, "documentdb": 1}, {"dynamodb": 5, "documentdb": 1}, "aurora_mysql"
    ) == (
        "Its writes are on DynamoDB and DocumentDB, so no single engine owns it and it stays "
        "on Aurora MySQL, the plan's relational engine; DynamoDB and DocumentDB read a copy "
        "kept current from Aurora MySQL via CDC."
    )


@pytest.mark.passes_on_main
def test_an_older_reports_reader_owner_keeps_its_basis() -> None:
    """A report written before #2166 names a reader as an unwritten table's owner: its
    line still says why (most queries), never that the relational step keeps it."""
    line = table_owner_line(
        {}, {"dynamodb": 3, "aurora_mysql": 1}, [], (), "dynamodb", "aurora_mysql"
    )
    assert (line["owner"], line["owner_basis"]) == ("dynamodb", "most_queries")


@pytest.mark.passes_on_main
def test_a_report_written_before_212_keeps_the_rule_its_waves_used() -> None:
    """A report filled at read time keeps its own waves' rule (the main reader), so its
    map and its stored waves still agree; only a report written now follows #2166."""
    line = table_owner_line(
        {}, {"dynamodb": 3, "aurora_mysql": 1}, [], (), None, "aurora_mysql", design_fallback=True
    )
    assert (line["owner"], line["owner_basis"]) == ("dynamodb", "most_queries")


def test_a_data_movement_step_reads_a_source_database_owner_as_the_source() -> None:
    """The wave plan's ``source_database`` owner is the plan's source engine."""
    wave = {"table_owners": [{"table": T, "owner": SOURCE_DATABASE, "sync": "CDC"}]}
    assert _owner_of(T, wave, {}, ["dynamodb"], "mysql") == "mysql"
