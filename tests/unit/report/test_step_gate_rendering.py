# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's gate evidence in the deliverables (#1801): formatted, never counted.

The engineering report's "Step 1 gate evidence" section, the executive report's wave plan
and the results payload print ``facts.step_gate`` as the facts layer words it, with the
cutover message verbatim wherever the evidence appears. Table names, DMS states and reasons
are escaped as data. A report whose step never ran its gate renders exactly as before.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import results
from src.api.services.local_s3 import LocalS3Service
from src.report.deliverables import render_deliverables
from src.report.facts import with_step_gate
from src.report.renderers import render_engineering_report_md
from src.report.step_gate import STEP_GATE_HEADING, STEP_GATE_PDF_POINTER, step_gate_md
from src.shared.cutover_message import CUTOVER_DISCLAIMER, REPLICATING_MESSAGE
from src.shared.report_facts import step_gate_facts, step_gate_key
from src.storage.local_store import LocalArtifactStore
from tests.executive_pdf import executive_text, normalize
from tests.unit.report import step_gate_fixtures as fx
from tests.unit.report.test_migration_waves_rendering import _legacy_report, _report_with_waves


def _with_gate(gate: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    return with_step_gate(report or _report_with_waves(), step_gate_facts(gate))


def _section(md: str) -> str:
    return md.split(STEP_GATE_HEADING + "\n", 1)[1].split("\n## ", 1)[0]


# ---------------------------------------------------------------------------
# The engineering report
# ---------------------------------------------------------------------------


def test_the_section_follows_the_migration_roadmap():
    md = render_engineering_report_md(_with_gate(fx.passed()))
    headings = [line for line in md.splitlines() if line.startswith("## ")]
    roadmap = next(i for i, h in enumerate(headings) if h.startswith("## Migration roadmap"))
    assert headings[roadmap + 1] == STEP_GATE_HEADING
    assert md.count(STEP_GATE_HEADING) == 1


def test_a_passed_gate_states_the_result_the_time_and_the_cutover_message():
    section = _section(render_engineering_report_md(_with_gate(fx.passed())))
    lines = section.splitlines()
    assert "on 2026-11-14 09:30 UTC" in lines[1]
    assert f"**Passed.** {REPLICATING_MESSAGE}" in lines
    # The cutover message, verbatim, right under the result, and only there.
    assert lines[lines.index(f"**Passed.** {REPLICATING_MESSAGE}") + 2] == f"*{CUTOVER_DISCLAIMER}*"
    assert section.count("The cutover is your responsibility.") == 1
    assert "- Full load at 100%: 1,234,567 rows loaded across 3 tables." in lines
    assert any(line.startswith("- Highest replication lag in the 15 minutes") for line in lines)
    assert "- 2 of 2 tables with a key validated. 2 named exceptions, listed below." in lines
    assert "Findings:" not in section


def test_each_table_and_named_exception_is_a_row():
    section = _section(render_engineering_report_md(_with_gate(fx.passed())))
    assert "| `shop.orders` | 1,233,321 | Validated | 0 | 0 | Validated |" in section
    assert "| `shop.audit_log` | 12 | No primary key | 0 | 0 | Named exception |" in section
    assert (
        "| `shop.audit_log` |  | Not validated | not validated: no primary or unique key | 12 |"
        in section
    )
    assert (
        "| `shop.order_totals` |  | View, not validated | not validated: no primary or unique key "
        "| not reported |" in section
    )


@pytest.mark.parametrize(
    ("gate", "word", "line"),
    [
        (fx.pending, "Pending", "The gate has not passed yet"),
        (fx.failed, "Failed", "The gate did not pass"),
    ],
)
def test_a_gate_that_did_not_pass_lists_its_findings(gate, word, line):
    section = _section(render_engineering_report_md(_with_gate(gate())))
    assert f"**{word}.** {line}" in section
    assert REPLICATING_MESSAGE not in section
    assert f"*{CUTOVER_DISCLAIMER}*" in section
    assert "Findings:" in section
    assert "- Validation: shop.orders: validation" in section


def test_findings_past_the_cap_are_stated_as_the_facts_count_them():
    section = _section(render_engineering_report_md(_with_gate(fx.many_findings(27))))
    assert "- And 7 more findings." in section


def test_collection_and_dms_text_is_escaped():
    section = _section(render_engineering_report_md(_with_gate(fx.hostile())))
    assert "<script>" not in section and "<b>" not in section
    rows = [line for line in section.splitlines() if line.startswith("| `shop.x")]
    assert len(rows) == 2
    for row in rows:
        # A pipe in a name or a reason never opens a new cell; backticks never close the span.
        assert row.startswith("| `shop.x\\|&lt;script&gt;alert(1)&lt;/script&gt;` |")
        assert row.replace("\\|", "").count("|") == row.count(" | ") + 2
    assert "| No \\| primary *key* |" in rows[0]  # DMS's state, in its cell
    assert "cannot \\| validate on &lt;b&gt;" in rows[1]


@pytest.mark.parametrize("report", [_report_with_waves, _legacy_report], ids=["current", "legacy"])
def test_without_a_gate_the_report_is_unchanged(report):
    md = render_engineering_report_md(report())
    assert md == render_engineering_report_md(with_step_gate(report(), None))
    assert STEP_GATE_HEADING not in md and CUTOVER_DISCLAIMER not in md
    assert step_gate_md(report()) == []


def test_a_legacy_report_with_a_gate_shows_it():
    md = render_engineering_report_md(_with_gate(fx.passed(), _legacy_report()))
    assert f"*{CUTOVER_DISCLAIMER}*" in _section(md)


# ---------------------------------------------------------------------------
# The executive report
# ---------------------------------------------------------------------------


def test_the_executive_report_states_the_result_the_lines_and_the_cutover_message():
    text = normalize(executive_text(_with_gate(fx.failed())))
    facts = step_gate_facts(fx.failed())
    gate = text.split("Step 1 gate, read 2026-11-14 09:30 UTC:", 1)[1]
    assert gate.lstrip().startswith(f"Failed. {facts['result_line']}")
    for key in ("load_line", "lag_line", "validation_line"):
        assert normalize(facts[key]) in gate
    assert CUTOVER_DISCLAIMER in gate and STEP_GATE_PDF_POINTER in gate
    # After every wave card: the wave plan's own sentences are where they were.
    assert text.index("Step 1 gate, read") > text.rindex("WAVE ")


def test_the_executive_report_without_a_gate_has_none():
    text = normalize(executive_text(_report_with_waves()))
    assert "Step 1 gate" not in text and CUTOVER_DISCLAIMER not in text


# ---------------------------------------------------------------------------
# Every deliverable, from the job's artifacts, and the results payload
# ---------------------------------------------------------------------------


def _job(tmp_path, gate: dict[str, Any] | None) -> tuple[LocalArtifactStore, str]:
    store = LocalArtifactStore(str(tmp_path))
    key = "shop/job1/synthesis/v1/report.json"
    store.write_json(key, _report_with_waves())
    if gate is not None:
        store.write_json(step_gate_key("shop", "job1"), gate)
    return store, key


def _item(rendered: Any, name: str) -> bytes:
    content: bytes = next(i.content for i in rendered.items if i.name == name)
    return content


def test_the_deliverables_read_the_gate_file_the_job_saved(tmp_path):
    store, key = _job(tmp_path, fx.pending())
    rendered = render_deliverables(store, "job1", "shop", key, assignment_version=1)
    md = _item(rendered, "engineering-report").decode()
    data = json.loads(_item(rendered, "assessment-data"))
    assert data["facts"]["step_gate"] == step_gate_facts(fx.pending())
    assert f"*{CUTOVER_DISCLAIMER}*" in _section(md)
    assert step_gate_facts(fx.pending())["result_line"] in _section(md)
    # The stored report is the system of record: the evidence is never written into it.
    assert "step_gate" not in (store.read_json(key).get("facts") or {})


def test_the_deliverables_of_a_job_with_no_gate_file_carry_no_block(tmp_path):
    store, key = _job(tmp_path, None)
    rendered = render_deliverables(store, "job1", "shop", key, assignment_version=1)
    data = json.loads(_item(rendered, "assessment-data"))
    assert "facts" not in data or "step_gate" not in (data["facts"] or {})
    assert STEP_GATE_HEADING not in _item(rendered, "engineering-report").decode()


def test_the_results_payload_carries_the_same_block(tmp_path):
    store, key = _job(tmp_path, fx.failed())
    sfn = MagicMock()
    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": "shop"},
    }
    s3 = MagicMock()  # the rest of the payload as tests/unit/api/test_results.py reads it
    s3.read_synthesis.return_value = store.read_json(key)
    s3.read_step_gate = LocalS3Service(store).read_step_gate
    results.sfn_service, results.s3_service = sfn, s3
    try:
        response = TestClient(app).get("/api/v1/assessments/job1/results")
    finally:
        results.sfn_service = results.s3_service = None
    assert response.status_code == 200, response.text
    assert response.json()["synthesis"]["facts"]["step_gate"] == step_gate_facts(fx.failed())
    assert LocalS3Service(store).read_step_gate("shop", "nojob") is None
