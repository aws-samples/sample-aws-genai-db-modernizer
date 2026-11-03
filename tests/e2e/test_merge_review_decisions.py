# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DynamoDB merge-review decision through the real path, in every report (#1780).

``designed_run.run_merge_review_wordpress`` builds the designed wordpress job's DynamoDB
design with the group merge itself: two drafts each give ``wordpress.wp_posts`` a home,
the merge keeps Posts as the authoritative home, and the query-pattern completion run
after the merge (#1602) then adds statements to the copy, so ranking the homes again
would name the copy. Synthesis and the render step run as ``/modernize`` runs them. The
decision, the overlap's risk and its stream sync must all state the home the merge
recorded, and the engineering report and the executive report must print
``facts.dynamodb_merge_reviews`` (the results page: ``tests/e2e/test_ui.py``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.report import escaping
from src.report.merge_reviews import MERGE_REVIEWS_PDF_NOTE, MERGE_REVIEWS_TITLE
from tests.e2e.designed_run import MERGE_REVIEW_COPY
from tests.e2e.pipeline import PipelineResult
from tests.executive_pdf import normalize, text

pytestmark = pytest.mark.deterministic


def _report(r: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(r.report_json().read_text())
    return report


def _fact(r: PipelineResult) -> dict[str, Any]:
    fact = _report(r)["facts"]["dynamodb_merge_reviews"]
    assert fact is not None
    return dict(fact)


def test_wordpress_merge_review_states_the_home_the_merge_recorded(
    designed_merge_review_run: PipelineResult,
) -> None:
    r = designed_merge_review_run
    [overlap] = [
        t
        for t in json.loads(
            next(r.job_dir().glob("schema-dynamodb/v*/schema_output.json")).read_text()
        )["trade_offs"]
        if t.get("authoritative_home")
    ]
    assert (overlap["authoritative_home"], overlap["derived_copies"]) == (
        "Posts",
        [MERGE_REVIEW_COPY],
    )
    report = _report(r)
    [decision] = _fact(r)["decisions"]
    assert (decision["authoritative_home"], decision["derived_copies"]) == (
        "Posts",
        [MERGE_REVIEW_COPY],
    )
    assert decision["reconstructed"] is False
    [risk] = [
        x
        for x in report["risk_assessment"]["risks"]
        if x["risk_type"] == "DATA_CONSISTENCY" and x["risk_id"] == decision["risk_id"]
    ]
    assert "in sync from Posts, the authoritative home" in risk["mitigation"]
    syncs = (report["facts"].get("derived_copy_sync") or {}).get("syncs") or []
    assert [(s["risk_id"], s["authoritative_home"]) for s in syncs] == [(risk["risk_id"], "Posts")]


def test_wordpress_merge_review_engineering_report_lists_the_fact(
    designed_merge_review_run: PipelineResult,
) -> None:
    r = designed_merge_review_run
    fact = _fact(r)
    md = r.deliverable("engineering-report.md").read_text()
    start = md.index(f"## {MERGE_REVIEWS_TITLE}")
    section = md[start : md.find("\n## ", start + 1)]

    assert escaping.md_text(fact["summary_line"]) in section
    assert escaping.md_text(fact["review_line"]) in section
    printed = [line[2:] for line in section.splitlines() if line.startswith("- ")]
    assert printed == [escaping.md_text(d["line"]) for d in fact["decisions"]]


def test_wordpress_merge_review_executive_report_states_it_and_names_no_table(
    designed_merge_review_run: PipelineResult,
) -> None:
    r = designed_merge_review_run
    fact = _fact(r)
    pdf = text(r.deliverable("executive-report.pdf"))
    card = pdf[pdf.index(MERGE_REVIEWS_TITLE) :]

    for sentence in (fact["summary_line"], fact["review_line"], MERGE_REVIEWS_PDF_NOTE):
        assert normalize(sentence) in card
    assert f"Data-consistency risks: {', '.join(fact['risk_ids'])}" in card
    card = card[: card.index(normalize(MERGE_REVIEWS_PDF_NOTE))]
    for name in (MERGE_REVIEW_COPY, "wp_posts"):
        assert name not in card
