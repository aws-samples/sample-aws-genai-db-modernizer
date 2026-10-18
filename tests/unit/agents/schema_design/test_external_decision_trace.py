"""External (Claude Code) DocumentDB and OpenSearch designs carry their decision trace (#1068).

The Bedrock path scoped the trace per call in #1066. The external path writes
the designer's input to a file instead (single pass: ``prepare_schema_design_input``,
grouped: ``input_group_<G>.json`` from ``--split``), and neither held the trace
although both skills tell the designer to read ``decision_trace``. No test
calls a model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.schema_design.handler import prepare_schema_design_input, run_schema_split
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-001"
ENGINES = ["documentdb", "opensearch"]


def _table(name: str) -> dict:
    return {
        "table_id": f"{DB}.{name}",
        "table_name": name,
        "row_count": 10,
        "columns": [{"column_name": "id", "data_type": "int", "is_primary_key": True}],
    }


def _query(qid: str, table: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table} WHERE id = 1",
        "tables_accessed": [f"{DB}.{table}"],
        "frequency_per_hour": 10.0,
    }


ORDERS = tuple(f"o{i}" for i in range(5))  # five queries make a group of their own
USERS = tuple(f"u{i}" for i in range(5))


def _trace(engine: str) -> dict:
    return {
        "summary": {"queries_analyzed": 11, "queries_matched": 11, "queries_unmatched": 0},
        "query_matches": [
            {"query_id": q, "matched_patterns": [], "matched_anti_patterns": []}
            for q in (*ORDERS, *USERS, "d1")
        ],
        "workload_classifications": [
            {"table_id": f"{DB}.orders", "workload_type": "SEARCH"},
            {"table_id": f"{DB}.users", "workload_type": "SEARCH"},
            {"table_id": f"{DB}.other", "workload_type": "SEARCH"},
        ],
        "embedding_candidates": [
            {"parent_table": f"{DB}.orders", "child_table": f"{DB}.orders"},
            {"parent_table": f"{DB}.users", "child_table": f"{DB}.users"},
        ],
        "trace_version": "1.0",
        "agent": f"{engine}-analysis-agent",
    }


def _seed(tmp_path: Path, engine: str, *, with_trace: bool = True) -> LocalArtifactStore:
    """orders (o1) and users (u1) go to ``engine``; other (d1) goes to DynamoDB."""
    store = LocalArtifactStore(str(tmp_path))
    base = f"{DB}/{JOB}"
    queries = [
        *(_query(q, "orders") for q in ORDERS),
        *(_query(q, "users") for q in USERS),
        _query("d1", "other"),
    ]
    store.write_json(
        f"{base}/collector/output.json",
        {
            "contract_version": "3.0",
            "database_schema": {"tables": [_table("orders"), _table("users"), _table("other")]},
            "queries": {"query_patterns": queries, "total_queries_analyzed": len(queries)},
        },
    )
    store.write_json(f"{base}/analysis-{engine}/analysis.json", {"contract_version": "2.1"})
    if with_trace:
        for e in ENGINES:
            store.write_json(f"{base}/analysis-{e}/decision-trace.json", _trace(e))
    store.write_json(
        f"{base}/assignment/v1/assignment.json",
        {
            "version": 1,
            "co_dependency_groups": [],
            "query_assignments": [
                {
                    "query_id": q["query_id"],
                    "assigned_engine": engine if q["query_id"] != "d1" else "dynamodb",
                    "in_scope": True,
                    "source_tables": q["tables_accessed"],
                }
                for q in queries
            ],
        },
    )
    return store


def _qids(trace: dict) -> set[str]:
    return {m["query_id"] for m in trace["query_matches"]}


@pytest.mark.parametrize("engine", ENGINES)
class TestSinglePassExternalInput:
    def test_carries_the_engines_own_trace_for_its_own_queries(self, tmp_path, engine):
        store = _seed(tmp_path, engine)
        out = prepare_schema_design_input(JOB, DB, engine, store, assignment_version=1)
        trace = out["decision_trace"]
        assert trace["agent"] == f"{engine}-analysis-agent"
        assert _qids(trace) == {*ORDERS, *USERS}
        assert {c["table_id"] for c in trace["workload_classifications"]} == {
            f"{DB}.orders",
            f"{DB}.users",
        }

    def test_without_a_trace_file_the_key_is_absent(self, tmp_path, engine):
        store = _seed(tmp_path, engine, with_trace=False)
        out = prepare_schema_design_input(JOB, DB, engine, store, assignment_version=1)
        assert "decision_trace" not in out


def test_an_engine_without_a_trace_skill_gets_no_trace_key(tmp_path):
    store = _seed(tmp_path, "documentdb")
    store.write_json(f"{DB}/{JOB}/analysis-dynamodb/analysis.json", {"contract_version": "2.1"})
    store.write_json(f"{DB}/{JOB}/analysis-dynamodb/decision-trace.json", _trace("dynamodb"))
    out = prepare_schema_design_input(JOB, DB, "dynamodb", store, assignment_version=1)
    assert "decision_trace" not in out


@pytest.mark.parametrize("engine", ENGINES)
class TestGroupedExternalInput:
    def _groups(self, store, engine) -> list[dict]:
        run_schema_split(JOB, DB, engine, store, assignment_version=1)
        root = f"{DB}/{JOB}/schema-{engine}/v1"
        manifest = store.read_json(f"{root}/groups_manifest.json")
        return [store.read_json(f"{root}/{g['input_file']}") for g in manifest["groups"]]

    def test_each_groups_input_carries_only_its_own_trace_entries(self, tmp_path, engine):
        groups = self._groups(_seed(tmp_path, engine), engine)
        assert len(groups) == 2
        seen = set()
        for g in groups:
            own = {q["query_id"] for q in g["collector_output"]["queries"]["query_patterns"]}
            tables = {t["table_id"] for t in g["collector_output"]["database_schema"]["tables"]}
            trace = g["decision_trace"]
            assert trace["agent"] == f"{engine}-analysis-agent"
            assert _qids(trace) == own
            assert {c["table_id"] for c in trace["workload_classifications"]} == tables
            seen |= own
        assert seen == {*ORDERS, *USERS}

    def test_without_a_trace_file_a_group_has_no_trace_key(self, tmp_path, engine):
        groups = self._groups(_seed(tmp_path, engine, with_trace=False), engine)
        assert groups and all("decision_trace" not in g for g in groups)


def test_a_dynamodb_group_input_has_no_trace(tmp_path):
    store = _seed(tmp_path, "dynamodb")
    store.write_json(f"{DB}/{JOB}/analysis-dynamodb/decision-trace.json", _trace("dynamodb"))
    run_schema_split(JOB, DB, "dynamodb", store, assignment_version=1)
    root = f"{DB}/{JOB}/schema-dynamodb/v1"
    for g in store.read_json(f"{root}/groups_manifest.json")["groups"]:
        assert "decision_trace" not in json.loads(store.read_bytes(f"{root}/{g['input_file']}"))


def test_a_table_shared_by_two_groups_leaks_no_other_groups_entries():
    from src.agents.schema_design.group_input import build_group_input

    shared = _table("shared")
    queries = {"a": _query("a1", "orders"), "b": _query("b1", "users")}
    trace = {
        "query_matches": [{"query_id": "a1"}, {"query_id": "b1"}, {"query_id": "z9"}],
        "workload_classifications": [
            {"table_id": f"{DB}.shared", "workload_type": "SEARCH"},
            {"table_id": f"{DB}.orders", "workload_type": "SEARCH"},
            {"table_id": f"{DB}.users", "workload_type": "SEARCH"},
        ],
        "embedding_candidates": [
            {"parent_table": f"{DB}.orders", "child_table": f"{DB}.shared"},
            {"parent_table": f"{DB}.users", "child_table": f"{DB}.shared"},
        ],
    }
    out = {}
    for key, own in (("a", "orders"), ("b", "users")):
        out[key] = build_group_input(
            job_id=JOB,
            database_name=DB,
            engine="documentdb",
            group_index=0,
            group_name=key,
            primary_tables=[f"{DB}.{own}"],
            group_queries=[queries[key]],
            group_tables=[_table(own), shared],
            collector_output={"queries": {}},
            analysis_output={},
            total_queries=2,
            decision_trace=trace,
        )["decision_trace"]
    for key, own, other in (("a", "orders", "users"), ("b", "users", "orders")):
        t = out[key]
        assert _qids(t) == {f"{key}1"}
        assert {c["table_id"] for c in t["workload_classifications"]} == {
            f"{DB}.{own}",
            f"{DB}.shared",
        }
        assert [e["parent_table"] for e in t["embedding_candidates"]] == [f"{DB}.{own}"]
        assert f"{DB}.{other}" not in json.dumps(t)


def test_the_group_size_budget_holds_with_the_trace_included(tmp_path):
    from src.agents.schema_design.group_splitter import MAX_GROUP_INPUT_CHARS

    engine = "documentdb"
    n = 18
    ids = [f"q{i}" for i in range(n)]
    store = LocalArtifactStore(str(tmp_path))
    base = f"{DB}/{JOB}"
    queries = [_query(q, "orders") for q in ids]
    store.write_json(
        f"{base}/collector/output.json",
        {
            "contract_version": "3.0",
            "database_schema": {"tables": [_table("orders")]},
            "queries": {"query_patterns": queries, "total_queries_analyzed": n},
        },
    )
    store.write_json(f"{base}/analysis-{engine}/analysis.json", {"contract_version": "2.1"})
    sizes = {}
    for label, pad in (("small", 10), ("large", MAX_GROUP_INPUT_CHARS // 6)):
        trace = {
            "query_matches": [
                {"query_id": q, "matched_patterns": ["P" * pad], "matched_anti_patterns": []}
                for q in ids
            ],
        }
        store.write_json(f"{base}/analysis-{engine}/decision-trace.json", trace)
        run_schema_split(JOB, DB, engine, store)
        root = f"{base}/schema-{engine}/v1"
        manifest = store.read_json(f"{root}/groups_manifest.json")
        files = [store.read_bytes(f"{root}/{g['input_file']}") for g in manifest["groups"]]
        assert all(len(f) <= MAX_GROUP_INPUT_CHARS for f in files)
        covered = {
            m["query_id"] for f in files for m in json.loads(f)["decision_trace"]["query_matches"]
        }
        assert covered == set(ids)
        sizes[label] = len(files)
    # The trace's size counts toward the budget: the large one forces a split.
    assert sizes["small"] == 1
    assert sizes["large"] > 1
