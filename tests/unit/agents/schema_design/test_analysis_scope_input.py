"""A design call's analysis input names only its own tables and queries (#162).

``filter_analysis_for_assignment`` narrows the engine's ``analysis.json`` to the tables and
queries of the assignment-filtered collector input: table recommendations by table,
aggregates by member table, patterns and anti-patterns by query or table (their ids
trimmed to the call's own). Names resolve through ``TableNameResolver`` on the whole
collection, as the collector side does (#319). The three single-call entry points use it:
the external request, the Bedrock call and the post-schema cascade, for an owner engine
and for the ElastiCache overlay (which owns no query: its scope is the reads it fronts).
Each DynamoDB design group (``build_group_input``) applies the same rule
(``group_input.narrow_analysis``). No test calls a model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb_deterministic
from src.agents.schema_design import handler
from src.agents.schema_design.group_input import build_group_input
from src.agents.schema_design.handler import filter_analysis_for_assignment
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import AnalysisOutputContract
from src.storage.local_store import LocalArtifactStore
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "ecommerce", "job-162"
ENGINE = "documentdb"
# The queries the assignment gives the engine; every other query goes to Aurora MySQL.
ON_ENGINE = {"q04-session-lookup", "q05-session-upsert", "q06-user-sessions", "q02-user-by-id"}
OWN_TABLES = {"ecommerce.sessions", "ecommerce.users"}


def _collector(tables: list[str], queries: dict[str, list[str]]) -> dict:
    return {
        "database_schema": {
            "tables": [{"table_id": t, "table_name": t.rsplit(".", 1)[-1]} for t in tables]
        },
        "queries": {
            "query_patterns": [{"query_id": q, "tables_accessed": ts} for q, ts in queries.items()]
        },
    }


def _rec(table_id: str) -> dict:
    return {"table_id": table_id, "confidence_score": 50}


FULL = _collector(
    ["shop.orders", "shop.users", "a.items", "b.items"],
    {"q1": ["shop.orders"], "q2": ["shop.users"], "q3": ["a.items"], "q4": ["b.items"]},
)
SCOPED = _collector(["shop.orders", "a.items"], {"q1": ["shop.orders"], "q3": ["a.items"]})


def _filter(analysis: dict) -> dict:
    return filter_analysis_for_assignment(analysis, FULL, SCOPED)


class TestTheFilter:
    def test_keeps_the_recommendations_of_its_own_tables_in_order(self):
        analysis = {
            "table_recommendations": [
                _rec("a.items"),
                _rec("shop.users"),
                _rec("shop.orders"),
                _rec("b.items"),
            ]
        }
        assert [r["table_id"] for r in _filter(analysis)["table_recommendations"]] == [
            "a.items",
            "shop.orders",
        ]

    def test_resolves_a_name_qualified_another_way(self):
        """A bare or differently qualified name resolves to its collector table (#319)."""
        analysis = {"table_recommendations": [_rec("orders"), _rec("SHOP.ORDERS"), _rec("users")]}
        assert [r["table_id"] for r in _filter(analysis)["table_recommendations"]] == [
            "orders",
            "SHOP.ORDERS",
        ]

    def test_an_ambiguous_bare_name_is_not_kept(self):
        """``items`` is two tables; only one of them is the engine's, so it never matches."""
        analysis = {"table_recommendations": [_rec("items"), _rec("b.items"), _rec("a.items")]}
        assert [r["table_id"] for r in _filter(analysis)["table_recommendations"]] == ["a.items"]

    def test_keeps_an_aggregate_with_one_of_its_tables(self):
        analysis = {
            "table_recommendations": [],
            "aggregate_recommendations": [
                {"root_table": "shop.users", "member_tables": ["shop.users", "shop.orders"]},
                {"root_table": "shop.users", "member_tables": ["shop.users", "b.items"]},
            ],
        }
        aggregates = _filter(analysis)["aggregate_recommendations"]
        assert [a["member_tables"] for a in aggregates] == [["shop.users", "shop.orders"]]

    def test_trims_each_pattern_to_its_own_queries_and_tables(self):
        analysis = {
            "table_recommendations": [],
            "workload_analysis": {
                "patterns_detected": [
                    {"pattern_id": "P1", "query_ids": ["q1", "q2"], "table_ids": ["shop.orders"]},
                    {"pattern_id": "P2", "query_ids": ["q2", "q4"], "table_ids": ["b.items"]},
                    {"pattern_id": "P3", "query_ids": None, "table_ids": ["a.items", "b.items"]},
                    {"pattern_id": "P4", "description": "whole workload"},
                ],
                "anti_patterns_detected": [
                    {"anti_pattern_id": "A1", "query_ids": ["q4"]},
                    {"anti_pattern_id": "A2", "query_ids": ["q3", "q4"], "table_ids": None},
                ],
                "summary": "kept",
            },
        }
        workload = _filter(analysis)["workload_analysis"]
        assert workload["patterns_detected"] == [
            {"pattern_id": "P1", "query_ids": ["q1"], "table_ids": ["shop.orders"]},
            {"pattern_id": "P3", "query_ids": None, "table_ids": ["a.items"]},
            {"pattern_id": "P4", "description": "whole workload"},
        ]
        assert workload["anti_patterns_detected"] == [
            {"anti_pattern_id": "A2", "query_ids": ["q3"], "table_ids": None}
        ]
        assert workload["summary"] == "kept"

    def test_keeps_every_other_field_and_never_mutates_its_input(self):
        analysis = {
            "agent_metadata": {"agent_name": "x"},
            "cost_estimate": {"monthly": 1},
            "table_recommendations": [_rec("shop.users")],
            "aggregate_recommendations": None,
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": None},
        }
        before = copy.deepcopy(analysis)
        out = _filter(analysis)
        assert analysis == before
        assert out["agent_metadata"] == before["agent_metadata"]
        assert out["cost_estimate"] == before["cost_estimate"]
        assert out["table_recommendations"] == []
        assert out["aggregate_recommendations"] is None
        assert out["workload_analysis"] == {"patterns_detected": [], "anti_patterns_detected": None}

    def test_an_empty_scope_keeps_no_table_or_query(self):
        empty = _collector([], {})
        analysis = {
            "table_recommendations": [_rec("shop.orders")],
            "workload_analysis": {"patterns_detected": [{"pattern_id": "P", "query_ids": ["q1"]}]},
        }
        out = filter_analysis_for_assignment(analysis, FULL, empty)
        assert out["table_recommendations"] == []
        assert out["workload_analysis"]["patterns_detected"] == []

    def test_a_collection_without_a_schema_compares_names_exactly(self):
        bare = {"queries": {"query_patterns": []}}
        scoped = _collector(["shop.orders"], {})
        analysis = {"table_recommendations": [_rec("shop.orders"), _rec("orders")]}
        out = filter_analysis_for_assignment(analysis, bare, scoped)
        assert [r["table_id"] for r in out["table_recommendations"]] == ["shop.orders"]


class TestTheGroupPathAgrees:
    """A DynamoDB design group and a single design call narrow the analysis the same way."""

    ANALYSIS = {
        "table_recommendations": [
            _rec("orders"),
            _rec("shop.users"),
            _rec("items"),
            _rec("a.items"),
            _rec("b.items"),
        ],
        "aggregate_recommendations": [
            {"root_table": "orders", "member_tables": ["orders", "shop.users"]},
            {"root_table": "shop.users", "member_tables": ["shop.users", "b.items"]},
        ],
        "workload_analysis": {
            "patterns_detected": [
                {"pattern_id": "P1", "query_ids": ["q1", "q2"], "table_ids": ["SHOP.ORDERS"]},
                {"pattern_id": "P2", "query_ids": ["q4"], "table_ids": ["b.items"]},
                {"pattern_id": "P3", "description": "whole workload"},
            ],
            "anti_patterns_detected": [
                {"anti_pattern_id": "A1", "query_ids": ["q3", "q4"]},
                {"anti_pattern_id": "A2", "description": "whole workload"},
            ],
        },
    }

    def _group(self) -> dict:
        tables = SCOPED["database_schema"]["tables"]
        group = build_group_input(
            job_id=JOB,
            database_name=DB,
            engine="dynamodb",
            group_index=0,
            group_name="g",
            primary_tables=[t["table_id"] for t in tables],
            group_queries=SCOPED["queries"]["query_patterns"],
            group_tables=tables,
            collector_output=FULL,
            analysis_output=self.ANALYSIS,
            total_queries=len(FULL["queries"]["query_patterns"]),
        )
        return dict(group["analysis_output"])

    @staticmethod
    def _view(analysis: dict) -> dict:
        workload = analysis["workload_analysis"]
        return {
            "tables": [r["table_id"] for r in analysis["table_recommendations"]],
            "aggregates": [
                a["root_table"] for a in analysis.get("aggregate_recommendations") or []
            ],
            "patterns": [
                (p.get("pattern_id"), p.get("query_ids") or [], p.get("table_ids") or [])
                for p in workload["patterns_detected"]
            ],
            "anti_patterns": [
                (p.get("anti_pattern_id"), p.get("query_ids") or [], p.get("table_ids") or [])
                for p in workload.get("anti_patterns_detected") or []
            ],
        }

    def test_both_paths_keep_the_same_entries(self):
        single = self._view(_filter(self.ANALYSIS))
        assert self._view(self._group()) == single
        assert single == {
            "tables": ["orders", "a.items"],
            "aggregates": ["orders"],
            "patterns": [("P1", ["q1"], ["SHOP.ORDERS"]), ("P3", [], [])],
            "anti_patterns": [("A1", ["q3"], []), ("A2", [], [])],
        }

    def test_a_group_keeps_a_workload_wide_pattern(self):
        patterns = self._group()["workload_analysis"]["patterns_detected"]
        assert {"pattern_id": "P3", "description": "whole workload"} in [
            {k: v for k, v in p.items() if k in ("pattern_id", "description")} for p in patterns
        ]

    def test_a_group_resolves_a_name_qualified_another_way(self):
        assert "orders" in [r["table_id"] for r in self._group()["table_recommendations"]]


# --- the entry points --------------------------------------------------------------


def _analysis(collector: dict) -> dict:
    contract, _trace, _ = analyze_for_documentdb_deterministic(
        AnalysisInput(
            job_id=JOB, collector_output=collector, target_database=TargetDatabase.DOCUMENTDB
        )
    )
    return contract.model_dump(mode="json")


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    collector = get_ecommerce_collector_output()
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    analysis = _analysis(collector)
    # The analysis recommends every table, not only the engine's.
    assert {r["table_id"] for r in analysis["table_recommendations"]} > OWN_TABLES
    store.write_json(f"{DB}/{JOB}/analysis-{ENGINE}/analysis.json", analysis)
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "query_assignments": [
                {
                    "query_id": q["query_id"],
                    "assigned_engine": ENGINE if q["query_id"] in ON_ENGINE else "aurora_mysql",
                    "in_scope": True,
                    "source_tables": q["tables_accessed"],
                }
                for q in collector["queries"]["query_patterns"]
            ]
        },
    )
    return store


def _tables(analysis: dict) -> set[str]:
    return {r["table_id"] for r in analysis["table_recommendations"]}


def _queries(analysis: dict) -> set[str]:
    workload = analysis["workload_analysis"]
    return {
        q
        for p in [*workload["patterns_detected"], *(workload.get("anti_patterns_detected") or [])]
        for q in p.get("query_ids") or []
    }


def test_the_external_request_carries_only_the_engines_own(store):
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    analysis = request["analysis_output"]
    assert _tables(analysis) == OWN_TABLES
    assert _queries(analysis) <= ON_ENGINE
    AnalysisOutputContract.model_validate(analysis)
    # The analysis on disk is unchanged.
    on_disk = store.read_json(f"{DB}/{JOB}/analysis-{ENGINE}/analysis.json")
    assert _tables(on_disk) > OWN_TABLES


@pytest.mark.passes_on_main
def test_a_legacy_unversioned_request_keeps_the_whole_analysis(store):
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=0)
    on_disk = store.read_json(f"{DB}/{JOB}/analysis-{ENGINE}/analysis.json")
    assert request["analysis_output"]["table_recommendations"] == on_disk["table_recommendations"]


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    def fake_dispatch(target_type, collector_path, analysis_path, **_kw):
        seen["analysis"] = json.loads(Path(analysis_path).read_text())
        raise RuntimeError("stop before any model call")

    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(handler, "_dispatch_schema_agent", fake_dispatch)
    return seen


def test_the_bedrock_call_reads_only_the_engines_own(store, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(RuntimeError, match="stop before"):
        handler.run_schema_design(JOB, DB, ENGINE, store, assignment_version=1)
    assert _tables(seen["analysis"]) == OWN_TABLES
    assert _queries(seen["analysis"]) <= ON_ENGINE


def test_the_cascade_reads_the_engines_own_and_the_injected_queries(store, monkeypatch):
    seen = _capture(monkeypatch)
    with pytest.raises(RuntimeError, match="stop before"):
        handler.run_schema_design_with_injected(
            JOB, DB, ENGINE, store, {"q14-product-reviews"}, assignment_version=1
        )
    assert _tables(seen["analysis"]) == OWN_TABLES | {"ecommerce.product_reviews"}
    assert _queries(seen["analysis"]) <= ON_ENGINE | {"q14-product-reviews"}


CACHE = "elasticache"
# The reads the cache fronts: their owner keeps them, the cache designs their keys.
CACHED_READS = {"q01-product-by-id", "q04-session-lookup"}


@pytest.fixture
def cache_store(tmp_path: Path) -> LocalArtifactStore:
    collector = get_ecommerce_collector_output()
    store = LocalArtifactStore(base_dir=str(tmp_path / "cache"))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(f"{DB}/{JOB}/analysis-{CACHE}/analysis.json", _analysis(collector))
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "query_assignments": [
                {
                    "query_id": q["query_id"],
                    "assigned_engine": "aurora_mysql",
                    "in_scope": True,
                    "source_tables": q["tables_accessed"],
                    **({"cache_engine": CACHE} if q["query_id"] in CACHED_READS else {}),
                }
                for q in collector["queries"]["query_patterns"]
            ]
        },
    )
    return store


def test_the_cache_overlay_keeps_each_cached_read_tables_recommendation(cache_store):
    """ElastiCache owns no query: its scope is the reads it fronts (#296)."""
    request = handler.prepare_schema_design_input(JOB, DB, CACHE, cache_store, assignment_version=1)
    analysis = request["analysis_output"]
    assert _tables(analysis) == {"ecommerce.products", "ecommerce.sessions"}
    assert _queries(analysis) <= CACHED_READS
    # The owners' writes on the cached tables stay as marked context.
    context = request["collector_output"]["cache_invalidation_context"]
    assert context["write_queries"]
    assert all(w["context_only"] for w in context["write_queries"])
