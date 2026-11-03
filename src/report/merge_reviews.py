# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DynamoDB merge-review decisions (#1780).

When two or more design groups each gave a source table its own home, the DynamoDB
group merge keeps one as the authoritative home, which takes every write, and the
others as derived copies kept in sync from its DynamoDB Streams (#333). The overlap is
also a data-consistency risk (#426), and its stream sync is priced under that risk
(#1527). ``facts.dynamodb_merge_reviews``
(``src.shared.report_facts.dynamodb_merge_review_facts``, ADR-032) states the decisions
themselves; this only formats them. The engineering report lists each decision, the
executive report states the totals and what to review (it names no table), and the
UI's results page lists the same lines (``analysis-results.merge-reviews.*``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import escaping
from .facts import report_facts

MERGE_REVIEWS_TITLE = "DynamoDB merge-review decisions"
MERGE_REVIEWS_PDF_NOTE = (
    "The engineering report names each source table, its authoritative home and its "
    "derived copies."
)
RISKS_LABEL = "Data-consistency risks"


def merge_review_facts(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``facts.dynamodb_merge_reviews``, or None when the merge chose no home."""
    fact = report_facts(dict(report)).get("dynamodb_merge_reviews")
    if not isinstance(fact, Mapping) or not fact.get("decisions"):
        return None
    return fact


def merge_reviews_md(report: Mapping[str, Any]) -> list[str]:
    """The engineering report's section: the totals, what to review, then each
    decision's line."""
    fact = merge_review_facts(report)
    if fact is None:
        return []
    out = [
        f"## {MERGE_REVIEWS_TITLE}",
        "",
        escaping.md_text(fact.get("summary_line") or ""),
        "",
        escaping.md_text(fact.get("review_line") or ""),
        "",
    ]
    for decision in fact.get("decisions") or []:
        if isinstance(decision, Mapping):
            out.append(f"- {escaping.md_text(decision.get('line') or '')}")
    return out + [""]


def merge_reviews_pdf(report: Mapping[str, Any]) -> list[tuple[str | None, str]] | None:
    """The executive report card's paragraphs, ``(label, text)``: the totals, what to
    review, the risks they leave and where the list is. It names no table."""
    fact = merge_review_facts(report)
    if fact is None:
        return None
    out: list[tuple[str | None, str]] = [
        (None, str(fact.get("summary_line") or "")),
        (None, str(fact.get("review_line") or "")),
    ]
    risk_ids = [str(r) for r in fact.get("risk_ids") or []]
    if risk_ids:
        out.append((RISKS_LABEL, ", ".join(risk_ids)))
    return out + [(None, MERGE_REVIEWS_PDF_NOTE)]
