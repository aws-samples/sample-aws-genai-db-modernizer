# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A triage.json saved before #493 still gates correctly (review of #911).

``text_pattern_match`` was called ``inverted_index`` until #493. Saved assessments keep
the old id in ``TriageOutput.query_capabilities``; read unmapped, every engine would
"lack" it, so a customer edit or a resumed reality check would gate Aurora PostgreSQL,
Aurora MySQL and DocumentDB out of every text-search query. Every reader maps it.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from src.agents.referee import reality_check, reality_check_handler, synthesis_handler
from src.agents.referee.capability_registry import (
    LEGACY_CAPABILITY_IDS,
    can_engine_serve_capability,
    missing_capabilities,
    triage_query_capabilities,
)
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine

OLD_TRIAGE = {
    "contract_version": "1.1",
    "selected_agents": [],
    "signals": [],
    "query_capabilities": {"q1": ["inverted_index"], "q2": ["aggregation", "inverted_index"]},
}
QUERY = {"query_id": "q1", "query_text": "SELECT id FROM posts WHERE title LIKE '%x%'"}


def _old_triage(tmp_path: Path) -> dict:
    path = tmp_path / "triage.json"
    path.write_text(json.dumps(OLD_TRIAGE))
    loaded: dict = json.loads(path.read_text())
    return loaded


def test_the_old_id_maps_to_the_new_one(tmp_path: Path) -> None:
    assert LEGACY_CAPABILITY_IDS == {"inverted_index": "text_pattern_match"}
    assert triage_query_capabilities(_old_triage(tmp_path)) == {
        "q1": ["text_pattern_match"],
        "q2": ["aggregation", "text_pattern_match"],
    }


def test_an_old_triage_gates_as_a_new_one(tmp_path: Path) -> None:
    analysis: dict[str, dict] = {
        e: {"workload_analysis": {}} for e in ("aurora_postgresql", "aurora_mysql", "documentdb")
    }
    ctx = ScoringContext.from_pipeline({**analysis, "dynamodb": {}}, _old_triage(tmp_path))
    for engine in analysis:
        assert score_query_for_engine(QUERY, engine, ctx).gate_kind is None, engine
    assert score_query_for_engine(QUERY, "dynamodb", ctx).score is None


def test_the_capability_check_reads_a_legacy_id_too() -> None:
    assert can_engine_serve_capability("aurora_mysql", ["inverted_index"])
    assert missing_capabilities("dynamodb", ["inverted_index"]) == ["text_pattern_match"]


def test_every_triage_capability_reader_maps_it() -> None:
    """The reality check (also behind --resume-reality-check), its handler and synthesis's
    query traces read triage's capabilities through the one mapping function."""
    for module in (reality_check, reality_check_handler, synthesis_handler):
        source = inspect.getsource(module)
        assert "triage_query_capabilities(triage)" in source, module.__name__
        assert '.get("query_capabilities"' not in source, module.__name__


def test_a_saved_report_still_reads_the_old_id_as_prose() -> None:
    """``query_traces.unservable[].required_capabilities`` in an old report.json."""
    from src.shared.capability_labels import capability_gap_phrase, capability_label

    assert capability_label("inverted_index") == capability_label("text_pattern_match")
    assert capability_gap_phrase(["inverted_index"]) == capability_gap_phrase(
        ["text_pattern_match"]
    )
