# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sources of truth and replicas, in every deliverable (#992, ADR-031 Amendment 9).

``facts.table_sources`` states, for each written table read on a replica, its
source of truth, the replica engines (the CDC direction runs from the source of
truth to them) and the eventual-consistency risk naming the reads a replica
serves. The engineering report formats it, after its wave plan, and the UI
results page does too (``SourcesOfTruthCallout``). They only format the facts
(ADR-032). The executive report names no table (#1020), so it leaves them out.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report.escaping import md_text
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from src.report.sources_of_truth import SOURCES_OF_TRUTH_INTRO, sources_of_truth_md
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, SOURCE_DATABASE_NAME, display_engine
from src.shared.report_facts import (
    TABLE_SOURCES_SHOWN,
    compute_report_facts,
    shared_replica_risk_text,
)
from tests.executive_pdf import executive_text

MD_HEADING = "## Sources of truth and replicas"


def _row(table: str, source: str, replicas: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "table": table,
        "source_of_truth": source,
        "write_engines": [source],
        "write_query_ids": [f"{table}-w1", f"{table}-w2"],
        "replicas": [{"engine": e, "read_query_ids": q} for e, q in replicas.items()],
    }


def _report(
    rows: list[dict[str, Any]] | None, mappings: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "trailgear",
        "job_id": "job-1",
        "timestamp": "2026-10-17T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 70, "workload_percent": 60.0},
            {"target": "aurora_postgresql", "confidence_score": 80, "workload_percent": 40.0},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {},
        "tied_queries": [],
    }
    if rows is not None:
        report["table_sources"] = rows
    if mappings is not None:
        report["table_mappings"] = mappings
    report["facts"] = compute_report_facts(report)
    return report


@pytest.fixture
def report() -> dict[str, Any]:
    return _report(
        [
            _row("trailgear.Shipment", "dynamodb", {"aurora_postgresql": ["r1", "r2"]}),
            _row("trailgear.Customer", "aurora_postgresql", {}),
        ]
    )


def _section_md(md: str) -> str:
    start = md.index(MD_HEADING)
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


# #951: the reads are counted, not listed (their ids are replica_read_query_ids),
# and the risk states its bound and what to do.
RISK = (
    "Eventually consistent: 2 reads on Aurora PostgreSQL read a copy kept current from "
    "DynamoDB via CDC, so each sees a write only after CDC applies it, behind by the "
    "replication lag; a read that must see its own write should read from DynamoDB."
)


def test_the_engineering_report_states_each_tables_source_replicas_cdc_and_risk(report) -> None:
    section = _section_md(render_engineering_report_md(report))
    assert section.startswith("## Sources of truth and replicas (2 written tables)")
    assert SOURCES_OF_TRUTH_INTRO in section
    row = next(line for line in section.splitlines() if line.startswith("| `trailgear.Shipment`"))
    assert row == "| `trailgear.Shipment` | DynamoDB | DynamoDB → Aurora PostgreSQL | 2 |"
    # #1170: one source of truth, so the risk is stated once for every table.
    assert (
        "Every replicated table above has this risk. " + md_text(shared_replica_risk_text())
        in section.splitlines()
    )
    assert report["facts"]["table_sources"]["replicated_tables"][0]["risk"] == RISK
    assert (
        "1 written table runs every query on the source of truth, with no replica: "
        "trailgear.Customer." in section
    )


def test_the_executive_report_leaves_the_table_sources_to_the_engineering_report(
    report,
) -> None:
    """#1020: the executive report names no table, so the per-table sources of
    truth and replicas stay in the engineering report."""
    text = executive_text(report)
    assert "Sources of truth and replicas" not in text
    assert "trailgear.Shipment" not in text and "Eventually consistent" not in text


def test_more_than_the_cap_says_how_many_more() -> None:
    report = _report(
        [
            _row(f"db.t{i:03d}", "dynamodb", {"aurora_postgresql": ["r"]})
            for i in range(TABLE_SOURCES_SHOWN + 2)
        ]
    )
    section = _section_md(render_engineering_report_md(report))
    assert "2 more replicated tables are not shown here." in section
    assert "`table_sources`" in section


def test_split_writes_are_named_not_hidden() -> None:
    row = _row("db.legacy", "dynamodb", {})
    row.update(source_of_truth=None, write_engines=["aurora_postgresql", "dynamodb"])
    section = _section_md(render_engineering_report_md(_report([row])))
    assert "1 table has writes on more than one engine and no single source of truth" in section


@pytest.mark.parametrize("rows", [None, []])
def test_no_section_without_any(rows) -> None:
    report = _report(rows)
    assert MD_HEADING not in render_engineering_report_md(report)


def test_the_renderers_print_the_stored_facts(report) -> None:
    doctored = copy.deepcopy(report)
    doctored["facts"]["table_sources"]["written_table_count"] = 9
    row = doctored["facts"]["table_sources"]["replicated_tables"][0]
    row["risk"] = "Stored risk."
    # #1232: the facts say whether a row shares the one risk sentence; the
    # renderer never decides it from the prose. #2321: it prints the facts' sentence.
    doctored["facts"]["table_sources"]["shared_risk_sentence"] = "Stored shared sentence."
    md = _section_md(render_engineering_report_md(doctored))
    assert "(9 written tables)" in md
    assert "Stored risk." not in md and "Stored shared sentence." in md
    row["shares_replica_risk"] = False
    doctored["facts"]["table_sources"]["shared_risk_sentence"] = None
    md = _section_md(render_engineering_report_md(doctored))
    assert "Stored risk." in md and "Stored shared sentence." not in md


OLD_WORDING = (
    Path(__file__).resolve().parents[3]
    / "src/ui/src/components/__tests__/fixtures/oldWordingTableSources.json"
)


def _old_wording() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(OLD_WORDING.read_text(encoding="utf-8"))
    return data


def test_an_older_reports_rows_get_shares_replica_risk_from_the_facts_layer() -> None:
    """#1232: a facts block stored between a5c40e70 and ca763a01 has the read
    counts but the older risk wording and no ``shares_replica_risk``; the facts
    layer fills it in (False for the older wording) and serves the fixture's block."""
    data = _old_wording()
    stored = copy.deepcopy(data["stored"])
    assert all("shares_replica_risk" not in r for r in stored["replicated_tables"])
    served = report_facts({"facts": {"engines": [], "table_sources": stored}})["table_sources"]
    assert served == data["served"]
    assert [r["shares_replica_risk"] for r in served["replicated_tables"]] == [True, False]
    assert stored == data["stored"]  # the stored block itself is not modified


def test_an_older_report_reads_the_same_in_both_renderers() -> None:
    """The engineering report prints the fixture's expected text for that block;
    SourcesOfTruthCallout.test.js checks the results page prints the same."""
    data = _old_wording()
    report = _report(None)
    report["facts"] = {"engines": [], "table_sources": copy.deepcopy(data["stored"])}
    md = "\n".join(sources_of_truth_md(report))
    expected = data["expected"]
    assert md.count(md_text(expected["shared"])) == 1
    own = expected["own_risk"]
    assert f"- `{own['table']}`: {md_text(own['risk'])}" in md
    assert md.count("Eventually consistent: ") == 2


def test_table_names_and_risk_text_are_escaped() -> None:
    row = _row("db.t<img src=x>|`", 'x"><b>', {"aurora_postgresql": ["<script>|"]})
    md = _section_md(render_engineering_report_md(_report([row])))
    assert "<script>" not in md and "<img" not in md
    line = next(line for line in md.splitlines() if line.startswith("| `db.t"))
    assert line.count(" | ") == 3  # no injected cell


def test_writes_moved_after_approval_are_disclosed_with_their_cause() -> None:
    """#992 round 4: a resumed job's saved split, re-routed after approval, is listed
    with the queries moved after approval, its cause in words."""
    report = _report(None)
    report["post_schema_reroutes"] = [
        {
            "query_id": "w2",
            "from_engine": "aurora_postgresql",
            "to_engine": "dynamodb",
            "reason": "Re-routed to one source of truth on DynamoDB",
            "reason_category": "source_of_truth",
            "score_before": 70,
            "score_after": 80,
            "approved_version": 1,
        }
    ]
    report["facts"] = compute_report_facts(report)
    md = render_engineering_report_md(report)
    assert (
        "After you approved the assignment (version 1), 1 write moved so that each "
        "table's writes land on one engine, its source of truth." in md
    )
    assert "its table's writes were moved to one engine, its source of truth" in md
    assert "schema design review found" not in md


# #2321: the results page puts the shared risk under its Table owners table, which
# lists every mapped table. "Every table above" was true only in the engineering
# report, whose table lists the replicated tables, so both now print the one sentence
# facts.table_sources.shared_risk_sentence names, with the same meaning in both.
def _ui_strings() -> dict[str, str]:
    en = Path(__file__).resolve().parents[3] / "src/ui/src/locales/en.json"
    strings: dict[str, str] = json.loads(en.read_text(encoding="utf-8"))
    return strings


def test_the_shared_risk_sentence_names_the_replicated_tables_once_in_the_facts() -> None:
    risk = shared_replica_risk_text()
    one = _report([_shipment()])["facts"]["table_sources"]
    assert one["shared_risk_sentence"] == f"Every replicated table above has this risk. {risk}"
    mixed = _report([_shipment(), _row("trailgear.Order", "dynamodb", {"documentdb": ["r9"]})])
    sources = mixed["facts"]["table_sources"]
    sources["replicated_tables"][1]["risk"] = "Older wording."
    del sources["shared_risk_sentence"]
    for row in sources["replicated_tables"]:
        row.pop("shares_replica_risk")
    served = report_facts({"facts": {"engines": [], "table_sources": sources}})["table_sources"]
    assert served["shared_risk_sentence"] == (
        f"Every other replicated table above has this risk. {risk}"
    )
    none = _report([_row("trailgear.Customer", "aurora_postgresql", {})])["facts"]
    assert none["table_sources"]["shared_risk_sentence"] is None


def test_the_engineering_report_prints_the_facts_shared_risk_sentence() -> None:
    report = _report([_shipment()])
    sentence = report["facts"]["table_sources"]["shared_risk_sentence"]
    lines = _section_md(render_engineering_report_md(report)).splitlines()
    assert md_text(sentence) in lines


def test_the_results_page_keeps_no_copy_of_the_shared_risk_words() -> None:
    """The UI prints facts.table_sources.shared_risk_sentence, so en.json holds no
    second copy of the lead or the risk that could drift from it."""
    en = _ui_strings()
    prefix = "analysis-results.sources-of-truth."
    for key in ("risk-lead", "risk-lead-other", "risk-one-source", "risk-many-sources"):
        assert prefix + key not in en, key
    assert not any("has this risk" in v for v in en.values())
    assert en[prefix + "intro"] == SOURCES_OF_TRUTH_INTRO


def _shipment() -> dict[str, Any]:
    return _row("trailgear.Shipment", "dynamodb", {"aurora_postgresql": ["r1", "r2"]})


def _mapping(table: str, owner: str, basis: str, readers: list[str], reads: int) -> dict[str, Any]:
    return {
        "source_table": table,
        "recommended_database": owner,
        "owner": owner,
        "owner_basis": basis,
        "also_read_by": readers,
        "cached_by": None,
        "replica_read_count": reads,
    }


def _named_sources(text: str) -> set[str]:
    """Every engine (or the source database) a sentence names."""
    names = [*ENGINE_DISPLAY_NAMES.values(), SOURCE_DATABASE_NAME]
    return {n for n in names if n.casefold() in text.casefold()}


def test_the_shared_risk_sentence_is_true_under_every_row_with_a_cdc_copy() -> None:
    """#2321 review: the results page prints the shared sentence under every Table owners
    row, and a row with a CDC to engine is a replicated table there. A table nothing
    writes is owned by the relational step and can still have CDC to DynamoDB, so a
    sentence that names the written tables' source (DynamoDB) is false for it. The
    sentence names no source: each copy is kept current from the table's owner."""
    report = _report(
        [_shipment()],
        [
            _mapping("trailgear.Shipment", "dynamodb", "writes", ["aurora_postgresql"], 2),
            _mapping("trailgear.Region", "aurora_postgresql", "retained", ["dynamodb"], 1),
        ],
    )
    facts = report_facts(report)
    sentence = facts["table_sources"]["shared_risk_sentence"]
    lines = facts["table_owner_lines"]["table_lines"]
    written = {r["table"] for r in report["table_sources"]}
    unwritten = [r for r in lines if r["table"] not in written and r["also_read_by"]]
    assert [(r["owner"], r["also_read_by"]) for r in unwritten] == [
        ("aurora_postgresql", ["dynamodb"])
    ]
    for line in lines:
        if line["also_read_by"]:
            named = _named_sources(sentence)
            assert named <= {display_engine(line["owner"])}, (line["table"], named)
    assert "kept current from the table's owner" in sentence
    lines_md = _section_md(render_engineering_report_md(report)).splitlines()
    assert md_text(sentence) in lines_md


def test_the_shared_risk_sentence_is_the_same_for_any_sources_and_any_number_of_rows() -> None:
    """Past the 25 listed rows, or with several sources of truth, the sentence still
    names no source, so it holds for a row the engineering report does not list."""
    one = _report([_shipment()])["facts"]["table_sources"]["shared_risk_sentence"]
    many = _report(
        [
            _row(
                f"db.t{i:03d}", "documentdb" if i % 2 else "dynamodb", {"aurora_postgresql": ["r"]}
            )
            for i in range(TABLE_SOURCES_SHOWN + 3)
        ]
    )["facts"]["table_sources"]["shared_risk_sentence"]
    assert many == one
    assert _named_sources(one) == set()


# #2459: a table nothing writes can have a CDC copy while no written table does. The
# Table owners row shows CDC to and a replica read, so a risk note has to be there.
def _only_an_unwritten_table_has_a_cdc_copy() -> dict[str, Any]:
    return _report(
        [_row("trailgear.Order", "dynamodb", {})],
        [
            _mapping("trailgear.Order", "dynamodb", "writes", [], 0),
            _mapping("trailgear.Region", "aurora_postgresql", "retained", ["dynamodb"], 1),
        ],
    )


def test_the_shared_risk_sentence_exists_when_only_an_unwritten_table_has_a_cdc_copy() -> None:
    facts = _only_an_unwritten_table_has_a_cdc_copy()["facts"]
    assert facts["table_sources"]["replicated_tables"] == []
    assert facts["table_sources"]["shared_risk_sentence"] == (
        f"Every table another engine reads through CDC has this risk. {shared_replica_risk_text()}"
    )


@pytest.mark.passes_on_main
def test_a_table_without_a_cdc_copy_anywhere_gets_no_sentence() -> None:
    report = _report(
        [_row("trailgear.Order", "dynamodb", {})],
        [_mapping("trailgear.Order", "dynamodb", "writes", [], 0)],
    )
    assert report["facts"]["table_sources"]["shared_risk_sentence"] is None


def test_the_engineering_report_prints_the_note_when_only_an_unwritten_table_has_a_cdc_copy() -> (
    None
):
    report = _only_an_unwritten_table_has_a_cdc_copy()
    sentence = report["facts"]["table_sources"]["shared_risk_sentence"]
    section = _section_md(render_engineering_report_md(report))
    assert md_text(sentence) in section.splitlines()
    assert "above" not in sentence
    assert "| Table |" not in section


def test_an_old_block_without_the_sentence_gets_it_at_read_time_from_the_owner_lines() -> None:
    stored = copy.deepcopy(_only_an_unwritten_table_has_a_cdc_copy())
    sentence = stored["facts"]["table_sources"]["shared_risk_sentence"]
    assert sentence
    for stale in (None, ""):
        stored["facts"]["table_sources"]["shared_risk_sentence"] = stale
        served = report_facts(stored)["table_sources"]["shared_risk_sentence"]
        assert served == sentence
    del stored["facts"]["table_sources"]["shared_risk_sentence"]
    assert report_facts(stored)["table_sources"]["shared_risk_sentence"] == sentence


def test_the_section_shows_with_no_written_table_and_says_what_it_holds() -> None:
    """#2459 review: the gate lets the section through on the sentence alone, so the
    heading must not read "(0 written tables)"."""
    section = _section_md(render_engineering_report_md(_report([], _mappings_unwritten_only())))
    assert section.splitlines()[0] == f"{MD_HEADING} (copies of tables nothing writes)"
    assert "0 written" not in section
    sentence = _report([], _mappings_unwritten_only())["facts"]["table_sources"]
    assert md_text(sentence["shared_risk_sentence"]) in section.splitlines()


def _mappings_unwritten_only() -> list[dict[str, Any]]:
    return [_mapping("trailgear.Region", "aurora_postgresql", "retained", ["dynamodb"], 1)]


def test_a_listed_row_with_its_own_risk_does_not_make_the_sentence_cover_no_table() -> None:
    """#2459 review: an old report whose listed rows each keep their own risk, and no
    table nothing writes has a CDC copy, gets no sentence. The listed row's own owner
    line has also_read_by too, and must not count."""
    stored = copy.deepcopy(
        _report(
            [_shipment(), _row("trailgear.Order", "dynamodb", {})],
            [
                _mapping("trailgear.Shipment", "dynamodb", "writes", ["aurora_postgresql"], 2),
                _mapping("trailgear.Order", "dynamodb", "writes", [], 0),
            ],
        )
    )
    sources = stored["facts"]["table_sources"]
    for row in sources["replicated_tables"]:
        row["shares_replica_risk"] = False
    sources["shared_risk_sentence"] = None
    assert report_facts(stored)["table_sources"]["shared_risk_sentence"] is None


def test_the_other_lead_names_a_table_past_the_listed_rows() -> None:
    stored = copy.deepcopy(
        _report(
            [_shipment()],
            [
                _mapping("trailgear.Shipment", "dynamodb", "writes", ["aurora_postgresql"], 2),
                _mapping("trailgear.Region", "aurora_postgresql", "retained", ["dynamodb"], 1),
            ],
        )
    )
    sources = stored["facts"]["table_sources"]
    sources["replicated_tables"][0]["shares_replica_risk"] = False
    sources["shared_risk_sentence"] = None
    sentence = report_facts(stored)["table_sources"]["shared_risk_sentence"]
    assert sentence.startswith("Every other table another engine reads through CDC has this risk.")


def test_the_unwritten_cdc_fixture_holds_the_sentence_synthesis_writes() -> None:
    """The results page test reads this block, so it cannot drift from Python."""
    fixture = json.loads(_UNWRITTEN_CDC_FIXTURE.read_text())
    expected = _only_an_unwritten_table_has_a_cdc_copy()["facts"]
    assert fixture["table_sources"] == expected["table_sources"]
    assert fixture["table_owner_lines"] == expected["table_owner_lines"]


_UNWRITTEN_CDC_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "src/ui/src/components/__tests__/fixtures/unwrittenCdcTableSources.json"
)
