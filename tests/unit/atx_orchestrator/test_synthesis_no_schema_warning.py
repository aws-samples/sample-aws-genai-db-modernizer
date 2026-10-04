"""run_synthesis_core's post-synthesis guards once databases list workload engines (#281).

Synthesis now lists every engine with assigned queries in
``recommended_architecture.databases`` even when no schema design ran (no
tables allocated), so "no schema design" no longer implies "no databases". The
known-gap warning keys on schema-design availability; the wrong-version raise
still keys on empty databases with an unread assignment.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _run(report: dict) -> dict:
    from src.atx_orchestrator import core

    store = MagicMock()
    store.exists.return_value = True
    store.read_json.return_value = report
    with (
        patch("src.agents.referee.synthesis_handler.run_synthesis"),
        patch("src.atx_orchestrator.core._build_graph"),
    ):
        return core.run_synthesis_core(
            job_id="job-1", database_name="mydb", assignment_version=1, store=store
        )


def test_no_schema_design_warns_even_with_workload_databases() -> None:
    summary = _run(
        {
            "ranking": [{"target": "dynamodb", "assigned_queries": 5}],
            "recommended_architecture": {"databases": [{"service": "dynamodb", "tables": []}]},
            "assignment_summary": {"x": 1},
        }
    )
    assert summary["recommended_databases"] == ["dynamodb"]
    (warning,) = summary["warnings"]
    assert "No engine has schema-design output" in warning
    assert "allocates no tables" in warning


def test_schema_design_present_does_not_warn() -> None:
    summary = _run(
        {
            "ranking": [{"target": "dynamodb", "schema_design_available": True}],
            "recommended_architecture": {"databases": [{"service": "dynamodb"}]},
            "assignment_summary": {"x": 1},
        }
    )
    assert summary["warnings"] == []


def test_unread_assignment_with_no_databases_still_raises() -> None:
    with pytest.raises(ValueError, match="never read the assignment"):
        _run({"ranking": [{"target": "dynamodb"}], "recommended_architecture": {}})


def test_top_engine_is_the_main_owner_not_the_first_entry() -> None:
    """#152: a report ordered by weight put the cache layer first; top_engine is the
    engine owning the largest workload share, never the cache layer."""
    summary = _run(
        {
            "ranking": [
                {"target": "elasticache", "role": "cache_layer", "workload_percent": 0.0},
                {"target": "opensearch", "workload_percent": 3.7, "schema_design_available": True},
                {"target": "dynamodb", "workload_percent": 91.6, "schema_design_available": True},
            ],
            "recommended_architecture": {"databases": [{"service": "dynamodb"}]},
            "assignment_summary": {"x": 1},
        }
    )
    assert summary["top_engine"] == "dynamodb"
