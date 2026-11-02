# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The access patterns an unresolved DynamoDB merge leaves unserved (#802).

DynamoDB is designed in parallel query groups whose drafts are merged into one design.
A merge failure that no fix round resolved (the Amazon Bedrock path has none) stays in
the design. ``facts.dynamodb_merge_conflicts``
(``src.shared.report_facts.dynamodb_merge_conflict_facts``, ADR-032) lists each failure
with its access patterns and what they are: unserved (the merged design runs them on a
table or GSI not designed for them), at risk (they run on a table the failure concerns,
until it is resolved), none named, or not recorded (a design merged before they were).
This only formats it. The engineering report lists every conflict, the executive report
states the totals (it names no table or query), and the UI's results page lists them
with the same sentences (``analysis-results.merge-conflicts.*``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import escaping
from .facts import report_facts

MERGE_CONFLICTS_TITLE = "Access patterns the merged DynamoDB design does not serve"
MERGE_CONFLICTS_INTRO = (
    "DynamoDB is designed in parallel query groups whose designs are merged into one. "
    "Resolve each conflict in the DynamoDB schema design and merge again before migration."
)
MERGE_CONFLICTS_PDF_NOTE = (
    "The engineering report lists each conflict and the access patterns it leaves unserved."
)
# The lead of each conflict's line, by what its patterns are (the UI's same strings).
STATUS_LABELS = {
    "unserved": "Not served",
    "at_risk": "At risk until this conflict is resolved",
}
NONE_NAMED_TEXT = "The merge names no access pattern this failure leaves unserved."
NOT_RECORDED_TEXT = (
    "Unserved access patterns were not recorded for this design (it was merged before "
    "they were); merge it again to list them."
)
ALSO_LISTED = "(also listed above)"


def merge_conflict_facts(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``facts.dynamodb_merge_conflicts``, or None when the merge left none."""
    fact = report_facts(dict(report)).get("dynamodb_merge_conflicts")
    if not isinstance(fact, Mapping) or not fact.get("conflicts"):
        return None
    return fact


def conflict_line(conflict: Mapping[str, Any], code: Callable[[Any], str]) -> str:
    """The line under a conflict: what its patterns are, the patterns (each one an
    earlier conflict lists says so), the table and the queries. ``code`` formats an id
    or a table name (a Markdown code span, or plain text)."""
    label = STATUS_LABELS.get(str(conflict.get("status")))
    patterns = list(conflict.get("pattern_ids") or [])
    if label is None or not patterns:
        return NOT_RECORDED_TEXT if conflict.get("status") == "not_recorded" else NONE_NAMED_TEXT
    repeated = set(conflict.get("repeated_pattern_ids") or [])
    shown = ", ".join(f"{code(p)} {ALSO_LISTED}" if p in repeated else code(p) for p in patterns)
    table = conflict.get("table_name")
    on = f" on {code(table)}" if table else ""
    queries = list(conflict.get("query_ids") or [])
    asked = f"; queries {', '.join(code(q) for q in queries)}" if queries else ""
    return f"{label}: {shown}{on}{asked}."


def _md_code(value: Any) -> str:
    return f"`{escaping.md_code(value)}`"


def merge_conflicts_md(report: Mapping[str, Any]) -> list[str]:
    """The engineering report's section: the totals, then each failure with the
    access patterns and queries it concerns."""
    fact = merge_conflict_facts(report)
    if fact is None:
        return []
    out = [
        f"## {MERGE_CONFLICTS_TITLE}",
        "",
        escaping.md_text(fact.get("summary_line") or ""),
        "",
        escaping.md_text(MERGE_CONFLICTS_INTRO),
        "",
    ]
    for conflict in fact.get("conflicts") or []:
        if isinstance(conflict, Mapping):
            # Customer values are code spans (md_code); the rest is this module's text.
            line = conflict_line(conflict, _md_code)
            out += [f"- {escaping.md_text(conflict.get('failure') or '')}", f"  - {line}"]
    return out + [""]


def merge_conflicts_pdf(report: Mapping[str, Any]) -> list[str] | None:
    """The executive report card's paragraphs: the totals and where the list is. It
    names no table, access pattern or query."""
    fact = merge_conflict_facts(report)
    if fact is None:
        return None
    return [str(fact.get("summary_line") or ""), MERGE_CONFLICTS_INTRO, MERGE_CONFLICTS_PDF_NOTE]
