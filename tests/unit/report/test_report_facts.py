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
from src.report.engine_choices import reroute_summary
from src.report.engineering_report import render_engineering_report_md
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts
from tests.executive_pdf import executive_text


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


def _tables_migrate(md: str) -> str:
    """The engine table's "N tables migrate" total in the engineering report."""
    m = re.search(r"^\| Total \| \| [^|]* \| (\d+) tables? migrates? \|", md, re.M)
    assert m, "no Total row"
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
    assert _tables_migrate(render_engineering_report_md(report)) == "42"
    assert "42 tables migrate" in executive_text(report)


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

    md = render_engineering_report_md(report)
    assert _tables_migrate(md) == str(tile)
    assert "- 1 table kept in sync from their owner: `db.t4` -> Aurora MySQL (CDC)" in md
    assert "moves with its owner): `db.t2` -> DynamoDB (CDC)" in md


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
    md = render_engineering_report_md(report)
    assert re.search(r"^\| Total \| \| 99% \|", md, re.M)


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
    assert "## Moved after approval (7 queries)" in render_engineering_report_md(report)
    # The engineering report's summary sentence reads it too.
    summary = reroute_summary(report)
    assert summary is not None and "found that 7 queries cannot run" in summary


# #584: the signal override is data, not reason text the deck parses
# --------------------------------------------------------------------------


def _override_report(reason: str) -> dict[str, Any]:
    """A 48% cache layer whose queries a leaderboard signal overrode onto it."""
    return {
        "database_name": "wordpress",
        "job_id": "job-584",
        "ranking": [
            {
                "target": "elasticache",
                "confidence_score": 48,
                "workload_percent": 31.8,
                "assignment_reason_summary": [reason],
            },
            {"target": "dynamodb", "confidence_score": 50, "workload_percent": 58.9},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 19}]},
        "schema_designs": {"elasticache": {"tables": [{}, {}]}},
    }


_SIGNALS = {
    "results": {
        "triage_summary": {
            "signals": [
                {"signal": "key_value_lookups", "query_count": 37, "targets": ["elasticache"]},
                {"signal": "leaderboard_pattern", "query_count": 15, "targets": ["elasticache"]},
            ]
        }
    }
}


def _confirm_card(f: dict[str, Any]) -> str:
    return next(str(d["against"]) for d in f["decisions"] if d["question"].startswith("Confirm"))


def test_584_the_stored_fact_decides_not_the_reason_text() -> None:
    report = _override_report("signal override: top-N listings → ElastiCache")
    report["facts"] = compute_report_facts(report, signal_overrides={})
    # No override recorded: the reason text names a signal, but the stored fact stands.
    engines = {e["engine"]: e for e in report_facts(report)["engines"]}
    assert engines["elasticache"]["signal_overrides"] == []


def test_584_an_older_report_reads_either_reason_form() -> None:
    for reason in (
        "signal override: leaderboard_pattern → elasticache",
        "signal override: top-N listings → ElastiCache | DynamoDB lacks required capability",
    ):
        facts = report_facts(_override_report(reason))
        engines = {e["engine"]: e for e in facts["engines"]}
        assert engines["elasticache"]["signal_overrides"] == ["leaderboard_pattern"], reason
        assert engines["dynamodb"]["signal_overrides"] == []


# --------------------------------------------------------------------------
# #546: an engine a customer edit re-added is not described as removed
# --------------------------------------------------------------------------


def _readded_report() -> dict[str, Any]:
    """The wordpress shape after the reality check removed DocumentDB and a
    customer edit then moved two queries back onto it (assignment v3)."""
    return {
        "database_name": "wordpress",
        "job_id": "job-546",
        "ranking": [
            {"target": "aurora_mysql", "assigned_queries": 62, "workload_percent": 57.9},
            {"target": "dynamodb", "assigned_queries": 43, "workload_percent": 40.2},
            {"target": "documentdb", "assigned_queries": 2, "workload_percent": 1.9},
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "dynamodb", "table_count": 0},
                {"service": "documentdb", "table_count": 0},
            ]
        },
        "schema_designs": {"aurora_mysql": {"status": "skipped"}},
        "tco_analysis": {
            "projected_monthly_cost": 784.6,
            "cost_breakdown": [
                {"database": "aurora_mysql", "monthly_cost_usd": 318.8},
                {"database": "dynamodb", "monthly_cost_usd": 98.41},
                {"database": "documentdb", "monthly_cost_usd": 367.39},
            ],
        },
        "trade_offs": [
            {
                "description": "Consolidated 3 queries from DocumentDB → Aurora MySQL: DocumentDB "
                "provides no unique capabilities. Removes $367.39/mo of infrastructure cost.",
                "impact": "same",
                "engine": "reality-check",
            },
            {
                "description": "Consolidated 5 queries from DocumentDB → DynamoDB: DocumentDB "
                "provides no unique capabilities. DocumentDB leaves the architecture.",
                "impact": "same",
                "engine": "reality-check",
            },
            {
                "description": "Recommended pattern: Event-Driven Sync.",
                "impact": "same",
                "engine": "reality-check",
            },
        ],
        "reality_check": {
            "consolidations": [
                {"from_engine": "documentdb", "to_engine": "aurora_mysql", "action": "full"},
                {"from_engine": "documentdb", "to_engine": "dynamodb", "action": "full"},
            ],
            "before_distribution": {"aurora_mysql": 60, "dynamodb": 39, "documentdb": 8},
            "after_distribution": {"aurora_mysql": 63, "dynamodb": 44},
        },
        "risk_assessment": {"overall_risk_level": "LOW", "risks": []},
    }


def test_546_the_facts_mark_the_consolidation_reversed_and_list_no_removed_engine() -> None:
    facts = _with_facts(_readded_report())["facts"]
    assert facts["removed_engines"] == []
    reversed_lines = [t for t in facts["reality_check_trade_offs"] if t["reversed"]]
    assert [(t["from_engine"], t["to_engine"]) for t in reversed_lines] == [
        ("documentdb", "aurora_mysql"),
        ("documentdb", "dynamodb"),
    ]
    assert facts["trade_off_count"] == 1


def test_546_no_deliverable_claims_the_removal_or_its_saving() -> None:
    report = _with_facts(_readded_report())
    md = render_engineering_report_md(report)
    trade_offs = md.split("## Migration trade-offs", 1)[1]
    assert "leaves the architecture" not in trade_offs
    assert "Removes $" not in trade_offs
    assert "## Migration trade-offs (1)" in md
    assert "### Reversed by a later assignment edit" in md
    assert "DocumentDB stays in the architecture" in md
    text = executive_text(report)
    assert "leaves the architecture" not in text and "Removes $" not in text


def test_546_a_removed_engine_still_lists_with_its_reasons() -> None:
    """Without the customer edit, DocumentDB is still listed, with both lines."""
    report = _readded_report()
    report["ranking"] = report["ranking"][:2]
    facts = _with_facts(report)["facts"]
    assert [e["engine"] for e in facts["removed_engines"]] == ["documentdb"]
    removed = facts["removed_engines"][0]
    assert removed["moved_to"] == ["aurora_mysql", "dynamodb"]
    assert len(removed["reasons"]) == 2
    assert not any(t["reversed"] for t in facts["reality_check_trade_offs"])


# --------------------------------------------------------------------------
# #861: a partial consolidation, or one a customer pin kept, is not "reversed"
# --------------------------------------------------------------------------


MOVED_TO_AURORA = ["a1", "a2", "a3"]
MOVED_TO_DYNAMODB = ["d1", "d2", "d3"]


def _pin_kept_report(moved_back: tuple[str, ...] = ()) -> dict[str, Any]:
    """The wordpress shape with two DocumentDB queries pinned by the customer
    before the reality check (#852 review): the check moved DocumentDB's other
    queries off, the pins stayed, and the sanity sweep kept DocumentDB for
    them. The check's own revision is the effective assignment, so DocumentDB
    is in its ``after_distribution``: nothing was moved back after it.

    ``moved_back``: the moved queries a later customer edit put back on
    DocumentDB (synthesis records them per consolidation, #861)."""
    report = _readded_report()
    report["trade_offs"][0]["description"] = (
        "Consolidated 3 queries from DocumentDB → Aurora MySQL: Partial consolidation: "
        "3 queries moved to Aurora MySQL; DocumentDB stays for 2 queries."
    )
    report["trade_offs"][1]["description"] = (
        "Consolidated 3 queries from DocumentDB → DynamoDB: Partial consolidation: "
        "3 queries moved to DynamoDB; DocumentDB stays for 2 queries."
    )
    report["trade_offs"].append(
        {
            "description": "DocumentDB kept: not every query can leave it: the customer "
            "chose DocumentDB for q1; the customer chose DocumentDB for q2",
            "impact": "same",
            "engine": "reality-check",
            "query_ids": ["q1", "q2"],
        }
    )
    rc = report["reality_check"]
    for c, moved in zip(rc["consolidations"], (MOVED_TO_AURORA, MOVED_TO_DYNAMODB), strict=True):
        c.update(
            action="partial",
            queries_retained=["q1", "q2"],
            moved_query_ids=list(moved),
            moved_back_query_ids=[q for q in moved if q in moved_back],
        )
    rc["after_distribution"] = {"aurora_mysql": 63, "dynamodb": 42, "documentdb": 2}
    rc["sweep_decisions"] = [
        {
            "engine": "documentdb",
            "decision": "kept",
            "reason": "not every query can leave it: the customer chose DocumentDB for q1",
            "blocking_query_ids": ["q1", "q2"],
            "blocking_rule": "customer_pin",
        }
    ]
    return report


def test_861_a_consolidation_the_check_itself_kept_partial_is_not_reversed() -> None:
    facts = _with_facts(_pin_kept_report())["facts"]
    assert not any(t["reversed"] for t in facts["reality_check_trade_offs"])
    assert facts["removed_engines"] == []
    assert facts["trade_off_count"] == 4
    kinds = [(d["kind"], d["engine"]) for d in facts["reality_check_decisions"]]
    assert kinds == [
        ("consolidation", "documentdb"),
        ("consolidation", "documentdb"),
        ("pattern", None),
        ("kept", "documentdb"),
    ]


def test_861_every_deliverable_shows_the_partial_consolidation() -> None:
    report = _with_facts(_pin_kept_report())
    md = render_engineering_report_md(report)
    assert "moved queries back" not in md
    assert "Reversed by a later assignment edit" not in md
    assert "## Migration trade-offs (4)" in md
    assert "DocumentDB stays for 2 queries" in md
    assert "the customer chose DocumentDB" in md
    text = executive_text(report)
    assert "DocumentDB stays for 2 queries" in text
    assert "the customer chose DocumentDB" in text


def test_861_a_customer_edit_after_the_check_still_reverses_it() -> None:
    """#546 stays: the check removed DocumentDB (not in its after_distribution)
    and a later edit put queries back on it. A report without the per-query
    record falls back to the engine rule."""
    report = _pin_kept_report()
    rc = report["reality_check"]
    rc["after_distribution"] = {"aurora_mysql": 63, "dynamodb": 44}
    for c in rc["consolidations"]:
        del c["moved_query_ids"], c["moved_back_query_ids"]
    facts = _with_facts(report)["facts"]
    assert [t["from_engine"] for t in facts["reality_check_trade_offs"] if t["reversed"]] == [
        "documentdb",
        "documentdb",
    ]


def test_861_an_edit_that_undoes_a_partial_consolidation_reverses_it() -> None:
    """The #891 review's wordpress case: the check kept DocumentDB partial (the
    pins), then a customer edit moved every moved query back. Per query, both
    lines are undone, so no deliverable states them (ADR-032 Decision 6)."""
    report = _with_facts(_pin_kept_report(moved_back=(*MOVED_TO_AURORA, *MOVED_TO_DYNAMODB)))
    facts = report["facts"]
    rows = facts["reality_check_trade_offs"]
    assert [
        (t["reversed"], t["queries_moved_back"], t["queries_still_moved"]) for t in rows[:2]
    ] == [
        (True, 3, 0),
        (True, 3, 0),
    ]
    assert [d["kind"] for d in facts["reality_check_decisions"]] == ["pattern", "kept"]
    assert facts["trade_off_count"] == 2
    md = render_engineering_report_md(report)
    current = md.split("## Migration trade-offs", 1)[1].split("### Reversed", 1)[0]
    assert "moved to DynamoDB" not in current and "moved to Aurora MySQL" not in current
    assert "### Reversed by a later assignment edit" in md
    text = executive_text(report).split("Reversed by a later assignment edit", 1)[0]
    assert "moved to DynamoDB" not in text


def test_861_an_edit_that_undoes_part_of_it_states_what_stayed_moved() -> None:
    """Two of the three DynamoDB moves undone: the line now says one query
    stays moved, in every deliverable, never the check's original three."""
    report = _with_facts(_pin_kept_report(moved_back=("d1", "d2")))
    facts = report["facts"]
    rows = facts["reality_check_trade_offs"]
    assert (rows[1]["reversed"], rows[1]["queries_moved_back"], rows[1]["queries_still_moved"]) == (
        False,
        2,
        1,
    )
    now = rows[1]["description_now"]
    assert now.startswith("Consolidated 1 query from DocumentDB → DynamoDB")
    assert "a later assignment edit moved 2 back to DocumentDB" in now
    assert rows[0]["description_now"] is None  # nothing on that line was undone
    decision = next(d for d in facts["reality_check_decisions"] if d["index"] == 1)
    assert decision["title"] == "Consolidated 1 query from DocumentDB → DynamoDB"
    md = render_engineering_report_md(report)
    assert "Consolidated 1 query from DocumentDB → DynamoDB" in md
    assert "3 queries moved to DynamoDB" not in md
    text = executive_text(report)  # the executive report's font draws "→" as "to"
    assert "Consolidated 1 query from DocumentDB to DynamoDB" in text
    assert "3 queries moved to DynamoDB" not in text


# --------------------------------------------------------------------------
# #595: reality-check trade-offs in every deliverable
# --------------------------------------------------------------------------


def _kept_report() -> dict[str, Any]:
    """The sanity sweep kept a small DocumentDB (#593): its verdict is a
    reality-check trade-off, next to a recommended pattern."""
    report = _readded_report()
    report["trade_offs"] = [
        {
            "description": "DocumentDB kept: 2 queries need nested-document updates "
            "no other selected engine supports.",
            "impact": "same",
            "engine": "reality-check",
            "query_ids": ["q1", "q2"],
        },
        {
            "description": "Recommended pattern: Event-Driven Sync. Use change streams to "
            "propagate changes between databases.",
            "impact": "same",
            "engine": "reality-check",
        },
        {"description": "GSI overloading", "impact": "fewer tables", "engine": "dynamodb"},
    ]
    report["reality_check"] = {
        "consolidations": [],
        "before_distribution": {"aurora_mysql": 62, "dynamodb": 43, "documentdb": 2},
        "after_distribution": {"aurora_mysql": 62, "dynamodb": 43, "documentdb": 2},
    }
    return report


def test_595_the_facts_carry_each_reality_check_decision() -> None:
    decisions = _with_facts(_kept_report())["facts"]["reality_check_decisions"]
    assert [(d["kind"], d["engine"], d["title"]) for d in decisions] == [
        ("kept", "documentdb", "DocumentDB kept"),
        ("pattern", None, "Recommended pattern: Event-Driven Sync"),
    ]
    assert decisions[0]["detail"].startswith("2 queries need nested-document updates")
    assert decisions[0]["query_ids"] == ["q1", "q2"]


def test_595_the_executive_report_and_engineering_report_show_them() -> None:
    report = _with_facts(_kept_report())
    text = executive_text(report)
    assert "DocumentDB kept: 2 queries need nested-document updates" in text
    assert "Recommended pattern: Event-Driven Sync" in text

    md = render_engineering_report_md(report)
    assert "DocumentDB kept: 2 queries need nested-document updates" in md
    assert "Recommended pattern: Event-Driven Sync" in md


def test_595_no_reality_check_decision_no_section() -> None:
    report = _kept_report()
    report["trade_offs"] = report["trade_offs"][2:]
    report = _with_facts(report)
    assert report["facts"]["reality_check_decisions"] == []
    assert "### reality-check" not in render_engineering_report_md(report)
