"""External-mode Aurora design: compact request, delta response, deterministic merge (#273).

``run_external`` writes a compact design view instead of the collector output
and the full draft; ``--finalize`` accepts either a delta (merged into the
rebuilt draft) or, for backward compatibility, a full output contract.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-1"


def _query(qid: str, table: str, qtype: str = "SELECT", cps: float = 1.0) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table} WHERE id = $1",
        "query_type": qtype,
        "frequency_per_hour": cps * 3600,
        "calls_per_second": cps,
        "tables_accessed": [f"shop.{table}"],
    }


def _collector(n_queries: int = 3) -> dict:
    return {
        "contract_version": "3.0",
        "job_id": JOB,
        "metadata": {
            "collection_timestamp": "2026-01-01T00:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {"engine": "postgresql", "version": "16", "hostname": "h"},
            "database_name": DB,
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.users",
                    "table_name": "users",
                    "row_count": 100,
                    "primary_key": ["id"],
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "bigint",
                            "normalized_data_type": None,
                            "nullable": False,
                        },
                        {
                            "column_name": "email",
                            "data_type": "character varying",
                            "normalized_data_type": "string",
                            "nullable": False,
                        },
                    ],
                    "indexes": [
                        {"index_name": "idx_users_email", "columns": ["email"], "is_unique": True}
                    ],
                },
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "row_count": 1000,
                    "primary_key": ["id"],
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "integer",
                            "normalized_data_type": "integer",
                            "nullable": False,
                        }
                    ],
                },
            ],
            "triggers": [
                {
                    "trigger_id": "trg_orders",
                    "trigger_name": "trg_orders",
                    "table_id": "orders",
                    "event_type": "INSERT",
                    "timing": "BEFORE",
                    "definition": "EXECUTE FUNCTION f()",
                }
            ],
        },
        "queries": {
            "query_patterns": [
                _query(f"q{i}", "users" if i % 2 else "orders", cps=float(i + 1))
                for i in range(n_queries)
            ]
        },
        "metrics": {"performance_metrics": {}},
    }


_ANALYSIS = {
    "contract_version": "2.1",
    "agent_metadata": {
        "agent_name": "aurora-postgresql-analysis-agent",
        "agent_version": "1.0.0",
        "target_database": "aurora_postgresql",
        "analysis_timestamp": "2026-01-01T00:00:00Z",
    },
    "table_recommendations": [],
    "workload_analysis": {"patterns_detected": []},
    "cost_estimate": {"monthly_cost_usd": 1.0, "cost_components": {}},
}


def _store(tmp_path: Path, n_queries: int = 3) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", _collector(n_queries))
    store.write_json(f"{DB}/{JOB}/analysis-aurora_postgresql/analysis.json", _ANALYSIS)
    return store


def _run(monkeypatch, capsys, root: Path, *extra: str) -> dict:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            "aurora_postgresql",
            "--artifact-root",
            str(root),
            "--assignment-version",
            "0",
            *extra,
        ],
    )
    run_schema_design.main()
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    status: dict = json.loads(lines[-1])
    return status


_RESPONSE = f"{DB}/{JOB}/llm_responses/schema_design_aurora_postgresql.json"


def _request(store: LocalArtifactStore) -> dict:
    return store.read_json(f"{DB}/{JOB}/llm_requests/schema_design_aurora_postgresql.json")


# ---------------------------------------------------------------------------
# Request: compact view, delta schema
# ---------------------------------------------------------------------------


def test_request_is_a_compact_view_with_the_delta_schema(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)

    status = _run(monkeypatch, capsys, tmp_path, "--llm-mode", "external")

    assert status["status"] == "awaiting_llm"
    request = _request(store)
    assert request["response_kind"] == "aurora_design_delta"
    # Neither the full collector output nor the draft is sent.
    assert "collector_output" not in request and "draft" not in request
    assert "analysis_output" not in request
    assert request["output_schema"]["title"] == "AuroraDesignDeltaContract"
    view = request["design_view"]
    assert view["migration_strategy"] == "carry_over"
    users = next(t for t in view["tables"] if t["table_name"] == "users")
    assert "id TEXT NOT NULL (residual; source bigint)" in users["columns"]
    assert users["indexes"] == ["idx_users_email UNIQUE (email)"]
    assert users["read_qps"] > 0
    residual_types = {r["source_data_type"]: r["count"] for r in view["residual_types"]}
    assert residual_types == {"bigint": 1, "character varying": 1}
    assert view["source_features"]["triggers"][0]["trigger_name"] == "trg_orders"
    assert "full_ddl" not in json.dumps(request)


def test_request_size_does_not_grow_with_query_count(monkeypatch, capsys, tmp_path):
    small_root, large_root = tmp_path / "small", tmp_path / "large"
    small = _store(small_root, n_queries=50)
    large = _store(large_root, n_queries=2000)

    _run(monkeypatch, capsys, small_root, "--llm-mode", "external")
    _run(monkeypatch, capsys, large_root, "--llm-mode", "external")

    small_view, large_view = _request(small)["design_view"], _request(large)["design_view"]
    assert len(large_view["hot_queries"]) == len(small_view["hot_queries"]) == 40
    assert large_view["in_scope_query_count"] == 2000
    # Only the per-table rates/counts differ; the size stays within a few percent.
    assert len(json.dumps(large_view)) < len(json.dumps(small_view)) * 1.05


# ---------------------------------------------------------------------------
# Finalize: delta merge, validation, backward compatibility
# ---------------------------------------------------------------------------


def test_finalize_with_delta_merges_and_validates(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    store.write_json(
        _RESPONSE,
        {
            "delta_version": "1.0",
            "type_rules": [
                {"source_data_type": "bigint", "aurora_type": "BIGINT"},
                {"source_data_type": "character varying", "aurora_type": "VARCHAR(320)"},
            ],
            "tables": [
                {
                    "table_name": "orders",
                    "add_indexes": ['CREATE INDEX "idx_orders_x" ON "orders" ("id")'],
                }
            ],
            "trade_offs": [{"description": "Carry over", "impact": "Low risk"}],
        },
    )

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "complete", status
    assert status["delta_summary"]["residuals_resolved_by_rule"] == 2
    assert status["delta_summary"]["residuals_unresolved"] == 0
    written = store.read_json(status["output_path"])
    contract = AuroraPostgresqlModelOutputContract.model_validate(written)
    assert contract.validation_passed is True
    assert '"id" BIGINT' in contract.generated_ddl
    assert '"email" VARCHAR(320)' in contract.generated_ddl
    assert 'CREATE INDEX "idx_orders_x" ON "orders" ("id");' in contract.generated_ddl
    assert {t.table_name for t in contract.table_definitions} == {"users", "orders"}


def test_finalize_with_empty_delta_writes_the_draft(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    store.write_json(_RESPONSE, {"delta_version": "1.0"})

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "complete", status
    written = store.read_json(status["output_path"])
    AuroraPostgresqlModelOutputContract.model_validate(written)
    assert len(written["table_definitions"]) == 2


def test_finalize_with_unknown_table_fails_and_writes_nothing(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    store.write_json(_RESPONSE, {"delta_version": "1.0", "tables": [{"table_name": "invoices"}]})

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "validation_failed"
    assert any("unknown table 'invoices'" in e for e in status["errors"])
    assert "output_path" not in status
    assert not store.exists(f"{DB}/{JOB}/schema-aurora_postgresql/v1/schema_output.json")


def test_finalize_delta_runs_the_scope_check(monkeypatch, capsys, tmp_path):
    """The merged design goes through the same scope check as a full contract."""
    store = _store(tmp_path)
    store.write_json(_RESPONSE, {"delta_version": "1.0"})
    calls = []
    from src.agents.schema_design import handler

    real = handler.apply_schema_scope

    def spy(store_, db, job, engine, output, version):
        calls.append(copy.deepcopy(output))
        return real(store_, db, job, engine, output, version)

    monkeypatch.setattr(handler, "apply_schema_scope", spy)

    _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert len(calls) == 1
    assert {t["table_name"] for t in calls[0]["table_definitions"]} == {"users", "orders"}


def test_finalize_still_accepts_a_full_contract(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    store.write_json(
        _RESPONSE,
        {
            "job_id": JOB,
            "source_database": DB,
            "migration_strategy": "carry_over",
            "table_definitions": [
                {
                    "table_name": "users",
                    "columns": [{"name": "id", "aurora_type": "BIGINT", "script_derived": False}],
                }
            ],
            "generated_ddl": 'CREATE TABLE "users" ("id" BIGINT);',
            "trade_offs": [{"description": "d", "impact": "i"}],
            "validation_passed": True,
        },
    )

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "complete", status
    assert "delta_summary" not in status
    written = store.read_json(status["output_path"])
    assert written["generated_ddl"] == 'CREATE TABLE "users" ("id" BIGINT);'


@pytest.mark.parametrize(
    "bad", [{"delta_version": "1.0", "tables": [{"table_name": "users", "typo": 1}]}]
)
def test_finalize_rejects_malformed_delta(bad, monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    store.write_json(_RESPONSE, bad)

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "validation_failed"
    assert status["errors"][0].startswith("Invalid design delta")


# ---------------------------------------------------------------------------
# Request layout (paging) and view content
# ---------------------------------------------------------------------------


def _request_text(store: LocalArtifactStore) -> str:
    key = f"{DB}/{JOB}/llm_requests/schema_design_aurora_postgresql.json"
    return store.read_bytes(key).decode("utf-8")


def test_request_puts_schema_and_evidence_before_one_table_per_line(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)

    _run(monkeypatch, capsys, tmp_path, "--llm-mode", "external")

    text = _request_text(store)
    json.loads(text)  # still one valid JSON document
    head = [
        text.index(f'"{key}":') for key in ("draft_fingerprint", "output_schema", "design_view")
    ]
    assert head == sorted(head)
    view_text = text[head[-1] :]
    order = [view_text.index(f'"{key}": [') for key in ("residual_types", "hot_queries", "tables")]
    assert order == sorted(order)
    table_lines = [ln for ln in text.splitlines() if ln.lstrip().startswith('{"table_name"')]
    assert len(table_lines) == 2
    assert all(ln.rstrip(",").endswith("}") for ln in table_lines)
    assert _request(store)["draft_fingerprint"].startswith("sha256:")


def test_view_carries_column_flags_foreign_keys_and_source_types(monkeypatch, capsys, tmp_path):
    collector = _collector()
    orders = collector["database_schema"]["tables"][1]
    orders["columns"].append(
        {
            "column_name": "user_id",
            "data_type": "bigint unsigned",
            "normalized_data_type": "integer",
            "nullable": True,
            "default_value": "0",
        }
    )
    orders["columns"][0]["is_auto_increment"] = True
    orders["foreign_keys"] = [
        {
            "constraint_name": "fk_orders_user",
            "columns": ["user_id"],
            "referenced_table": "users",
            "referenced_columns": ["id"],
        }
    ]
    store = _store(tmp_path)
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)

    _run(monkeypatch, capsys, tmp_path, "--llm-mode", "external")

    view = _request(store)["design_view"]
    orders_view = next(t for t in view["tables"] if t["table_name"] == "orders")
    assert orders_view["columns"] == [
        "id BIGINT NOT NULL AI (source integer)",  # integer widened to BIGINT
        "user_id BIGINT DEFAULT 0 (source bigint unsigned)",
    ]
    assert orders_view["foreign_keys"] == ["user_id -> users(id)"]


def test_hot_queries_rank_by_load_and_by_call_rate(monkeypatch, capsys, tmp_path):
    collector = _collector(n_queries=100)
    patterns = collector["queries"]["query_patterns"]
    # q0 is the slowest-called query but carries most of the database load.
    patterns[0].update(
        {
            "db_load_contribution_percent": 60.0,
            "total_time_ms": 9e6,
            "rows_examined_avg": 50000.0,
            "execution_time_ms_p95": 900.0,
            "queries_with_bad_index": 7,
        }
    )
    store = _store(tmp_path)
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)

    _run(monkeypatch, capsys, tmp_path, "--llm-mode", "external")

    hot = _request(store)["design_view"]["hot_queries"]
    assert len(hot) == 40
    assert hot[0]["query_id"] == "q0"  # ordered by load
    assert hot[0]["db_load_contribution_percent"] == 60.0
    assert hot[0]["rows_examined_avg"] == 50000.0
    assert hot[0]["execution_time_ms_p95"] == 900.0
    assert hot[0]["queries_with_bad_index"] == 7
    assert "q99" in {q["query_id"] for q in hot}  # the highest call rate is kept too


def test_long_tables_wrap_but_stay_under_the_read_line_limit(monkeypatch, capsys, tmp_path):
    from src.tools.schema.aurora_common.design_view import MAX_LINE

    collector = _collector()
    users = collector["database_schema"]["tables"][0]
    users["columns"] += [
        {
            "column_name": f"attribute_number_{i:03d}",
            "data_type": "character varying",
            "normalized_data_type": "string",
            "max_length": 255,
            "nullable": True,
        }
        for i in range(200)
    ]
    store = _store(tmp_path)
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)

    _run(monkeypatch, capsys, tmp_path, "--llm-mode", "external")

    text = _request_text(store)
    assert max(len(ln) for ln in text.splitlines()) <= MAX_LINE
    assert len(json.loads(text)["design_view"]["tables"][0]["columns"]) == 202


# ---------------------------------------------------------------------------
# Finalize: fingerprint, warnings, deprecation
# ---------------------------------------------------------------------------


def test_finalize_refuses_a_delta_for_a_changed_draft(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    _run(monkeypatch, capsys, tmp_path, "--llm-mode", "external")
    collector = _collector()
    collector["database_schema"]["tables"][1]["columns"][0]["data_type"] = "bigint"
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)  # inputs changed
    store.write_json(_RESPONSE, {"delta_version": "1.0"})

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "validation_failed"
    assert "draft_fingerprint mismatch" in status["errors"][0]


def test_finalize_matching_fingerprint_completes_with_warnings(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    _run(monkeypatch, capsys, tmp_path, "--llm-mode", "external")
    store.write_json(
        _RESPONSE,
        {
            "delta_version": "1.0",
            "type_rules": [{"source_data_type": "money", "aurora_type": "NUMERIC(19,4)"}],
        },
    )

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "complete", status
    assert any("'money' matched no residual" in w for w in status["warnings"])
    assert any("2 residual column(s)" in w for w in status["warnings"])


def test_finalize_full_contract_warns_it_is_deprecated(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    store.write_json(
        _RESPONSE,
        {
            "job_id": JOB,
            "source_database": DB,
            "migration_strategy": "carry_over",
            "table_definitions": [
                {
                    "table_name": "users",
                    "columns": [{"name": "id", "aurora_type": "BIGINT", "script_derived": False}],
                }
            ],
            "generated_ddl": "",
            "trade_offs": [{"description": "d", "impact": "i"}],
            "validation_passed": True,
        },
    )

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "complete"
    assert any(
        w.startswith("Deprecated: the Aurora response is a full") for w in status["warnings"]
    )


def test_finalize_rejects_ddl_injection_in_types(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path)
    store.write_json(
        _RESPONSE,
        {
            "delta_version": "1.0",
            "type_rules": [
                {"source_data_type": "bigint", "aurora_type": "BIGINT); DROP TABLE users; --"}
            ],
        },
    )

    status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert status["status"] == "validation_failed"
    assert not store.exists(f"{DB}/{JOB}/schema-aurora_postgresql/v1/schema_output.json")
