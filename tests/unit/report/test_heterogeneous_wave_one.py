"""#1120: a heterogeneous source's wave 1 moves every source table, and says how far
the plan has looked at that move.

On trailgear (SQL Server), wave 1 converts the whole schema to the chosen Aurora
PostgreSQL, but the deliverables read "Aurora PostgreSQL | Migration target | 0 source
tables" and "Total 23 tables migrate": wave 1 never counted towards "Tables migrate"
(#321), which is right for a homogeneous source (its tables stay on a dialect they
already speak) and wrong for a cross-engine one, where every table moves in wave 1.

Maintainer direction (2026-10-19): wave 1 moves every source table to the chosen
relational engine and is counted in full, once (a later wave moves a subset of those
tables on, so it adds none); the deliverables say plainly that wave 1 is not yet
optimized for heterogeneous migrations and list the detected relational gaps
(``facts.relational_gaps``, #471) under wave 1. Later waves are unchanged, and so is a
homogeneous wave 1.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report.engineering_report import render_engineering_report_md
from src.report.facts import report_facts
from src.shared.catalog.gaps import GAP_IMPACTS
from src.shared.prose_style import find_style_hits
from src.shared.report_facts import compute_report_facts
from tests.executive_pdf import executive_text, normalize

TABLES = [f"db.t{i}" for i in range(1, 6)]
VIEW = "db.v1"
NOT_OPTIMIZED = "Wave 1 is not yet optimized for heterogeneous migrations"


def _gaps(gaps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    gaps = (
        [
            {
                "rule_id": "sqlserver-top-clause",
                "feature": "TOP row limits",
                "impact": "needs_rewrite",
                "reason": "TOP (n) becomes LIMIT n.",
                "docs": "Aurora PostgreSQL docs",
                "tables": ["db.t1", "db.t2"],
                "queries": ["q1", "q2"],
                "objects": [],
            },
            {
                "rule_id": "sqlserver-babelfish-option",
                "feature": "Babelfish for Aurora PostgreSQL",
                "impact": "informational",
                "reason": "Babelfish accepts T-SQL.",
                "docs": "Babelfish docs",
                "tables": [],
                "queries": [],
                "objects": [],
            },
        ]
        if gaps is None
        else gaps
    )
    by_impact = {
        k: sum(1 for g in gaps if g["impact"] == k)
        for k in ("blocks_move", "needs_rewrite", "behaves_differently", "informational")
    }
    return {
        "source_engine": "sqlserver",
        "source_version": "15.0.4322.2",
        "target_engine": "aurora_postgresql",
        "rules_checked": 13,
        "gaps": gaps,
        "gaps_by_impact": by_impact,
    }


def _report(homogeneity: str = "cross_engine") -> dict[str, Any]:
    """Wave 1 carries the 5 source tables and a view to Aurora; wave 2 moves 3 of the
    tables on to DynamoDB (it serves 4, the 4th stays on Aurora, kept in sync)."""
    aurora = "aurora_postgresql" if homogeneity == "cross_engine" else "aurora_mysql"
    source = "sqlserver" if homogeneity == "cross_engine" else "mysql"
    aurora_db = [{"service": aurora, "table_count": 0}] if homogeneity == "cross_engine" else []
    return {
        "job_id": "job-1120",
        "database_name": "db",
        "timestamp": "2026-10-19T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 80, "workload_percent": 60.0},
            {"target": aurora, "confidence_score": 70, "workload_percent": 40.0},
        ],
        "recommended_architecture": {
            "architecture_type": "polyglot",
            "databases": [{"service": "dynamodb", "table_count": 0}, *aurora_db],
        },
        "schema_designs": {
            aurora: {"status": "not_available"},
            "dynamodb": {"status": "not_available"},
        },
        "table_mappings": [],
        "tco_analysis": {
            "current_monthly_cost": 250.0,
            "projected_monthly_cost": 300.0,
            "savings_percent": -20.0,
            "cost_breakdown": [
                {"database": "dynamodb", "monthly_cost_usd": 100.0},
                {"database": aurora, "monthly_cost_usd": 200.0},
            ],
        },
        "risk_assessment": {"overall_risk_level": "LOW", "risks": []},
        "source_table_count": len(TABLES),
        "migration_waves": [
            {
                "wave": 1,
                "title": "Move off SQL Server to Aurora PostgreSQL (cross-engine)",
                "engines": [aurora],
                "moves_from": [source],
                "homogeneity": homogeneity,
                "tables": [*TABLES, VIEW],
                "table_count": len(TABLES) + 1,
                "query_count": 4,
                "cutover_query_count": 10,
                "workload_share_percent": 40.0,
                "share_basis": "queries",
                "rationale": "The whole source database moves first.",
                "gate": "Parity validated.",
            },
            {
                "wave": 2,
                "title": "Move key-value and point-lookup queries to DynamoDB",
                "engines": ["dynamodb"],
                "moves_from": [aurora],
                "tables": TABLES[:4],
                "table_count": 4,
                "table_owners": [{"table": "db.t4", "owner": aurora, "sync": "CDC"}],
                "query_count": 6,
                "workload_share_percent": 60.0,
                "share_basis": "queries",
                "rationale": "Key-value reads.",
                "gate": "Parity confirmed.",
            },
        ],
        "tied_queries": [],
    }


def _with_facts(
    report: dict[str, Any], gaps: dict[str, Any] | None = None, views: bool = True
) -> dict[str, Any]:
    out = copy.deepcopy(report)
    out["facts"] = compute_report_facts(
        out, view_ids={VIEW} if views else None, relational_gaps=gaps
    )
    return out


def _engine(facts: dict[str, Any], engine: str) -> dict[str, Any]:
    return next(e for e in facts["engines"] if e["engine"] == engine)


# --------------------------------------------------------------------------
# The facts
# --------------------------------------------------------------------------


def test_the_chosen_aurora_engine_gets_every_source_table() -> None:
    facts = _with_facts(_report(), _gaps())["facts"]
    aurora = _engine(facts, "aurora_postgresql")
    assert aurora["engine_source_tables"] == len(TABLES)  # tables, not the view
    assert aurora["tables_migrate"] == len(TABLES)
    wave1 = facts["waves"][0]
    assert wave1["moving_tables"] == len(TABLES)
    assert wave1["moving_by_engine"] == {"aurora_postgresql": len(TABLES)}


def test_tables_migrate_counts_wave_1_in_full_and_each_table_once() -> None:
    """Wave 2's 3 tables are among wave 1's 5: the total is 5, not 3 and not 8."""
    facts = _with_facts(_report(), _gaps())["facts"]
    assert facts["tables_migrate"] == len(TABLES)
    assert facts["tables_migrate"] <= facts["source_table_count"]


def test_without_view_ids_wave_1_still_counts_the_source_tables() -> None:
    """A report rebuilt without the collection: wave 1's tables are the analyzed ones."""
    facts = _with_facts(_report(), None, views=False)["facts"]
    assert _engine(facts, "aurora_postgresql")["engine_source_tables"] == len(TABLES)
    assert facts["tables_migrate"] == len(TABLES)


def test_later_waves_are_unchanged() -> None:
    het = _with_facts(_report(), _gaps())["facts"]["waves"][1]
    hom = _with_facts(_report("homogeneous"))["facts"]["waves"][1]
    for key in ("migrating", "served_tables", "moving_tables", "kept_in_sync_tables"):
        assert het[key] == hom[key], key
    assert het["moving_tables"] == 3
    assert het["heterogeneous_note"] is None


def test_a_homogeneous_wave_1_is_unchanged() -> None:
    """The source-compatible engine keeps its tables on their dialect: wave 1 moves
    nothing to count, so "Tables migrate" is the later waves' moves only."""
    facts = _with_facts(_report("homogeneous"))["facts"]
    wave1 = facts["waves"][0]
    assert wave1["moving_tables"] is None
    assert wave1["moving_by_engine"] is None
    assert wave1["heterogeneous_note"] is None
    assert facts["tables_migrate"] == 3


def test_the_note_states_the_move_and_the_gap_counts() -> None:
    note = _with_facts(_report(), _gaps())["facts"]["waves"][0]["heterogeneous_note"]
    assert note == (
        f"{NOT_OPTIMIZED}: it converts all 5 source tables from SQL Server to Aurora "
        "PostgreSQL with their current data model, and the plan does not yet analyze how to "
        "redesign the schema or the queries for Aurora PostgreSQL. The relational gap "
        "check found 2 gaps in this conversion: 1 needs a rewrite, 1 is informational."
    )
    assert find_style_hits(note) == []


def test_the_note_phrases_every_impact() -> None:
    gaps = [
        {"rule_id": f"r{i}", "feature": f"F{i}", "impact": impact, "reason": "R."}
        for i, impact in enumerate(GAP_IMPACTS)
    ]
    note = _with_facts(_report(), _gaps(gaps))["facts"]["waves"][0]["heterogeneous_note"]
    assert note.endswith(
        "found 4 gaps in this conversion: 1 blocks the move, 1 needs a rewrite, "
        "1 behaves differently, 1 is informational."
    )


def test_the_note_with_no_gaps_found_says_so() -> None:
    note = _with_facts(_report(), _gaps([]))["facts"]["waves"][0]["heterogeneous_note"]
    assert note.startswith(NOT_OPTIMIZED)
    assert note.endswith("The relational gap check found no gaps in this conversion.")


def test_the_note_without_a_gap_check_leaves_the_gap_sentence_out() -> None:
    """A report rebuilt without the collection has no gap check to report."""
    note = _with_facts(_report(), None)["facts"]["waves"][0]["heterogeneous_note"]
    assert note.startswith(NOT_OPTIMIZED)
    assert "gap check" not in note


@pytest.mark.passes_on_main
def test_the_facts_validate_against_the_contract() -> None:
    report = _with_facts(_report(), _gaps())
    ReportFacts.model_validate(report["facts"])
    SynthesisOutputContract.model_validate(
        {**report, "summary": "s", "summary_deterministic": "s", "query_groups": []}
    )


# --------------------------------------------------------------------------
# The deliverables
# --------------------------------------------------------------------------


def _wave_section(md: str, n: int) -> str:
    start = md.index(f"### Wave {n}:")
    end = md.find("\n### ", start + 1)
    return md[start : end if end != -1 else len(md)]


def test_engineering_report_scope_total_and_role_note() -> None:
    md = render_engineering_report_md(_with_facts(_report(), _gaps()))
    assert re.search(
        r"^\| Aurora PostgreSQL \| Migration target \| 40% \| 5 source tables \|", md, re.M
    )
    assert re.search(r"^\| Total \| \| 100% \| 5 tables migrate \|", md, re.M)
    assert (
        "Wave 1 moves all 5 source tables to Aurora PostgreSQL; later waves move some of "
        "them on to DynamoDB." in md
    )
    assert "assigned to DynamoDB, Aurora PostgreSQL" not in md


def test_engineering_report_lists_the_gaps_under_wave_1() -> None:
    md = render_engineering_report_md(_with_facts(_report(), _gaps()))
    wave1 = _wave_section(md, 1)
    assert f"- {NOT_OPTIMIZED}: it converts all 5 source tables" in wave1
    assert "- Relational gaps:" in wave1
    assert (
        "  - TOP row limits (needs a rewrite): TOP \\(n\\) becomes LIMIT n. "
        "Found in: `db.t1`, `db.t2`; queries `q1`, `q2`" in wave1
    )
    assert "  - Babelfish for Aurora PostgreSQL (informational): Babelfish accepts T-SQL." in wave1
    assert NOT_OPTIMIZED not in _wave_section(md, 2)


def test_executive_report_states_the_note_and_the_gaps_without_identifiers() -> None:
    text = normalize(executive_text(_with_facts(_report(), _gaps())))
    assert f"{NOT_OPTIMIZED}: it converts all 5 source tables" in text
    assert (
        "Relational gaps: TOP row limits (needs a rewrite); "
        "Babelfish for Aurora PostgreSQL (informational)" in text
    )
    # The reasons name target functions; they stay in the engineering report.
    assert "becomes LIMIT n" not in text
    assert "Total 100% 5 tables migrate" in text
    assert "db.t1" not in text


@pytest.mark.passes_on_main
def test_a_homogeneous_report_shows_no_note() -> None:
    """A homogeneous wave 1 keeps its gap check in report.json, and its deliverables
    read as before (the gap list belongs to the heterogeneous wave 1 only)."""
    report = _with_facts(_report("homogeneous"), _gaps())
    md = render_engineering_report_md(report)
    assert NOT_OPTIMIZED not in md
    assert "Relational gaps" not in md
    assert NOT_OPTIMIZED not in normalize(executive_text(report))


# --------------------------------------------------------------------------
# Review round 1: edge cases
# --------------------------------------------------------------------------


def test_a_wave_1_with_no_aurora_engine_picked_is_unchanged() -> None:
    """``homogeneity="heterogeneous"``: no engine to give the tables to (it happens when
    the resolver's Aurora candidate has no ranking entry), so nothing is patched."""
    report = _report()
    wave1 = report["migration_waves"][0]
    wave1.update(homogeneity="heterogeneous", engines=[], cutover_query_count=None)
    facts = _with_facts(report, _gaps())["facts"]
    row = facts["waves"][0]
    assert row["moving_tables"] is None
    assert row["moving_by_engine"] is None
    assert row["heterogeneous_note"] is None
    assert row["lists_relational_gaps"] is False
    assert _engine(facts, "aurora_postgresql")["engine_source_tables"] == 0
    assert facts["tables_migrate"] == 3  # wave 2's moves only, as before
    md = render_engineering_report_md(_with_facts(report, _gaps()))
    assert NOT_OPTIMIZED not in md and "Relational gaps" not in md


def _designed(served: list[str]) -> dict[str, Any]:
    """Aurora PostgreSQL has a completed design mapping 2 tables whose target tables
    read ``served`` source tables."""
    report = _report()
    report["recommended_architecture"]["databases"][1]["table_count"] = 2
    report["schema_designs"]["aurora_postgresql"] = {
        "status": "completed",
        "tables": [{"name": f"x{i}", "source_tables": [t]} for i, t in enumerate(served)],
    }
    return report


def test_incl_shared_is_dropped_once_the_engine_has_every_source_table() -> None:
    """Before #1120 the design's 4 served tables read "2 source tables (4 incl. shared)";
    wave 1 now gives the engine all 5, so there is nothing beyond its own to count."""
    before = compute_report_facts(_designed(TABLES[:4]), view_ids={VIEW})
    row = _engine(before, "aurora_postgresql")
    assert (row["engine_source_tables"], row["shared_source_tables"]) == (5, None)
    md = render_engineering_report_md(_with_facts(_designed(TABLES[:4]), _gaps()))
    assert re.search(r"^\| Aurora PostgreSQL \| [^|]* \| [^|]* \| 5 source tables \|", md, re.M)
    assert "incl. shared" not in md


def test_incl_shared_stays_when_the_design_serves_more_than_the_source_tables() -> None:
    """The design also reads the view: 6 served against 5 source tables."""
    facts = compute_report_facts(_designed([*TABLES, VIEW]), view_ids={VIEW})
    row = _engine(facts, "aurora_postgresql")
    assert (row["engine_source_tables"], row["shared_source_tables"]) == (5, 6)


def test_a_gap_check_for_another_engine_is_neither_counted_nor_listed() -> None:
    """The gap check targets the resolver's choice; wave 1 can fall back to the other
    Aurora engine. Its gaps are then not wave 1's."""
    other = {**_gaps(), "target_engine": "aurora_mysql"}
    report = _with_facts(_report(), other)
    row = report["facts"]["waves"][0]
    assert row["lists_relational_gaps"] is False
    assert row["heterogeneous_note"].startswith(NOT_OPTIMIZED)
    assert "gap check" not in row["heterogeneous_note"]
    md = render_engineering_report_md(report)
    assert "Relational gaps" not in md and "TOP row limits" not in md
    text = normalize(executive_text(report))
    assert "Relational gaps" not in text and "TOP row limits" not in text
    assert _with_facts(_report(), _gaps())["facts"]["waves"][0]["lists_relational_gaps"]


def test_the_note_counts_are_the_stored_gaps_by_impact() -> None:
    """One figure per impact: the gap check's own ``gaps_by_impact``."""
    gaps = {**_gaps(), "gaps_by_impact": {"needs_rewrite": 3, "informational": 0}}
    note = _with_facts(_report(), gaps)["facts"]["waves"][0]["heterogeneous_note"]
    assert note.endswith("found 3 gaps in this conversion: 3 need a rewrite.")


def test_the_executive_report_points_to_the_gap_reasons() -> None:
    text = normalize(executive_text(_with_facts(_report(), _gaps())))
    assert (
        "What to do about each gap, and where it was found: the engineering report's "
        "Migration roadmap (wave 1) and Risk register." in text
    )


FIXTURE_1_39 = Path(__file__).parent / "fixtures" / "trailgear_1_39_report.json"


def test_a_1_39_report_gets_the_wave_1_figures_and_note_from_the_facts_reader() -> None:
    """ADR-032 Decision 4: a trailgear job synthesized at 1.39 (the bundled sample's real
    report, query traces and groups trimmed) stored "0 source tables", "23 tables
    migrate" and no note; re-rendered, it reads as a job synthesized with #1120 does."""
    report = json.loads(FIXTURE_1_39.read_text())
    assert report["contract_version"] == "1.39"
    stored = report["facts"]
    assert all("heterogeneous_note" not in w for w in stored["waves"])
    assert _engine(stored, "aurora_postgresql")["engine_source_tables"] == 0
    assert stored["tables_migrate"] == 23

    facts = report_facts(report)
    assert _engine(facts, "aurora_postgresql")["engine_source_tables"] == 30
    assert _engine(facts, "aurora_postgresql")["tables_migrate"] == 30
    assert _engine(facts, "dynamodb")["tables_migrate"] == 23
    assert facts["tables_migrate"] == 30
    wave1, *later = facts["waves"]
    assert wave1["moving_tables"] == 30 and wave1["lists_relational_gaps"] is True
    assert wave1["heterogeneous_note"].startswith(f"{NOT_OPTIMIZED}: it converts all 30")
    assert wave1["heterogeneous_note"].endswith(
        "found 11 gaps in this conversion: 5 need a rewrite, 4 behave differently, "
        "2 are informational."
    )
    assert all(w["heterogeneous_note"] is None for w in later)
    assert [w["moving_tables"] for w in later] == [w["moving_tables"] for w in stored["waves"][1:]]
    ReportFacts.model_validate(facts)

    md = render_engineering_report_md(report)
    assert re.search(r"^\| Total \| \| 100% \| 30 tables migrate \|", md, re.M)
    assert f"- {NOT_OPTIMIZED}" in _wave_section(md, 1)
    assert "TOP row limits (needs a rewrite)" in _wave_section(md, 1)


@pytest.mark.passes_on_main
def test_a_stored_current_block_is_returned_as_stored() -> None:
    report = _with_facts(_report(), _gaps())
    assert report_facts(report) is report["facts"]


def test_a_gap_the_risk_register_explains_is_stated_once() -> None:
    """#1170 (state it once): with #1149's ``relational.gaps`` risk in the register, a
    gap it lists keeps only its impact and evidence under wave 1, and the register
    gives the reason; the informational gap, which raises no risk, keeps its reason."""
    from src.shared.migration_risk_rules import relational_gap_risks

    report = _report()
    risks = relational_gap_risks(_gaps(), report["migration_waves"])
    assert [r["rule"] for r in risks] == ["relational.gaps"]
    report["risk_assessment"]["risks"] = risks
    md = render_engineering_report_md(_with_facts(report, _gaps()))
    wave1 = _wave_section(md, 1)
    assert (
        "- Relational gaps (the Risk register gives the reason and documentation of each one "
        "it lists):" in wave1
    )
    assert "  - TOP row limits (needs a rewrite). Found in: `db.t1`, `db.t2`" in wave1
    assert "  - Babelfish for Aurora PostgreSQL (informational): Babelfish accepts T-SQL." in wave1
    assert md.count("TOP \\(n\\) becomes LIMIT n") == 1
    assert "becomes LIMIT n" not in wave1
