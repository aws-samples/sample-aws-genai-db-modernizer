"""Cache overlay: customer pins, legacy cache owners, persisted safety net,
invalidation context and the OpenSearch absorber rule (#296 review).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agents.referee.assignment_overrides import (
    QueryOverrideInput,
    apply_assignment_overrides,
    load_assignment_for_edit,
)
from src.agents.referee.assignment_validator import AssignmentValidator
from src.agents.referee.cache_overlay import (
    CUSTOMER_CACHE_REASON,
    apply_cache_overlay,
    apply_schema_safety_net,
    normalize_cache_owners,
)
from src.agents.referee.reality_check import _find_best_absorber_for_query, may_absorb
from src.agents.referee.reality_check_handler import run_reality_check_deterministic
from src.agents.schema_design.handler import filter_collector_for_assignment
from src.agents.schema_design.scope import assess_schema_scope
from src.contracts.assignment_models import Assignment
from src.storage.local_store import LocalArtifactStore

FIXTURE = Path(__file__).parents[3] / "fixtures" / "issue_296_main_wordpress.json.gz"
DB, JOB = "wordpress", "main-shape"


@pytest.fixture(scope="module")
def main_wordpress() -> dict:
    data: dict = json.loads(gzip.open(FIXTURE).read())
    return data


@pytest.fixture
def legacy_store(tmp_path, main_wordpress) -> LocalArtifactStore:
    """main's wordpress job: ElastiCache owns 31 queries in v1 and 34 in v2."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    fx = main_wordpress
    store.write_json(f"{DB}/{JOB}/collector/output.json", fx["collector"])
    store.write_json(f"{DB}/{JOB}/referee-triage/triage.json", fx["triage"])
    for engine in fx["engines"]:
        store.write_json(
            f"{DB}/{JOB}/analysis-{engine}/analysis.json",
            {"table_recommendations": [], "workload_analysis": {}},
        )
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", fx["assignment_v1"])
    v2 = {**fx["assignment_v2"], "source": "reality_check", "previous_version": 1}
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", v2)
    return store


def _owners(qas) -> set[str]:
    return {qa["assigned_engine"] if isinstance(qa, dict) else qa.assigned_engine for qa in qas}


# ---------------------------------------------------------------------------
# Legacy assignments (ElastiCache owners) are converted on load
# ---------------------------------------------------------------------------


class TestLegacy:
    def test_fixture_is_the_legacy_shape(self, main_wordpress):
        v2 = main_wordpress["assignment_v2"]["query_assignments"]
        assert sum(qa["assigned_engine"] == "elasticache" for qa in v2) == 34

    def test_load_moves_cache_owners_to_the_source_compatible_engine(self, legacy_store):
        raw, collector, analysis = load_assignment_for_edit(legacy_store, DB, JOB, 2)
        assert "elasticache" not in _owners(raw["query_assignments"])
        moved = [
            qa
            for qa in raw["query_assignments"]
            if qa["assignment_reason"].startswith("cache overlay migration")
        ]
        assert len(moved) == 34
        assert {qa["assigned_engine"] for qa in moved} == {"aurora_mysql"}
        assert "34 queries owned by elasticache" in raw["cache_notes"][0]
        assert raw["cache_overlay"]["query_count"] > 0
        assert not any(t["primary_engine"] == "elasticache" for t in raw["table_assignments"])
        assignment = Assignment.model_validate(raw)
        assert AssignmentValidator().validate(assignment, collector, analysis).valid

    def test_legacy_assignment_is_editable(self, legacy_store, main_wordpress):
        qid = main_wordpress["assignment_v2"]["query_assignments"][0]["query_id"]
        result = apply_assignment_overrides(
            legacy_store, DB, JOB, [QueryOverrideInput(query_id=qid, in_scope=False)]
        )
        assert result.validation.valid
        assert result.assignment.version == 3
        assert "elasticache" not in _owners(result.assignment.query_assignments)
        assert result.assignment.cache_notes

    def test_reality_check_on_a_legacy_v1_has_no_cache_owner(self, legacy_store):
        det = run_reality_check_deterministic(JOB, DB, legacy_store, assignment_version=1)
        assert "elasticache" not in _owners(det["revised_assignments"])
        assert "elasticache" not in det["before_distribution"]
        assert any(qa.get("cache_engine") for qa in det["revised_assignments"])

    def test_normalize_is_a_no_op_without_cache_owners(self):
        assignment = {"query_assignments": [{"query_id": "q", "assigned_engine": "dynamodb"}]}
        assert normalize_cache_owners(assignment, [], ["dynamodb"]) == []
        assert "cache_notes" not in assignment


# ---------------------------------------------------------------------------
# Customer pins
# ---------------------------------------------------------------------------


def _store_with(tmp_path, qas: list[dict], queries: list[dict]) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {"tables": [{"table_id": "users"}]},
            "queries": {"query_patterns": queries},
        },
    )
    for engine in ("aurora_mysql", "dynamodb", "elasticache"):
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "job_id": JOB,
            "version": 1,
            "status": "auto_generated",
            "timestamp": "2026-10-04T00:00:00Z",
            "query_assignments": qas,
            "table_assignments": [],
            "co_dependency_groups": [],
            "validation_warnings": [],
        },
    )
    return store


def _qa(qid: str, engine: str, **extra) -> dict:
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "confidence": 50,
        "source_tables": ["users"],
        "assignment_reason": "t",
        **extra,
    }


def _query(qid: str, cps: float, qtype: str = "SELECT") -> dict:
    text = "SELECT * FROM users WHERE id = ?" if qtype == "SELECT" else "UPDATE users SET a = ?"
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "calls_per_second": cps,
        "rows_returned_avg": 1,
        "tables_accessed": ["users"],
    }


@pytest.fixture
def pin_store(tmp_path) -> LocalArtifactStore:
    return _store_with(
        tmp_path,
        [_qa("hot", "dynamodb"), _qa("cold", "dynamodb"), _qa("w", "dynamodb")],
        [_query("hot", 5.0), _query("cold", 0.01), _query("w", 0.5, "UPDATE")],
    )


class TestCustomerPin:
    def test_naming_the_cache_as_engine_pins_it_and_keeps_the_owner(self, pin_store):
        result = apply_assignment_overrides(
            pin_store, DB, JOB, [QueryOverrideInput(query_id="cold", assigned_engine="elasticache")]
        )
        assert result.validation.valid
        cold = next(qa for qa in result.assignment.query_assignments if qa.query_id == "cold")
        assert cold.assigned_engine == "dynamodb"
        assert cold.customer_override is False
        assert (cold.cache_engine, cold.cache_reason) == ("elasticache", CUSTOMER_CACHE_REASON)
        assert cold.cache_customer_override is True
        assert any("hot-read rule" in w for w in cold.warnings)
        assert any(
            "cache layer, not a system of record" in n for n in result.assignment.cache_notes
        )
        assert result.assignment.cache_overlay.query_count == 1

    def test_cached_flag_pins_and_unpins(self, pin_store):
        apply_assignment_overrides(
            pin_store, DB, JOB, [QueryOverrideInput(query_id="hot", cached=True)]
        )
        result = apply_assignment_overrides(
            pin_store, DB, JOB, [QueryOverrideInput(query_id="hot", cached=False)]
        )
        hot = next(qa for qa in result.assignment.query_assignments if qa.query_id == "hot")
        assert hot.cache_engine is None and hot.cache_customer_override is True
        assert hot.assigned_engine == "dynamodb"

    def test_reevaluation_never_clears_a_pin(self):
        qas = [_qa("cold", "dynamodb", cache_engine="elasticache", cache_customer_override=True)]
        apply_cache_overlay(qas, [_query("cold", 0.01)], ["dynamodb", "elasticache"])
        assert qas[0]["cache_engine"] == "elasticache"
        assert qas[0]["cache_reason"] == CUSTOMER_CACHE_REASON
        assert any("hot-read rule" in w for w in qas[0]["warnings"])

    def test_safety_net_keeps_a_pin_with_a_warning(self):
        assignment = {
            "query_assignments": [
                _qa("cold", "dynamodb", cache_engine="elasticache", cache_customer_override=True)
            ]
        }
        assert apply_schema_safety_net(assignment, {"access_patterns": []}, []) == []
        qa = assignment["query_assignments"][0]
        assert qa["cache_engine"] == "elasticache"
        assert any("no in-scope access pattern" in w for w in qa["warnings"])

    def test_api_put_cached(self, pin_store, monkeypatch):
        from src.api.main import app
        from src.api.routes import assignments

        monkeypatch.setattr(assignments, "artifact_store", pin_store)
        client = TestClient(app)
        r = client.put(
            f"/api/v1/assessments/{JOB}/assignments",
            params={"database_name": DB},
            json={"overrides": [{"query_id": "hot", "cached": True}]},
        )
        assert r.status_code == 200, r.text
        hot = next(q for q in r.json()["assignment"]["query_assignments"] if q["query_id"] == "hot")
        assert hot["cache_customer_override"] is True and hot["assigned_engine"] == "dynamodb"

        r = client.put(
            f"/api/v1/assessments/{JOB}/assignments",
            params={"database_name": DB},
            json={"overrides": [{"query_id": "cold", "assigned_engine": "elasticache"}]},
        )
        assert r.status_code == 200, r.text  # converted, not a 422

    def test_api_get_converts_a_legacy_assignment(self, legacy_store, monkeypatch):
        from src.api.main import app
        from src.api.routes import assignments

        monkeypatch.setattr(assignments, "artifact_store", legacy_store)
        r = TestClient(app).get(
            f"/api/v1/assessments/{JOB}/assignments", params={"database_name": DB}
        )
        assert r.status_code == 200, r.text
        qas = r.json()["assignment"]["query_assignments"]
        assert "elasticache" not in {q["assigned_engine"] for q in qas}


# ---------------------------------------------------------------------------
# Safety net persisted into the assignment artifact
# ---------------------------------------------------------------------------


def test_safety_net_drop_is_persisted(tmp_path):
    from src.agents.referee.synthesis_handler import run_synthesis

    store = _store_with(
        tmp_path,
        [
            _qa("a", "dynamodb", cache_engine="elasticache", cache_pattern="point_lookup"),
            _qa("b", "dynamodb", cache_engine="elasticache", cache_pattern="point_lookup"),
        ],
        [_query("a", 5.0), _query("b", 5.0)],
    )
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in ("dynamodb", "elasticache")]},
    )
    store.write_json(
        f"{DB}/{JOB}/schema-elasticache/v1/schema_output.json",
        {"access_patterns": [{"pattern_id": "EC-AP-1", "source_query_ids": ["a"]}]},
    )
    run_synthesis(JOB, DB, store, assignment_version=1, llm_mode="none")
    raw = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
    b = next(qa for qa in raw["query_assignments"] if qa["query_id"] == "b")
    assert b["cache_engine"] is None and b["cache_dropped"] is True
    assert b["assigned_engine"] == "dynamodb"
    assert "no in-scope elasticache access pattern" in b["cache_reason"]
    assert raw["cache_notes"] and raw["cache_overlay"]["query_count"] == 1
    Assignment.model_validate(raw)

    # A second synthesis still reports the drop (it now comes from the artifact)
    run_synthesis(JOB, DB, store, assignment_version=1, llm_mode="none")
    report = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    assert report["cache_overlay"]["dropped_query_ids"] == ["b"]
    assert report["cache_overlay"]["notes"]


# ---------------------------------------------------------------------------
# Invalidation context for the cache design
# ---------------------------------------------------------------------------


class TestInvalidationContext:
    ASSIGNMENT = {
        "query_assignments": [
            _qa("r", "dynamodb", cache_engine="elasticache"),
            _qa("w", "dynamodb"),
            {**_qa("w2", "dynamodb"), "source_tables": ["orders"]},
        ]
    }
    COLLECTOR = {
        "database_schema": {"tables": [{"table_id": "users"}, {"table_id": "orders"}]},
        "queries": {
            "query_patterns": [
                _query("r", 5.0),
                _query("w", 1.0, "UPDATE"),
                {**_query("w2", 1.0, "INSERT"), "tables_accessed": ["orders"]},
            ]
        },
    }

    def test_cache_input_carries_owner_writes_as_context(self):
        out = filter_collector_for_assignment(self.COLLECTOR, self.ASSIGNMENT, "elasticache")
        assert [q["query_id"] for q in out["queries"]["query_patterns"]] == ["r"]
        ctx = out["cache_invalidation_context"]
        assert [w["query_id"] for w in ctx["write_queries"]] == ["w"]  # w2: uncached table
        assert ctx["write_queries"][0]["context_only"] is True
        assert "source_write_query_ids" in ctx["note"]

    def test_other_engines_get_no_context(self):
        out = filter_collector_for_assignment(self.COLLECTOR, self.ASSIGNMENT, "dynamodb")
        assert "cache_invalidation_context" not in out

    def test_writes_are_not_design_scope(self):
        ok = {
            "key_designs": [{"key_pattern": "users:{id}", "source_tables": ["users"]}],
            "access_patterns": [{"pattern_id": "EC-AP-1", "source_query_ids": ["r"]}],
            "cache_invalidation": [{"source_write_query_ids": ["w"]}],
        }
        assert assess_schema_scope("elasticache", ok, self.ASSIGNMENT).violations == []
        bad = {**ok, "access_patterns": [{"pattern_id": "EC-AP-2", "source_query_ids": ["w"]}]}
        assert assess_schema_scope("elasticache", bad, self.ASSIGNMENT).violations


# ---------------------------------------------------------------------------
# OpenSearch absorbs only search / aggregation queries
# ---------------------------------------------------------------------------


class TestOpenSearchAbsorber:
    def _absorber(self, signals):
        query_map = {"q": {"tables_accessed": ["t"], "query_type": "SELECT"}}
        analysis = {
            "opensearch": {"table_recommendations": [{"table_id": "t", "confidence_score": 95}]},
            "dynamodb": {"table_recommendations": [{"table_id": "t", "confidence_score": 40}]},
        }
        return _find_best_absorber_for_query(
            {"query_id": "q"},
            {"opensearch", "dynamodb"},
            "aurora_mysql",
            {"q": signals},
            query_map,
            analysis,
            {},
            set(),
            "dynamodb",
        )["target_engine"]

    def test_plain_lookup_never_goes_to_opensearch(self):
        assert self._absorber([]) == "dynamodb"
        assert self._absorber(["key_value_lookups"]) == "dynamodb"

    def test_search_and_aggregation_may(self):
        assert self._absorber(["text_search"]) == "opensearch"
        assert self._absorber(["aggregations"]) == "opensearch"

    def test_capability_counts(self):
        assert may_absorb("opensearch", "q", {}, {}, {"q": ["inverted_index"]})
        assert not may_absorb("opensearch", "q", {}, {}, {})
        assert may_absorb("dynamodb", "q", {}, {}, {})
