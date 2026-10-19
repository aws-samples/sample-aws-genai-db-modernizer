"""The executive report: a landscape briefing built from report.json's facts (#1020).

Covers what does not need the pipeline: byte-identical renders, the page set and
its order, the figures read from the stored facts, no table name and no SQL, and
that nothing is clipped however long the content (it wraps, shrinks or continues
on the next page). ``tests/e2e/test_report_pdf.py`` runs the same checks on the
three samples.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from src.report import deliverables, executive_report
from src.report.executive_report import table_names, without_table_names
from src.report.facts import report_facts
from src.report.formatting import _cost_baseline_text, _fmt_usd
from src.report.risks import NO_OPEN_RISKS_CAVEAT
from tests.executive_pdf import (
    body_text,
    outline,
    outside_the_page,
    page_texts,
    printed,
    sql_hits,
    table_name_hits,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[3]
FIXTURE_NAMES = sorted(p.name for p in FIXTURES.glob("*report*.json"))


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _render(report: dict[str, Any]) -> bytes:
    return executive_report.render_executive_report_pdf(report)


def _body(report: dict[str, Any]) -> str:
    return body_text(_render(report), str(report.get("database_name") or "?"))


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return _fixture("wordpress_no_schema_design_report.json")


@pytest.fixture(scope="module")
def e2e09() -> dict[str, Any]:
    """Retained, migration targets, a cache layer, a search read model, 28 risks."""
    return _fixture("e2e09_report.json")


# -- the document ---------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_two_renders_are_byte_identical(name: str) -> None:
    first = _render(_fixture(name))
    assert first == _render(_fixture(name))
    assert first.startswith(b"%PDF")


def test_the_document_carries_no_wall_clock_date(report: dict[str, Any]) -> None:
    meta = PdfReader(io.BytesIO(_render(report))).metadata
    assert meta is not None
    assert meta.creation_date is not None and meta.creation_date.year == 2000  # invariant mode
    assert meta.author == executive_report.PRODUCT_NAME


def test_every_page_is_a_landscape_slide(e2e09: dict[str, Any]) -> None:
    pages = PdfReader(io.BytesIO(_render(e2e09))).pages
    sizes = {(float(p.mediabox.width), float(p.mediabox.height)) for p in pages}
    assert sizes == {(executive_report.PAGE_W, executive_report.PAGE_H)}
    assert executive_report.PAGE_W > executive_report.PAGE_H


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_section_once_in_order(name: str) -> None:
    """Older saved reports (no facts block, no waves) render every section too."""
    assert outline(_render(_fixture(name))) == list(executive_report.SECTION_HEADINGS)


def test_an_empty_report_renders_every_section() -> None:
    data = _render({"database_name": "x"})
    assert outline(data) == list(executive_report.SECTION_HEADINGS)
    assert outside_the_page(data) == []


def test_the_cover_and_every_page_after_it(e2e09: dict[str, Any]) -> None:
    pages = page_texts(_render(e2e09))
    assert executive_report.PRODUCT_NAME in pages[0]
    assert executive_report.REPORT_NAME in pages[0]
    assert f"Source database {e2e09['database_name']}" in pages[0]
    for n, page in enumerate(pages, start=1):
        assert page.count(executive_report.COPYRIGHT) == 1, n
        if n > 1:
            assert f"Page {n} of {len(pages)}" in page, n
            assert any(h in page for h in executive_report.SECTION_HEADINGS), page[:80]


def test_the_copyright_line_is_the_license_files_line() -> None:
    license_lines = (REPO / "LICENSE").read_text(encoding="utf-8").splitlines()
    assert executive_report.COPYRIGHT in license_lines
    assert (
        executive_report.FOOTER == f"{executive_report.COPYRIGHT} For informational purposes only."
    )


def test_the_closing_pointer_ends_the_document_and_names_real_files(
    report: dict[str, Any],
) -> None:
    pages = page_texts(_render(report))
    assert executive_report.CLOSING_POINTER in pages[-1]
    named = {w for w in executive_report.CLOSING_POINTER.replace(",", " ").split() if "." in w}
    assert named == {"engineering-report.md", "index.md"}
    assert named <= set(deliverables.FOLDER_FILENAMES)


# -- the figures are the stored facts --------------------------------------------


def test_the_tiles_print_the_stored_facts(e2e09: dict[str, Any]) -> None:
    """Changing a stored figure changes the printed one: no tile recounts."""
    rep = copy.deepcopy(e2e09)
    rep["facts"] = copy.deepcopy(report_facts(e2e09))
    rep["facts"].update(
        projected_monthly_cost_usd=1234.5,
        engine_count=9,
        moving_query_count=42,
        in_scope_query_count=99,
        tables_migrate=7,
        open_risk_count=31,
        resolved_risk_count=17,
    )
    text = _body(rep)
    assert "$1,234.50 Projected monthly cost" in text
    assert "9 Engines in the target architecture" in text
    assert "42 of 99 queries move to a new engine 7 tables migrate" in text
    assert "31 Open risks" in text
    assert "17 Resolved by the assignment" in text


def test_every_engine_cost_and_the_total(e2e09: dict[str, Any]) -> None:
    facts = report_facts(e2e09)
    text = _body(e2e09)
    for e in facts["engines"]:
        if e["monthly_cost_usd"] is not None:
            assert _fmt_usd(e["monthly_cost_usd"]) in text, e["engine"]
    assert f"Total {_fmt_usd(facts['engine_cost_total_usd'])}" in text


@pytest.mark.parametrize("known", [False, True])
def test_the_cost_baseline_text(report: dict[str, Any], known: bool) -> None:
    rep = copy.deepcopy(report)
    rep["tco_analysis"]["current_cost_known"] = known
    rep["tco_analysis"]["current_monthly_cost"] = 900.0 if known else 0.0
    rep["tco_analysis"]["savings_percent"] = 12.5
    current, savings = _cost_baseline_text(rep["tco_analysis"])
    text = _body(rep)
    # #949 (#1028): an unknown baseline's two lines are left out, not "not provided".
    assert (f"Current monthly (source): {current}" in text) is known
    assert (f"Savings: {savings}" in text) is known
    assert "source cost not provided" not in text
    assert "not available" not in text


def test_only_a_retired_engine_saving_is_printed(report: dict[str, Any]) -> None:
    """From ``retired_engine_savings`` (an engine the source runs today), never from
    ``eliminated_engine_costs`` (a dropped candidate, #949)."""
    rep = copy.deepcopy(report)
    rep["tco_analysis"]["retired_engine_savings"] = [
        {"database": "elasticache", "monthly_cost_usd": 271.8}
    ]
    rep["tco_analysis"]["eliminated_engine_costs"] = [
        {"database": "documentdb", "monthly_cost_usd": 4242.42}
    ]
    text = _body(rep)
    assert "Retired by this plan: ElastiCache (runs today), $271.80/mo" in text
    assert "4242.42" not in text


def test_the_engines_removed_by_the_reality_check(e2e09: dict[str, Any]) -> None:
    rep = copy.deepcopy(e2e09)
    rep["facts"] = copy.deepcopy(report_facts(e2e09))
    rep["facts"]["removed_engines"] = [
        {"engine": "opensearch", "moved_to": ["aurora_postgresql"], "reasons": []}
    ]
    text = _body(rep)
    assert "OpenSearch Removed by the reality check" in text
    assert "OpenSearch: queries moved to Aurora PostgreSQL" in text
    rep["facts"]["removed_engines"] = []
    assert "None Removed by the reality check" in _body(rep)


def test_each_wave_with_its_counts_and_reason(report: dict[str, Any]) -> None:
    text = _body(report)
    for w in report["migration_waves"]:
        assert f"WAVE {w['wave']}" in text
        reason = without_table_names(w["rationale"], table_names(report))
        assert printed(f"Why: {reason}") in text
        assert f"{w['query_count']} quer" in text


# -- no table names, no traceability ---------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_no_table_name_and_no_sql(name: str) -> None:
    rep = _fixture(name)
    text = _body(rep)
    assert table_name_hits(text, table_names(rep)) == []
    assert sql_hits(text) == []


def test_the_older_model_run_fixture_prints_none_of_its_identifiers(
    e2e09: dict[str, Any],
) -> None:
    """Review of #1030: model-written risk text named a function, a view and columns and
    quoted predicates; none of it reaches the executive report."""
    text = _body(e2e09)
    for absent in (
        "api_key_auth_check",
        "topic_post_ordering",
        "revoked_at",
        "IS NULL",
        "sort_order",
        "user_batch_lookup",
    ):
        assert absent not in text, absent
    assert "procedure." not in text and "function:" not in text and "view:" not in text
    # RISK-019 loses its kind label, its name and the IN-list sentence, keeps the rest.
    assert "On cache miss for any user, fetch from DB and populate Redis." in text


def test_the_summary_is_not_printed_it_names_tables(report: dict[str, Any]) -> None:
    rep = copy.deepcopy(report)
    rep["summary"] = rep["summary_deterministic"] = "Top query groups: wordpress.wp_posts."
    assert "Top query groups" not in _body(rep)


def test_no_traceability_appendix_and_no_table_list(e2e09: dict[str, Any]) -> None:
    text = _body(e2e09).lower()
    for absent in ("traceability", "appendix", "per-query routing", "migration map"):
        assert absent not in text


class TestWithoutTableNames:
    NAMES = {"shop.orders", "orders", "shop.order_items", "order_items", "Product"}

    def test_a_parenthetical_list_goes_the_sentence_stays(self) -> None:
        text = "15 tables (shop.orders, shop.order_items) stay in sync via CDC."
        assert without_table_names(text, self.NAMES) == "15 tables stay in sync via CDC."

    def test_a_sentence_is_cut_back_to_the_clause_before_its_list(self) -> None:
        text = "Schema design gap: 2 queries have no access pattern: UPDATE `orders` SET x = ?"
        assert without_table_names(text, self.NAMES) == (
            "Schema design gap: 2 queries have no access pattern."
        )

    def test_a_sentence_with_no_leading_clause_is_left_out(self) -> None:
        text = "Keep it simple. Move order_items first. Then verify."
        assert without_table_names(text, self.NAMES) == "Keep it simple. Then verify."

    def test_a_plain_word_name_is_prose_unless_it_reads_as_an_identifier(self) -> None:
        text = "The customer's own product choice reorders nothing."
        assert without_table_names(text, self.NAMES) == text
        for named in ("Reads `Product` rows.", "Reads shop.Product rows.", "Reads FROM orders."):
            assert without_table_names(named + " Keep it.", self.NAMES) == "Keep it.", named
        assert without_table_names("A table: orders is hot. Keep it.", self.NAMES) == "Keep it."

    def test_names_match_in_any_case_and_with_any_schema_prefix(self) -> None:
        for text in (
            "Locked by ORDER_ITEMS writes. Keep it.",
            "Hot rows in dbo.order_items today. Keep it.",
            "Hot rows in [dbo].[order_items] today. Keep it.",
            'Hot rows in "order_items" today. Keep it.',
        ):
            assert without_table_names(text, self.NAMES) == "Keep it.", text

    def test_any_snake_case_word_is_an_identifier(self) -> None:
        assert without_table_names("Check revoked_at on reads. Keep it.", set()) == "Keep it."

    def test_sql_in_any_case_goes(self) -> None:
        for text in (
            "Slow: SELECT a FROM b",
            "Slow: select post_title from x where post_status = 'publish'",
            "Count COUNT(*) of approved comments per post.",
            "Application must check that the token IS NULL first.",
            "Filter where status = ? on reads.",
            "Uses [dbo].[Thing] daily.",
        ):
            assert without_table_names(text, set()) == "", text

    def test_the_kind_label_and_its_name_go_the_sentence_stays(self) -> None:
        text = "procedure: user_batch_lookup — Many queries call it in a loop. Batch them."
        assert without_table_names(text, set()) == "Many queries call it in a loop. Batch them."

    def test_an_abbreviation_does_not_end_a_sentence(self) -> None:
        names = {"wp_x"}
        assert without_table_names("Move reads, e.g. wp_x and others. Keep writes.", names) == (
            "Keep writes."
        )
        assert without_table_names("Mr. Smith said wp_x is hot. OK.", names) == "OK."
        text = "Up to 100 items (86 incl. shared) cost $15.20/month, i.e. little. Fine."
        assert without_table_names(text, names) == text

    def test_a_clause_too_short_to_read_is_dropped_not_kept(self) -> None:
        assert without_table_names("Slow: SELECT a FROM b. Keep it.", set()) == "Keep it."
        assert without_table_names("Two queries slow: SELECT 1. Keep it.", set()) == "Keep it."

    def test_empty_and_clean_input(self) -> None:
        assert without_table_names(None, self.NAMES) == ""
        assert without_table_names("", self.NAMES) == ""
        assert without_table_names("Nothing (wave 1) to drop.", self.NAMES) == (
            "Nothing (wave 1) to drop."
        )


# -- nothing clipped -----------------------------------------------------------------

LONG = (
    "This sentence is long on purpose so the card it sits in has to wrap it over many "
    "lines and, repeated, over more than one page. "
)


def _stress(report: dict[str, Any]) -> dict[str, Any]:
    """Far more, and far longer, content than any page holds."""
    rep = copy.deepcopy(report)
    rep.pop("facts", None)
    rep["database_name"] = "a_database_name_long_enough_to_test_the_cover_" * 3
    rep["risk_assessment"]["risks"] = [
        {
            "risk_id": f"RISK-{i:03d}",
            "risk_type": "MIGRATION_COMPLEXITY",
            "severity": ("CRITICAL", "HIGH", "MEDIUM", "LOW")[i % 4],
            "description": f"[dynamodb] Risk {i} TAIL-{i}-A. " + LONG * (1 + i % 3),
            "mitigation": f"Mitigate risk {i} with care TAIL-{i}-M.",
        }
        for i in range(25)
    ]
    rep["risk_assessment"]["risks"][0]["description"] = "[dynamodb] " + LONG * 40 + "END-OF-RISK."
    rep["risk_assessment"]["mitigation_strategies"] = [f"Step {i}: {LONG}" for i in range(15)]
    for w in rep["migration_waves"]:
        w["rationale"] = LONG * 6 + f"WAVE-{w['wave']}-WHY-END."
        w["gate"] = "Parity confirmed on every shard. " * 30 + "FINAL-CRITERION-ENDS-HERE."
    rep["tied_queries"] = [
        {
            "query_id": f"q{i}",
            "tied_engines": ["documentdb", "dynamodb"],
            "assigned_engine": "dynamodb",
        }
        for i in range(40)
    ]
    rep["tco_analysis"]["retired_engine_savings"] = [
        {"database": e, "monthly_cost_usd": 1234567.89}
        for e in ("elasticache", "opensearch", "documentdb", "aurora_mysql")
    ]
    return rep


def test_nothing_is_drawn_outside_the_page_and_nothing_is_cut(
    report: dict[str, Any],
) -> None:
    rep = _stress(report)
    data = _render(rep)
    assert outside_the_page(data) == []
    text = body_text(data, rep["database_name"])
    assert "…" not in text and "..." not in text
    for i in range(1, 25):
        assert f"TAIL-{i}-A." in text and f"Mitigate risk {i} with care TAIL-{i}-M." in text
    assert "END-OF-RISK." in text
    for w in rep["migration_waves"]:
        assert f"WAVE-{w['wave']}-WHY-END." in text
        assert "FINAL-CRITERION-ENDS-HERE." in text
    assert printed(f"Step 14: {LONG}") in text
    assert "Retired by this plan:" in text and text.count("$1,234,567.89/mo") == 4
    assert executive_report.CLOSING_POINTER in page_texts(data)[-1]


def test_a_section_too_long_for_one_page_continues_under_its_title(
    report: dict[str, Any],
) -> None:
    pages = page_texts(_render(_stress(report)))
    assert len(pages) > len(page_texts(_render(report)))
    for heading in (executive_report.H_RISKS, executive_report.H_NEXT, executive_report.H_WAVES):
        assert any(heading + executive_report.CONTINUED in p for p in pages), heading


def test_a_card_moves_whole_to_the_next_page_when_it_fits_one(report: dict[str, Any]) -> None:
    """Short cards are never split between two pages: each risk header and its text
    are on one page."""
    pages = page_texts(_render(_stress(report)))
    for i in range(1, 25):
        on = [n for n, p in enumerate(pages) if f"RISK-{i:03d}" in p and "Risk" in p]
        tail = [n for n, p in enumerate(pages) if f"TAIL-{i}-M." in p]
        assert on and on == tail, (i, on, tail)


def test_a_long_word_wraps_inside_its_card(report: dict[str, Any]) -> None:
    rep = copy.deepcopy(report)
    rep["risk_assessment"]["risks"][0]["mitigation"] = "x" * 900
    assert outside_the_page(_render(rep)) == []


# -- risks and decisions ---------------------------------------------------------------


def test_a_report_with_no_open_risk_prints_the_caveat() -> None:
    rep = {"database_name": "x", "risk_assessment": {"overall_risk_level": "LOW", "risks": []}}
    text = _body(rep)
    assert normalize_caveat() in text
    assert executive_report.RISK_RULE_NOTE not in text


def normalize_caveat() -> str:
    return " ".join(NO_OPEN_RISKS_CAVEAT.split())


def test_open_risks_print_how_the_level_is_derived(e2e09: dict[str, Any]) -> None:
    text = _body(e2e09)
    assert executive_report.RISK_RULE_NOTE in text
    level = e2e09["risk_assessment"]["overall_risk_level"]
    assert f"{level} Overall risk" in text


def test_the_pending_engine_choices(e2e09: dict[str, Any]) -> None:
    rep = copy.deepcopy(e2e09)
    rep.pop("facts", None)
    rep["tied_queries"] = [
        {
            "query_id": "q1",
            "tied_engines": ["dynamodb", "documentdb"],
            "assigned_engine": "dynamodb",
        },
        {
            "query_id": "q2",
            "tied_engines": ["documentdb", "dynamodb"],
            "assigned_engine": "dynamodb",
        },
    ]
    text = _body(rep)
    assert "Unresolved engine ties (2 queries)" in text
    assert executive_report.TIED_QUERIES_NOTE in text
    assert text.count("Tied between: DocumentDB and DynamoDB") == 1
    assert "q1" not in text.split(executive_report.H_DECISIONS, 1)[1]
    assert executive_report.L_NO_DECISIONS not in text


def test_no_pending_choice_says_so(e2e09: dict[str, Any]) -> None:
    rep = copy.deepcopy(e2e09)
    rep.pop("facts", None)
    rep["tied_queries"] = []
    rep["post_schema_reroutes"] = []
    assert executive_report.L_NO_DECISIONS in _body(rep)


def test_queries_moved_after_approval_are_a_decision(e2e09: dict[str, Any]) -> None:
    from src.report.engine_choices import REROUTE_OVERRIDE_TEXT

    rep = copy.deepcopy(e2e09)
    rep.pop("facts", None)
    rep["post_schema_reroutes"] = [
        {
            "query_id": "q2",
            "from_engine": "dynamodb",
            "to_engine": "aurora_mysql",
            "reason_category": "client_side_join",
            "score_before": 80,
            "score_after": 70,
            "approved_version": 2,
        }
    ]
    text = _body(rep)
    assert "Moved after approval (1 query)" in text
    assert " ".join(REROUTE_OVERRIDE_TEXT.split()) in text
