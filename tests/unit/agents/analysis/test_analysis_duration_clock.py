# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Analysis and design durations come from a monotonic clock (#2599).

Each analysis agent timed itself with ``datetime.now()``. A system clock that
stepped back during the run (an NTP adjustment) gave a negative duration, and
``AgentMetadata.analysis_duration_seconds`` (at least 0) failed the phase. These
tests step the wall clock back on every read and give the module a monotonic
clock that moves forward, at each module's own ``time`` and ``datetime`` names.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from src.agents.analysis import (
    aurora_mysql_analysis_agent,
    aurora_pg_analysis_agent,
    documentdb_analysis_agent,
    dynamodb_analysis_agent,
    elasticache_analysis_agent,
    opensearch_analysis_agent,
)
from src.contracts.analysis_input import AnalysisInput
from src.contracts.analysis_output import TargetDatabase
from src.tools.schema import documentdb_schema_agent, dynamodb_schema_agent, opensearch_schema_agent

STEP_S = 2.5


def _monotonic_clock(step: float = STEP_S) -> types.SimpleNamespace:
    """A stand-in for a module's ``time``: ``monotonic`` moves ``step`` seconds per read.

    Its wall clock never moves, so a duration read from it is 0, not ``step``.
    """
    readings = iter(100.0 + step * n for n in range(1_000))
    return types.SimpleNamespace(monotonic=lambda: next(readings), time=lambda: 0.0)


def _wall_clock_stepping_back(seen: list[datetime]) -> type[datetime]:
    """A ``datetime`` whose ``now()`` is a second earlier on every call."""

    class SteppingBack(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            value = datetime(2026, 11, 24, 12, 0, 0, tzinfo=tz) - timedelta(seconds=len(seen))
            seen.append(value)
            return value

    return SteppingBack


AGENTS: list[tuple[types.ModuleType, Callable, TargetDatabase]] = [
    (
        aurora_mysql_analysis_agent,
        aurora_mysql_analysis_agent.analyze_for_aurora_mysql_deterministic,
        TargetDatabase.AURORA_MYSQL,
    ),
    (
        aurora_pg_analysis_agent,
        aurora_pg_analysis_agent.analyze_for_aurora_pg_deterministic,
        TargetDatabase.AURORA_POSTGRESQL,
    ),
    (
        documentdb_analysis_agent,
        documentdb_analysis_agent.analyze_for_documentdb_deterministic,
        TargetDatabase.DOCUMENTDB,
    ),
    (
        dynamodb_analysis_agent,
        dynamodb_analysis_agent.analyze_for_dynamodb_deterministic,
        TargetDatabase.DYNAMODB,
    ),
    (
        elasticache_analysis_agent,
        elasticache_analysis_agent.analyze_for_elasticache,
        TargetDatabase.ELASTICACHE,
    ),
    (
        opensearch_analysis_agent,
        opensearch_analysis_agent.analyze_for_opensearch,
        TargetDatabase.OPENSEARCH,
    ),
]


@pytest.mark.parametrize(
    ("module", "analyze", "target"),
    AGENTS,
    ids=[m.__name__.rsplit(".", 1)[1] for m, _, _ in AGENTS],
)
def test_a_wall_clock_step_back_never_fails_the_analysis(monkeypatch, module, analyze, target):
    seen: list[datetime] = []
    monkeypatch.setattr(module, "datetime", _wall_clock_stepping_back(seen))
    monkeypatch.setattr(module, "time", _monotonic_clock(), raising=False)
    analysis_input = AnalysisInput(
        job_id="clock-step",
        collector_output={
            "metadata": {"source_database": {"engine": "postgresql"}},
            "database_schema": {"tables": []},
            "queries": {"query_patterns": []},
        },
        target_database=target,
    )

    contract = analyze(analysis_input)[0]

    # The duration is the monotonic clock's two reads apart.
    assert contract.agent_metadata.analysis_duration_seconds == STEP_S
    # The timestamp is still the wall clock's.
    assert contract.agent_metadata.analysis_timestamp in seen


@pytest.mark.parametrize(
    "module",
    [documentdb_schema_agent, dynamodb_schema_agent, opensearch_schema_agent],
    ids=["documentdb", "dynamodb", "opensearch"],
)
def test_a_design_trace_total_is_measured_on_the_monotonic_clock(monkeypatch, module):
    """The DocumentDB trace never set its start, so its total was the seconds
    since 1970; every trace now starts its monotonic clock when it is made."""
    monkeypatch.setattr(module, "time", _monotonic_clock(step=3.25), raising=False)

    trace = module.SchemaDesignTrace()

    assert trace.to_dict()["total_duration_seconds"] == 3.25
