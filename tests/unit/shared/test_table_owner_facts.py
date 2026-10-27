"""Each source table's owner, the engines that also read it and its cache (#212).

ADR-031 Amendment 9: a table has one writer, the engine that takes its writes; that
engine owns the table. Every other engine with an in-scope query on it reads a copy
kept in sync from the owner via CDC, and the cache layer fronts its hot reads. A
table no query writes is owned by the engine serving most of its queries. A
cache-only engine owns no table, whatever the schema design maps to it.

The figures are computed once, in ``src.shared.report_facts`` (ADR-032), from the
final assignment and its write groups; a report written before them gets the same
lines from its ``table_sources`` and ``query_pattern_groups`` through the facts
reader.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.facts import report_facts
from src.shared.report_facts import (
    OWNER_BY_MOST_QUERIES,
    OWNER_BY_RETAINED,
    OWNER_BY_SPLIT_WRITES,
    OWNER_BY_WRITES,
    assignment_table_usage,
    compute_report_facts,
    table_owner_fields,
    table_owner_line,
)

REPO = Path(__file__).resolve().parents[3]
SAMPLE_RUN = REPO / "tests/unit/report/fixtures/wordpress_sample_run_0_3_report.json"


# --------------------------------------------------------------------------
# The rule, one table at a time
# --------------------------------------------------------------------------


def test_the_one_writer_owns_the_table_and_every_other_engine_also_reads_it() -> None:
    line = table_owner_line(
        writes={"dynamodb": 4},
        queries={"dynamodb": 4, "aurora_mysql": 10},
        cached_by=["elasticache"],
        design_engines=["aurora_mysql", "dynamodb"],
    )
    assert line == {
        "owner": "dynamodb",
        "owner_basis": OWNER_BY_WRITES,
        "also_read_by": ["aurora_mysql"],
        "cached_by": "elasticache",
    }


def test_a_table_no_query_writes_is_owned_by_the_engine_serving_most_of_its_queries() -> None:
    line = table_owner_line(
        writes={}, queries={"dynamodb": 1, "aurora_mysql": 3}, cached_by=[], design_engines=[]
    )
    assert line["owner"] == "aurora_mysql"
    assert line["owner_basis"] == OWNER_BY_MOST_QUERIES
    assert line["also_read_by"] == ["dynamodb"]
    assert line["cached_by"] is None


def test_a_cache_only_engine_never_owns_a_table() -> None:
    # The cache engine's design maps the table first (the recommended engine was
    # ElastiCache on the wordpress run in #212), and it fronts the reads.
    line = table_owner_line(
        writes={},
        queries={"aurora_mysql": 2, "elasticache": 9},
        cached_by=[],
        design_engines=["elasticache", "aurora_mysql"],
    )
    assert line["owner"] == "aurora_mysql"
    assert line["also_read_by"] == []
    assert line["cached_by"] == "elasticache"
    # Mapped by the cache design alone, with no query on it and no retained engine.
    alone = table_owner_line(writes={}, queries={}, cached_by=[], design_engines=["elasticache"])
    assert alone["owner"] is None and alone["owner_basis"] is None
    assert alone["cached_by"] == "elasticache"


def test_the_wave_plans_owner_is_the_owner() -> None:
    """The caller passes the wave plan's owner (``wave_table_owners``); the line keeps
    it, so the map and the wave plan name one engine."""
    line = table_owner_line(
        writes={},
        queries={"dynamodb": 3, "aurora_mysql": 1},
        cached_by=[],
        design_engines=["dynamodb"],
        owner="aurora_mysql",
    )
    assert (line["owner"], line["owner_basis"]) == ("aurora_mysql", OWNER_BY_MOST_QUERIES)
    assert line["also_read_by"] == ["dynamodb"]


def test_a_tie_follows_the_resolvers_rule_never_the_design_order() -> None:
    """With no wave owner (an older report) the resolver's own rule applies
    (``primary_owner_engine``): a tie goes to wave 1's retained engine, whatever the
    design order says."""
    tied = {"dynamodb": 1, "aurora_mysql": 1}
    for design in (["dynamodb", "aurora_mysql"], ["aurora_mysql", "dynamodb"], []):
        line = table_owner_line(
            writes={},
            queries=tied,
            cached_by=[],
            design_engines=design,
            retained_engine="aurora_mysql",
        )
        assert line["owner"] == "aurora_mysql", design
    other = table_owner_line(
        writes={}, queries=tied, cached_by=[], design_engines=[], retained_engine="dynamodb"
    )
    assert other["owner"] == "dynamodb"


def test_writes_on_two_engines_have_no_single_writer() -> None:
    """A split keeps the owner the resolver's rule names (most queries), marked
    ``split_writes``, as Sources of truth lists it with no source of truth."""
    line = table_owner_line(
        writes={"dynamodb": 2, "aurora_mysql": 1},
        queries={"dynamodb": 2, "aurora_mysql": 6},
        cached_by=[],
        design_engines=["dynamodb"],
        retained_engine="aurora_mysql",
    )
    assert (line["owner"], line["owner_basis"]) == ("aurora_mysql", OWNER_BY_SPLIT_WRITES)
    assert line["also_read_by"] == ["dynamodb"]


def test_a_table_no_owner_engine_queries_is_owned_by_the_retained_engine() -> None:
    line = table_owner_line(
        writes={},
        queries={"elasticache": 2},
        cached_by=[],
        design_engines=["elasticache", "dynamodb"],
        retained_engine="aurora_mysql",
    )
    assert (line["owner"], line["owner_basis"]) == ("aurora_mysql", OWNER_BY_RETAINED)
    assert line["cached_by"] == "elasticache"


def test_a_search_engine_never_owns_a_table() -> None:
    line = table_owner_line(
        writes={}, queries={"opensearch": 4, "dynamodb": 1}, cached_by=[], design_engines=[]
    )
    assert line["owner"] == "dynamodb"
    assert line["also_read_by"] == ["opensearch"]


def test_also_read_by_lists_most_queries_first_then_by_id() -> None:
    line = table_owner_line(
        writes={"dynamodb": 1},
        queries={"dynamodb": 1, "opensearch": 1, "aurora_mysql": 4, "documentdb": 1},
        cached_by=[],
        design_engines=[],
    )
    assert line["also_read_by"] == ["aurora_mysql", "documentdb", "opensearch"]


# --------------------------------------------------------------------------
# From the assignment
# --------------------------------------------------------------------------


def _qa(qid: str, engine: str, tables: list[str], **kw: Any) -> dict[str, Any]:
    return {"query_id": qid, "assigned_engine": engine, "source_tables": tables, **kw}


def test_the_assignment_gives_writes_queries_and_cached_reads_per_table() -> None:
    assignment = {
        "query_assignments": [
            _qa("w1", "dynamodb", ["db.t"]),
            _qa("r1", "aurora_mysql", ["db.t", "db.u"], cache_engine="elasticache"),
            _qa("r2", "aurora_mysql", ["db.u"], cache_engine="elasticache", cache_dropped=True),
            _qa("r3", "documentdb", ["db.u"], in_scope=False),
        ],
        "write_groups": {"db.t": ["w1"]},
    }
    usage = assignment_table_usage(assignment)
    assert usage["db.t"] == {
        "writes": {"dynamodb": 1},
        "queries": {"dynamodb": 1, "aurora_mysql": 1},
        "cached_by": ["elasticache"],
    }
    # The dropped cache and the out-of-scope query do not count.
    assert usage["db.u"] == {
        "writes": {},
        "queries": {"aurora_mysql": 2},
        "cached_by": ["elasticache"],
    }


def test_every_mapping_row_gets_its_owner_line() -> None:
    mappings = [
        {
            "source_table": "db.t",
            "recommended_database": "elasticache",
            "target_table": "t:{id}",
            "confidence_score": 90,
            "alternatives": [
                {"database": "aurora_mysql", "target_table": "t", "confidence_score": 70}
            ],
        },
        {"source_table": "db.v", "recommended_database": "dynamodb", "confidence_score": 50},
    ]
    usage = {
        "db.t": {"writes": {"aurora_mysql": 1}, "queries": {"aurora_mysql": 3}, "cached_by": []}
    }
    rows = table_owner_fields(mappings, usage)
    assert [(r["owner"], r["owner_basis"], r["cached_by"]) for r in rows] == [
        ("aurora_mysql", OWNER_BY_WRITES, "elasticache"),
        (None, None, None),
    ]
    assert mappings[0].get("owner") is None, "the input rows are not changed"


# --------------------------------------------------------------------------
# The facts block, and an old report through the facts reader
# --------------------------------------------------------------------------


def _report(mappings: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ranking": [
            {"target": "dynamodb", "confidence_score": 70, "workload_percent": 60.0},
            {"target": "aurora_mysql", "confidence_score": 70, "workload_percent": 40.0},
            {
                "target": "elasticache",
                "confidence_score": 70,
                "role": "cache_layer",
                "cache_overlay_queries": 3,
            },
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 2}]},
        "schema_designs": {"dynamodb": {"status": "completed"}},
        "table_mappings": mappings,
        **extra,
    }
    return report


def test_the_facts_block_counts_the_owners_the_readers_and_the_cached_tables() -> None:
    mappings = table_owner_fields(
        [
            {"source_table": "db.a", "recommended_database": "dynamodb", "confidence_score": 1},
            {"source_table": "db.b", "recommended_database": "elasticache", "confidence_score": 1},
            {"source_table": "db.c", "recommended_database": "elasticache", "confidence_score": 1},
        ],
        {
            "db.a": {
                "writes": {"dynamodb": 1},
                "queries": {"dynamodb": 1, "aurora_mysql": 2},
                "cached_by": ["elasticache"],
            },
            "db.b": {"writes": {}, "queries": {"aurora_mysql": 1}, "cached_by": []},
        },
    )
    facts = compute_report_facts(_report(mappings))["table_owner_lines"]
    assert facts["table_count"] == 3
    assert facts["owned_table_counts"] == {"aurora_mysql": 1, "dynamodb": 1}
    assert facts["no_owner_table_count"] == 1
    assert facts["also_read_table_count"] == 1
    # db.a by a cached read of it, db.b and db.c by the cache design that keys them.
    assert facts["cached_table_count"] == 3
    assert [r["table"] for r in facts["table_lines"]] == ["db.a", "db.b", "db.c"]
    # The cache engine is never an owner, in the rows or the counts.
    assert all(r["owner"] != "elasticache" for r in facts["table_lines"])


def test_no_table_mappings_gives_no_facts_block() -> None:
    assert compute_report_facts(_report([]))["table_owner_lines"] is None


def _sample_run() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(SAMPLE_RUN.read_text())
    return report


def _line(facts: dict[str, Any], table: str) -> dict[str, Any]:
    return next(r for r in facts["table_owner_lines"]["table_lines"] if r["table"] == table)


def test_an_old_report_gets_its_owner_lines_from_the_facts_reader() -> None:
    """The recorded wordpress run (contract 1.50) has no owner fields: the reader
    fills them in from its table_sources and query_pattern_groups."""
    report = _sample_run()
    assert "table_owner_lines" not in report["facts"]
    assert all("owner" not in m for m in report["table_mappings"])
    facts = report_facts(report)
    # #212: the map's recommended engine was Aurora MySQL, but DynamoDB takes the
    # writes; Aurora MySQL reads a copy kept in sync from it.
    postmeta = _line(facts, "wordpress.wp_postmeta")
    assert (postmeta["owner"], postmeta["owner_basis"]) == ("dynamodb", OWNER_BY_WRITES)
    assert postmeta["also_read_by"] == ["aurora_mysql"]
    assert postmeta["cached_by"] == "elasticache"
    # Recommended engine ElastiCache: the table still has a system-of-record owner.
    groups = _line(facts, "wordpress.wp_actionscheduler_groups")
    assert groups["owner"] not in (None, "elasticache")
    assert groups["cached_by"] == "elasticache"
    assert all(r["owner"] != "elasticache" for r in facts["table_owner_lines"]["table_lines"])
    assert facts["table_owner_lines"]["table_count"] == len(report["table_mappings"])


def test_an_old_reports_migration_map_split_has_no_table_without_an_owner() -> None:
    """The "N tables migrate" against "Migration map (N tables)" mismatch (#212): a
    table the map put on the cache layer is counted by its owner instead."""
    report = _sample_run()
    stored = report["facts"]["table_basis"]["mapped_tables_by_move"]
    assert stored.get("cache_layer"), "the recorded run has a table mapped to the cache"
    split = report_facts(report)["table_basis"]["mapped_tables_by_move"]
    assert "cache_layer" not in split
    assert sum(split.values()) == len(report["table_mappings"])
    assert split["migrate"] == stored["migrate"]


def test_an_old_report_still_validates_and_keeps_its_stored_figures() -> None:
    report = _sample_run()
    before = copy.deepcopy(report["facts"])
    facts = report_facts(report)
    SynthesisOutputContract.model_validate({**report, "facts": facts})
    for key in ("tables_migrate", "engines", "table_sources", "source_table_count"):
        assert facts[key] == before[key], key


@pytest.mark.parametrize("missing", ["table_sources", "query_pattern_groups"])
def test_an_old_report_with_less_to_go_on_still_gets_an_owner_for_every_row(missing: str) -> None:
    report = _sample_run()
    report.pop(missing)
    rows = report_facts(report)["table_owner_lines"]["table_lines"]
    assert len(rows) == len(report["table_mappings"])
    assert all(r["owner"] and r["owner"] != "elasticache" for r in rows)
