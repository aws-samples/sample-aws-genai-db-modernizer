"""A schema revision's designer inputs stay inside its engine's assignment (#1524).

``_dispatch_llm_redesign`` hands the designer the collector and analysis files of the
engine in the assignment version downstream phases use, narrowed by the helpers the
first design applies (``filter_collector_for_assignment`` and
``filter_analysis_for_assignment``): the same tables, queries and analysis entries the
Bedrock design of that engine reads. Checked on every bundled sample for every
engine the latest assignment designs, and on wordpress Aurora MySQL by size. Deterministic
pipeline; no model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.schema_design import handler, revision_handler
from src.contracts.schema_revision_models import (
    NewPattern,
    SchemaRevisionRequest,
)
from tests.e2e.pipeline import PipelineResult
from tests.e2e.test_schema_design_analysis_scope import _Case, _cases


def _revision_inputs(case: _Case, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """The collector and analysis files ``_dispatch_llm_redesign`` writes for the agent."""
    seen: dict[str, Any] = {}

    def fake_dispatch(target_type, collector_path, analysis_path, **_kw):
        seen["collector"] = json.loads(Path(collector_path).read_text())
        seen["analysis"] = json.loads(Path(analysis_path).read_text())
        return json.dumps({"target_type": target_type}), None

    monkeypatch.setattr(handler, "_dispatch_schema_agent", fake_dispatch)
    request = SchemaRevisionRequest(
        base_version=case.version,
        pattern_modifications=[],
        table_modifications=[],
        new_patterns=[
            NewPattern(
                description="a lookup the customer needs",
                target_engine=case.engine,
                source_tables=[],
            )
        ],
    )
    revision_handler._dispatch_llm_redesign(
        case.run.job_id, case.run.db, case.engine, {}, request, case.store
    )
    return seen["collector"], seen["analysis"]


def _first_design(case: _Case, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """What the Bedrock first design of the same engine hands the agent."""
    seen: dict[str, Any] = {}

    def fake_dispatch(target_type, collector_path, analysis_path, **_kw):
        seen["collector"] = json.loads(Path(collector_path).read_text())
        seen["analysis"] = json.loads(Path(analysis_path).read_text())
        raise RuntimeError("stop before any model call")

    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(handler, "_dispatch_schema_agent", fake_dispatch)
    with pytest.raises(RuntimeError, match="stop before"):
        handler.run_schema_design(
            case.run.job_id, case.run.db, case.engine, case.store, case.version
        )
    return seen["collector"], seen["analysis"]


def test_a_revision_names_only_the_engines_own_tables(
    run: PipelineResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case in _cases(run):
        collector, analysis = _revision_inputs(case, monkeypatch)
        case.check(collector, analysis)


def test_a_revision_input_equals_the_single_call_design_input(
    run: PipelineResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same tables, queries and analysis entries as the first design, for every engine."""
    for case in _cases(run):
        collector, analysis = _revision_inputs(case, monkeypatch)
        first_collector, first_analysis = _first_design(case, monkeypatch)
        where = (run.db, case.engine)
        assert collector == first_collector, where
        assert analysis == first_analysis, where


def test_an_aurora_mysql_revision_holds_fewer_tables_than_the_collection(
    run: PipelineResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wordpress: 20 tables and 20 recommendations of the 50 collected (#1524)."""
    cases = [c for c in _cases(run) if c.engine == "aurora_mysql"]
    if not cases:
        # No Aurora MySQL design in this assignment (a source-compatible engine won).
        assert run.db != "wordpress", "wordpress designs Aurora MySQL"
        return
    case = cases[0]
    full = case.collector
    collector, analysis = _revision_inputs(case, monkeypatch)
    assert len(collector["database_schema"]["tables"]) == len(case.tables)
    assert len(analysis["table_recommendations"]) == len(case.tables)
    assert len(case.tables) < len(full["database_schema"]["tables"])
    assert len(collector["queries"]["query_patterns"]) < len(full["queries"]["query_patterns"])
    if run.db == "wordpress":
        assert (len(case.tables), len(full["database_schema"]["tables"])) == (20, 50)
