"""Unit tests for schema revision verifier pure functions.

Tests cover:
- check_coverage: query-to-pattern coverage across DynamoDB, OpenSearch, DocumentDB
- check_consistency: duplicate names and missing required fields per engine
- check_conflicts: reassigned patterns present/absent in target outputs
- check_cost_delta: cost increase threshold enforcement
- verify_revision: orchestration, passed/failed classification
"""

import pytest

from src.agents.schema_design.revision_verifier import (
    check_conflicts,
    check_consistency,
    check_cost_delta,
    check_coverage,
    verify_revision,
)
from src.contracts.schema_revision_models import VerificationIssue, VerificationResult

# ---------------------------------------------------------------------------
# Fixtures — minimal schema_output dicts per engine
# ---------------------------------------------------------------------------


def _dynamodb_schema(access_patterns=None, table_definitions=None, index_designs=None):
    """Build a minimal DynamoDB schema_output dict."""
    return {
        "access_patterns": access_patterns or [],
        "table_definitions": table_definitions or [],
        "index_designs": index_designs or [],
        "collection_designs": [],
    }


def _opensearch_schema(access_patterns=None, index_designs=None, collection_designs=None):
    """Build a minimal OpenSearch schema_output dict."""
    return {
        "access_patterns": access_patterns or [],
        "index_designs": index_designs or [],
        "collection_designs": collection_designs or [],
    }


def _documentdb_schema(access_patterns=None, collection_designs=None, index_designs=None):
    """Build a minimal DocumentDB schema_output dict."""
    return {
        "access_patterns": access_patterns or [],
        "collection_designs": collection_designs or [],
        "index_designs": index_designs or [],
    }


def _ap(pattern_id, query_ids):
    """Build a minimal access_pattern dict."""
    return {"pattern_id": pattern_id, "query_ids": query_ids}


def _index_design(index_name, query_ids=None):
    """Build a minimal index_design dict."""
    return {"index_name": index_name, "query_ids": query_ids or []}


def _collection_design(collection_name, query_ids=None):
    """Build a minimal collection_design dict."""
    return {"collection_name": collection_name, "query_ids": query_ids or []}


def _table_def(table_name, partition_key=None):
    """Build a minimal table_definition dict."""
    result = {"table_name": table_name}
    if partition_key is not None:
        result["partition_key"] = partition_key
    return result


# ---------------------------------------------------------------------------
# check_coverage tests
# ---------------------------------------------------------------------------


class TestCheckCoverage:
    def test_all_queries_covered_returns_empty(self):
        """When all in-scope query IDs are covered by access_patterns, no issues."""
        schema = _dynamodb_schema(access_patterns=[_ap("AP-1", ["Q1", "Q2"]), _ap("AP-2", ["Q3"])])
        issues = check_coverage(schema, in_scope_query_ids=["Q1", "Q2", "Q3"], engine="dynamodb")
        assert issues == []

    def test_orphaned_query_returns_coverage_error(self):
        """A query not in any pattern returns a VerificationIssue with category=coverage."""
        schema = _dynamodb_schema(access_patterns=[_ap("AP-1", ["Q1"])])
        issues = check_coverage(schema, in_scope_query_ids=["Q1", "Q2"], engine="dynamodb")
        assert len(issues) == 1
        issue = issues[0]
        assert isinstance(issue, VerificationIssue)
        assert issue.category == "coverage"
        assert issue.severity == "error"
        assert "Q2" in issue.message
        assert len(issue.suggested_resolutions) > 0

    def test_empty_schema_with_queries_returns_one_error_per_orphan(self):
        """Empty schema with 3 in-scope queries → 3 coverage errors."""
        schema = _dynamodb_schema()
        issues = check_coverage(schema, in_scope_query_ids=["Q1", "Q2", "Q3"], engine="dynamodb")
        assert len(issues) == 3
        assert all(i.category == "coverage" for i in issues)
        assert all(i.severity == "error" for i in issues)

    def test_empty_query_ids_returns_empty(self):
        """No in-scope queries means nothing to check — returns empty."""
        schema = _dynamodb_schema()
        issues = check_coverage(schema, in_scope_query_ids=[], engine="dynamodb")
        assert issues == []

    def test_coverage_via_index_designs_query_ids(self):
        """Queries covered by index_designs[].query_ids count as covered."""
        schema = _opensearch_schema(
            index_designs=[_index_design("products-idx", query_ids=["Q1", "Q2"])]
        )
        issues = check_coverage(schema, in_scope_query_ids=["Q1", "Q2"], engine="opensearch")
        assert issues == []

    def test_coverage_via_collection_designs_query_ids(self):
        """Queries covered by collection_designs[].query_ids count as covered."""
        schema = _documentdb_schema(
            collection_designs=[_collection_design("users", query_ids=["Q1"])]
        )
        issues = check_coverage(schema, in_scope_query_ids=["Q1"], engine="documentdb")
        assert issues == []

    def test_coverage_combined_sources(self):
        """Coverage drawn from access_patterns, index_designs, and collection_designs together."""
        schema = {
            "access_patterns": [_ap("AP-1", ["Q1"])],
            "index_designs": [_index_design("idx", query_ids=["Q2"])],
            "collection_designs": [_collection_design("col", query_ids=["Q3"])],
        }
        issues = check_coverage(schema, in_scope_query_ids=["Q1", "Q2", "Q3"], engine="dynamodb")
        assert issues == []

    def test_partial_coverage_reports_only_uncovered(self):
        """Only uncovered queries are reported, not covered ones."""
        schema = _dynamodb_schema(access_patterns=[_ap("AP-1", ["Q1"])])
        issues = check_coverage(schema, in_scope_query_ids=["Q1", "Q2", "Q3"], engine="dynamodb")
        assert len(issues) == 2
        assert all(i.category == "coverage" for i in issues)


# ---------------------------------------------------------------------------
# check_consistency tests
# ---------------------------------------------------------------------------


class TestCheckConsistency:
    # DynamoDB
    def test_dynamodb_duplicate_table_name_returns_error(self):
        """Duplicate table_definitions[].table_name → consistency error."""
        schema = _dynamodb_schema(
            table_definitions=[
                _table_def("UsersTable", partition_key={"attribute_name": "PK"}),
                _table_def("UsersTable", partition_key={"attribute_name": "PK"}),
            ]
        )
        issues = check_consistency(schema, engine="dynamodb")
        assert len(issues) >= 1
        assert any(i.category == "consistency" and i.severity == "error" for i in issues)

    def test_dynamodb_missing_partition_key_returns_error(self):
        """table_definition without partition_key → consistency error."""
        schema = _dynamodb_schema(table_definitions=[_table_def("UsersTable")])  # no partition_key
        issues = check_consistency(schema, engine="dynamodb")
        assert len(issues) >= 1
        assert any(i.category == "consistency" and i.severity == "error" for i in issues)

    def test_dynamodb_valid_schema_returns_empty(self):
        """Valid DynamoDB schema with unique table names and partition keys → no issues."""
        schema = _dynamodb_schema(
            table_definitions=[
                _table_def("UsersTable", partition_key={"attribute_name": "PK"}),
                _table_def("OrdersTable", partition_key={"attribute_name": "PK"}),
            ]
        )
        issues = check_consistency(schema, engine="dynamodb")
        assert issues == []

    # OpenSearch
    def test_opensearch_duplicate_index_name_returns_error(self):
        """Duplicate index_designs[].index_name → consistency error."""
        schema = _opensearch_schema(
            index_designs=[
                _index_design("products-idx"),
                _index_design("products-idx"),
            ]
        )
        issues = check_consistency(schema, engine="opensearch")
        assert len(issues) >= 1
        assert any(i.category == "consistency" and i.severity == "error" for i in issues)

    def test_opensearch_unique_index_names_returns_empty(self):
        """OpenSearch with unique index names → no issues."""
        schema = _opensearch_schema(
            index_designs=[_index_design("products-idx"), _index_design("orders-idx")]
        )
        issues = check_consistency(schema, engine="opensearch")
        assert issues == []

    # DocumentDB
    def test_documentdb_duplicate_collection_name_returns_error(self):
        """Duplicate collection_designs[].collection_name → consistency error."""
        schema = _documentdb_schema(
            collection_designs=[
                _collection_design("users"),
                _collection_design("users"),
            ]
        )
        issues = check_consistency(schema, engine="documentdb")
        assert len(issues) >= 1
        assert any(i.category == "consistency" and i.severity == "error" for i in issues)

    def test_documentdb_unique_collection_names_returns_empty(self):
        """DocumentDB with unique collection names → no issues."""
        schema = _documentdb_schema(
            collection_designs=[_collection_design("users"), _collection_design("orders")]
        )
        issues = check_consistency(schema, engine="documentdb")
        assert issues == []

    def test_unknown_engine_returns_empty(self):
        """An unrecognised engine with no schema data → no issues (graceful fallback)."""
        schema = {}
        issues = check_consistency(schema, engine="redis")
        assert issues == []


# ---------------------------------------------------------------------------
# check_conflicts tests
# ---------------------------------------------------------------------------


class TestCheckConflicts:
    def _make_target_outputs(self, pattern_ids):
        """Build target_outputs with access_patterns carrying given pattern_ids."""
        return {
            "dynamodb": {
                "access_patterns": [_ap(pid, ["Q1"]) for pid in pattern_ids],
                "index_designs": [],
                "collection_designs": [],
            }
        }

    def test_pattern_found_in_target_output_returns_empty(self):
        """Reassigned pattern present in the target engine's output → no conflict."""
        reassignments = [{"pattern_id": "AP-1", "target_engine": "dynamodb"}]
        target_outputs = self._make_target_outputs(["AP-1"])
        issues = check_conflicts(reassignments, target_outputs)
        assert issues == []

    def test_pattern_missing_from_target_returns_conflict_warning(self):
        """Reassigned pattern absent from target output → conflict warning (needs redesign)."""
        reassignments = [{"pattern_id": "AP-99", "target_engine": "dynamodb"}]
        target_outputs = self._make_target_outputs(["AP-1"])
        issues = check_conflicts(reassignments, target_outputs)
        assert len(issues) == 1
        assert issues[0].category == "conflict"
        assert issues[0].severity == "warning"
        assert "AP-99" in issues[0].message

    def test_target_engine_not_in_outputs_returns_conflict_error(self):
        """Reassignment to an engine not in target_outputs → conflict error."""
        reassignments = [{"pattern_id": "AP-1", "target_engine": "opensearch"}]
        target_outputs = {}  # opensearch not present
        issues = check_conflicts(reassignments, target_outputs)
        assert len(issues) == 1
        assert issues[0].category == "conflict"

    def test_empty_reassignments_returns_empty(self):
        """No reassignments → no conflicts."""
        issues = check_conflicts([], {})
        assert issues == []

    def test_pattern_found_via_index_designs(self):
        """Pattern found in target's index_designs[].pattern_id → no conflict."""
        reassignments = [{"pattern_id": "AP-OS-1", "target_engine": "opensearch"}]
        target_outputs = {
            "opensearch": {
                "access_patterns": [],
                "index_designs": [{"pattern_id": "AP-OS-1", "query_ids": []}],
                "collection_designs": [],
            }
        }
        issues = check_conflicts(reassignments, target_outputs)
        assert issues == []

    def test_pattern_found_via_collection_designs(self):
        """Pattern found in target's collection_designs[].pattern_id → no conflict."""
        reassignments = [{"pattern_id": "AP-DOC-1", "target_engine": "documentdb"}]
        target_outputs = {
            "documentdb": {
                "access_patterns": [],
                "index_designs": [],
                "collection_designs": [{"pattern_id": "AP-DOC-1", "query_ids": []}],
            }
        }
        issues = check_conflicts(reassignments, target_outputs)
        assert issues == []

    def test_multiple_reassignments_some_missing(self):
        """Mixed reassignments — only the missing ones produce errors."""
        reassignments = [
            {"pattern_id": "AP-1", "target_engine": "dynamodb"},
            {"pattern_id": "AP-2", "target_engine": "dynamodb"},
        ]
        target_outputs = self._make_target_outputs(["AP-1"])  # AP-2 absent
        issues = check_conflicts(reassignments, target_outputs)
        assert len(issues) == 1
        assert "AP-2" in issues[0].message


# ---------------------------------------------------------------------------
# check_cost_delta tests
# ---------------------------------------------------------------------------


class TestCheckCostDelta:
    def test_increase_above_threshold_returns_warning(self):
        """Cost increase > 20% → warning with category=cost and cost_delta set."""
        issues = check_cost_delta(previous_cost=100.0, current_cost=125.0, threshold=0.20)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.category == "cost"
        assert issue.severity == "warning"
        assert issue.cost_delta is not None
        assert issue.cost_delta == pytest.approx(25.0)

    def test_increase_exactly_at_threshold_returns_empty(self):
        """Cost increase exactly equal to threshold → no warning (boundary is exclusive)."""
        issues = check_cost_delta(previous_cost=100.0, current_cost=120.0, threshold=0.20)
        assert issues == []

    def test_increase_below_threshold_returns_empty(self):
        """Cost increase < 20% → no issues."""
        issues = check_cost_delta(previous_cost=100.0, current_cost=115.0, threshold=0.20)
        assert issues == []

    def test_cost_decrease_returns_empty(self):
        """Cost going down → no issues regardless of magnitude."""
        issues = check_cost_delta(previous_cost=200.0, current_cost=50.0, threshold=0.20)
        assert issues == []

    def test_previous_cost_none_returns_empty(self):
        """previous_cost=None → cannot compute delta, return empty."""
        issues = check_cost_delta(previous_cost=None, current_cost=100.0, threshold=0.20)
        assert issues == []

    def test_current_cost_none_returns_empty(self):
        """current_cost=None → cannot compute delta, return empty."""
        issues = check_cost_delta(previous_cost=100.0, current_cost=None, threshold=0.20)
        assert issues == []

    def test_both_costs_none_returns_empty(self):
        """Both costs None → return empty."""
        issues = check_cost_delta(previous_cost=None, current_cost=None, threshold=0.20)
        assert issues == []

    def test_custom_threshold_respected(self):
        """A custom threshold of 0.10 triggers warning at 11% increase."""
        issues = check_cost_delta(previous_cost=100.0, current_cost=111.0, threshold=0.10)
        assert len(issues) == 1
        assert issues[0].category == "cost"

    def test_cost_delta_value_is_absolute_difference(self):
        """cost_delta is set to the absolute cost increase in USD."""
        issues = check_cost_delta(previous_cost=500.0, current_cost=650.0, threshold=0.20)
        assert len(issues) == 1
        assert issues[0].cost_delta == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# verify_revision tests
# ---------------------------------------------------------------------------


class TestVerifyRevision:
    def _base_schema(self):
        return _dynamodb_schema(
            access_patterns=[_ap("AP-1", ["Q1"])],
            table_definitions=[_table_def("UsersTable", partition_key={"attribute_name": "PK"})],
        )

    def test_all_clean_returns_passed_true(self):
        """Clean schema with all queries covered → passed=True, no hard_errors or warnings."""
        result = verify_revision(
            schema_output=self._base_schema(),
            in_scope_query_ids=["Q1"],
            engine="dynamodb",
            reassignments=[],
            target_outputs={},
            previous_cost=None,
            current_cost=None,
        )
        assert isinstance(result, VerificationResult)
        assert result.passed is True
        assert result.hard_errors == []
        assert result.warnings == []

    def test_coverage_error_sets_passed_false(self):
        """An uncovered query produces a hard_error → passed=False."""
        result = verify_revision(
            schema_output=self._base_schema(),
            in_scope_query_ids=["Q1", "Q_ORPHAN"],
            engine="dynamodb",
            reassignments=[],
            target_outputs={},
            previous_cost=None,
            current_cost=None,
        )
        assert result.passed is False
        assert len(result.hard_errors) >= 1
        assert all(e.severity == "error" for e in result.hard_errors)

    def test_consistency_error_sets_passed_false(self):
        """Duplicate table name → hard_error → passed=False."""
        schema = _dynamodb_schema(
            access_patterns=[_ap("AP-1", ["Q1"])],
            table_definitions=[
                _table_def("DupTable", partition_key={"attribute_name": "PK"}),
                _table_def("DupTable", partition_key={"attribute_name": "PK"}),
            ],
        )
        result = verify_revision(
            schema_output=schema,
            in_scope_query_ids=["Q1"],
            engine="dynamodb",
            reassignments=[],
            target_outputs={},
            previous_cost=None,
            current_cost=None,
        )
        assert result.passed is False
        assert any(e.category == "consistency" for e in result.hard_errors)

    def test_conflict_warning_does_not_set_passed_false(self):
        """Missing reassigned pattern → warning (not hard_error) → passed=True."""
        result = verify_revision(
            schema_output=self._base_schema(),
            in_scope_query_ids=["Q1"],
            engine="dynamodb",
            reassignments=[{"pattern_id": "AP-MISSING", "target_engine": "opensearch"}],
            target_outputs={},  # opensearch absent
            previous_cost=None,
            current_cost=None,
        )
        assert result.passed is True
        assert any(e.category == "conflict" for e in result.warnings)

    def test_cost_warning_does_not_set_passed_false(self):
        """Cost warning is a non-blocking warning → passed=True."""
        result = verify_revision(
            schema_output=self._base_schema(),
            in_scope_query_ids=["Q1"],
            engine="dynamodb",
            reassignments=[],
            target_outputs={},
            previous_cost=100.0,
            current_cost=200.0,  # 100% increase — warning
        )
        assert result.passed is True
        assert len(result.warnings) >= 1
        assert all(w.category == "cost" for w in result.warnings)
        assert result.hard_errors == []

    def test_errors_go_to_hard_errors_warnings_to_warnings(self):
        """Errors in hard_errors list; warnings in warnings list — not mixed."""
        schema = _dynamodb_schema(
            access_patterns=[_ap("AP-1", ["Q1"])],
            table_definitions=[],  # valid coverage, no tables needed for this check
        )
        result = verify_revision(
            schema_output=schema,
            in_scope_query_ids=["Q1", "Q_ORPHAN"],  # coverage error
            engine="dynamodb",
            reassignments=[],
            target_outputs={},
            previous_cost=100.0,
            current_cost=200.0,  # cost warning
        )
        assert result.passed is False
        assert all(e.severity == "error" for e in result.hard_errors)
        assert all(w.severity == "warning" for w in result.warnings)

    def test_returns_verification_result_type(self):
        """verify_revision always returns a VerificationResult instance."""
        result = verify_revision(
            schema_output={},
            in_scope_query_ids=[],
            engine="dynamodb",
            reassignments=[],
            target_outputs={},
            previous_cost=None,
            current_cost=None,
        )
        assert isinstance(result, VerificationResult)
