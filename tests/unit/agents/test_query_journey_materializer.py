"""Unit tests for query_journey_materializer — materialize_source function.

TDD: tests written before implementation.
"""

from typing import Any
from unittest.mock import MagicMock

from src.storage.artifact_store import ArtifactStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_store() -> MagicMock:
    store = MagicMock(spec=ArtifactStore)
    return store


def _make_query_pattern(
    query_id: str = "q_001",
    query_text: str = "SELECT * FROM orders WHERE id = ?",
    query_type: str = "SELECT",
    tables_accessed: list | None = None,
    frequency_per_hour: float = 100.0,
    calls_per_second: float = 0.03,
    execution_time_ms_avg: float = 10.0,
    execution_time_ms_p50: float = 8.0,
    execution_time_ms_p95: float = 25.0,
    execution_time_ms_p99: float = 50.0,
    rows_returned_avg: float = 5.0,
    rows_examined_avg: float = 100.0,
    scan_efficiency_pct: float = 5.0,
    full_table_scans: int = 0,
    db_load_contribution_percent: float = 1.5,
    lock_time_ms: float = 0.0,
    total_time_ms: float = 1000.0,
    has_joins: bool = False,
    join_count: int = 0,
    has_aggregations: bool = False,
    has_subqueries: bool = False,
    has_text_search: bool = False,
    has_time_range_filter: bool = False,
    filter_columns: list | None = None,
    sort_columns: list | None = None,
) -> dict:
    return {
        "query_id": query_id,
        "query_text": query_text,
        "query_type": query_type,
        "tables_accessed": tables_accessed if tables_accessed is not None else ["orders"],
        "frequency_per_hour": frequency_per_hour,
        "calls_per_second": calls_per_second,
        "execution_time_ms_avg": execution_time_ms_avg,
        "execution_time_ms_p50": execution_time_ms_p50,
        "execution_time_ms_p95": execution_time_ms_p95,
        "execution_time_ms_p99": execution_time_ms_p99,
        "rows_returned_avg": rows_returned_avg,
        "rows_examined_avg": rows_examined_avg,
        "scan_efficiency_pct": scan_efficiency_pct,
        "full_table_scans": full_table_scans,
        "db_load_contribution_percent": db_load_contribution_percent,
        "lock_time_ms": lock_time_ms,
        "total_time_ms": total_time_ms,
        "has_joins": has_joins,
        "join_count": join_count,
        "has_aggregations": has_aggregations,
        "has_subqueries": has_subqueries,
        "has_text_search": has_text_search,
        "has_time_range_filter": has_time_range_filter,
        "filter_columns": filter_columns if filter_columns is not None else [],
        "sort_columns": sort_columns if sort_columns is not None else [],
    }


def _make_collector_output(query_patterns: list | None = None) -> dict:
    if query_patterns is None:
        query_patterns = [_make_query_pattern()]
    return {
        "queries": {
            "query_patterns": query_patterns,
        }
    }


# ---------------------------------------------------------------------------
# Import the module under test (will fail until implementation exists)
# ---------------------------------------------------------------------------

from src.agents.query_journey_materializer import (  # noqa: E402
    _filter_trade_offs,
    _get_query_ids,
    _journey_path,
    _project_assignment,
    _project_unsupported,
    materialize_assignment,
    materialize_design,
    materialize_load_test,
    materialize_source,
)

# ---------------------------------------------------------------------------
# Tests for _journey_path helper
# ---------------------------------------------------------------------------


class TestJourneyPath:
    def test_correct_path_format(self):
        path = _journey_path("mydb", "job-001", "q_001")
        assert path == "mydb/job-001/query-journeys/q_001.json"

    def test_path_uses_all_arguments(self):
        path = _journey_path("acme_db", "job-XYZ", "q_999")
        assert path == "acme_db/job-XYZ/query-journeys/q_999.json"


# ---------------------------------------------------------------------------
# Tests for materialize_source
# ---------------------------------------------------------------------------


class TestMaterializeSourceFileCreation:
    def test_creates_one_journey_file_per_query(self):
        """One write_json call per query pattern."""
        store = _mock_store()
        patterns = [
            _make_query_pattern(query_id="q_001"),
            _make_query_pattern(query_id="q_002"),
            _make_query_pattern(query_id="q_003"),
        ]
        collector_output = _make_collector_output(patterns)

        materialize_source(collector_output, "mydb", "job-001", store)

        assert store.write_json.call_count == 3

    def test_journey_file_path_is_correct(self):
        """The path passed to write_json matches the expected pattern."""
        store = _mock_store()
        collector_output = _make_collector_output([_make_query_pattern(query_id="q_001")])

        materialize_source(collector_output, "mydb", "job-001", store)

        store.write_json.assert_called_once()
        path_arg = store.write_json.call_args[0][0]
        assert path_arg == "mydb/job-001/query-journeys/q_001.json"

    def test_each_query_uses_correct_path(self):
        """Each of multiple queries gets its own correctly-named path."""
        store = _mock_store()
        patterns = [
            _make_query_pattern(query_id="q_001"),
            _make_query_pattern(query_id="q_002"),
        ]
        collector_output = _make_collector_output(patterns)

        materialize_source(collector_output, "testdb", "job-42", store)

        written_paths = [c[0][0] for c in store.write_json.call_args_list]
        assert "testdb/job-42/query-journeys/q_001.json" in written_paths
        assert "testdb/job-42/query-journeys/q_002.json" in written_paths

    def test_empty_query_list_writes_nothing(self):
        """When query_patterns is empty, no files are written."""
        store = _mock_store()
        collector_output = _make_collector_output([])

        materialize_source(collector_output, "mydb", "job-001", store)

        store.write_json.assert_not_called()


class TestMaterializeSourceJourneyStructure:
    def _get_written_data(self, store: MagicMock) -> dict[str, Any]:
        """Return the data dict from the first write_json call."""
        return dict(store.write_json.call_args[0][1])

    def test_journey_has_query_id(self):
        store = _mock_store()
        collector_output = _make_collector_output([_make_query_pattern(query_id="q_007")])
        materialize_source(collector_output, "mydb", "job-001", store)

        data = self._get_written_data(store)
        assert data["query_id"] == "q_007"

    def test_journey_has_source_section(self):
        store = _mock_store()
        collector_output = _make_collector_output()
        materialize_source(collector_output, "mydb", "job-001", store)

        data = self._get_written_data(store)
        assert "source" in data

    def test_source_section_has_top_level_fields(self):
        store = _mock_store()
        pattern = _make_query_pattern(
            query_text="SELECT id FROM users",
            query_type="SELECT",
            tables_accessed=["users"],
            frequency_per_hour=200.0,
            calls_per_second=0.055,
        )
        collector_output = _make_collector_output([pattern])
        materialize_source(collector_output, "mydb", "job-001", store)

        source = self._get_written_data(store)["source"]
        assert source["query_text"] == "SELECT id FROM users"
        assert source["query_type"] == "SELECT"
        assert source["tables_accessed"] == ["users"]
        assert source["frequency_per_hour"] == 200.0
        assert source["calls_per_second"] == 0.055

    def test_source_section_has_performance_subsection(self):
        store = _mock_store()
        collector_output = _make_collector_output()
        materialize_source(collector_output, "mydb", "job-001", store)

        source = self._get_written_data(store)["source"]
        assert "performance" in source

    def test_performance_subsection_fields(self):
        store = _mock_store()
        pattern = _make_query_pattern(
            execution_time_ms_avg=10.0,
            execution_time_ms_p50=8.0,
            execution_time_ms_p95=25.0,
            execution_time_ms_p99=50.0,
            rows_returned_avg=5.0,
            rows_examined_avg=100.0,
            scan_efficiency_pct=5.0,
            full_table_scans=0,
            db_load_contribution_percent=1.5,
            lock_time_ms=0.0,
            total_time_ms=1000.0,
        )
        collector_output = _make_collector_output([pattern])
        materialize_source(collector_output, "mydb", "job-001", store)

        perf = self._get_written_data(store)["source"]["performance"]
        assert perf["execution_time_ms_avg"] == 10.0
        assert perf["execution_time_ms_p50"] == 8.0
        assert perf["execution_time_ms_p95"] == 25.0
        assert perf["execution_time_ms_p99"] == 50.0
        assert perf["rows_returned_avg"] == 5.0
        assert perf["rows_examined_avg"] == 100.0
        assert perf["scan_efficiency_pct"] == 5.0
        assert perf["full_table_scans"] == 0
        assert perf["db_load_contribution_percent"] == 1.5
        assert perf["lock_time_ms"] == 0.0
        assert perf["total_time_ms"] == 1000.0

    def test_source_section_has_characteristics_subsection(self):
        store = _mock_store()
        collector_output = _make_collector_output()
        materialize_source(collector_output, "mydb", "job-001", store)

        source = self._get_written_data(store)["source"]
        assert "characteristics" in source

    def test_characteristics_subsection_fields(self):
        store = _mock_store()
        pattern = _make_query_pattern(
            has_joins=True,
            join_count=2,
            has_aggregations=True,
            has_subqueries=False,
            has_text_search=False,
            has_time_range_filter=True,
            filter_columns=["created_at", "status"],
            sort_columns=["id"],
        )
        collector_output = _make_collector_output([pattern])
        materialize_source(collector_output, "mydb", "job-001", store)

        chars = self._get_written_data(store)["source"]["characteristics"]
        assert chars["has_joins"] is True
        assert chars["join_count"] == 2
        assert chars["has_aggregations"] is True
        assert chars["has_subqueries"] is False
        assert chars["has_text_search"] is False
        assert chars["has_time_range_filter"] is True
        assert chars["filter_columns"] == ["created_at", "status"]
        assert chars["sort_columns"] == ["id"]


class TestMaterializeSourceNullSections:
    def _get_written_data(self, store: MagicMock) -> dict[str, Any]:
        return dict(store.write_json.call_args[0][1])

    def test_assignment_section_is_null(self):
        store = _mock_store()
        collector_output = _make_collector_output()
        materialize_source(collector_output, "mydb", "job-001", store)

        data = self._get_written_data(store)
        assert data["assignment"] is None

    def test_design_section_is_null(self):
        store = _mock_store()
        collector_output = _make_collector_output()
        materialize_source(collector_output, "mydb", "job-001", store)

        data = self._get_written_data(store)
        assert data["design"] is None

    def test_load_test_section_is_null(self):
        store = _mock_store()
        collector_output = _make_collector_output()
        materialize_source(collector_output, "mydb", "job-001", store)

        data = self._get_written_data(store)
        assert data["load_test"] is None

    def test_sdk_code_section_is_null(self):
        store = _mock_store()
        collector_output = _make_collector_output()
        materialize_source(collector_output, "mydb", "job-001", store)

        data = self._get_written_data(store)
        assert data["sdk_code"] is None


class TestMaterializeSourceIdempotency:
    def test_idempotent_same_output_on_rerun(self):
        """Calling materialize_source twice produces identical write_json calls."""
        store1 = _mock_store()
        store2 = _mock_store()
        patterns = [
            _make_query_pattern(query_id="q_001"),
            _make_query_pattern(query_id="q_002"),
        ]
        collector_output = _make_collector_output(patterns)

        materialize_source(collector_output, "mydb", "job-001", store1)
        materialize_source(collector_output, "mydb", "job-001", store2)

        calls1 = store1.write_json.call_args_list
        calls2 = store2.write_json.call_args_list

        assert len(calls1) == len(calls2)
        for c1, c2 in zip(calls1, calls2, strict=False):
            assert c1[0][0] == c2[0][0]  # same path
            assert c1[0][1] == c2[0][1]  # same data

    def test_idempotent_does_not_read_existing_file(self):
        """materialize_source overwrites without checking existence."""
        store = _mock_store()
        collector_output = _make_collector_output()

        materialize_source(collector_output, "mydb", "job-001", store)
        materialize_source(collector_output, "mydb", "job-001", store)

        # exists() should never be called — pure write, no conditional logic
        store.exists.assert_not_called()
        assert store.write_json.call_count == 2


# ---------------------------------------------------------------------------
# Helpers for materialize_assignment tests
# ---------------------------------------------------------------------------


def _make_assignment_entry(
    query_id: str = "q_001",
    assigned_engine: str = "dynamodb",
    confidence: int = 85,
    assignment_reason: str = "Key-value access",
    in_scope: bool = True,
    customer_override: bool = False,
    warnings: list | None = None,
) -> dict:
    return {
        "query_id": query_id,
        "assigned_engine": assigned_engine,
        "confidence": confidence,
        "assignment_reason": assignment_reason,
        "in_scope": in_scope,
        "customer_override": customer_override,
        "warnings": warnings if warnings is not None else [],
    }


def _make_assignment_output(entries: list | None = None) -> dict:
    if entries is None:
        entries = [_make_assignment_entry()]
    return {"query_assignments": entries}


def _make_existing_journey(query_id: str = "q_001") -> dict:
    return {
        "query_id": query_id,
        "source": {
            "query_text": "SELECT * FROM orders WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": ["orders"],
            "frequency_per_hour": 100.0,
            "calls_per_second": 0.03,
            "performance": {},
            "characteristics": {},
        },
        "assignment": None,
        "design": None,
        "load_test": None,
        "sdk_code": None,
    }


# ---------------------------------------------------------------------------
# Tests for _project_assignment helper
# ---------------------------------------------------------------------------


class TestProjectAssignment:
    def test_returns_correct_shape(self):
        entry = _make_assignment_entry(
            assigned_engine="dynamodb",
            confidence=85,
            assignment_reason="Key-value access",
            in_scope=True,
            customer_override=False,
            warnings=[],
        )
        result = _project_assignment(entry)
        assert result == {
            "assigned_engine": "dynamodb",
            "confidence": 85,
            "assignment_reason": "Key-value access",
            "in_scope": True,
            "customer_override": False,
            "warnings": [],
        }

    def test_does_not_include_query_id(self):
        entry = _make_assignment_entry(query_id="q_999")
        result = _project_assignment(entry)
        assert "query_id" not in result

    def test_preserves_warnings_list(self):
        entry = _make_assignment_entry(warnings=["low confidence", "ambiguous access pattern"])
        result = _project_assignment(entry)
        assert result["warnings"] == ["low confidence", "ambiguous access pattern"]


# ---------------------------------------------------------------------------
# Tests for materialize_assignment
# ---------------------------------------------------------------------------


class TestMaterializeAssignment:
    def test_updates_assignment_section_in_existing_journey(self):
        """Assignment section is populated from the entry."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        store.read_json.return_value = existing

        entry = _make_assignment_entry(
            query_id="q_001",
            assigned_engine="dynamodb",
            confidence=85,
            assignment_reason="Key-value access",
            in_scope=True,
            customer_override=False,
            warnings=[],
        )
        assignment_output = _make_assignment_output([entry])

        materialize_assignment(assignment_output, "mydb", "job-001", store)

        store.write_json.assert_called_once()
        written_path, written_data = store.write_json.call_args[0]
        assert written_path == "mydb/job-001/query-journeys/q_001.json"
        assert written_data["assignment"] == {
            "assigned_engine": "dynamodb",
            "confidence": 85,
            "assignment_reason": "Key-value access",
            "in_scope": True,
            "customer_override": False,
            "warnings": [],
        }

    def test_skips_query_when_journey_file_not_found(self):
        """If read_json raises, the query is silently skipped (no write)."""
        store = _mock_store()
        store.read_json.side_effect = Exception("file not found")

        entry = _make_assignment_entry(query_id="q_missing")
        assignment_output = _make_assignment_output([entry])

        materialize_assignment(assignment_output, "mydb", "job-001", store)

        store.write_json.assert_not_called()

    def test_preserves_all_existing_sections(self):
        """Source, design, load_test, sdk_code are preserved after update."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        existing["design"] = {"some": "design_data"}
        existing["load_test"] = {"tps": 100}
        existing["sdk_code"] = {"language": "python"}
        store.read_json.return_value = existing

        entry = _make_assignment_entry(query_id="q_001")
        assignment_output = _make_assignment_output([entry])

        materialize_assignment(assignment_output, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        assert written_data["source"] == existing["source"]
        assert written_data["design"] == {"some": "design_data"}
        assert written_data["load_test"] == {"tps": 100}
        assert written_data["sdk_code"] == {"language": "python"}
        assert written_data["query_id"] == "q_001"

    def test_handles_multiple_queries(self):
        """Each query in query_assignments is read, updated, and written."""
        store = _mock_store()

        journey_q1 = _make_existing_journey("q_001")
        journey_q2 = _make_existing_journey("q_002")
        store.read_json.side_effect = [journey_q1, journey_q2]

        entries = [
            _make_assignment_entry(query_id="q_001", assigned_engine="dynamodb"),
            _make_assignment_entry(query_id="q_002", assigned_engine="opensearch"),
        ]
        assignment_output = _make_assignment_output(entries)

        materialize_assignment(assignment_output, "mydb", "job-001", store)

        assert store.write_json.call_count == 2
        written_paths = [c[0][0] for c in store.write_json.call_args_list]
        assert "mydb/job-001/query-journeys/q_001.json" in written_paths
        assert "mydb/job-001/query-journeys/q_002.json" in written_paths

        written_data_by_path = {c[0][0]: c[0][1] for c in store.write_json.call_args_list}
        assert (
            written_data_by_path["mydb/job-001/query-journeys/q_001.json"]["assignment"][
                "assigned_engine"
            ]
            == "dynamodb"
        )
        assert (
            written_data_by_path["mydb/job-001/query-journeys/q_002.json"]["assignment"][
                "assigned_engine"
            ]
            == "opensearch"
        )

    def test_mixed_existing_and_missing_queries(self):
        """Existing queries are written; missing ones are silently skipped."""
        store = _mock_store()

        journey_q1 = _make_existing_journey("q_001")

        def _read_side_effect(path: str) -> "dict[str, Any]":
            if "q_001" in path:
                return dict(journey_q1)
            raise Exception("not found")

        store.read_json.side_effect = _read_side_effect

        entries = [
            _make_assignment_entry(query_id="q_001"),
            _make_assignment_entry(query_id="q_missing"),
        ]
        assignment_output = _make_assignment_output(entries)

        materialize_assignment(assignment_output, "mydb", "job-001", store)

        assert store.write_json.call_count == 1
        written_path = store.write_json.call_args[0][0]
        assert "q_001" in written_path


# ---------------------------------------------------------------------------
# Helpers for materialize_design tests
# ---------------------------------------------------------------------------


def _make_access_pattern(
    query_ids: list[str] | None = None,
    source_query_ids: list[str] | None = None,
    index_name: str = "PK-SK-index",
    key_schema: dict | None = None,
) -> dict:
    """Build an access pattern using either query_ids or source_query_ids."""
    pattern: dict = {
        "index_name": index_name,
        "key_schema": key_schema or {"partition_key": "PK", "sort_key": "SK"},
    }
    if query_ids is not None:
        pattern["query_ids"] = query_ids
    if source_query_ids is not None:
        pattern["source_query_ids"] = source_query_ids
    return pattern


def _make_unsupported_pattern(
    query_ids: list[str] | None = None,
    source_query_ids: list[str] | None = None,
    reason: str | None = None,
    pattern_type: str | None = None,
    recommendation: str | None = None,
    workaround: str | None = None,
) -> dict:
    pattern: dict = {}
    if query_ids is not None:
        pattern["query_ids"] = query_ids
    if source_query_ids is not None:
        pattern["source_query_ids"] = source_query_ids
    if reason is not None:
        pattern["reason"] = reason
    if pattern_type is not None:
        pattern["pattern_type"] = pattern_type
    if recommendation is not None:
        pattern["recommendation"] = recommendation
    if workaround is not None:
        pattern["workaround"] = workaround
    return pattern


def _make_schema_output(
    access_patterns: list | None = None,
    unsupported_patterns: list | None = None,
    trade_offs: list | None = None,
) -> dict:
    return {
        "access_patterns": access_patterns or [],
        "unsupported_patterns": unsupported_patterns or [],
        "trade_offs": trade_offs or [],
    }


# ---------------------------------------------------------------------------
# Tests for _get_query_ids helper
# ---------------------------------------------------------------------------


class TestGetQueryIds:
    def test_returns_query_ids_field(self):
        pattern = {"query_ids": ["q_001", "q_002"]}
        assert _get_query_ids(pattern) == ["q_001", "q_002"]

    def test_returns_source_query_ids_field(self):
        pattern = {"source_query_ids": ["q_003"]}
        assert _get_query_ids(pattern) == ["q_003"]

    def test_returns_empty_list_when_neither_field(self):
        pattern = {"index_name": "something"}
        assert _get_query_ids(pattern) == []

    def test_query_ids_takes_precedence_over_source_query_ids(self):
        """When both exist, query_ids wins (or statement — first non-falsy)."""
        pattern = {"query_ids": ["q_001"], "source_query_ids": ["q_002"]}
        result = _get_query_ids(pattern)
        assert result == ["q_001"]


# ---------------------------------------------------------------------------
# Tests for _filter_trade_offs helper
# ---------------------------------------------------------------------------


class TestFilterTradeOffs:
    def test_keeps_trade_off_referencing_query(self):
        trade_offs = [
            {"query_ids": ["q_001", "q_002"], "description": "Cost", "impact": "Medium"},
        ]
        result = _filter_trade_offs(trade_offs, "q_001")
        assert result == [{"description": "Cost", "impact": "Medium"}]

    def test_drops_trade_off_not_referencing_query(self):
        trade_offs = [
            {"query_ids": ["q_002"], "description": "Cost", "impact": "Medium"},
        ]
        result = _filter_trade_offs(trade_offs, "q_001")
        assert result == []

    def test_drops_string_trade_offs(self):
        """ElastiCache plain-string trade-offs are skipped entirely."""
        trade_offs = ["Some trade-off string", "Another string"]
        result = _filter_trade_offs(trade_offs, "q_001")
        assert result == []

    def test_mixed_dict_and_string_trade_offs(self):
        trade_offs = [
            "plain string",
            {"query_ids": ["q_001"], "description": "RCU cost", "impact": "Low"},
        ]
        result = _filter_trade_offs(trade_offs, "q_001")
        assert result == [{"description": "RCU cost", "impact": "Low"}]

    def test_returns_empty_for_empty_list(self):
        assert _filter_trade_offs([], "q_001") == []

    def test_projects_away_query_ids_field(self):
        trade_offs = [
            {"query_ids": ["q_001"], "description": "Storage cost", "impact": "Low"},
        ]
        result = _filter_trade_offs(trade_offs, "q_001")
        assert "query_ids" not in result[0]


# ---------------------------------------------------------------------------
# Tests for _project_unsupported helper
# ---------------------------------------------------------------------------


class TestProjectUnsupported:
    def test_uses_reason_field_when_present(self):
        pattern = {"reason": "Requires full-table scan", "recommendation": "Use OpenSearch"}
        result = _project_unsupported(pattern)
        assert result["reason"] == "Requires full-table scan"

    def test_falls_back_to_pattern_type(self):
        pattern = {"pattern_type": "full_scan", "recommendation": "Use OpenSearch"}
        result = _project_unsupported(pattern)
        assert result["reason"] == "full_scan"

    def test_uses_recommendation_field_when_present(self):
        pattern = {"reason": "x", "recommendation": "Use a GSI"}
        result = _project_unsupported(pattern)
        assert result["recommendation"] == "Use a GSI"

    def test_falls_back_to_workaround(self):
        pattern = {"reason": "x", "workaround": "Denormalize data"}
        result = _project_unsupported(pattern)
        assert result["recommendation"] == "Denormalize data"

    def test_recommendation_defaults_to_empty_string(self):
        pattern = {"reason": "x"}
        result = _project_unsupported(pattern)
        assert result["recommendation"] == ""

    def test_returns_only_reason_and_recommendation(self):
        pattern = {
            "reason": "x",
            "recommendation": "y",
            "query_ids": ["q_001"],
            "extra_field": "z",
        }
        result = _project_unsupported(pattern)
        assert set(result.keys()) == {"reason", "recommendation"}


# ---------------------------------------------------------------------------
# Tests for materialize_design
# ---------------------------------------------------------------------------


class TestMaterializeDesign:
    def test_designed_pattern_dynamodb_style(self):
        """Designed pattern using DynamoDB-style query_ids field."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        store.read_json.return_value = existing

        access_pattern = _make_access_pattern(query_ids=["q_001"])
        schema_output = _make_schema_output(access_patterns=[access_pattern])

        materialize_design(schema_output, "dynamodb", 1, "mydb", "job-001", store)

        store.write_json.assert_called_once()
        _, written_data = store.write_json.call_args[0]
        design = written_data["design"]
        assert design["engine"] == "dynamodb"
        assert design["schema_version"] == 1
        assert design["status"] == "designed"
        assert design["access_pattern"] is not None
        assert design["unsupported"] is None
        assert isinstance(design["trade_offs"], list)

    def test_designed_pattern_documentdb_style(self):
        """Designed pattern using DocumentDB-style source_query_ids field."""
        store = _mock_store()
        existing = _make_existing_journey("q_002")
        store.read_json.return_value = existing

        access_pattern = _make_access_pattern(source_query_ids=["q_002"])
        schema_output = _make_schema_output(access_patterns=[access_pattern])

        materialize_design(schema_output, "documentdb", 1, "mydb", "job-001", store)

        store.write_json.assert_called_once()
        _, written_data = store.write_json.call_args[0]
        design = written_data["design"]
        assert design["engine"] == "documentdb"
        assert design["status"] == "designed"
        assert design["access_pattern"] is not None

    def test_unsupported_pattern_dynamodb_style(self):
        """Unsupported query sets status=unsupported and populates unsupported field."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        store.read_json.return_value = existing

        unsupported = _make_unsupported_pattern(
            query_ids=["q_001"],
            reason="No sort key available",
            recommendation="Use a GSI",
        )
        schema_output = _make_schema_output(unsupported_patterns=[unsupported])

        materialize_design(schema_output, "dynamodb", 1, "mydb", "job-001", store)

        store.write_json.assert_called_once()
        _, written_data = store.write_json.call_args[0]
        design = written_data["design"]
        assert design["status"] == "unsupported"
        assert design["access_pattern"] is None
        assert design["unsupported"] == {
            "reason": "No sort key available",
            "recommendation": "Use a GSI",
        }
        assert design["trade_offs"] == []

    def test_unsupported_pattern_documentdb_style(self):
        """Unsupported query with source_query_ids (DocumentDB) is handled."""
        store = _mock_store()
        existing = _make_existing_journey("q_003")
        store.read_json.return_value = existing

        unsupported = _make_unsupported_pattern(
            source_query_ids=["q_003"],
            pattern_type="full_collection_scan",
            workaround="Add a compound index",
        )
        schema_output = _make_schema_output(unsupported_patterns=[unsupported])

        materialize_design(schema_output, "documentdb", 2, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        design = written_data["design"]
        assert design["status"] == "unsupported"
        assert design["unsupported"]["reason"] == "full_collection_scan"
        assert design["unsupported"]["recommendation"] == "Add a compound index"

    def test_trade_offs_filtered_to_query(self):
        """Only trade-offs referencing the query's ID are included."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        store.read_json.return_value = existing

        access_pattern = _make_access_pattern(query_ids=["q_001"])
        trade_offs = [
            {"query_ids": ["q_001"], "description": "RCU cost", "impact": "Low"},
            {"query_ids": ["q_002"], "description": "Unrelated", "impact": "High"},
        ]
        schema_output = _make_schema_output(
            access_patterns=[access_pattern],
            trade_offs=trade_offs,
        )

        materialize_design(schema_output, "dynamodb", 1, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        design = written_data["design"]
        assert len(design["trade_offs"]) == 1
        assert design["trade_offs"][0]["description"] == "RCU cost"
        assert "query_ids" not in design["trade_offs"][0]

    def test_skips_query_without_journey_file(self):
        """If read_json raises for the query, no write occurs."""
        store = _mock_store()
        store.read_json.side_effect = Exception("not found")

        access_pattern = _make_access_pattern(query_ids=["q_missing"])
        schema_output = _make_schema_output(access_patterns=[access_pattern])

        materialize_design(schema_output, "dynamodb", 1, "mydb", "job-001", store)

        store.write_json.assert_not_called()

    def test_pattern_with_multiple_query_ids_updates_all_journeys(self):
        """One access pattern covering multiple query_ids writes all journey files."""
        journey_q1 = _make_existing_journey("q_001")
        journey_q2 = _make_existing_journey("q_002")

        store = _mock_store()
        store.read_json.side_effect = [journey_q1, journey_q2]

        access_pattern = _make_access_pattern(query_ids=["q_001", "q_002"])
        schema_output = _make_schema_output(access_patterns=[access_pattern])

        materialize_design(schema_output, "dynamodb", 1, "mydb", "job-001", store)

        assert store.write_json.call_count == 2
        written_paths = [c[0][0] for c in store.write_json.call_args_list]
        assert "mydb/job-001/query-journeys/q_001.json" in written_paths
        assert "mydb/job-001/query-journeys/q_002.json" in written_paths

    def test_revision_overwrites_existing_design_section(self):
        """schema_version=2 overwrites an already-set design section."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        existing["design"] = {
            "engine": "dynamodb",
            "schema_version": 1,
            "status": "designed",
            "access_pattern": {"index_name": "old-index"},
            "unsupported": None,
            "trade_offs": [],
        }
        store.read_json.return_value = existing

        new_pattern = _make_access_pattern(query_ids=["q_001"], index_name="new-index")
        schema_output = _make_schema_output(access_patterns=[new_pattern])

        materialize_design(schema_output, "dynamodb", 2, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        design = written_data["design"]
        assert design["schema_version"] == 2
        assert design["access_pattern"]["index_name"] == "new-index"

    def test_access_pattern_excludes_query_ids_field(self):
        """The projected access_pattern must not contain query_ids or source_query_ids."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        store.read_json.return_value = existing

        access_pattern = _make_access_pattern(query_ids=["q_001"])
        schema_output = _make_schema_output(access_patterns=[access_pattern])

        materialize_design(schema_output, "dynamodb", 1, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        ap = written_data["design"]["access_pattern"]
        assert "query_ids" not in ap
        assert "source_query_ids" not in ap

    def test_elasticache_string_trade_offs_are_skipped(self):
        """ElastiCache trade-offs are plain strings — they should not appear in design."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        store.read_json.return_value = existing

        access_pattern = _make_access_pattern(query_ids=["q_001"])
        trade_offs = ["Cache invalidation is manual", "TTL must be set explicitly"]
        schema_output = _make_schema_output(
            access_patterns=[access_pattern],
            trade_offs=trade_offs,
        )

        materialize_design(schema_output, "elasticache", 1, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        assert written_data["design"]["trade_offs"] == []


# ---------------------------------------------------------------------------
# Integration test: full pipeline flow
# ---------------------------------------------------------------------------


class TestFullPipelineFlow:
    """Simulate collector -> assignment -> schema design writing to same journey files."""

    def test_progressive_materialization(self):
        """Journey file builds up section by section across pipeline stages."""
        data_store: dict[str, dict] = {}

        store = MagicMock()

        def _write(path: str, data: dict) -> None:
            data_store[path] = data

        def _read(path: str) -> dict:
            if path not in data_store:
                raise FileNotFoundError(path)
            return dict(data_store[path])

        store.write_json.side_effect = _write
        store.read_json.side_effect = _read

        db, job = "testdb", "job-1"

        # Stage 1: Collector
        collector = {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q_001",
                        "query_text": "SELECT * FROM users WHERE id=1",
                        "query_type": "SELECT",
                        "tables_accessed": ["users"],
                        "frequency_per_hour": 600.0,
                        "calls_per_second": 0.17,
                        "execution_time_ms_avg": 12.0,
                        "execution_time_ms_p50": 10.0,
                        "execution_time_ms_p95": 30.0,
                        "execution_time_ms_p99": 50.0,
                        "rows_returned_avg": 1.0,
                        "rows_examined_avg": 1.0,
                        "scan_efficiency_pct": 100.0,
                        "full_table_scans": 0,
                        "db_load_contribution_percent": 2.0,
                        "lock_time_ms": 0.0,
                        "total_time_ms": 7200.0,
                        "has_joins": False,
                        "join_count": 0,
                        "has_aggregations": False,
                        "has_subqueries": False,
                        "has_text_search": False,
                        "has_time_range_filter": False,
                        "filter_columns": ["id"],
                        "sort_columns": [],
                    },
                    {
                        "query_id": "q_002",
                        "query_text": "SELECT * FROM orders WHERE description LIKE '%keyword%'",
                        "query_type": "SELECT",
                        "tables_accessed": ["orders"],
                        "frequency_per_hour": 200.0,
                        "calls_per_second": 0.06,
                        "execution_time_ms_avg": 45.0,
                        "execution_time_ms_p50": 35.0,
                        "execution_time_ms_p95": 120.0,
                        "execution_time_ms_p99": 200.0,
                        "rows_returned_avg": 15.0,
                        "rows_examined_avg": 5000.0,
                        "scan_efficiency_pct": 0.3,
                        "full_table_scans": 1,
                        "db_load_contribution_percent": 5.0,
                        "lock_time_ms": 0.0,
                        "total_time_ms": 9000.0,
                        "has_joins": False,
                        "join_count": 0,
                        "has_aggregations": False,
                        "has_subqueries": False,
                        "has_text_search": True,
                        "has_time_range_filter": False,
                        "filter_columns": ["description"],
                        "sort_columns": [],
                    },
                ],
            },
        }
        materialize_source(collector, db, job, store)

        # Verify: both queries have source, nothing else
        j1 = data_store[f"{db}/{job}/query-journeys/q_001.json"]
        assert j1["source"]["query_text"] == "SELECT * FROM users WHERE id=1"
        assert j1["assignment"] is None
        assert j1["design"] is None

        j2 = data_store[f"{db}/{job}/query-journeys/q_002.json"]
        assert j2["source"]["characteristics"]["has_text_search"] is True
        assert j2["assignment"] is None

        # Stage 2: Assignment
        assignment = {
            "query_assignments": [
                {
                    "query_id": "q_001",
                    "assigned_engine": "dynamodb",
                    "confidence": 92,
                    "assignment_reason": "Key-value lookup by primary key",
                    "in_scope": True,
                    "customer_override": False,
                    "warnings": [],
                },
                {
                    "query_id": "q_002",
                    "assigned_engine": "opensearch",
                    "confidence": 88,
                    "assignment_reason": "Full-text search pattern",
                    "in_scope": True,
                    "customer_override": False,
                    "warnings": [],
                },
            ],
        }
        materialize_assignment(assignment, db, job, store)

        j1 = data_store[f"{db}/{job}/query-journeys/q_001.json"]
        assert j1["assignment"]["assigned_engine"] == "dynamodb"
        assert j1["assignment"]["confidence"] == 92
        assert j1["design"] is None

        j2 = data_store[f"{db}/{job}/query-journeys/q_002.json"]
        assert j2["assignment"]["assigned_engine"] == "opensearch"

        # Stage 3: Schema design (DynamoDB only — only q_001)
        dynamo_schema = {
            "access_patterns": [
                {
                    "pattern_id": "AP-1",
                    "pattern_group": "User lookups",
                    "description": "Get user by ID",
                    "operation": "GetItem",
                    "table_name": "UsersTable",
                    "gsi_name": None,
                    "key_condition": "PK = 'USER#<id>'",
                    "design_rps": 0.25,
                    "avg_items_returned": 1.0,
                    "item_size_bytes": 256,
                    "strongly_consistent": True,
                    "source_tables": ["users"],
                    "in_scope": True,
                    "out_of_scope_reason": None,
                    "query_ids": ["q_001"],
                },
            ],
            "unsupported_patterns": [],
            "trade_offs": [
                {
                    "description": "Single-table design for users",
                    "impact": "Simple key-value access, no joins needed",
                    "query_ids": ["q_001"],
                    "source_tables": ["users"],
                    "target_tables": ["UsersTable"],
                    "engine": "dynamodb",
                },
            ],
        }
        materialize_design(dynamo_schema, "dynamodb", 1, db, job, store)

        # q_001 now has full journey
        j1 = data_store[f"{db}/{job}/query-journeys/q_001.json"]
        assert j1["source"]["query_text"] == "SELECT * FROM users WHERE id=1"
        assert j1["assignment"]["assigned_engine"] == "dynamodb"
        assert j1["design"]["status"] == "designed"
        assert j1["design"]["engine"] == "dynamodb"
        assert j1["design"]["schema_version"] == 1
        assert j1["design"]["access_pattern"]["pattern_id"] == "AP-1"
        assert j1["design"]["access_pattern"]["operation"] == "GetItem"
        assert len(j1["design"]["trade_offs"]) == 1
        assert j1["load_test"] is None
        assert j1["sdk_code"] is None

        # q_002 untouched by DynamoDB schema design
        j2 = data_store[f"{db}/{job}/query-journeys/q_002.json"]
        assert j2["design"] is None
        assert j2["assignment"]["assigned_engine"] == "opensearch"


# ---------------------------------------------------------------------------
# Tests for materialize_load_test
# ---------------------------------------------------------------------------


class TestMaterializeLoadTest:
    def test_enriches_existing_journey(self):
        """load_test section is populated from the result dict."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        store.read_json.return_value = existing

        results = [
            {
                "query_id": "q_001",
                "tps": 150.0,
                "p95_latency_ms": 12.5,
                "error_rate": 0.0,
            }
        ]

        materialize_load_test(results, "mydb", "job-001", store)

        store.write_json.assert_called_once()
        written_path, written_data = store.write_json.call_args[0]
        assert written_path == "mydb/job-001/query-journeys/q_001.json"
        assert written_data["load_test"] == {
            "tps": 150.0,
            "p95_latency_ms": 12.5,
            "error_rate": 0.0,
        }

    def test_load_test_does_not_include_query_id(self):
        """query_id is stripped from the load_test section."""
        store = _mock_store()
        store.read_json.return_value = _make_existing_journey("q_001")

        results = [{"query_id": "q_001", "tps": 50.0}]
        materialize_load_test(results, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        assert "query_id" not in written_data["load_test"]

    def test_preserves_all_existing_sections(self):
        """Source, assignment, design, sdk_code are preserved after load_test update."""
        store = _mock_store()
        existing = _make_existing_journey("q_001")
        existing["assignment"] = {"assigned_engine": "dynamodb"}
        existing["design"] = {"status": "designed"}
        existing["sdk_code"] = {"language": "python"}
        store.read_json.return_value = existing

        results = [{"query_id": "q_001", "tps": 200.0}]
        materialize_load_test(results, "mydb", "job-001", store)

        _, written_data = store.write_json.call_args[0]
        assert written_data["source"] == existing["source"]
        assert written_data["assignment"] == {"assigned_engine": "dynamodb"}
        assert written_data["design"] == {"status": "designed"}
        assert written_data["sdk_code"] == {"language": "python"}
        assert written_data["query_id"] == "q_001"

    def test_skips_missing_journey_file(self):
        """If read_json raises, the query is silently skipped (no write)."""
        store = _mock_store()
        store.read_json.side_effect = Exception("file not found")

        results = [{"query_id": "q_missing", "tps": 100.0}]
        materialize_load_test(results, "mydb", "job-001", store)

        store.write_json.assert_not_called()

    def test_handles_multiple_results(self):
        """Each result in the list is processed independently."""
        store = _mock_store()

        journey_q1 = _make_existing_journey("q_001")
        journey_q2 = _make_existing_journey("q_002")
        store.read_json.side_effect = [journey_q1, journey_q2]

        results = [
            {"query_id": "q_001", "tps": 100.0},
            {"query_id": "q_002", "tps": 200.0},
        ]
        materialize_load_test(results, "mydb", "job-001", store)

        assert store.write_json.call_count == 2
        written_paths = [c[0][0] for c in store.write_json.call_args_list]
        assert "mydb/job-001/query-journeys/q_001.json" in written_paths
        assert "mydb/job-001/query-journeys/q_002.json" in written_paths

    def test_skips_missing_and_continues_to_next(self):
        """A missing journey file is skipped; subsequent results are still processed."""
        store = _mock_store()

        journey_q2 = _make_existing_journey("q_002")
        store.read_json.side_effect = [Exception("not found"), journey_q2]

        results = [
            {"query_id": "q_missing", "tps": 100.0},
            {"query_id": "q_002", "tps": 200.0},
        ]
        materialize_load_test(results, "mydb", "job-001", store)

        store.write_json.assert_called_once()
        written_path, written_data = store.write_json.call_args[0]
        assert written_path == "mydb/job-001/query-journeys/q_002.json"
        assert written_data["load_test"] == {"tps": 200.0}

    def test_empty_results_writes_nothing(self):
        """When load_test_results is empty, no files are written."""
        store = _mock_store()
        materialize_load_test([], "mydb", "job-001", store)
        store.write_json.assert_not_called()
