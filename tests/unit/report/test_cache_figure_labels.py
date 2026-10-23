"""Each cache figure says what it counts, and the change is stated once (#1252, ADR-032).

On the WordPress sample the approval gate has 20 cache candidates (hot reads routing put under
the cache overlay) and the ElastiCache design covers 14 of them: 14 designed cached reads, the
other 6 staying on their owner engine. Every deliverable labels the figure it shows, reads it from
``facts.cache_layer``, and states the change in one short sentence where it happens.
"""

from __future__ import annotations

import copy
from typing import Any

from src.report.engineering_report import render_engineering_report_md
from src.report.executive_report import render_executive_report_pdf
from src.report.facts import report_facts
from src.report.printed_summary import render_final_summary, render_gate_block
from src.shared.report_facts import cache_layer_facts
from tests.executive_pdf import text as pdf_text
from tests.unit.report.test_cache_layer_deliverables import _report, _shrink_report

SENTENCE = "20 cache candidates, 14 designed; 6 stay on their owner engine."
GATE = {
    "before_distribution": {"dynamodb": 20},
    "after_distribution": {"dynamodb": 20},
    "cache_overlay": {"query_count": 20, "call_share_percent": 83.4},
    "schema_design_engines": ["dynamodb", "elasticache"],
}


def _final(report: dict[str, Any]) -> str:
    return render_final_summary(
        report, database_name="wordpress", job_id="job-1", deliverables_dir="d/", deliverables=[]
    )


def _covers_every_candidate() -> dict[str, Any]:
    """The design covers all 20 candidates: nothing dropped, no change sentence."""
    report = _report()
    report["cache_overlay"] = {
        **{k: v for k, v in report["cache_overlay"].items() if k != "dropped_query_ids"},
        "candidate_query_count": 20,
        "candidate_call_share_percent": 83.4,
        "dropped_query_ids": [],
        "notes": [],
        "safety_net_notes": [],
    }
    return report


class TestFacts:
    def test_candidates_and_designed_reads_are_two_labelled_facts(self) -> None:
        assert cache_layer_facts(_shrink_report()) == {
            "engine": "elasticache",
            "candidate_read_count": 20,
            "candidate_call_share_percent": 83.4,
            "designed_read_count": 14,
            "designed_call_share_percent": 76.1,
            "owner_only_read_count": 6,
        }
        assert report_facts(_shrink_report())["cache_layer"]["owner_only_read_count"] == 6

    def test_a_design_that_covers_every_candidate_has_none_left_on_the_owner(self) -> None:
        facts = cache_layer_facts(_covers_every_candidate())
        assert facts is not None
        assert (facts["candidate_read_count"], facts["designed_read_count"]) == (20, 20)
        assert facts["owner_only_read_count"] == 0

    def test_a_report_with_no_drop_written_before_the_candidates_reads_as_no_change(self) -> None:
        report = _report()
        report["cache_overlay"].pop("candidate_query_count", None)
        report["cache_overlay"]["dropped_query_ids"] = []
        facts = cache_layer_facts(report)
        assert facts is not None
        assert facts["candidate_read_count"] == facts["designed_read_count"] == 20
        assert facts["candidate_call_share_percent"] == 83.4

    def test_an_older_report_that_recorded_a_drop_counts_the_dropped_reads_back_in(self) -> None:
        report = _shrink_report()
        for key in ("candidate_query_count", "candidate_call_share_percent"):
            del report["cache_overlay"][key]
        facts = cache_layer_facts(report)
        assert facts is not None
        assert (facts["candidate_read_count"], facts["owner_only_read_count"]) == (20, 6)
        assert facts["candidate_call_share_percent"] is None  # the share was never stored
        assert report_facts(report)["cache_layer"] == facts
        assert SENTENCE in _final(report)

    def test_no_overlay_no_fact(self) -> None:
        assert cache_layer_facts({}) is None

    def test_a_full_drop_keeps_the_candidates(self) -> None:
        report = _shrink_report()
        report["cache_overlay"] = {
            "candidate_query_count": 20,
            "candidate_call_share_percent": 83.4,
            "safety_net_notes": [],
        }
        facts = cache_layer_facts(report)
        assert facts is not None
        assert (facts["designed_read_count"], facts["owner_only_read_count"]) == (0, 20)


class TestGate:
    def test_the_gate_says_cache_candidates(self) -> None:
        text = render_gate_block(GATE, database_name="wordpress", job_id="job-1")
        assert "ElastiCache has 20 cache candidates (83.4% of calls)" in text
        assert "cached reads" not in text


class TestFinalSummary:
    def test_the_engine_line_says_designed_cached_reads(self) -> None:
        text = _final(_shrink_report())
        assert "14 designed cached reads · 76.1% of calls" in text
        assert "20 cached reads" not in text and "14 cached reads" not in text

    def test_the_change_is_one_short_sentence_with_both_numbers_stated_once(self) -> None:
        text = _final(_shrink_report())
        assert text.count(SENTENCE) == 1
        assert text.count("owner engine") == 1

    def test_no_change_sentence_when_the_design_covers_every_candidate(self) -> None:
        text = _final(_covers_every_candidate())
        assert "Cache layer note" not in text
        assert "stay on their owner engine" not in text


class TestEngineeringReport:
    def test_labelled_figures_and_the_sentence_once(self) -> None:
        md = render_engineering_report_md(_shrink_report())
        assert "14 designed cached reads · 76.1% of calls" in md
        assert md.count(SENTENCE) == 1
        assert "20 cached reads" not in md

    def test_no_sentence_when_the_design_covers_every_candidate(self) -> None:
        md = render_engineering_report_md(_covers_every_candidate())
        assert "cache candidates" not in md


class TestExecutivePdf:
    def test_pdf_shows_the_labelled_figure_and_the_sentence_once(self) -> None:
        text = pdf_text(render_executive_report_pdf(_shrink_report()))
        assert "14 designed cached reads · 76.1% of calls" in text
        assert text.count(SENTENCE) == 1

    def test_pdf_has_no_sentence_when_the_design_covers_every_candidate(self) -> None:
        text = pdf_text(render_executive_report_pdf(_covers_every_candidate()))
        assert "cache candidates" not in text


def test_the_deliverables_agree_on_the_figures() -> None:
    report = _shrink_report()
    facts = report_facts(copy.deepcopy(report))["cache_layer"]
    figure = f"{facts['designed_read_count']} designed cached reads"
    for text in (
        _final(report),
        render_engineering_report_md(report),
        pdf_text(render_executive_report_pdf(report)),
    ):
        assert figure in text
        assert f"{facts['candidate_read_count']} cache candidates" in text


def test_the_change_sentence_is_read_from_the_stored_fact_not_recomputed() -> None:
    """ADR-032: with a stored facts block, every deliverable states facts.cache_layer's numbers."""
    report = _shrink_report()
    facts = report_facts(copy.deepcopy(report))
    facts["cache_layer"] = {
        **facts["cache_layer"],
        "candidate_read_count": 30,
        "designed_read_count": 10,
        "owner_only_read_count": 20,
    }
    report["facts"] = facts
    sentence = "30 cache candidates, 10 designed; 20 stay on their owner engine."
    assert sentence in _final(report)
    assert sentence in render_engineering_report_md(report)
    assert sentence in pdf_text(render_executive_report_pdf(report))
