# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's gate evidence and the cutover message (#1801, ADR-033 Decision 10).

``/move``'s read-only gate saves what it read from DMS and CloudWatch; the facts layer
words it once (``facts.step_gate``, ``src.shared.report_facts.step_gate_facts``, ADR-032)
and the deliverables attach it when they read the report (``facts.with_step_gate``). This
module formats it for the engineering report, after the migration roadmap: the result,
the time of the read, the full load, lag and validation lines, every table and named
exception, the findings of a check that did not pass, and the cutover message verbatim.
Nothing here counts a table or a row. Table names, DMS states and reasons are the
collection's and DMS's text, escaped as data.
"""

from __future__ import annotations

from typing import Any

from . import escaping
from .facts import report_facts

STEP_GATE_HEADING = "## Step 1 gate evidence"
STEP_GATE_INTRO = (
    "What step 1's gate read from DMS and CloudWatch, read-only, on {read_at}. "
    "The figures are as DMS reported them at that time."
)
NOT_REPORTED = "not reported"
#: The executive report states the result and the lines; the engineering report lists each table.
STEP_GATE_PDF_POINTER = (
    "The engineering report lists every table with its validation, and every named exception."
)


def step_gate_block(report: dict[str, Any]) -> dict[str, Any] | None:
    """``facts.step_gate``, or None when the step's gate never ran."""
    block = report_facts(report).get("step_gate")
    return block if isinstance(block, dict) and block.get("status") else None


def _rows(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else NOT_REPORTED


def _code_cell(value: Any) -> str:
    """A table or column name in a table cell: a code span, its pipes and markup escaped."""
    return escaping.md_cell(f"`{escaping.md_code(value)}`") if value else ""


def step_gate_md(report: dict[str, Any]) -> list[str]:
    """The engineering report's "Step 1 gate evidence" section; empty without a gate."""
    gate = step_gate_block(report)
    if gate is None:
        return []
    out = [
        STEP_GATE_HEADING,
        "",
        escaping.md_text(STEP_GATE_INTRO.format(read_at=gate.get("read_at_text"))),
        "",
        f"**{escaping.md_text(gate.get('status_text'))}.** {escaping.md_text(gate.get('result_line'))}",
        "",
        f"*{escaping.md_text(gate.get('cutover_message'))}*",
        "",
        f"- {escaping.md_text(gate.get('load_line'))}",
        f"- {escaping.md_text(gate.get('lag_line'))}",
        f"- {escaping.md_text(gate.get('validation_line'))}",
        "",
    ]
    findings = [f for f in gate.get("gate_findings") or [] if isinstance(f, dict)]
    if findings:
        out += ["Findings:", ""]
        out += [
            f"- {escaping.md_text(f.get('check_text'))}: {escaping.md_text(f.get('text'))}"
            for f in findings
        ]
        hidden = gate.get("gate_findings_not_shown") or 0
        if hidden:
            out.append(f"- And {hidden} more {'finding' if hidden == 1 else 'findings'}.")
        out.append("")
    tables = [t for t in gate.get("gate_tables") or [] if isinstance(t, dict)]
    if tables:
        out += [
            "| Table | Rows loaded | DMS validation state | Failed records | Suspended records | Result |",
            "|---|---:|---|---:|---:|---|",
        ]
        out += [
            f"| {_code_cell(t.get('table'))} | {_rows(t.get('rows_loaded'))} "
            f"| {escaping.md_cell(t.get('validation_state') or NOT_REPORTED)} "
            f"| {_rows(t.get('failed_records'))} | {_rows(t.get('suspended_records'))} "
            f"| {escaping.md_cell(t.get('status_text'))} |"
            for t in tables
        ]
        out.append("")
    exceptions = [e for e in gate.get("named_exceptions") or [] if isinstance(e, dict)]
    if exceptions:
        out += [
            "Named exceptions: what the gate cannot vouch for, and why.",
            "",
            "| Table | Column | Exception | Reason | Rows loaded |",
            "|---|---|---|---|---:|",
        ]
        out += [
            f"| {_code_cell(e.get('table'))} | {_code_cell(e.get('column'))} "
            f"| {escaping.md_cell(e.get('kind_text'))} | {escaping.md_cell(e.get('reason'))} "
            f"| {_rows(e.get('rows_loaded'))} |"
            for e in exceptions
        ]
        out.append("")
    return out
