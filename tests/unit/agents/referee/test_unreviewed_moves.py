# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reality-check moves no model reviewed, in every deliverable (#746 review).

A consolidation review batch with no usable verdict (a call error, or, since
the validator answers only through its tool, a text answer) keeps its moves
unreviewed and is recorded in the reality check's ``validation_incomplete``.
Synthesis copies that list into ``report.json``'s ``reality_check`` block, the
facts layer counts the moves once (``facts.unreviewed_move_count``), and the
engineering report, the PDF report, the UI results page and the Bedrock run's
synthesis status line state it when it is not zero. No test calls a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.report.renderers import render_engineering_report_md
from src.report.unreviewed import unreviewed_moves_line
from src.shared.report_facts import compute_report_facts, unreviewed_move_count
from tests.executive_pdf import executive_text

REPO = Path(__file__).resolve().parents[4]


def _batch(batch: str, total: int, **kw: Any) -> dict[str, Any]:
    return {
        "from_engine": "documentdb",
        "to_engine": "dynamodb",
        "batch": batch,
        "total": total,
        "error": "boto3: no structured answer matching the flag contract",
        **kw,
    }


def _report(incomplete: list[dict] | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-18T00:00:00Z",
        "ranking": [{"target": "dynamodb", "confidence_score": 80, "workload_percent": 100.0}],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {},
        "tied_queries": [],
    }
    if incomplete is not None:
        report["reality_check"] = {"consolidations": [], "validation_incomplete": incomplete}
    report["facts"] = compute_report_facts(report)
    return report


class TestTheCount:
    def test_every_unreviewed_batch_counts_its_moves(self) -> None:
        report = {
            "reality_check": {"validation_incomplete": [_batch("1-30", 35), _batch("31-35", 35)]}
        }
        assert unreviewed_move_count(report) == 35

    @pytest.mark.parametrize("incomplete", [None, []])
    def test_none_without_unreviewed_batches(self, incomplete) -> None:
        assert _report(incomplete)["facts"]["unreviewed_move_count"] == 0

    def test_a_malformed_batch_counts_nothing(self) -> None:
        report = {
            "reality_check": {"validation_incomplete": [_batch("?", 3), "x", _batch("4-6", 6)]}
        }
        assert unreviewed_move_count(report) == 3


class TestEveryDeliverableStatesIt:
    def test_the_engineering_report_and_the_executive_report(self) -> None:
        report = _report([_batch("1-4", 4)])
        line = unreviewed_moves_line(report)
        assert line and line.startswith("4 reality-check moves were not reviewed by the model")
        assert f"**Not reviewed:** {line}" in render_engineering_report_md(report)
        assert " ".join(line.split()) in executive_text(report)

    def test_one_move_reads_in_the_singular(self) -> None:
        line = unreviewed_moves_line(_report([_batch("1-1", 1)]))
        assert line == (
            "1 reality-check move was not reviewed by the model: its review of that move "
            "failed, so it stands as the routing rules placed it."
        )

    def test_nothing_is_stated_when_every_move_was_reviewed(self) -> None:
        report = _report([])
        assert unreviewed_moves_line(report) is None
        assert "Not reviewed:" not in render_engineering_report_md(report)
        assert "not reviewed by the model" not in executive_text(report)

    def test_the_renderers_print_the_stored_fact(self) -> None:
        report = _report([_batch("1-4", 4)])
        report["facts"]["unreviewed_move_count"] = 9
        assert "9 reality-check moves" in render_engineering_report_md(report)

    def test_the_ui_says_the_same(self) -> None:
        en = json.loads((REPO / "src/ui/src/locales/en.json").read_text())
        for n, key in ((1, "one"), (3, "other")):
            report = _report([_batch(f"1-{n}", n)])
            text = en[f"analysis-results.unreviewed-moves_{key}"].replace("{{count}}", str(n))
            assert unreviewed_moves_line(report) == text


def test_synthesis_copies_the_batches_and_counts_them(tmp_path) -> None:
    from src.agents.referee.synthesis_handler import (
        _write_synthesis_report,
        run_synthesis_deterministic,
    )
    from tests.unit.agents.referee.test_synthesis_unservable_capabilities import (
        DB,
        JOB,
    )
    from tests.unit.agents.referee.test_synthesis_unservable_capabilities import (
        store as seeded_store,
    )

    store = seeded_store.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    store.write_json(
        f"{DB}/{JOB}/reality-check/output.json",
        {
            "source_assignment_version": 1,
            "consolidations": [],
            "architectural_patterns": [],
            "recommendations": [],
            "before_distribution": {},
            "after_distribution": {},
            "validation_incomplete": [_batch("1-2", 2)],
        },
    )
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    assert report["reality_check"]["validation_incomplete"] == [_batch("1-2", 2)]
    assert report["facts"]["unreviewed_move_count"] == 2

    import scripts.run_assessment as run_assessment

    status = run_assessment._synthesis_status(store, JOB, DB)
    assert status["unreviewed_moves"] == 2


def test_a_text_answer_lands_in_validation_incomplete() -> None:
    """End to end on the Bedrock handler: the validator answers in text (no tool
    use), the batch fails, and the reality-check output records it."""
    from src.agents.referee import consolidation_validator as cv
    from src.agents.referee.reality_check_handler import run_reality_check_handler
    from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

    store = _mock_store()
    text = {"output": {"message": {"content": [{"text": '{"consolidation_corrections": []}'}]}}}
    with (
        patch.object(cv, "_try_strands_validator", return_value=None),
        patch("boto3.client") as client,
        patch(
            "src.agents.referee.reality_check_handler._generate_executive_summary",
            return_value=None,
        ),
    ):
        client.return_value.converse.return_value = text
        run_reality_check_handler("job-1", "mydb", store, llm_mode="bedrock")
    output = next(v for k, v in store._written.items() if k.endswith("output.json"))
    [batch] = output["validation_incomplete"]
    assert (batch["from_engine"], batch["to_engine"], batch["batch"]) == (
        "documentdb",
        "dynamodb",
        "1-1",
    )
    assert "no structured answer" in batch["error"]
