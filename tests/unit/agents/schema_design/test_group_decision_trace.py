"""Grouped DocumentDB and OpenSearch designs load their own decision trace (#1060).

A single-pass design hands the designer the engine's analysis decision trace.
A grouped design used to hand it none. Each group now gets the trace scoped to
its own queries and tables, in its own file. No test calls a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.schema_design.group_trace import scope_decision_trace
from src.agents.schema_design.handler import run_schema_design_auto
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-001"


def _query(qid: str, table: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table}",
        "tables_accessed": [f"{DB}.{table}"],
        "frequency_per_hour": 1.0,
    }


def _table(name: str) -> dict:
    return {"table_id": f"{DB}.{name}", "table_name": name, "row_count": 1, "columns": []}


def _group_collector(queries: list[dict], tables: list[str]) -> dict:
    return {
        "queries": {"query_patterns": queries},
        "database_schema": {"tables": [_table(t) for t in tables]},
    }


A, B = ("a1", "a2"), ("b1", "b2", "b3")
GROUP_A = _group_collector([_query(q, "orders") for q in A], ["orders", "items"])
GROUP_B = _group_collector([_query(q, "users") for q in B], ["users", "profiles"])


def _trace() -> dict:
    """A trace of the shape both engines write, over both groups' queries."""
    ids = [*A, *B]
    return {
        "summary": {"queries_analyzed": 5, "queries_matched": 5, "queries_unmatched": 0},
        "query_matches": [
            {"query_id": q, "matched_patterns": ["P"], "matched_anti_patterns": []} for q in ids
        ],
        "pattern_summaries": [
            {"pattern_id": "P1", "tables_involved": [f"{DB}.orders"]},
            {"pattern_id": "P2", "tables_involved": [f"{DB}.users"]},
        ],
        "workload_classifications": [
            {"table_id": f"{DB}.orders", "workload_type": "SEARCH"},
            {"table_id": f"{DB}.users", "workload_type": "NOT_SUITABLE"},
        ],
        "recommendation_derivations": [
            {"table_id": f"{DB}.orders"},
            {"table_id": f"{DB}.users"},
        ],
        "embedding_candidates": [
            {"parent_table": f"{DB}.orders", "child_table": f"{DB}.items"},
            {"parent_table": f"{DB}.users", "child_table": f"{DB}.profiles"},
            {"parent_table": f"{DB}.orders", "child_table": f"{DB}.users"},
        ],
        "relationships": [
            {"type": "1:M", "from_table": f"{DB}.items", "to_table": f"{DB}.orders"},
            {"type": "1:M", "from_table": f"{DB}.profiles", "to_table": f"{DB}.users"},
        ],
        "timestamp_columns": {
            "read_by": "column type",
            "name_fallbacks": [{"query_id": "a1"}, {"query_id": "b2"}],
        },
        "denormalization_strategies": [
            {"parent_table": f"{DB}.orders", "child_table": f"{DB}.items", "strategy": "embed"},
            {"parent_table": f"{DB}.users", "child_table": f"{DB}.profiles", "strategy": "embed"},
        ],
        "documentdb_compatibility": {
            "target_version": "8.0",
            "version_dependent_features_detected": [
                "$graphLookup (recursive/hierarchical queries): needs 8.0"
            ],
            "unsupported_features_detected": [],
        },
        "trace_version": "1.0",
        "agent": "documentdb-analysis-agent",
        "llm_advisor": {"status": "skipped"},
    }


RECURSIVE = (
    "WITH RECURSIVE t AS (SELECT id FROM users UNION ALL SELECT u.id FROM users u JOIN t ON 1=1) "
    "SELECT * FROM t"
)


HANDLED_KEYS = frozenset(
    {
        "summary",
        "query_matches",
        "pattern_summaries",
        "workload_classifications",
        "recommendation_derivations",
        "embedding_candidates",
        "denormalization_strategies",
        "polymorphic_tables",
        "table_groups",
        "relationships",
        "timestamp_columns",
        "documentdb_compatibility",
    }
)


def _qids(trace: dict) -> set[str]:
    return {m["query_id"] for m in trace["query_matches"]}


class TestScopeDecisionTrace:
    def test_keeps_only_the_groups_queries_and_tables(self):
        scoped = scope_decision_trace(_trace(), GROUP_A)
        assert _qids(scoped) == set(A)
        assert [p["pattern_id"] for p in scoped["pattern_summaries"]] == ["P1"]
        assert [c["table_id"] for c in scoped["workload_classifications"]] == [f"{DB}.orders"]
        assert [d["table_id"] for d in scoped["recommendation_derivations"]] == [f"{DB}.orders"]
        assert [c["child_table"] for c in scoped["embedding_candidates"]] == [f"{DB}.items"]
        assert [r["from_table"] for r in scoped["relationships"]] == [f"{DB}.items"]
        assert scoped["timestamp_columns"]["name_fallbacks"] == [{"query_id": "a1"}]

    def test_the_groups_do_not_overlap_and_together_hold_every_query(self):
        a = _qids(scope_decision_trace(_trace(), GROUP_A))
        b = _qids(scope_decision_trace(_trace(), GROUP_B))
        assert a.isdisjoint(b)
        assert a | b == {*A, *B}

    def test_the_summary_counts_are_the_groups_own(self):
        trace = _trace()
        trace["query_matches"][0]["matched_patterns"] = []
        scoped = scope_decision_trace(trace, GROUP_A)
        assert scoped["summary"]["queries_analyzed"] == 2
        assert scoped["summary"]["queries_matched"] == 1
        assert scoped["summary"]["queries_unmatched"] == 1
        assert scoped["summary"]["patterns_detected"] == 1
        assert scoped["summary"]["tables_search"] == 1

    def test_the_input_trace_is_not_changed(self):
        trace = _trace()
        before = json.dumps(trace, sort_keys=True)
        scope_decision_trace(trace, GROUP_A)
        assert json.dumps(trace, sort_keys=True) == before

    def test_the_denormalization_strategies_are_the_groups_own(self):
        scoped = scope_decision_trace(_trace(), GROUP_A)
        assert [s["child_table"] for s in scoped["denormalization_strategies"]] == [f"{DB}.items"]

    def test_the_feature_lists_cover_the_groups_queries_only(self):
        group_b = _group_collector([_query(q, "users") for q in B], ["users", "profiles"])
        group_b["queries"]["query_patterns"][0]["query_text"] = RECURSIVE
        a = scope_decision_trace(_trace(), GROUP_A)["documentdb_compatibility"]
        b = scope_decision_trace(_trace(), group_b)["documentdb_compatibility"]
        assert a["version_dependent_features_detected"] == []
        assert [f.split(" ")[0] for f in b["version_dependent_features_detected"]] == [
            "$graphLookup"
        ]
        assert a["target_version"] == b["target_version"] == "8.0"

    def test_a_pattern_summary_names_only_the_groups_tables_and_no_workload_counts(self):
        trace = _trace()
        trace["pattern_summaries"][0].update(
            tables_involved=[f"{DB}.orders", f"{DB}.users"],
            queries_matched_count=9,
            total_calls_per_second=3.0,
        )
        [p] = scope_decision_trace(trace, GROUP_A)["pattern_summaries"]
        assert p["tables_involved"] == [f"{DB}.orders"]
        assert "queries_matched_count" not in p and "total_calls_per_second" not in p

    def test_workload_wide_keys_pass_through_and_unhandled_keys_are_dropped(self):
        trace = {**_trace(), "a_new_key_naming_tables": [f"{DB}.users"]}
        scoped = scope_decision_trace(trace, GROUP_A)
        assert scoped["agent"] == "documentdb-analysis-agent"
        assert scoped["llm_advisor"] == {"status": "skipped"}
        assert "a_new_key_naming_tables" not in scoped
        assert "polymorphic_tables" not in scoped
        assert scope_decision_trace({}, GROUP_A) == {}

    def test_a_typed_trace_keeps_its_contract_version(self):
        """The designer checks the scoped trace's version (#1188), so scoping keeps it."""
        trace = {**_trace(), "contract_version": "1.0"}
        assert scope_decision_trace(trace, GROUP_A)["contract_version"] == "1.0"


class TestEveryTraceKeyIsHandled:
    """A key an analysis starts writing must be scoped or declared workload-wide."""

    def _traces(self) -> dict[str, dict]:
        from src.agents.analysis.documentdb_analysis_agent import (
            analyze_for_documentdb_deterministic,
            apply_documentdb_llm_output,
        )
        from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
        from src.contracts.analysis_input import AnalysisInput, TargetDatabase
        from tests.fixtures.documentdb_pattern_fixtures import get_nested_document_fixture
        from tests.fixtures.opensearch_pattern_fixtures import get_fulltext_search_fixture
        from tests.unit.agents.analysis.test_documentdb_llm_seam import _make_llm_output

        doc_in = get_nested_document_fixture()
        result, doc, _ = analyze_for_documentdb_deterministic(
            AnalysisInput(
                job_id="j", collector_output=doc_in, target_database=TargetDatabase.DOCUMENTDB
            )
        )
        # The advisor's strategies are added to the trace by the seam.
        _, doc = apply_documentdb_llm_output(result, _make_llm_output(), doc)
        os_in = get_fulltext_search_fixture()
        _, search, _ = analyze_for_opensearch(
            AnalysisInput(
                job_id="j", collector_output=os_in, target_database=TargetDatabase.OPENSEARCH
            )
        )
        return {"documentdb": doc, "opensearch": search}

    def test_no_key_of_a_real_trace_is_unhandled(self):
        from src.agents.schema_design.group_trace import WORKLOAD_WIDE_KEYS

        handled = WORKLOAD_WIDE_KEYS | HANDLED_KEYS
        for engine, trace in self._traces().items():
            assert set(trace) <= handled, (
                f"{engine} trace has keys the group scoper neither scopes nor lists as "
                f"workload-wide: {sorted(set(trace) - handled)}"
            )

    def test_the_handled_keys_are_all_in_the_scoper(self):
        """HANDLED_KEYS here and the scoper agree: no key is silently dropped."""
        trace = {k: _trace().get(k, [] if k != "summary" else {}) for k in HANDLED_KEYS}
        scoped = scope_decision_trace(trace, GROUP_A)
        assert set(scoped) == set(trace)

    def test_a_group_with_no_queries_keeps_no_per_query_entries(self):
        scoped = scope_decision_trace(_trace(), _group_collector([], []))
        assert scoped["query_matches"] == []
        assert scoped["embedding_candidates"] == []
        assert scoped["summary"]["queries_analyzed"] == 0


def _seed(tmp_path: Path, engine: str, trace: dict | None) -> LocalArtifactStore:
    store = LocalArtifactStore(str(tmp_path))
    base = f"{DB}/{JOB}"
    queries = [_query(q, "t") for q in (*A, *B)]
    store.write_json(
        f"{base}/collector/output.json",
        {
            "contract_version": "3.0",
            "database_schema": {"tables": [_table("t")]},
            "queries": {"query_patterns": queries * 8},
        },
    )
    store.write_json(f"{base}/analysis-{engine}/analysis.json", {"contract_version": "2.1"})
    if trace is not None:
        store.write_json(f"{base}/analysis-{engine}/decision-trace.json", trace)
        # The other engine's trace must never reach this engine's designers.
        other = "opensearch" if engine == "documentdb" else "documentdb"
        store.write_json(f"{base}/analysis-{other}/decision-trace.json", {"query_matches": []})
    store.write_json(
        f"{base}/assignment/v1/assignment.json",
        {
            "version": 1,
            "co_dependency_groups": [],
            "query_assignments": [
                {
                    "query_id": q["query_id"],
                    "assigned_engine": engine,
                    "in_scope": True,
                    "source_tables": q["tables_accessed"],
                }
                for q in queries
            ],
        },
    )
    return store


def _fake_split(groups: dict[int, dict]):
    def split(job_id, database_name, target_type, store, assignment_version=0):
        root = f"{database_name}/{job_id}/schema-{target_type}/v1"
        store.write_json(
            f"{root}/groups_manifest.json",
            {"groups": [{"group_index": i, "group_name": f"g{i}"} for i in groups]},
        )
        for i, collector in groups.items():
            store.write_json(
                f"{root}/input_group_{i}.json",
                {"collector_output": collector, "analysis_output": {"contract_version": "2.1"}},
            )

    return split


@pytest.mark.parametrize("engine", ["documentdb", "opensearch"])
def test_every_group_designer_receives_a_trace_of_only_its_own_queries(tmp_path, engine):
    store = _seed(tmp_path, engine, _trace())
    seen: dict[str, dict] = {}

    def dispatch(target_type, collector_path=None, analysis_path=None, **kw):
        group = json.loads(Path(collector_path).read_text())
        trace_path = kw.get("decision_trace_path")
        assert trace_path, "the group's designer was given no decision trace"
        key = ",".join(sorted(q["query_id"] for q in group["queries"]["query_patterns"]))
        seen[key] = json.loads(Path(trace_path).read_text())
        return json.dumps({"ok": True}), json.dumps({"iterations": []})

    with (
        patch(
            "src.agents.schema_design.handler.run_schema_split",
            side_effect=_fake_split({0: GROUP_A, 1: GROUP_B}),
        ),
        patch("src.agents.schema_design.handler._dispatch_schema_agent", side_effect=dispatch),
        patch("src.agents.schema_design.handler.run_schema_merge"),
    ):
        run_schema_design_auto(JOB, DB, engine, store, assignment_version=1)

    assert {k: _qids(v) for k, v in seen.items()} == {
        "a1,a2": set(A),
        "b1,b2,b3": set(B),
    }


def test_a_group_whose_trace_is_missing_still_designs_without_one(tmp_path):
    store = _seed(tmp_path, "opensearch", None)
    paths: list = []

    def dispatch(target_type, collector_path=None, analysis_path=None, **kw):
        paths.append(kw.get("decision_trace_path"))
        return json.dumps({"ok": True}), json.dumps({"iterations": []})

    with (
        patch(
            "src.agents.schema_design.handler.run_schema_split",
            side_effect=_fake_split({0: GROUP_A}),
        ),
        patch("src.agents.schema_design.handler._dispatch_schema_agent", side_effect=dispatch),
        patch("src.agents.schema_design.handler.run_schema_merge"),
    ):
        run_schema_design_auto(JOB, DB, "opensearch", store, assignment_version=1)
    assert paths == [None]


def test_the_group_trace_files_are_removed_after_the_design(tmp_path):
    store = _seed(tmp_path, "documentdb", _trace())
    paths: list[str] = []

    def dispatch(target_type, collector_path=None, analysis_path=None, **kw):
        paths.append(kw["decision_trace_path"])
        return json.dumps({"ok": True}), json.dumps({"iterations": []})

    with (
        patch(
            "src.agents.schema_design.handler.run_schema_split",
            side_effect=_fake_split({0: GROUP_A, 1: GROUP_B}),
        ),
        patch("src.agents.schema_design.handler._dispatch_schema_agent", side_effect=dispatch),
        patch("src.agents.schema_design.handler.run_schema_merge"),
    ):
        run_schema_design_auto(JOB, DB, "documentdb", store, assignment_version=1)
    assert len(set(paths)) == 2
    assert not any(Path(p).exists() for p in paths)


class _Stop(Exception):
    """Ends a design call once its designer's inputs were captured."""


def _two_engine_store(tmp_path: Path, engine: str) -> LocalArtifactStore:
    """Two queries: q-mine assigned to ``engine``, q-other to DynamoDB."""
    store = LocalArtifactStore(str(tmp_path))
    base = f"{DB}/{JOB}"
    mine, other = _query("q-mine", "orders"), _query("q-other", "users")
    store.write_json(
        f"{base}/collector/output.json",
        {
            "contract_version": "3.0",
            "database_schema": {"tables": [_table("orders"), _table("users")]},
            "queries": {"query_patterns": [mine, other]},
        },
    )
    store.write_json(f"{base}/analysis-{engine}/analysis.json", {"contract_version": "2.1"})
    trace = {
        "query_matches": [{"query_id": "q-mine"}, {"query_id": "q-other"}],
        "marker": engine,
    }
    # Each engine's own trace says which engine wrote it, through its summary.
    for e in ("documentdb", "opensearch"):
        store.write_json(
            f"{base}/analysis-{e}/decision-trace.json",
            {**trace, "summary": {"queries_analyzed": 2}, "agent": f"{e}-analysis-agent"},
        )
    store.write_json(
        f"{base}/assignment/v1/assignment.json",
        {
            "version": 1,
            "co_dependency_groups": [],
            "query_assignments": [
                {
                    "query_id": q["query_id"],
                    "assigned_engine": e,
                    "in_scope": True,
                    "source_tables": q["tables_accessed"],
                }
                for q, e in ((mine, engine), (other, "dynamodb"))
            ],
        },
    )
    return store


def _capture(seen: dict):
    def dispatch(target_type, collector_path=None, analysis_path=None, **kw):
        seen["trace"] = json.loads(Path(kw["decision_trace_path"]).read_text())
        raise _Stop

    return dispatch


@pytest.mark.parametrize("engine", ["documentdb", "opensearch"])
class TestOneCallIsItsOwnQueries:
    """A single-pass or cascade design gets its engine's trace, for its queries only."""

    def test_single_pass(self, tmp_path, engine):
        from src.agents.schema_design.handler import run_schema_design

        store, seen = _two_engine_store(tmp_path, engine), {}
        with (
            patch("src.agents.schema_design.handler._dispatch_schema_agent", _capture(seen)),
            pytest.raises(_Stop),
        ):
            run_schema_design(JOB, DB, engine, store, assignment_version=1)
        assert seen["trace"]["agent"] == f"{engine}-analysis-agent"
        assert _qids(seen["trace"]) == {"q-mine"}

    def test_cascade_pass(self, tmp_path, engine):
        from src.agents.schema_design.handler import run_schema_design_with_injected

        store, seen = _two_engine_store(tmp_path, engine), {}
        with (
            patch("src.agents.schema_design.handler._dispatch_schema_agent", _capture(seen)),
            pytest.raises(_Stop),
        ):
            run_schema_design_with_injected(
                JOB, DB, engine, store, injected_query_ids=set(), assignment_version=1
            )
        assert seen["trace"]["agent"] == f"{engine}-analysis-agent"
        assert _qids(seen["trace"]) == {"q-mine"}
