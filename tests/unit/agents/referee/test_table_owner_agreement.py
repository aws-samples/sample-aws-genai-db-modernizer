"""The Migration map's owner is the wave plan's owner (#212 review).

ADR-032 (#1169): "kept in sync" has one definition, the wave plan's, and ADR-031
Amendment 9: a table's writes have one source of truth, or none when they are split.
The map's Owner column (``table_owner_fields``) takes each table's owner from the wave
plan (``migration_waves.wave_table_owners``, the same ``_durable_owner`` call the waves
make), so the map row, the wave plan's owner lines and kept-in-sync count, and Sources
of truth cannot name different engines. These are the two cases the review reproduced
where a second rule disagreed, run through the real resolver, wave builder and facts
layer.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.agents.referee.migration_waves import build_migration_waves, wave_table_owners
from src.agents.referee.table_assignments import derive_table_assignments
from src.agents.referee.write_groups import table_sources
from src.contracts.assignment_models import QueryAssignment
from src.report.facts import report_facts
from src.report.table_owners import owner_cell
from src.shared.report_facts import (
    assignment_table_usage,
    compute_report_facts,
    table_owner_fields,
    table_source_facts,
    wave_retained_engine,
)

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


def _job(qas: list[QueryAssignment], write_groups: dict[str, list[str]]) -> dict[str, Any]:
    """The report a synthesis of this assignment writes, with its waves and owners."""
    tas, _ = derive_table_assignments(
        qas, retained_engine="aurora_mysql", write_groups=write_groups
    )
    rows = [t.model_dump() for t in tas]
    assignment = {
        "query_assignments": [q.model_dump() for q in qas],
        "write_groups": write_groups,
    }
    counts: dict[str, int] = {}
    for q in qas:
        counts[q.assigned_engine] = counts.get(q.assigned_engine, 0) + 1
    ranking = [
        {"target": e, "assigned_queries": n, "workload_percent": 100.0 * n / len(qas)}
        for e, n in sorted(counts.items())
    ]
    args: dict[str, Any] = {
        "ranking": ranking,
        "table_assignments": rows,
        "query_assignments": assignment["query_assignments"],
        "source_engine": "mysql",
        "known_tables": {T, OTHER},
    }
    waves = build_migration_waves(**args, co_dependency_groups=[], cache_overlay=None) or []
    owners = wave_table_owners(**args)
    mappings = [{"source_table": T, "recommended_database": "dynamodb", "confidence_score": 50}]
    report: dict[str, Any] = {
        "ranking": ranking,
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {"dynamodb": {"status": "completed"}},
        "migration_waves": waves,
        "table_sources": table_sources(assignment),
        # One query pattern per query, as synthesis groups them (#1146): what the facts
        # reader's backfill reads an older report's readers from.
        "query_pattern_groups": [
            {
                "pattern_id": f"PAT-{q.query_id}",
                "tables": [t.rsplit(".", 1)[-1] for t in q.source_tables],
                "members": [{"query_id": q.query_id, "engine": q.assigned_engine}],
            }
            for q in qas
        ],
        "table_mappings": table_owner_fields(
            mappings, assignment_table_usage(assignment), owners, wave_retained_engine(waves)
        ),
    }
    report["facts"] = compute_report_facts(report)
    return {"report": report, "owners": owners, "rows": rows, "waves": waves}


def _dynamodb_wave(waves: list[dict[str, Any]]) -> dict[str, Any]:
    return next(w for w in waves if w["engines"] == ["dynamodb"])


def _line(report: dict[str, Any]) -> dict[str, Any]:
    (line,) = report["facts"]["table_owner_lines"]["table_lines"]
    return dict(line)


def _backfilled(report: dict[str, Any]) -> dict[str, Any]:
    """The same report as one written before #212: no owner fields, no facts block."""
    old = copy.deepcopy(report)
    for m in old["table_mappings"]:
        for key in ("owner", "owner_basis", "also_read_by", "cached_by"):
            m.pop(key)
    old["facts"].pop("table_owner_lines")
    (line,) = report_facts(old)["table_owner_lines"]["table_lines"]
    return dict(line)


def test_one_read_on_each_engine_and_no_write() -> None:
    """A 1-1 read tie on a table no query writes, recommended DynamoDB: the wave plan's
    rule gives it to the retained engine, and the map, the DynamoDB wave's kept-in-sync
    line and Sources of truth all agree."""
    job = _job(
        [
            _qa("r1", "dynamodb", [T]),
            _qa("r2", "aurora_mysql", [T]),
            _qa("w1", "dynamodb", [OTHER]),
        ],
        {OTHER: ["w1"]},
    )
    report = job["report"]
    line = _line(report)
    assert job["owners"][T] == "aurora_mysql"
    assert (line["owner"], line["owner_basis"]) == ("aurora_mysql", "most_queries")
    assert line["also_read_by"] == ["dynamodb"]
    # The wave plan: DynamoDB's wave keeps it in sync from Aurora MySQL.
    wave = _dynamodb_wave(job["waves"])
    assert {"table": T, "owner": "aurora_mysql", "sync": "CDC"} in wave["table_owners"]
    assert report["facts"]["table_basis"]["mapped_tables_by_move"] == {"kept_in_sync": 1}
    # Sources of truth: no query writes it, so it is not listed.
    assert T not in {r["table"] for r in report["table_sources"]}
    assert _backfilled(report) == line


def test_writes_split_two_to_one() -> None:
    """Writes 2 on DynamoDB and 1 on Aurora MySQL, plus 5 Aurora MySQL reads: Sources of
    truth has no source of truth for it, the map says "no single writer", and the owner
    it names is the wave plan's own (``_durable_owner``: most queries)."""
    job = _job(
        [
            _qa("w1", "dynamodb", [T]),
            _qa("w2", "dynamodb", [T]),
            _qa("w3", "aurora_mysql", [T]),
            *(_qa(f"r{i}", "aurora_mysql", [T]) for i in range(5)),
        ],
        {T: ["w1", "w2", "w3"]},
    )
    report = job["report"]
    line = _line(report)
    sources = table_source_facts(report)
    assert sources is not None and sources["split_write_table_count"] == 1
    assert (report["table_sources"][0]["source_of_truth"], line["owner_basis"]) == (
        None,
        "split_writes",
    )
    assert line["owner"] == job["owners"][T] == "aurora_mysql"
    assert owner_cell(line) == "Aurora MySQL (no single writer)"
    # The DynamoDB wave writes the table, so it never lists it as kept in sync (#992),
    # and the map heading's split is the wave plan's own.
    wave = _dynamodb_wave(job["waves"])
    assert T not in {o["table"] for o in wave.get("table_owners") or []}
    assert report["facts"]["table_basis"]["mapped_tables_by_move"] == {"migrate": 1}
    assert _backfilled(report) == line


@pytest.mark.xfail(
    strict=True,
    reason="#1329: a table whose only writes are unservable must not get a reader as owner",
)
def test_a_table_whose_only_writes_are_unservable_has_no_owner() -> None:
    usage = {T: {"writes": {}, "queries": {"aurora_mysql": 3}, "cached_by": []}}
    # Today the unplaced write is dropped from ``writes``, so a reader becomes the
    # owner; once #1329 records unservable writes, the line must name no owner.
    (row,) = table_owner_fields(
        [{"source_table": T, "recommended_database": "aurora_mysql", "confidence_score": 1}],
        usage,
        retained_engine="aurora_mysql",
    )
    assert row["owner"] is None
