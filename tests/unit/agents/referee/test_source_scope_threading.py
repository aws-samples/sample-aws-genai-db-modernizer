"""#835: every consumer of the system-object rule gets the collected database.

Each consumer runs on a collector whose database is literally named ``sys``
with the application table ``sys.public.orders``. A spy records the
``SourceScope`` each call site passes down; dropping the scope at any one site
(passing ``None``) fails the case for that consumer.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from src.agents.referee import (
    assignment_resolver,
    capability_registry,
    migration_waves,
    query_shape,
    synthesis_report,
    triage,
)
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.agents.referee.synthesis_report import build_query_groups
from src.contracts.schema_design_input import AgentQueryPattern, QueryType
from src.storage.local_store import LocalArtifactStore
from src.tools.analysis import aurora_common_analysis_tools as analysis_tools
from src.tools.schema.aurora_common import design_view

TABLE = "sys.public.orders"
TEXT = "SELECT * FROM sys.public.orders WHERE id = ?"
METADATA = {"source_database": {"engine": "postgresql", "database_name": "sys"}}


def _query(qid: str = "q1", tables: list[str] | None = None) -> dict:
    return {
        "query_id": qid,
        "query_text": TEXT,
        "query_type": "SELECT",
        "tables_accessed": [TABLE] if tables is None else tables,
        "join_count": 0,
        "has_joins": False,
        "has_aggregations": False,
        "calls_per_second": 5.0,
        "rows_returned_avg": 1.0,
    }


def _collector(queries: list[dict] | None = None) -> dict:
    return {
        "metadata": METADATA,
        "database_schema": {
            "tables": [
                {
                    "table_id": TABLE,
                    "table_name": "orders",
                    "primary_key": ["id"],
                    "foreign_keys": [],
                    "columns": [],
                }
            ]
        },
        "queries": {"query_patterns": queries if queries is not None else [_query()]},
    }


class _Spy:
    """Wraps ``module.name``; records the scope arg (positional ``pos`` or keyword ``kw``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: Any, name: str, pos: int, kw: str):
        self.scopes: list[Any] = []
        real = getattr(module, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.scopes.append(
                kwargs[kw] if kw in kwargs else args[pos] if len(args) > pos else None
            )
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    def assert_every_call_has_the_scope(self, at_least: int = 1) -> None:
        assert len(self.scopes) >= at_least, f"expected >= {at_least} calls, saw {len(self.scopes)}"
        assert [getattr(s, "name", None) for s in self.scopes] == ["sys"] * len(self.scopes)


def _resolver(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
    spy = _Spy(mp, assignment_resolver, "is_utility_statement", 2, "source")
    triage_out = {"selected_agents": [{"agent_type": "aurora_postgresql"}], "signals": []}
    analysis = {
        "aurora_postgresql": {
            "table_recommendations": [{"table_id": TABLE, "confidence_score": 70}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        }
    }
    AssignmentResolver().resolve(triage_out, analysis, _collector())
    return [spy]


def _synthesis_groups(site: str) -> Callable[[pytest.MonkeyPatch, Any], list[_Spy]]:
    def run(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
        spy = _Spy(mp, synthesis_report, "is_utility_statement", 2, "source")
        query = _query(tables=["unknown"])
        data = SynthesisData(job_id="j", database_name="sys", collector=_collector([query]))
        if site == "design":
            data.engines["dynamodb"] = EngineArtifacts(
                engine="dynamodb",
                schema_design={
                    "access_patterns": [
                        {
                            "pattern_id": "p1",
                            "source_tables": ["unknown"],
                            "sql_text": TEXT,
                            "query_ids": ["q1"],
                        }
                    ]
                },
            )
        else:
            data.assignment = {
                "query_assignments": [
                    {
                        "query_id": "q1",
                        "assigned_engine": "aurora_postgresql",
                        "source_tables": ["unknown"],
                    }
                ]
            }
        build_query_groups(data)
        return [spy]

    return run


def _waves(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
    spy = _Spy(mp, migration_waves, "is_engine_system_object", 1, "source")
    store = LocalArtifactStore(base_dir=str(tmp_path))
    db, job = "sys", "job-sys"
    store.write_json(
        f"{db}/{job}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": "aurora_postgresql"}], "signals": []},
    )
    store.write_json(f"{db}/{job}/collector/output.json", _collector())
    store.write_json(
        f"{db}/{job}/analysis-aurora_postgresql/analysis.json",
        {
            "table_recommendations": [{"table_id": TABLE, "confidence_score": 70}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            "cost_estimate": {"monthly_cost_usd": 10},
        },
    )
    store.write_json(
        f"{db}/{job}/assignment/v1/assignment.json",
        {
            "job_id": job,
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "aurora_postgresql", "source_tables": [TABLE]}
            ],
            "table_assignments": [
                {
                    "table_id": TABLE,
                    "primary_engine": "aurora_postgresql",
                    "engines": ["aurora_postgresql"],
                    "query_count": 1,
                }
            ],
            "co_dependency_groups": [],
        },
    )
    result = run_synthesis_deterministic(job, db, store, assignment_version=1)
    # Behaviour as well as the spy: the application table stays in wave 1.
    assert result["migration_waves"][0]["tables"] == [TABLE]
    return [spy]


def _design_view(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
    spy = _Spy(mp, design_view, "is_utility_statement", 2, "source")
    pattern = AgentQueryPattern(
        query_id="q1",
        query_text=TEXT,
        query_type=QueryType.SELECT,
        frequency_per_hour=1.0,
        tables_accessed=[TABLE],
    )
    collector = SimpleNamespace(queries=SimpleNamespace(query_patterns=[pattern]))
    design_view._hot_queries(collector, 5, _collector(), {design_view._key(TABLE)})  # type: ignore[arg-type]
    return [spy]


def _anti_patterns(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
    admin = _Spy(mp, analysis_tools, "_is_admin_statement", 1, "source")
    shape = _Spy(mp, analysis_tools, "single_table_access_shape", 99, "source")
    analysis_tools.analyze_aurora_common_use_cases(_collector())
    # Three admin-skip sites (the scan, anti-05, anti-06) and two shape sites.
    admin.assert_every_call_has_the_scope(at_least=3)
    shape.assert_every_call_has_the_scope(at_least=2)
    return []


def _query_shape(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
    spy = _Spy(mp, query_shape, "detect_required_capabilities", 3, "source")
    query_shape.single_table_access_shape(_query(), source=_scope())
    return [spy]


def _scope() -> Any:
    from src.agents.referee.table_resolution import SourceScope

    return SourceScope.from_collector(_collector())


def _triage(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
    spy = _Spy(mp, triage, "detect_required_capabilities", 3, "source")
    triage.detect_query_capabilities(_collector(), [])
    return [spy]


def _capability(mp: pytest.MonkeyPatch, tmp_path: Any) -> list[_Spy]:
    spy = _Spy(mp, capability_registry, "is_utility_statement", 2, "source")
    capability_registry.detect_required_capabilities(TEXT, [], [TABLE], _scope())
    return [spy]


CONSUMERS: dict[str, Callable[[pytest.MonkeyPatch, Any], list[_Spy]]] = {
    "assignment_resolver": _resolver,
    "synthesis_report access patterns": _synthesis_groups("design"),
    "synthesis_report routing records": _synthesis_groups("routing"),
    "synthesis_handler migration waves": _waves,
    "design_view hot queries": _design_view,
    "aurora anti-pattern scan": _anti_patterns,
    "query_shape": _query_shape,
    "triage capabilities": _triage,
    "capability_registry": _capability,
}


@pytest.mark.parametrize("name", list(CONSUMERS))
def test_consumer_passes_the_collected_database_down(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    spies = CONSUMERS[name](monkeypatch, tmp_path)
    for spy in spies:
        spy.assert_every_call_has_the_scope()
