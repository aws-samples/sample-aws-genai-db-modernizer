"""The PDF report names other targets by display name, even from a stored summary (#544).

``build_summary`` writes "Other targets evaluated: ..." with ``display_name``, but a
``report.json`` written before that (or any stored ``summary_deterministic``) still
carries raw engine ids. The renderer shows the stored text, so the rewording happens
at render time, the same way ``label_summary_counts`` relabels older counts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.report.formatting import label_other_targets, label_summary_counts
from tests.executive_pdf import engineering_text

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wordpress_no_schema_design_report.json"


def _fixture() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def test_stored_raw_ids_become_display_names() -> None:
    text = (
        "Other targets evaluated: documentdb (consolidated into aurora_mysql by the reality "
        "check), opensearch (consolidated into aurora_mysql by the reality check)."
    )
    assert label_other_targets(text) == (
        "Other targets evaluated: DocumentDB (consolidated into Aurora MySQL by the reality "
        "check), OpenSearch (consolidated into Aurora MySQL by the reality check)."
    )


def test_issue_544_case_reads_as_the_deck_does() -> None:
    text = "Other targets evaluated: documentdb (consolidated into dynamodb by the reality check)."
    assert label_other_targets(text) == (
        "Other targets evaluated: DocumentDB (consolidated into DynamoDB by the reality check)."
    )


def test_rewording_is_idempotent() -> None:
    once = label_other_targets("Other targets evaluated: documentdb (no queries assigned).")
    assert label_other_targets(once) == once


def test_only_the_other_targets_clause_is_touched() -> None:
    text = (
        "Top query groups by throughput: dynamodb_items. Other targets evaluated: "
        "documentdb (no queries assigned). Stored in dynamodb 50.5% of the time."
    )
    assert label_other_targets(text) == (
        "Top query groups by throughput: dynamodb_items. Other targets evaluated: "
        "DocumentDB (no queries assigned). Stored in dynamodb 50.5% of the time."
    )


def test_a_summary_without_the_clause_is_unchanged() -> None:
    text = "Top recommendation: dynamodb with 50% average fit."
    assert label_other_targets(text) == text


def test_summary_counts_labelling_includes_the_other_targets_clause() -> None:
    text = "Other targets evaluated: documentdb (no queries assigned)."
    assert "DocumentDB (no queries assigned)" in label_summary_counts(text)


def test_the_md_shows_display_names_for_a_stored_summary() -> None:
    text = engineering_text(_fixture(), trust_generated_summary=False)
    assert "Other targets evaluated: DocumentDB (consolidated into Aurora MySQL" in text
    assert not re.search(r"Other targets evaluated: [^.]*\b(documentdb|aurora_mysql)\b", text)
