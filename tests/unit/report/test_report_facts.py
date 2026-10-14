"""The facts layer (#615, ADR-032) and the figure bugs it fixes.

Each deliverable reads one shared fact from ``report.json``'s ``facts``
(``src.report.facts.report_facts``); these tests build the cases each bug
needed and check that every deliverable now prints that one fact:

- #581: a merged DynamoDB/DocumentDB wave where DocumentDB serves a table
  DynamoDB owns -- the wave headline's move count is the "Tables migrate"
  tile's figure (``owned_counts_by_engine``), not ``table_count -
  len(table_owners)``.
- #533: a partial schema design -- the deck's "Basis of the analysis" table
  count is never below the tile and says which engines have a design.
- #578: wave 1 carries a view on top of the analyzed tables -- every
  deliverable states the tables and the views apart, so "312" reconciles
  with "Analyzed 311 source tables".
"""

from __future__ import annotations

import copy
import re
from typing import Any

from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report import pptx_report
from src.report.decision_report import render_decision_report_html
from src.report.engine_choices import reroute_summary
from src.report.engineering_report import render_engineering_report_md
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts


def _wave1(tables: list[str], views: int = 0) -> dict[str, Any]:
    return {
        "wave": 1,
        "title": "Move to Aurora MySQL",
        "engines": ["aurora_mysql"],
        "moves_from": ["mysql"],
        "homogeneity": "homogeneous",
        "tables": tables,
        "table_count": len(tables),
        "query_count": 10,
        "cutover_query_count": 30,
        "workload_share_percent": 33.3,
        "share_basis": "queries",
        "rationale": "The whole source database moves first.",
        "gate": "Parity validated.",
    }


def _merged_report() -> dict[str, Any]:
    """Wave 2 merges DynamoDB and DocumentDB. DocumentDB serves ``t2``, which
    DynamoDB owns, and ``t4``, which Aurora owns: DynamoDB moves t1 and t2,
    DocumentDB moves t3, and only t4 stays on its owner, kept in sync."""
    tables = ["db.t1", "db.t2", "db.t3", "db.t4", "db.t5"]
    return {
        "job_id": "job-1",
        "database_name": "db",
        "timestamp": "2026-10-14T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 80, "workload_percent": 40.0},
            {"target": "documentdb", "confidence_score": 70, "workload_percent": 26.7},
            {"target": "aurora_mysql", "confidence_score": 90, "workload_percent": 33.3},
        ],
        "recommended_architecture": {
            "architecture_type": "polyglot",
            "databases": [
                {"service": "dynamodb", "table_count": 0},
                {"service": "documentdb", "table_count": 0},
            ],
        },
        "schema_designs": {"aurora_mysql": {"status": "skipped"}},
        "table_mappings": [],
        "tco_analysis": {
            "current_monthly_cost": 500.0,
            "projected_monthly_cost": 600.0,
            "savings_percent": -20.0,
            "cost_breakdown": [
                {"database": "dynamodb", "monthly_cost_usd": 100.0},
                {"database": "documentdb", "monthly_cost_usd": 200.0},
                {"database": "aurora_mysql", "monthly_cost_usd": 300.0},
            ],
        },
        "risk_assessment": {"overall_risk_level": "LOW", "risks": []},
        "source_table_count": 5,
        "migration_waves": [
            _wave1(tables),
            {
                "wave": 2,
                "title": "Move key-value and document-shaped data to DynamoDB and DocumentDB",
                "engines": ["dynamodb", "documentdb"],
                "moves_from": ["aurora_mysql"],
                "tables": ["db.t1", "db.t2", "db.t3", "db.t4"],
                "table_count": 4,
                "table_owners": [
                    {"table": "db.t2", "owner": "dynamodb", "sync": "CDC"},
                    {"table": "db.t4", "owner": "aurora_mysql", "sync": "CDC"},
                ],
                "table_counts_by_engine": {"dynamodb": 2, "documentdb": 3},
                "owned_counts_by_engine": {"dynamodb": 2, "documentdb": 1},
                "query_count": 20,
                "workload_share_percent": 66.7,
                "share_basis": "queries",
                "rationale": "Key-value and document reads.",
                "gate": "Parity confirmed.",
            },
        ],
        "tied_queries": [],
    }


def _with_facts(report: dict[str, Any], view_ids: set[str] | None = None) -> dict[str, Any]:
    out = copy.deepcopy(report)
    out["facts"] = compute_report_facts(out, view_ids=view_ids)
    return out


def _decision_tile(html: str, label: str) -> str:
    m = re.search(rf"<p>{re.escape(label)}</p><p class=val>([^<]*)</p>", html)
    assert m, label
    return m.group(1)


# --------------------------------------------------------------------------
# The facts block itself
# --------------------------------------------------------------------------


def test_facts_validate_against_the_contract() -> None:
    report = _with_facts(_merged_report())
    ReportFacts.model_validate(report["facts"])
    SynthesisOutputContract.model_validate(
        {
            **report,
            "summary": "s",
            "summary_deterministic": "s",
            "query_groups": [],
        }
    )


def test_a_report_without_facts_gets_the_same_block_synthesis_writes() -> None:
    """A report synthesized before ``facts`` existed: one answer per figure."""
    legacy = _merged_report()
    assert report_facts(legacy) == _with_facts(legacy)["facts"]


def test_renderers_read_the_stored_facts_not_their_own_copy() -> None:
    """Change the stored fact: every deliverable prints the changed figure."""
    report = _with_facts(_merged_report())
    report["facts"]["tables_migrate"] = 42
    html = render_decision_report_html(report)
    assert _decision_tile(html, "Tables migrate") == "42"
    assert pptx_report.derive(report, {})["migrated"] == 42


# --------------------------------------------------------------------------
# #581: merged-wave headline move count vs the Tables migrate tile
# --------------------------------------------------------------------------


def test_581_merged_wave_moving_is_the_sum_of_owned_counts() -> None:
    facts = _with_facts(_merged_report())["facts"]
    wave2 = next(w for w in facts["waves"] if w["wave"] == 2)
    assert wave2["moving_tables"] == 3  # not 4 - len(table_owners) == 2
    assert wave2["kept_in_sync_tables"] == 1
    assert wave2["moving_by_engine"] == {"dynamodb": 2, "documentdb": 1}
    assert [o["table"] for o in wave2["kept_in_sync"]] == ["db.t4"]
    assert [o["table"] for o in wave2["shared_in_wave"]] == ["db.t2"]
    assert facts["tables_migrate"] == wave2["moving_tables"]


def test_581_every_deliverable_shows_the_tile_figure_in_the_wave_headline() -> None:
    report = _with_facts(_merged_report())
    tile = report["facts"]["tables_migrate"]

    html = render_decision_report_html(report)
    assert _decision_tile(html, "Tables migrate") == str(tile)
    stats = re.search(r'data-wave="2">.*?<span data-figure=wave-stats>([^<]*)</span>', html)
    assert stats and stats.group(1).endswith(f", 4 served, {tile} move")

    md = render_engineering_report_md(report)
    assert "- 1 table kept in sync from their owner: `db.t4` -> Aurora MySQL (CDC)" in md
    assert "moves with its owner): `db.t2` -> DynamoDB (CDC)" in md

    f = pptx_report.derive(report, {})
    assert f["migrated"] == tile
    wave2 = next(w for w in f["waves"] if w["engines"] == ["dynamodb", "documentdb"])
    assert wave2["sync_summary"] == (
        "DynamoDB moves 2 tables; DocumentDB moves 1 table. "
        "Kept in sync from Aurora MySQL: 1 table."
    )


# --------------------------------------------------------------------------
# #533: the deck's basis count with a partial schema design
# --------------------------------------------------------------------------


def _partial_design_report() -> dict[str, Any]:
    """The issue's two-engine example: DynamoDB has a completed design mapping
    5 tables, DocumentDB has none and 20 tables from its migration wave."""
    dynamo_tables = [f"db.d{i}" for i in range(5)]
    doc_tables = [f"db.c{i}" for i in range(20)]
    all_tables = dynamo_tables + doc_tables + [f"db.a{i}" for i in range(5)]
    return {
        "job_id": "job-2",
        "database_name": "db",
        "ranking": [
            {"target": "documentdb", "confidence_score": 70, "workload_percent": 50.0},
            {"target": "dynamodb", "confidence_score": 80, "workload_percent": 30.0},
            {"target": "aurora_mysql", "confidence_score": 90, "workload_percent": 20.0},
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "dynamodb", "table_count": 5},
                {"service": "documentdb", "table_count": 0},
            ]
        },
        "schema_designs": {
            "dynamodb": {"status": "completed", "tables": [{"source_tables": dynamo_tables}]},
            "documentdb": {"status": "skipped"},
            "aurora_mysql": {"status": "skipped"},
        },
        "table_mappings": [
            {"source_table": t, "recommended_database": "dynamodb", "confidence_score": 80}
            for t in dynamo_tables
        ],
        "source_table_count": len(all_tables),
        "migration_waves": [
            _wave1(all_tables),
            {
                "wave": 2,
                "title": "Move document-shaped data to DocumentDB",
                "engines": ["documentdb"],
                "moves_from": ["aurora_mysql"],
                "tables": doc_tables,
                "table_count": len(doc_tables),
                "query_count": 15,
                "workload_share_percent": 50.0,
                "share_basis": "queries",
                "rationale": "Document data.",
                "gate": "Parity confirmed.",
            },
        ],
        "risk_assessment": {"overall_risk_level": "LOW", "risks": []},
        "tco_analysis": {"projected_monthly_cost": 0, "savings_percent": 0},
    }


def test_533_partial_design_basis_is_never_below_tables_migrate() -> None:
    report = _with_facts(_partial_design_report())
    facts = report["facts"]
    assert facts["tables_migrate"] == 25
    basis = facts["table_basis"]
    assert basis["design_coverage"] == "partial"
    assert basis["designed_engines"] == ["dynamodb"]
    assert basis["pending_engines"] == ["documentdb"]
    assert basis["mapped_tables"] == 5
    assert basis["basis_table_count"] >= facts["tables_migrate"]


def test_533_the_deck_bullet_says_only_some_engines_have_a_design() -> None:
    f = pptx_report.derive(_with_facts(_partial_design_report()), {})
    assert f["schema_mapped"] is False
    assert f["n_tables"] >= f["migrated"] == 25
    line = f["basis_table_line"]
    assert line.startswith(
        "Schema mapping has run for DynamoDB only, so only 5 tables have a per-table fit score"
    ), line
    assert "each with its own fit score" not in line


# --------------------------------------------------------------------------
# #578: wave 1's tables and views next to "Analyzed N source tables"
# --------------------------------------------------------------------------


def _view_report() -> dict[str, Any]:
    report = _merged_report()
    report["migration_waves"][0] = _wave1([*report["migration_waves"][0]["tables"], "badge_posts"])
    return report


def test_578_wave1_states_tables_and_views_apart() -> None:
    report = _with_facts(_view_report(), view_ids={"badge_posts", "pg_stat_statements"})
    facts = report["facts"]
    wave1 = facts["waves"][0]
    assert facts["source_view_count"] == 1
    assert (wave1["served_tables"], wave1["tables_only"], wave1["views"]) == (6, 5, 1)
    assert wave1["tables_only"] == report["source_table_count"]

    html = render_decision_report_html(report)
    stats = re.search(r'data-wave="1">.*?<span data-figure=wave-stats>([^<]*)</span>', html)
    assert stats and stats.group(1).endswith(", 6 source tables and views (5 tables, 1 view)")
    md = render_engineering_report_md(report)
    assert "- 6 source tables and views (5 tables, 1 view): " in md


def test_578_an_older_report_without_view_ids_still_splits_wave1() -> None:
    """No collected view ids (a report synthesized before facts): the view
    count is what wave 1 carries beyond the analyzed tables."""
    facts = report_facts(_view_report())
    assert facts["source_view_count"] == 1
    assert facts["waves"][0]["tables_only"] == 5


# --------------------------------------------------------------------------
# Review round 1: the last renderer computations, and rounding once
# --------------------------------------------------------------------------


def test_the_migration_map_split_and_the_deck_engine_count_read_the_facts() -> None:
    report = _with_facts(_merged_report())
    report["facts"]["table_basis"]["mapped_tables_by_role"] = {
        "Migration target": 21,
        "Cache layer": 1,
    }
    report["facts"]["engine_count"] = 9
    from src.report.architecture import _mapping_split

    assert _mapping_split(report) == ": 21 migrate, 1 to the cache layer"
    f = pptx_report.derive(report, {})
    assert f["engine_count"] == 9


def test_the_mapped_tables_by_role_fact() -> None:
    report = _merged_report()
    report["table_mappings"] = [
        {"source_table": "db.t1", "recommended_database": "dynamodb", "confidence_score": 80},
        {"source_table": "db.t2", "recommended_database": "aurora_mysql", "confidence_score": 80},
        {"source_table": "db.t3", "recommended_database": "neptune", "confidence_score": 80},
    ]
    basis = _with_facts(report)["facts"]["table_basis"]
    assert basis["mapped_tables_by_role"] == {"Migration target": 1, "Retained": 1, "Other": 1}


def test_a_percentage_total_is_rounded_once_by_the_renderer() -> None:
    """99.45% prints "99%", as before the facts layer: never 99.5 then "100%"."""
    report = _merged_report()
    report["ranking"][0]["workload_percent"] = 40.0
    report["ranking"][1]["workload_percent"] = 26.15
    report["ranking"][2]["workload_percent"] = 33.3
    report = _with_facts(report)
    assert report["facts"]["workload_total_percent"] == 99.45
    html = render_decision_report_html(report)
    assert re.search(r"<tr data-figure=total><td colspan=2>Total</td><td>99%</td>", html)


def test_the_moved_after_approval_count_is_a_fact() -> None:
    """#629's "Moved after your approval (N queries)" reads the facts layer's count."""
    report = _merged_report()
    report["post_schema_reroutes"] = [
        {"query_id": "q1", "from_engine": "dynamodb", "to_engine": "aurora_mysql"},
        {"query_id": "q2", "from_engine": "dynamodb", "to_engine": "aurora_mysql"},
    ]
    report = _with_facts(report)
    assert report["facts"]["post_schema_reroute_count"] == 2
    report["facts"]["post_schema_reroute_count"] = 7
    assert "Moved after your approval (7 queries)" in render_decision_report_html(report)
    assert "## Moved after approval (7 queries)" in render_engineering_report_md(report)
    decision = next(
        d for d in pptx_report.derive(report, {})["decisions"] if d["badge"].endswith("queries")
    )
    assert decision["badge"] == "7 queries"
    # The shared summary sentence (decision and engineering reports) reads it too;
    # the interactive report's JS is pinned by ExportReport.postSchemaRerouteCount.test.js.
    summary = reroute_summary(report)
    assert summary is not None and "found that 7 queries cannot run" in summary
