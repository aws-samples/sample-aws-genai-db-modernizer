"""Unit tests for referee-triage agent."""

import json
from dataclasses import asdict
from unittest.mock import MagicMock

from src.agents.referee.triage import triage
from src.storage.artifact_store import ArtifactStore

# ---------------------------------------------------------------------------
# Group 1: Key-value lookups → DynamoDB, ElastiCache
# ---------------------------------------------------------------------------


class TestTriageKeyValue:
    def test_pk_lookups_select_dynamodb_and_elasticache(self):
        co = _co(queries=[_q("SELECT * FROM users WHERE id = ?", rows_returned_avg=1, cps=50)])
        result = triage(co)
        assert "dynamodb" in result.selected
        assert "elasticache" in result.selected

    def test_high_frequency_reads_select_dynamodb(self):
        co = _co(queries=[_q("SELECT name FROM config WHERE key = ?", cps=100)])
        result = triage(co)
        assert "dynamodb" in result.selected

    def test_atomic_counter_max_select_dynamodb(self):
        co = _co(queries=[_q("SELECT COALESCE(MAX(sequence), 0) FROM pay_timeline")])
        result = triage(co)
        assert "dynamodb" in result.selected

    def test_kv_signal_includes_query_ids(self):
        co = _co(queries=[_q("SELECT * FROM users WHERE id = ?", rows_returned_avg=1, qid="q1")])
        result = triage(co)
        kv_sig = _find_signal(result, "key_value_lookups")
        assert kv_sig is not None
        assert "q1" in kv_sig.query_ids


# ---------------------------------------------------------------------------
# Group 2: Range queries → DynamoDB
# ---------------------------------------------------------------------------


class TestTriageRangeQueries:
    def test_range_query_selects_dynamodb(self):
        co = _co(
            queries=[
                _q(
                    "SELECT * FROM orders WHERE user_id = ? AND created_at BETWEEN ? AND ? ORDER BY created_at"
                )
            ]
        )
        result = triage(co)
        assert _has_reason(result, "dynamodb", "range_queries")

    def test_range_signal_includes_query_ids(self):
        co = _co(
            queries=[
                _q(
                    "SELECT * FROM events WHERE user_id = ? AND id BETWEEN ? AND ? ORDER BY id",
                    qid="q-range",
                )
            ]
        )
        result = triage(co)
        sig = _find_signal(result, "range_queries")
        assert sig is not None
        assert "q-range" in sig.query_ids


# ---------------------------------------------------------------------------
# Group 3: Status-based filters → Aurora
# ---------------------------------------------------------------------------


class TestTriageStatusFilters:
    def test_status_equality_selects_aurora_baseline(self):
        co = _co(queries=[_q("SELECT id FROM payment WHERE status = 'PE' AND type_id = 203")])
        result = triage(co)
        assert "aurora" in result.baseline
        reasons = " ".join(result.baseline["aurora"])
        assert "status_filters" in reasons

    def test_exists_subquery_selects_aurora_baseline(self):
        co = _co(
            queries=[
                _q(
                    "SELECT id FROM payment WHERE EXISTS(SELECT 1 FROM entity_tag WHERE entity_id = payment.id)",
                    has_subqueries=True,
                )
            ]
        )
        result = triage(co)
        assert "aurora" in result.baseline

    def test_is_null_with_join_selects_aurora_baseline(self):
        co = _co(
            queries=[
                _q(
                    "SELECT r.* FROM refund r JOIN payment p ON r.payment_id = p.id WHERE r.settlement_id IS NULL",
                    has_joins=True,
                )
            ]
        )
        result = triage(co)
        reasons = " ".join(result.baseline["aurora"])
        assert "status_filters" in reasons


class TestTriageAuroraBaseline:
    def test_aurora_always_in_baseline(self):
        co = _co(queries=[])
        result = triage(co)
        assert "aurora" in result.baseline
        assert "aurora" not in result.selected

    def test_aurora_not_in_selected(self):
        co = _co(queries=[_q("SELECT id FROM payment WHERE status = 'PE'")])
        result = triage(co)
        assert "aurora" not in result.selected
        assert "aurora" not in result.skipped


# ---------------------------------------------------------------------------
# Group 4: Write operations → DynamoDB, Keyspaces
# ---------------------------------------------------------------------------


class TestTriageWriteHeavy:
    def test_high_freq_inserts_select_dynamodb_and_keyspaces(self):
        co = _co(
            queries=[_q("INSERT INTO events (id, data) VALUES (?, ?)", qtype="INSERT", cps=20)]
        )
        result = triage(co)
        assert "dynamodb" in result.selected
        assert "keyspaces" in result.deferred

    def test_low_freq_writes_dont_trigger(self):
        co = _co(
            queries=[_q("INSERT INTO config (key, val) VALUES (?, ?)", qtype="INSERT", cps=0.1)]
        )
        result = triage(co)
        # write_heavy signal requires cps >= 5
        assert "keyspaces" not in result.selected
        assert "keyspaces" not in result.deferred


# ---------------------------------------------------------------------------
# Group 5: Complex joins (3+ tables) → DocumentDB
# ---------------------------------------------------------------------------


class TestTriageComplexJoins:
    def test_3_table_join_selects_documentdb(self):
        co = _co(
            queries=[
                _q(
                    "SELECT p.*, m.name, c.code FROM payment p JOIN merchant m ON p.merchant_id = m.id JOIN country c ON m.country_id = c.id JOIN payment_type pt ON p.type_id = pt.id",
                    has_joins=True,
                    join_count=3,
                )
            ]
        )
        result = triage(co)
        assert "documentdb" in result.selected

    def test_4_table_join_from_text(self):
        co = _co(
            queries=[
                _q(
                    "SELECT * FROM payment p JOIN payment_cielo pc ON p.id = pc.payment_id JOIN settlement_payment sp ON p.id = sp.payment_id JOIN merchant m ON p.merchant_id = m.id GROUP BY pc.bandeira",
                    has_joins=True,
                )
            ]
        )
        result = triage(co)
        assert "documentdb" in result.selected


# ---------------------------------------------------------------------------
# Group 6: Aggregations → DocumentDB, OpenSearch
# ---------------------------------------------------------------------------


class TestTriageAggregations:
    def test_sum_group_by_selects_documentdb(self):
        co = _co(
            queries=[
                _q(
                    "SELECT SUM(set_gross), COUNT(sp.id) FROM settlement_payment sp JOIN payment p ON sp.payment_id = p.id GROUP BY currency_code",
                    has_joins=True,
                )
            ]
        )
        result = triage(co)
        assert "documentdb" in result.selected

    def test_count_with_group_by_selects_opensearch(self):
        co = _co(queries=[_q("SELECT payment_type, COUNT(*) FROM payment GROUP BY payment_type")])
        result = triage(co)
        assert "opensearch" in result.selected


# ---------------------------------------------------------------------------
# Group 7: Large result sets → Aurora, OpenSearch
# ---------------------------------------------------------------------------


class TestTriageLargeScans:
    def test_count_star_with_date_range_detected(self):
        co = _co(
            queries=[
                _q(
                    "SELECT COUNT(1) FROM payment WHERE open_date >= '2025-01-01' AND open_date < '2026-01-01'",
                    has_time_range_filter=True,
                )
            ]
        )
        result = triage(co)
        reasons = " ".join(result.baseline.get("aurora", []))
        assert "large_scans" in reasons or "Relational baseline" in reasons

    def test_full_table_scan_with_many_rows(self):
        co = _co(
            queries=[_q("SELECT * FROM big_table", rows_returned_avg=5000, full_table_scans=100)]
        )
        result = triage(co)
        reasons = " ".join(result.selected.get("opensearch", []))
        assert "large_scans" in reasons


# ---------------------------------------------------------------------------
# Group 8: Time-series → DynamoDB, Keyspaces
# ---------------------------------------------------------------------------


class TestTriageTimeSeries:
    def test_timestamp_insert_selects_dynamodb(self):
        co = _co(
            queries=[
                _q(
                    "INSERT INTO events (id, event_time, data) VALUES (?, ?, ?)",
                    qtype="INSERT",
                    cps=10,
                )
            ]
        )
        result = triage(co)
        assert _has_reason(result, "dynamodb", "time_series")

    def test_timestamp_order_by_selects_dynamodb(self):
        co = _co(
            queries=[_q("SELECT * FROM logs WHERE user_id = ? ORDER BY created_at DESC LIMIT 50")]
        )
        result = triage(co)
        assert _has_reason(result, "dynamodb", "time_series")


# ---------------------------------------------------------------------------
# Group 9: Session store → DynamoDB, ElastiCache
# ---------------------------------------------------------------------------


class TestTriageSessionStore:
    def test_session_keyword_selects_dynamodb(self):
        co = _co(queries=[_q("SELECT * FROM session WHERE session_id = ?", rows_returned_avg=1)])
        result = triage(co)
        assert _has_reason(result, "dynamodb", "session_store")

    def test_token_keyword_selects_elasticache(self):
        co = _co(queries=[_q("SELECT * FROM auth_token WHERE token = ?", rows_returned_avg=1)])
        result = triage(co)
        assert _has_reason(result, "elasticache", "session_store")


# ---------------------------------------------------------------------------
# Group 10: Metadata/config → DynamoDB
# ---------------------------------------------------------------------------


class TestTriageMetadataConfig:
    def test_small_result_high_reads_selects_dynamodb(self):
        co = _co(
            queries=[_q("SELECT value FROM config WHERE key = ?", rows_returned_avg=1, cps=50)]
        )
        result = triage(co)
        assert _has_reason(result, "dynamodb", "metadata_config")


# ---------------------------------------------------------------------------
# Text search → OpenSearch
# ---------------------------------------------------------------------------


class TestTriageTextSearch:
    def test_text_search_selects_opensearch(self):
        co = _co(
            queries=[_q("SELECT * FROM posts WHERE title LIKE '%search%'", has_text_search=True)]
        )
        result = triage(co)
        assert "opensearch" in result.selected


# ---------------------------------------------------------------------------
# Time range → DynamoDB, Keyspaces, OpenSearch
# ---------------------------------------------------------------------------


class TestTriageTimeRange:
    def test_time_range_selects_keyspaces_and_dynamodb(self):
        co = _co(
            queries=[_q("SELECT * FROM events WHERE created_at > ?", has_time_range_filter=True)]
        )
        result = triage(co)
        assert "keyspaces" in result.deferred
        assert "dynamodb" in result.selected
        assert "elasticache" not in result.deferred


# ---------------------------------------------------------------------------
# Leaderboard → ElastiCache
# ---------------------------------------------------------------------------


class TestTriageLeaderboard:
    def test_order_by_limit_selects_elasticache(self):
        co = _co(queries=[_q("SELECT * FROM scores ORDER BY points DESC LIMIT 10")])
        result = triage(co)
        assert "elasticache" in result.selected


# ---------------------------------------------------------------------------
# Schema signals
# ---------------------------------------------------------------------------


class TestTriageSchemaSignals:
    def test_json_columns_select_documentdb(self):
        co = _co(tables=[_table("products", json_columns=["attributes"])])
        result = triage(co)
        assert "documentdb" in result.selected

    def test_json_signal_includes_table_ids(self):
        co = _co(tables=[_table("products", json_columns=["attributes"])])
        result = triage(co)
        sig = _find_signal(result, "json_columns")
        assert sig is not None
        assert "db.products" in sig.table_ids

    def test_junction_tables_select_neptune(self):
        co = _co(tables=[_table("user_roles", columns=["user_id", "role_id"], fk_count=2)])
        result = triage(co)
        assert "neptune" in result.deferred

    def test_junction_tables_also_select_dynamodb(self):
        co = _co(tables=[_table("user_roles", columns=["user_id", "role_id"], fk_count=2)])
        result = triage(co)
        assert "dynamodb" in result.selected

    def test_high_fk_density_selects_neptune(self):
        co = _co(tables=[_table("t1", fk_count=3), _table("t2", fk_count=3)])
        result = triage(co)
        assert "neptune" in result.deferred

    def test_self_referential_fk_selects_neptune(self):
        co = _co(
            tables=[
                {
                    "table_name": "categories",
                    "table_id": "db.categories",
                    "columns": [
                        {"column_name": "id", "data_type": "int"},
                        {"column_name": "parent_id", "data_type": "int"},
                    ],
                    "foreign_keys": [
                        {
                            "constraint_name": "fk_parent",
                            "columns": ["parent_id"],
                            "referenced_table": "categories",
                            "referenced_columns": ["id"],
                        }
                    ],
                    "size_mb": 10,
                }
            ]
        )
        result = triage(co)
        assert "neptune" in result.deferred
        assert _has_reason_deferred(result, "neptune", "self_referential_fk")

    def test_eav_table_selects_dynamodb(self):
        co = _co(tables=[_table("entity_metadata", columns=["entity_id", "name", "value"])])
        result = triage(co)
        assert "dynamodb" in result.selected


# ---------------------------------------------------------------------------
# Skipping
# ---------------------------------------------------------------------------


class TestTriageSkipping:
    def test_no_signals_skips_agent(self):
        co = _co(queries=[_q("SELECT * FROM users WHERE id = ?", rows_returned_avg=1)])
        result = triage(co)
        assert "neptune" not in result.selected

    def test_empty_collector_only_selects_aurora(self):
        co = _co(queries=[], tables=[])
        result = triage(co)
        assert result.selected == {}
        assert "aurora" in result.baseline
        assert len(result.skipped) == 6  # 4 NoSQL + 2 Aurora agents (no source engine metadata)


# ---------------------------------------------------------------------------
# Signal traceability
# ---------------------------------------------------------------------------


class TestTriageSignalTraceability:
    def test_signals_included_in_result(self):
        co = _co(queries=[_q("SELECT * FROM users WHERE id = ?", rows_returned_avg=1, cps=50)])
        result = triage(co)
        assert len(result.signals) > 0

    def test_query_ids_are_deduplicated(self):
        co = _co(
            queries=[
                _q("SELECT MAX(id) FROM t1", qid="q1"),
                _q("SELECT MAX(id) FROM t1", qid="q1"),
            ]
        )
        result = triage(co)
        kv_sig = _find_signal(result, "key_value_lookups")
        if kv_sig:
            assert kv_sig.query_ids.count("q1") == 1

    def test_signals_serializable(self):
        co = _co(
            queries=[_q("SELECT * FROM users WHERE id = ?", rows_returned_avg=1)],
            tables=[_table("products", json_columns=["attrs"])],
        )
        result = triage(co)
        serialized = [asdict(s) for s in result.signals]
        json.dumps(serialized)  # must not raise


# ---------------------------------------------------------------------------
# Handler integration test
# ---------------------------------------------------------------------------


class TestTriageHandler:
    def test_handler_reads_collector_and_writes_triage(self):
        collector_output = {
            "database_schema": {"tables": []},
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM users WHERE id = ?",
                        "query_type": "SELECT",
                        "rows_returned_avg": 1,
                        "calls_per_second": 50,
                        "frequency_per_hour": 180000,
                        "tables_accessed": ["users"],
                    },
                ]
            },
        }
        store = MagicMock(spec=ArtifactStore)
        store.read_json.return_value = collector_output

        from src.agents.referee.triage_handler import run_triage

        run_triage("job-001", "mydb", store)

        store.write_json.assert_called_once()
        call_args = store.write_json.call_args
        assert call_args[0][0] == "mydb/job-001/referee-triage/triage.json"
        body = call_args[0][1]
        selected_types = [a["agent_type"] for a in body["selected_agents"]]
        assert "dynamodb" in selected_types
        # Aurora is baseline, not a dispatched analysis agent
        assert "aurora" not in selected_types
        assert "aurora" in body["baseline"]
        # Verify signals are included in output
        assert "signals" in body
        assert isinstance(body["signals"], list)

    def test_handler_signals_have_query_ids(self):
        collector_output = {
            "database_schema": {"tables": []},
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q-abc",
                        "query_text": "SELECT * FROM users WHERE id = ?",
                        "query_type": "SELECT",
                        "rows_returned_avg": 1,
                        "calls_per_second": 50,
                        "frequency_per_hour": 180000,
                        "tables_accessed": ["users"],
                    },
                ]
            },
        }
        store = MagicMock(spec=ArtifactStore)
        store.read_json.return_value = collector_output

        from src.agents.referee.triage_handler import run_triage

        run_triage("job-002", "mydb", store)

        call_args = store.write_json.call_args
        body = call_args[0][1]
        kv_signals = [s for s in body["signals"] if s["signal"] == "key_value_lookups"]
        assert len(kv_signals) == 1
        assert "q-abc" in kv_signals[0]["query_ids"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _co(queries: list | None = None, tables: list | None = None) -> dict:
    return {
        "database_schema": {"tables": tables or []},
        "queries": {"query_patterns": queries or []},
    }


def _q(
    text: str,
    qtype: str = "SELECT",
    cps: float = 1,
    rows_returned_avg: float = 10,
    has_joins: bool = False,
    join_count: int = 0,
    has_text_search: bool = False,
    has_time_range_filter: bool = False,
    has_aggregations: bool = False,
    has_subqueries: bool = False,
    full_table_scans: int = 0,
    qid: str | None = None,
) -> dict:
    return {
        "query_id": qid or f"q-{hash(text) % 10000}",
        "query_text": text,
        "query_type": qtype,
        "calls_per_second": cps,
        "frequency_per_hour": cps * 3600,
        "rows_returned_avg": rows_returned_avg,
        "tables_accessed": ["unknown"],
        "has_joins": has_joins,
        "join_count": join_count,
        "has_text_search": has_text_search,
        "has_time_range_filter": has_time_range_filter,
        "has_aggregations": has_aggregations,
        "has_subqueries": has_subqueries,
        "full_table_scans": full_table_scans,
    }


def _table(
    name: str,
    columns: list[str] | None = None,
    fk_count: int = 0,
    size_mb: float = 10,
    json_columns: list[str] | None = None,
) -> dict:
    cols = [{"column_name": c, "data_type": "int"} for c in (columns or ["id", "name", "value"])]
    if json_columns:
        cols.extend({"column_name": c, "data_type": "json"} for c in json_columns)
    fks = [
        {
            "constraint_name": f"fk_{i}",
            "columns": [f"col_{i}"],
            "referenced_table": f"ref_{i}",
            "referenced_columns": ["id"],
        }
        for i in range(fk_count)
    ]
    return {
        "table_name": name,
        "table_id": f"db.{name}",
        "columns": cols,
        "foreign_keys": fks,
        "size_mb": size_mb,
    }


def _find_signal(result, signal_name: str):
    for s in result.signals:
        if s.signal == signal_name:
            return s
    return None


def _has_reason(result, agent: str, keyword: str) -> bool:
    return any(keyword in r for r in result.selected.get(agent, []))


def _has_reason_deferred(result, agent: str, keyword: str) -> bool:
    return any(keyword in r for r in result.deferred.get(agent, []))
