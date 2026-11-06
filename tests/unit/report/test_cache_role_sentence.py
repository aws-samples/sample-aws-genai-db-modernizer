# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cache layer is stated as the assessment's recommendation, with its reason (#399).

Cache-aside in front of a durable owner is what this tool recommends for hot, small
reads, not a limit of ElastiCache: ElastiCache for Valkey 9.0 and later offers
durability on Multi-AZ clusters, which the tool does not use as a system of record
today (#401). The sentence is one fact, ``facts.cache_layer.role_sentence``, and every
deliverable prints it from there (ADR-032).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.report.engineering_report import render_engineering_report_md
from src.report.executive_report import render_executive_report_pdf
from src.report.facts import report_facts
from src.report.printed_summary import render_final_summary
from src.shared.cache_role import cache_role_sentence
from src.shared.report_facts import cache_layer_facts
from tests.executive_pdf import text as pdf_text
from tests.unit.report.test_cache_layer_deliverables import _report, _shrink_report

STORED = "Stored cache role sentence, read from the facts."


def _final(report: dict[str, Any]) -> str:
    return render_final_summary(
        report, database_name="wordpress", job_id="job-1", deliverables_dir="d/", deliverables=[]
    )


def _deliverables(report: dict[str, Any]) -> dict[str, str]:
    return {
        "final summary": _final(report),
        "engineering report": render_engineering_report_md(report),
        "executive report": " ".join(pdf_text(render_executive_report_pdf(report)).split()),
    }


class TestTheSentence:
    def test_it_is_a_recommendation_with_its_reason(self) -> None:
        sentence = cache_role_sentence("elasticache")
        assert sentence.startswith("ElastiCache is recommended as a cache-aside layer")
        assert "hot, small" in sentence
        assert "rebuilt from that engine" in sentence
        assert "not the whole dataset" in sentence

    def test_it_notes_valkey_durability_and_that_the_tool_does_not_use_it(self) -> None:
        sentence = cache_role_sentence("elasticache")
        assert "ElastiCache for Valkey 9.0 and later offers durability on Multi-AZ" in sentence
        assert "does not use it as a system of record today" in sentence

    @pytest.mark.parametrize(
        "limit", ["never holds the only copy", "cannot be durable", "not a system of record"]
    )
    def test_it_states_no_limit_of_the_service(self, limit: str) -> None:
        assert limit not in cache_role_sentence("elasticache")

    def test_no_engine_still_reads(self) -> None:
        assert cache_role_sentence(None).startswith("The cache layer is recommended")


class TestTheFact:
    def test_the_cache_layer_fact_carries_the_sentence(self) -> None:
        facts = cache_layer_facts(_report())
        assert facts is not None
        assert facts["role_sentence"] == cache_role_sentence("elasticache")

    def test_a_block_written_before_the_sentence_gets_it(self) -> None:
        report = _report()
        stored = report_facts(copy.deepcopy(report))
        stored["cache_layer"].pop("role_sentence")
        report["facts"] = stored
        assert report_facts(report)["cache_layer"]["role_sentence"] == cache_role_sentence(
            "elasticache"
        )


@pytest.mark.parametrize("make", [_report, _shrink_report])
def test_every_deliverable_states_it_once(make) -> None:
    sentence = cache_role_sentence("elasticache")
    for name, text in _deliverables(make()).items():
        assert text.count(sentence) == 1, name


def test_every_deliverable_reads_the_stored_fact() -> None:
    """ADR-032: with a stored facts block, every deliverable prints its sentence."""
    report = _report()
    facts = report_facts(copy.deepcopy(report))
    facts["cache_layer"] = {**facts["cache_layer"], "role_sentence": STORED}
    report["facts"] = facts
    for name, text in _deliverables(report).items():
        assert STORED in text, name
        assert cache_role_sentence("elasticache") not in text, name


def test_no_cache_layer_no_sentence() -> None:
    report = _report()
    report.pop("cache_overlay")
    report["ranking"] = [r for r in report["ranking"] if r.get("role") != "cache_layer"]
    for name, text in _deliverables(report).items():
        assert "is recommended as a cache-aside layer" not in text, name


def test_the_engineering_report_no_longer_says_the_cache_owns_none_of_the_workload() -> None:
    md = render_engineering_report_md(_report())
    assert "It owns none of the workload" not in md
    assert "each cached read stays with its owner engine" in md
