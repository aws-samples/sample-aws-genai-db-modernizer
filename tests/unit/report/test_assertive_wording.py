"""The renderers' own fixed wording is concise and agrees with the plan (#951).

The rubric judge, on a wordpress model run, cited: 64-character query hashes in
the engineering report's Close calls table, a "Group mean 0" column that says
nothing, a Migration map whose "Target engine" read as the owner while Sources
of truth named another engine, a wave headline "18 served, 15 move" no other
deliverable explained, "highest fit for X; highest fit for X" reasons in
"Queries on Aurora by table", and a "Scored higher: DocumentDB scored higher"
line in the per-query appendix. Each renderer only formats the facts it reads.
"""

from __future__ import annotations

import re

from src.report.close_calls import close_calls_md
from src.report.engineering_report import _aurora_queries_by_table_md, render_engineering_report_md
from src.report.query_traces import _trace_md
from src.report.roadmap import _roadmap_md, _wave_headline_stats

_HASH = re.compile(r"\b[0-9a-f]{64}\b")
_IDS = [f"{i:x}" * 64 for i in range(1, 8)]
_IDS = [i[:64] for i in _IDS]


def _close_call_report(group_mean: int) -> dict:
    return {
        "facts": {
            "engines": [],
            "close_calls": {
                "margin_points": 5,
                "close_call_query_count": 7,
                "settled_tie_count": 0,
                "engine_pairs": [
                    {
                        "engine": "aurora_mysql",
                        "alternative": "dynamodb",
                        "pair_query_count": 7,
                        "exact_tie_count": 0,
                        "group_mean_count": group_mean,
                        "min_gap": 0.5,
                        "max_gap": 4.0,
                        "query_ids": _IDS[:5],
                        "more_query_count": 2,
                    }
                ],
            },
        }
    }


def test_close_calls_name_queries_by_a_short_id() -> None:
    md = "\n".join(close_calls_md(_close_call_report(0)))
    assert not _HASH.search(md), md
    assert f"`{_IDS[0][:8]}`" in md
    assert "(+2 more)" in md
    assert "full query id" in md


def test_close_calls_leave_out_an_all_zero_group_mean_column() -> None:
    md = "\n".join(close_calls_md(_close_call_report(0)))
    assert "Group mean" not in md
    header = next(line for line in md.splitlines() if line.startswith("| Went to"))
    row = next(line for line in md.splitlines() if line.startswith("| Aurora MySQL"))
    assert header.count("|") == row.count("|")


def test_close_calls_keep_the_group_mean_column_when_a_pair_has_one() -> None:
    md = "\n".join(close_calls_md(_close_call_report(3)))
    assert "Compared on group mean" in md
    row = next(line for line in md.splitlines() if line.startswith("| Aurora MySQL"))
    assert "| 3 |" in row


# ---------------------------------------------------------------------------
# Migration map: the recommended engine, not the owner
# ---------------------------------------------------------------------------


def _mapping_report(written: int = 0) -> dict:
    return {
        "database_name": "db",
        "table_mappings": [
            {
                "source_table": "db.posts",
                "recommended_database": "aurora_mysql",
                "target_table": "posts",
                "aggregate_pattern": "relational_table",
                "confidence_score": 80,
            }
        ],
        "facts": {
            "engines": [],
            "table_basis": {"mapped_tables": 1},
            "table_sources": {"written_table_count": written} if written else None,
        },
    }


def _map_intro(md: str) -> str:
    return md.split("## Migration map", 1)[1].split("| Source table", 1)[0]


def test_the_migration_map_labels_the_recommended_engine() -> None:
    md = render_engineering_report_md(_mapping_report(written=2))
    assert "| Source table | Recommended engine | Target |" in md
    assert "| Source table | Target engine |" not in md
    assert "summary label" in _map_intro(md)
    assert "source of truth, listed under Sources of truth and replicas" in _map_intro(md)


def test_the_migration_map_points_at_no_section_the_report_leaves_out() -> None:
    md = render_engineering_report_md(_mapping_report())
    assert "summary label" in _map_intro(md)
    assert "Sources of truth" not in _map_intro(md)


# ---------------------------------------------------------------------------
# Wave headline: every table figure adds up
# ---------------------------------------------------------------------------


def _kv_wave() -> dict:
    return {
        "wave": 3,
        "engines": ["dynamodb"],
        "title": "Move key-value queries to DynamoDB",
        "moves_from": ["aurora_mysql"],
        "query_count": 39,
        "workload_share_percent": 36.4,
        "share_basis": "queries",
        "table_count": 18,
        "tables": [f"db.t{i}" for i in range(18)],
    }


def _kv_facts(moving: int = 15, kept: int = 3) -> dict:
    return {
        "wave": 3,
        "engines": ["dynamodb"],
        "migrating": True,
        "served_tables": moving + kept,
        "moving_tables": moving,
        "kept_in_sync_tables": kept,
        "kept_in_sync": [
            {"table": f"db.t{i}", "owner": "aurora_mysql", "sync": "CDC"} for i in range(kept)
        ],
        "shared_in_wave": [],
    }


def test_the_wave_headline_says_what_the_tables_it_does_not_move_are() -> None:
    count, tables = _wave_headline_stats(_kv_wave(), _kv_facts())
    assert count + tables == (
        "39 queries (36.4% of query patterns), 18 source tables: 15 to move, 3 kept in "
        "sync from their owners"
    )
    assert "served" not in tables


def test_the_wave_headline_agrees_in_number_for_one_table() -> None:
    wave = {**_kv_wave(), "table_count": 1, "tables": ["db.t0"]}
    _, tables = _wave_headline_stats(wave, _kv_facts(moving=0, kept=1))
    assert tables == ", 1 source table: 0 to move, 1 kept in sync from its owner"


def test_the_engineering_roadmap_states_how_many_tables_move() -> None:
    report = {"migration_waves": [_kv_wave()], "facts": {"engines": [], "waves": [_kv_facts()]}}
    md = "\n".join(_roadmap_md(report))
    assert "- 18 source tables (15 to move): " in md
    assert "- 3 tables kept in sync from their owner: " in md


def test_a_wave_with_nothing_kept_in_sync_states_the_plain_count() -> None:
    wave = {**_kv_wave(), "table_count": 4, "tables": [f"db.t{i}" for i in range(4)]}
    facts = {**_kv_facts(moving=4, kept=0), "kept_in_sync": []}
    report = {"migration_waves": [wave], "facts": {"engines": [], "waves": [facts]}}
    md = "\n".join(_roadmap_md(report))
    assert "- 4 source tables: " in md
    assert "to move" not in md


# ---------------------------------------------------------------------------
# Queries on Aurora by table: each reason once, never cut mid-sentence
# ---------------------------------------------------------------------------


def test_aurora_reasons_are_stated_once_and_whole() -> None:
    reasons = [
        "highest fit for Aurora MySQL",
        "highest fit for Aurora MySQL | DynamoDB lacks required capability: aggregation",
        "highest fit for Aurora MySQL (scores tie with OpenSearch; Aurora MySQL is the "
        "source-compatible relational engine)",
        "utility/metadata statement, kept on the source-compatible relational engine "
        "(Aurora MySQL) | DocumentDB lacks required capability: a utility or DDL statement",
    ]
    group = {
        "group_name": "db.posts",
        "reasons": reasons,
        "access_patterns": [
            {"engine": "aurora_mysql", "query_ids": [f"q{i}"], "reason_index": i, "design_rps": 1}
            for i in range(len(reasons))
        ],
    }
    md = "\n".join(_aurora_queries_by_table_md({"query_groups": [group]}))
    row = next(line for line in md.splitlines() if line.startswith("| db.posts"))
    cell = row.rsplit("|", 2)[-2].strip()
    assert "…" not in cell, cell
    # Three variants of one rule: named once, without the per-query detail.
    assert cell.count("highest fit for Aurora MySQL") == 1, cell
    assert "scores tie with OpenSearch" not in cell, cell
    assert cell.count("utility/metadata statement") == 1, cell
    assert "lacks required capability" not in cell, cell


# ---------------------------------------------------------------------------
# Per-query appendix: "Scored higher" does not repeat itself
# ---------------------------------------------------------------------------


def test_a_reason_one_query_gives_keeps_its_detail() -> None:
    group = {
        "group_name": "db.posts",
        "reasons": [
            "highest fit for Aurora MySQL (scores tie with OpenSearch; Aurora MySQL is the "
            "source-compatible relational engine) | DynamoDB lacks required capability: "
            "aggregation",
            "reality check: consolidated from DocumentDB → Aurora MySQL (no unique value: "
            "DocumentDB scores 79.0, Aurora MySQL scores 76.0)",
            "reality check: consolidated from DocumentDB → Aurora MySQL (no unique value: "
            "DocumentDB scores 78.0, Aurora MySQL scores 75.0)",
            "co-dependency group → Aurora MySQL",
            "utility/metadata statement, kept on the source-compatible relational engine",
        ],
        "access_patterns": [
            {"engine": "aurora_mysql", "query_ids": [f"q{i}"], "reason_index": i, "design_rps": 1}
            for i in range(5)
        ],
    }
    md = "\n".join(_aurora_queries_by_table_md({"query_groups": [group]}))
    row = next(line for line in md.splitlines() if line.startswith("| db.posts"))
    cell = row.rsplit("|", 2)[-2].strip()
    # The cell lists the table's reasons in sorted order.
    assert cell == (
        "co-dependency group → Aurora MySQL; highest fit for Aurora MySQL (scores tie with "
        "OpenSearch; Aurora MySQL is the source-compatible relational engine); reality check: "
        "consolidated from DocumentDB → Aurora MySQL (+1 more)"
    ), cell


def test_the_outscored_line_does_not_repeat_its_label() -> None:
    trace = {
        "query_id": "a" * 64,
        "source_tables": ["db.users"],
        "final_engine_name": "Aurora MySQL",
        "first_assignment": {"to_engine_name": "DocumentDB", "step": 1, "version": 1},
        "moves": [],
        "outscored_by": [
            {
                "engine": "documentdb",
                "explanation": "DocumentDB scored higher (81 against 74), but the query left "
                "it in version 2",
            }
        ],
    }
    md = "\n".join(_trace_md(trace, {"moved": True}))
    assert "Scored higher: DocumentDB scored higher" not in md, md
    assert "- DocumentDB scored higher \\(81 against 74\\), but the query left it" in md, md
