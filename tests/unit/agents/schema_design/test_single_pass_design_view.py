# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DocumentDB, OpenSearch and ElastiCache requests are a design view (#2463).

``run_external`` writes each single-pass engine's ``schema_design_<engine>.json`` from
``prepare_schema_design_input``. The file keeps every query, table, column, rate and
figure the designer reads, and drops what it does not read: routines outside the
engine's design, collector fields outside the schema design projection, null and empty
fields, and the ``<field>_lines`` copy of a text whose line Read returns whole. No test
calls a model.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from scripts.run_schema_design import run_external
from src.agents.prompt_framing import UNTRUSTED_NOTICE_KEY
from src.agents.schema_design.external_request import render_external_request
from src.agents.schema_design.handler import prepare_schema_design_input
from src.contracts.schema_design_input import AgentColumn, AgentQueryPattern
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-2463"
ENGINES = ["documentdb", "opensearch", "elasticache"]
# The single-pass engines whose output contract has migration notes (#2463 review).
ROUTINE_READERS = {"documentdb", "elasticache"}
# The fields whose [] the collector contract tells apart from null.
EMPTY_IS_A_VALUE = {"parent_routine_ids", "depends_on", "extensions", "enum_types"}
READ_LINE_LIMIT = 2000  # Read cuts a line longer than this
OWNER = "aurora_postgresql"  # the owner of every query ElastiCache fronts

# A text long enough that its line in the file is over Read's line cut.
LONG_TEXT = (
    "SELECT o.id, "
    + ", ".join(f"o.extra_column_{i}" for i in range(140))
    + " FROM public.orders o WHERE o.customer_id = $1 ORDER BY o.created_at DESC"
)
# A text over the old 1500-character copy threshold whose line Read still returns whole.
MEDIUM_TEXT = (
    "SELECT o.id, "
    + ", ".join(f"o.col_{i:03d}" for i in range(140))
    + " FROM public.orders o WHERE o.status = $1"
)
assert 1500 < len(json.dumps(MEDIUM_TEXT)) < 1900 < 2100 < len(json.dumps(LONG_TEXT))


def _column(name: str, position: int, data_type: str = "bigint", **extra: Any) -> dict:
    return {
        "column_name": name,
        "ordinal_position": position,
        "data_type": data_type,
        "normalized_data_type": None,
        "max_length": None,
        "max_value_bytes": None,
        "numeric_precision": None,
        "numeric_scale": None,
        "datetime_precision": None,
        "nullable": position > 1,
        "default_value": None,
        "is_auto_increment": position == 1,
        "cardinality": None,
        "character_set": None,
        "collation": None,
        **extra,
    }


def _table(name: str, columns: list[dict], **extra: Any) -> dict:
    return {
        "table_id": f"public.{name}",
        "table_name": name,
        "schema_name": "public",
        "row_count": 125_000,
        "row_count_basis": "estimate",
        "stats_analyzed_at": None,
        "size_mb": 48.5,
        "from_ddl": False,
        "data_size_mb": 40.25,
        "columns": columns,
        "indexes": [
            {
                "index_name": f"{name}_pkey",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "BTREE",
                "predicate": None,
            }
        ],
        "primary_key": ["id"],
        "foreign_keys": None,
        "storage_engine": None,
        "collation": None,
        **extra,
    }


def _query(qid: str, text: str, qtype: str, tables: list[str], rate: float, **extra) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "frequency_per_hour": round(rate * 3600, 3),
        "calls_per_second": rate,
        "rate_window_basis": "stats_reset",
        "rate_window_seconds": 144080.0,
        "rate_sample": None,
        "tables_accessed": tables,
        "rows_returned_avg": 12.5,
        "rows_examined_avg": 250.0,
        "rows_returned_p95": 40,
        "execution_time_ms_avg": 1.75,
        "execution_time_ms_p50": 1.5,
        "execution_time_ms_p99": 9.0,
        "total_time_ms": 81234.5,
        "db_load_contribution_percent": 3.25,
        "has_joins": False,
        "has_aggregations": False,
        "has_text_search": qid == "q-search",
        "filter_columns": ["customer_id"],
        "sort_columns": ["created_at"],
        "io_read_time_ms": 0.0,
        "wait_events": None,
        "parsed_facts": {"predicates": [{"column": "customer_id", "op": "="}], "limit": None},
        "view_resolved_text": None,
        "views_read": None,
        "parent_routine_ids": None,
        **extra,
    }


def _pg_function(name: str, body: str, kind: str = "FUNCTION") -> str:
    """A PostgreSQL ``CREATE``, as ``pg_get_functiondef`` gives it (the collector's
    ``definition``)."""
    returns = "" if kind == "PROCEDURE" else "\n RETURNS integer"
    return (
        f"CREATE OR REPLACE {kind} public.{name}(){returns}\n LANGUAGE plpgsql\n"
        f"AS $function$\n{body}\n$function$\n"
    )


def _routine(name: str, *, definition: str | None, refs: list[str] | None = None, **extra):
    return {
        "procedure_id": f"public.{name}",
        "procedure_name": name,
        "schema_name": "public",
        "procedure_type": "FUNCTION",
        "definition": definition,
        "language": "PLPGSQL" if definition else "C",
        "sql_mode": None,
        "return_type": "integer",
        "parameters": None,
        "referenced_tables": refs,
        "execution_stats": None,
        "specific_name": None,
        "owner": None,
        "security_definer": None,
        "depends_on": None,
        "body_status": "collected" if definition else None,
        "body_status_reason": None,
        "body_chars": len(definition) if definition else None,
        "body_exact": None,
        **extra,
    }


def _collector() -> dict:
    orders = _table(
        "orders",
        [
            _column("id", 1),
            _column("customer_id", 2, cardinality=9000),
            _column("status", 3, "character varying", max_length=20),
            _column("created_at", 4, "timestamp with time zone"),
        ],
        foreign_keys=[
            {
                "constraint_name": "orders_customer_fk",
                "columns": ["customer_id"],
                "referenced_table": "public.customers",
                "referenced_columns": ["id"],
                "on_delete": "CASCADE",
            }
        ],
    )
    customers = _table("customers", [_column("id", 1), _column("email", 2, "text")])
    ledger = _table("ledger", [_column("id", 1), _column("amount", 2, "numeric")])
    queries = [
        _query("q-long", LONG_TEXT, "SELECT", ["public.orders"], 4.5),
        _query(
            "q-medium",
            MEDIUM_TEXT,
            "SELECT",
            ["public.orders"],
            2.25,
            views_read=[{"view_id": "public.order_totals", "view_name": "order_totals"}],
        ),
        _query(
            "q-search",
            "SELECT id, email FROM public.customers WHERE email ILIKE $1",
            "SELECT",
            ["public.customers"],
            0.75,
            parent_routine_ids=["public.find_customer"],
        ),
        _query(
            "q-write",
            "UPDATE public.orders SET status = $1 WHERE id = $2",
            "UPDATE",
            ["public.orders"],
            1.5,
            parent_routine_ids=[],  # the source says it ran in no routine
        ),
        _query(
            "q-ledger",
            "INSERT INTO public.ledger (id, amount) VALUES ($1, $2)",
            "INSERT",
            ["public.ledger"],
            9.0,
        ),
    ]
    routines = [
        _routine(
            "order_count",
            definition=_pg_function(
                "order_count",
                "DECLARE n integer;\nBEGIN\n  SELECT count(*) INTO n FROM public.orders;\n"
                "  RETURN n;\nEND;",
            ),
            refs=["public.orders"],
        ),
        _routine(
            "find_customer",
            definition=_pg_function(
                "find_customer",
                "BEGIN\n  PERFORM 1 FROM public.audit_log;\n  RETURN 1;\nEND;",
            ),
        ),
        _routine(
            "touch_orders",
            definition=_pg_function(
                "touch_orders", "BEGIN\n  NEW.created_at := now();\n  RETURN NEW;\nEND;"
            ),
            procedure_type="TRIGGER_FUNCTION",
        ),
        _routine(
            "ledger_total",
            definition=_pg_function(
                "ledger_total",
                "DECLARE n integer;\nBEGIN\n  SELECT sum(amount) INTO n FROM public.ledger;\n"
                "  RETURN n;\nEND;",
            ),
            depends_on=[],  # [] is "none recorded", not "not collected"
        ),
        _routine(
            "ledger_audit_fn",
            definition=_pg_function(
                "ledger_audit_fn",
                "BEGIN\n  INSERT INTO public.ledger_history SELECT NEW.*;\n  RETURN NEW;\nEND;",
            ),
            procedure_type="TRIGGER_FUNCTION",
        ),
        # Extension functions: compiled code, no body, no table, not run by any query
        # (discourse's 246 C and INTERNAL ones).
        *(_routine(f"hstore_fn_{i}", definition=None) for i in range(60)),
    ]
    trigger = {
        "trigger_id": "public.orders_touch",
        "trigger_name": "orders_touch",
        "schema_name": "public",
        "table_id": "public.orders",
        "event_type": "UPDATE",
        "timing": "BEFORE",
        "for_each": "ROW",
        "events": ["UPDATE"],
        "definition": "CREATE TRIGGER orders_touch BEFORE UPDATE ON orders "
        "FOR EACH ROW EXECUTE FUNCTION touch_orders()",
        "is_enabled": True,
        "enabled_mode": "O",
        "function_id": "touch_orders",
        "body_status": None,
    }
    other_trigger = {**trigger, "trigger_id": "public.ledger_audit", "table_id": "public.ledger"}
    other_trigger |= {"trigger_name": "ledger_audit", "function_id": "ledger_audit_fn"}
    other_trigger["definition"] = (
        "CREATE TRIGGER ledger_audit AFTER INSERT ON ledger "
        "FOR EACH ROW EXECUTE FUNCTION ledger_audit_fn()"
    )
    view = {
        "view_id": "public.order_totals",
        "view_name": "order_totals",
        "schema_name": "public",
        "definition": "SELECT customer_id, count(*) FROM orders GROUP BY customer_id",
        "is_updatable": False,
        "referenced_tables": ["public.orders"],
        "column_list": None,
        "lineage_status": "resolved",
    }
    other_view = {**view, "view_id": "public.ledger_view", "view_name": "ledger_view"}
    other_view["referenced_tables"] = ["public.ledger"]
    # A view over an in-scope table that no in-scope query reads.
    status_view = {**view, "view_id": "public.order_status", "view_name": "order_status"}
    status_view["definition"] = "SELECT id, status FROM orders"
    return {
        "contract_version": "5.19",
        "job_id": JOB,
        "metadata": {
            "collection_timestamp": "2026-11-18T10:00:00Z",
            "source_database": {
                "engine": "postgresql",
                "version": "16.10",
                "database_name": DB,
                "settings": None,
            },
            "query_capture": {"captured": 5, "writes_dropped_by_table": {}},
            "extensions": None,
        },
        "database_schema": {
            "tables": [orders, customers, ledger],
            "views": [view, other_view, status_view],
            "procedures": routines,
            "triggers": [trigger, other_trigger],
            "enum_types": [],
            "events": [{"event_name": "nightly_purge", "definition": "DELETE FROM ledger"}],
        },
        "queries": {
            "query_patterns": queries,
            "total_queries_analyzed": 5,
            "query_log_source": "pg_stat_statements",
            "collection_start_time": None,
        },
        "metrics": {
            "performance_metrics": {"qps_avg": 18.0, "qps_peak": 41.5},
            "source_host": None,
        },
    }


def _analysis(engine: str) -> dict:
    return {
        "contract_version": "2.0",
        "agent_metadata": {"agent": engine, "notes": None},
        "table_recommendations": [
            {
                "table_id": "public.orders",
                "confidence_score": 81,
                "rationale": "Orders are read by customer, newest first.",
                "score_breakdown": {"pattern": 0.75, "scale": 0.0},
                "supporting_patterns": [],
                "concerns": None,
                "migration_complexity": "MEDIUM",
            },
            {
                "table_id": "public.customers",
                "confidence_score": 64,
                "rationale": "Customers are looked up by email.",
                "score_breakdown": {"pattern": 0.5},
                "supporting_patterns": ["PAT-1"],
                "concerns": ["email search is a text match"],
                "migration_complexity": "LOW",
            },
        ],
        "workload_analysis": {
            "patterns_detected": [
                {
                    "pattern_id": "PAT-1",
                    "pattern_type": "key_lookup",
                    "confidence": "high",
                    "description": "Orders by customer",
                    "query_ids": ["q-long", "q-medium"],
                    "table_ids": ["public.orders"],
                    "frequency_percent": 37.5,
                }
            ],
            "anti_patterns_detected": [],
        },
        "aggregate_recommendations": [
            {
                "aggregate_id": "agg-orders",
                "root_table": "public.customers",
                "member_tables": ["public.customers", "public.orders"],
                "co_access_confidence": 72,
                "combined_migration_complexity": "LOW",
                "partition_key": None,
                "sort_key": None,
            }
        ],
        "key_scoped_writes": [],
        "load_test_results": None,
    }


def _assignment(engine: str) -> dict:
    """Every query but the ledger insert on ``engine`` (ElastiCache: the reads fronted by
    the cache, owned by Aurora PostgreSQL); the ledger insert on Aurora PostgreSQL."""

    def entry(q: dict) -> dict:
        mine = q["query_id"] != "q-ledger"
        cached = engine == "elasticache" and mine and q["query_type"] == "SELECT"
        return {
            "query_id": q["query_id"],
            "assigned_engine": engine if mine and engine != "elasticache" else OWNER,
            **({"cache_engine": "elasticache"} if cached else {}),
            "in_scope": True,
            "source_tables": q["tables_accessed"],
        }

    return {
        "version": 1,
        "co_dependency_groups": [],
        "query_assignments": [entry(q) for q in _collector()["queries"]["query_patterns"]],
    }


def _seed(tmp_path: Path, engine: str) -> LocalArtifactStore:
    store = LocalArtifactStore(str(tmp_path))
    base = f"{DB}/{JOB}"
    store.write_json(f"{base}/collector/output.json", _collector())
    store.write_json(f"{base}/analysis-{engine}/analysis.json", _analysis(engine))
    store.write_json(f"{base}/assignment/v1/assignment.json", _assignment(engine))
    if engine in ("documentdb", "opensearch"):
        store.write_json(
            f"{base}/analysis-{engine}/decision-trace.json",
            {
                "trace_version": 5,
                "query_matches": [
                    {"query_id": "q-long", "score": 0.875, "tables": ["public.orders"]},
                    {"query_id": "q-ledger", "score": 0.25, "tables": ["public.ledger"]},
                ],
                "relationships": [],
            },
        )
    return store


def _run(tmp_path: Path, engine: str, capsys) -> tuple[dict, dict, str]:
    """(the full prepared input, the request file as JSON, its text)."""
    store = _seed(tmp_path, engine)
    full = prepare_schema_design_input(JOB, DB, engine, store, assignment_version=1)
    run_external(store, JOB, DB, engine, assignment_version=1)
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    text = (Path(store.base_dir) / status["llm_request"]).read_text(encoding="utf-8")
    assert "__trigger_table__" not in text  # the parser's placeholder header is never written
    assert not re.search(r"CREATE (PROCEDURE|FUNCTION|TRIGGER) `[rt]`", text)
    return full, json.loads(text), text


def _leaves(node: Any, out: Counter) -> Counter:
    """Every non-empty leaf value (string, number, boolean), counted."""
    if isinstance(node, dict):
        for value in node.values():
            _leaves(value, out)
    elif isinstance(node, list):
        for value in node:
            _leaves(value, out)
    elif node is not None and node != "":
        out[(type(node).__name__, node)] += 1
    return out


def _filled(record: dict, fields: set[str]) -> dict:
    return {k: v for k, v in record.items() if k in fields and v not in (None, "", [], {})}


@pytest.mark.parametrize("engine", ENGINES)
class TestTheViewKeepsWhatTheDesignerReads:
    @pytest.mark.passes_on_main  # a guard: main keeps these too, the view must not lose them
    def test_every_query_keeps_its_id_text_rates_and_figures(self, tmp_path, capsys, engine):
        full, request, _ = _run(tmp_path, engine, capsys)
        source = full["collector_output"]["queries"]["query_patterns"]
        kept = {q["query_id"]: q for q in request["collector_output"]["queries"]["query_patterns"]}
        assert source and sorted(kept) == sorted(q["query_id"] for q in source)
        for query in source:
            expected = _filled(query, set(AgentQueryPattern.model_fields))
            assert {k: kept[query["query_id"]][k] for k in expected} == expected
            assert kept[query["query_id"]]["query_text"] == query["query_text"]

    def test_every_table_keeps_its_columns_keys_and_sizes(self, tmp_path, capsys, engine):
        full, request, _ = _run(tmp_path, engine, capsys)
        source = full["collector_output"]["database_schema"]["tables"]
        kept = {t["table_id"]: t for t in request["collector_output"]["database_schema"]["tables"]}
        assert source and sorted(kept) == sorted(t["table_id"] for t in source)
        column_fields = set(AgentColumn.model_fields) | {"data_type"}
        for table in source:
            mine = kept[table["table_id"]]
            for key in ("table_name", "row_count", "size_mb", "primary_key"):
                assert mine[key] == table[key]
            for key in ("indexes", "foreign_keys"):
                assert mine.get(key) == (
                    [_filled(x, set(x)) for x in table[key]] if table.get(key) else None
                )
            assert [c["column_name"] for c in mine["columns"]] == [
                c["column_name"] for c in table["columns"]
            ]
            for column, kept_column in zip(table["columns"], mine["columns"], strict=True):
                expected = _filled(column, column_fields - {"is_auto_increment"})
                assert {k: kept_column[k] for k in expected} == expected

    @pytest.mark.passes_on_main  # a guard: main keeps these too, the view must not lose them
    def test_metrics_trace_and_analysis_values_are_all_kept(self, tmp_path, capsys, engine):
        full, request, _ = _run(tmp_path, engine, capsys)
        assert request.get("design_metrics") == full.get("design_metrics")
        assert request.get("decision_trace") == full.get("decision_trace")
        assert _leaves(request["analysis_output"], Counter()) == _leaves(
            full["analysis_output"], Counter()
        )
        for key in ("metadata", "metrics", "cache_invalidation_context"):
            assert _leaves(request["collector_output"].get(key), Counter()) == _leaves(
                full["collector_output"].get(key), Counter()
            )
        header = {
            k: v
            for k, v in full["collector_output"]["queries"].items()
            if k != "query_patterns" and v not in (None, "", [], {})
        }
        assert {k: request["collector_output"]["queries"][k] for k in header} == header
        assert request["output_schema"]["title"]

    @pytest.mark.passes_on_main  # a guard: main keeps these too, the view must not lose them
    def test_the_file_still_says_its_content_is_data(self, tmp_path, capsys, engine):
        _, request, _ = _run(tmp_path, engine, capsys)
        assert next(iter(request)) == UNTRUSTED_NOTICE_KEY
        assert "data" in request[UNTRUSTED_NOTICE_KEY]

    def test_triggers_and_views_of_its_scope_are_kept_others_dropped(
        self, tmp_path, capsys, engine
    ):
        _, request, _ = _run(tmp_path, engine, capsys)
        schema = request["collector_output"]["database_schema"]
        assert [t["trigger_id"] for t in schema["triggers"]] == ["public.orders_touch"]
        assert schema["triggers"][0]["definition"].startswith("CREATE TRIGGER orders_touch")
        # The view the queries read and the view defined on an in-scope table; the view
        # over another engine's table is dropped.
        assert [v["view_id"] for v in schema["views"]] == [
            "public.order_totals",
            "public.order_status",
        ]
        assert "events" not in schema

    def test_collector_fields_outside_the_projection_and_empty_fields_are_dropped(
        self, tmp_path, capsys, engine
    ):
        _, request, _ = _run(tmp_path, engine, capsys)
        allowed = set(AgentQueryPattern.model_fields) | {"query_text_lines"}
        if engine in ROUTINE_READERS:
            allowed.add("parent_routine_ids")  # the link to the routines it keeps
        for query in request["collector_output"]["queries"]["query_patterns"]:
            assert set(query) <= allowed
            assert not {"parsed_facts", "io_read_time_ms", "rate_window_seconds"} & set(query)
        for table in request["collector_output"]["database_schema"]["tables"]:
            assert not {"from_ddl", "data_size_mb", "collation"} & set(table)
        collector = request["collector_output"]
        analysis = request["analysis_output"]
        # The script's lossless trim (drop null and empty fields) finds nothing left to
        # drop, but a [] the contract reads apart from null.
        for section in (collector, analysis):
            assert {p.rsplit(".", 1)[-1] for p in _empties(section)} <= EMPTY_IS_A_VALUE

    def test_a_text_read_whole_gets_no_lines_copy_a_cut_one_keeps_it(
        self, tmp_path, capsys, engine
    ):
        _, request, text = _run(tmp_path, engine, capsys)
        queries = {
            q["query_id"]: q for q in request["collector_output"]["queries"]["query_patterns"]
        }
        if "q-medium" in queries:
            assert "query_text_lines" not in queries["q-medium"]
        assert " ".join(queries["q-long"]["query_text_lines"]) == " ".join(LONG_TEXT.split())
        # The only lines Read would cut are original texts that have a short-line copy.
        cut = [x for x in text.splitlines() if len(x) >= READ_LINE_LIMIT]
        assert cut and all('"query_text": ' in x and LONG_TEXT[:80] in x for x in cut)


def _empties(node: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if value is None or value == "" or value == [] or value == {}:
                found.append(f"{path}.{key}")
            else:
                found.extend(_empties(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for value in node:
            found.extend(_empties(value, f"{path}[]"))
    return found


def _routine_ids(request: dict) -> list[str]:
    schema = request["collector_output"]["database_schema"]
    return [p["procedure_id"] for p in schema.get("procedures") or []]


@pytest.mark.parametrize("engine", sorted(ROUTINE_READERS))
def test_an_engine_with_migration_notes_keeps_the_routines_its_design_reads(
    tmp_path, capsys, engine
):
    """Its skill writes a migration note per routine that needs app-layer logic: the
    routine whose body reads its table, the one its query runs in and its trigger's
    function. The ledger routine (another engine's table) and the bodyless extension
    functions are not its design."""
    _, request, _ = _run(tmp_path, engine, capsys)
    assert sorted(_routine_ids(request)) == [
        "public.find_customer",
        "public.order_count",
        "public.touch_orders",
    ]
    routines = request["collector_output"]["database_schema"]["procedures"]
    assert all(p["definition"].startswith("CREATE OR REPLACE") for p in routines)
    query = next(
        q
        for q in request["collector_output"]["queries"]["query_patterns"]
        if q["query_id"] == "q-search"
    )
    assert query["parent_routine_ids"] == ["public.find_customer"]


def test_opensearch_gets_no_routines(tmp_path, capsys):
    """Its output contract has no migration notes: no routine changes its design."""
    _, request, text = _run(tmp_path, "opensearch", capsys)
    assert _routine_ids(request) == []
    assert "hstore_fn_" not in text and "order_count" not in text


def test_the_routine_readers_are_the_engines_whose_contract_has_migration_notes():
    """No hand list: the view reads routines when the output schema has
    ``migration_notes`` (the reviewer's suggestion on #2485)."""
    from scripts.run_schema_design import _get_output_schema
    from src.agents.schema_design.single_pass_view import (
        SINGLE_PASS_VIEW_ENGINES,
        reads_routines,
    )

    found = {e for e in SINGLE_PASS_VIEW_ENGINES if reads_routines(_get_output_schema(e))}
    assert found == ROUTINE_READERS
    assert not reads_routines({"properties": {"tables": {}}})
    assert not reads_routines(None)


def test_a_contract_empty_list_is_kept_apart_from_null(tmp_path, capsys):
    """``parent_routine_ids: []`` says the statement ran in no routine and
    ``depends_on: []`` that the catalog records none; null says neither was recorded.
    The view keeps the [] (#2485 review)."""
    _, request, _ = _run(tmp_path, "documentdb", capsys)
    queries = {q["query_id"]: q for q in request["collector_output"]["queries"]["query_patterns"]}
    assert queries["q-write"]["parent_routine_ids"] == []
    assert "parent_routine_ids" not in queries["q-long"]  # null stays dropped

    from src.agents.schema_design.single_pass_view import drop_empty

    assert drop_empty({"depends_on": [], "parent_routine_ids": [], "notes": []}) == {
        "depends_on": [],
        "parent_routine_ids": [],
    }
    assert drop_empty({"depends_on": None, "x": {"enum_types": []}}) == {"x": {"enum_types": []}}


@pytest.mark.parametrize("engine", ENGINES)
def test_the_request_is_smaller_than_the_full_input_rendered(tmp_path, capsys, engine):
    """In the spirit of the #2463 size check: the request the model reads against the
    full prepared input rendered the same way. The 60 bodyless routines alone are about
    a third of the full file."""
    full, request, _ = _run(tmp_path, engine, capsys)
    added = ("output_schema", UNTRUSTED_NOTICE_KEY)  # the full input has neither yet
    text, _ = render_external_request({k: v for k, v in request.items() if k not in added})
    full_text, _ = render_external_request(full)
    assert len(text) < 0.5 * len(full_text)


# The bundled samples with their routine sets added, as the e2e and e2e-llm runs read
# them (``ci/llm/sample_routines.py``): real MySQL and PostgreSQL routine records, whose
# ``referenced_tables`` no collector fills and whose ``depends_on`` is [] or null.
REPO = Path(__file__).resolve().parents[4]


def _sample_collector(name: str, dest: Path) -> dict:
    import zipfile

    from ci.llm.sample_routines import add_sample_routines
    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.tools.database.collection_text import extract_json_object
    from src.tools.database.offline_parser import parse_offline_collection

    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        z.extractall(dest)
    collection = dest / f"{name}-collection.json"
    add_sample_routines(collection, name, REPO)
    raw = json.loads(extract_json_object(collection.read_text(encoding="utf-8")))
    parsed = parse_offline_collection(raw, database_name=name)
    return build_offline_collector_output(parsed, job_id=JOB, database_name=name, s3_key="x")


@pytest.fixture(scope="module")
def samples(tmp_path_factory) -> dict[str, dict]:
    return {
        name: _sample_collector(name, tmp_path_factory.mktemp(name))
        for name in ("wordpress", "discourse")
    }


def _scoped_request(collector: dict, tables: set[str], queries: list[dict] | None = None):
    """A request whose scope is ``tables`` (and the collection's queries on them only, or
    ``queries``), shaped as ``prepare_schema_design_input`` scopes it."""
    schema = dict(collector["database_schema"])
    schema["tables"] = [t for t in schema["tables"] if t["table_id"] in tables]
    assert {t["table_id"] for t in schema["tables"]} == tables
    if queries is None:
        queries = [
            q
            for q in collector["queries"]["query_patterns"]
            if set(q.get("tables_accessed") or []) <= tables and q.get("tables_accessed")
        ]
    return {
        "collector_output": {
            **collector,
            "database_schema": schema,
            "queries": {**collector["queries"], "query_patterns": queries},
        }
    }


def _view(request: dict, engine: str) -> dict:
    from scripts.run_schema_design import _get_output_schema
    from src.agents.schema_design.single_pass_view import single_pass_design_view

    return single_pass_design_view(request, _get_output_schema(engine))


def _kept(view: dict, key: str, id_key: str) -> set[str]:
    return {r[id_key] for r in view["collector_output"]["database_schema"].get(key) or []}


@pytest.mark.parametrize("engine", sorted(ROUTINE_READERS))
def test_wordpress_routines_on_its_tables_are_kept_from_their_bodies(samples, engine):
    """``wp_refresh_comment_count`` updates ``wp_posts`` and ``wp_post_comment_count``
    reads ``wp_comments``; neither has ``referenced_tables`` or ``depends_on``, so only
    their MySQL bodies say so. ``wp_bulk_import_mode`` touches no table."""
    request = _scoped_request(samples["wordpress"], {"wordpress.wp_posts", "wordpress.wp_comments"})
    view = _view(request, engine)
    assert _kept(view, "procedures", "procedure_id") == {
        "wp_refresh_comment_count",
        "wp_post_comment_count",
    }
    assert _kept(view, "triggers", "trigger_id") == {"wp_comments_count_after_insert"}


def test_elasticache_keeps_the_routine_that_writes_a_cached_table(samples):
    """A cache's invalidation needs every write to a cached table: the procedure that
    updates ``wp_posts`` is kept, and the function it calls with it."""
    view = _view(_scoped_request(samples["wordpress"], {"wordpress.wp_posts"}), "elasticache")
    assert _kept(view, "procedures", "procedure_id") == {
        "wp_refresh_comment_count",
        "wp_post_comment_count",
    }
    # The trigger on wp_comments writes wp_posts: kept for its body, not its table.
    assert _kept(view, "triggers", "trigger_id") == {"wp_comments_count_after_insert"}


def test_a_routine_an_in_scope_query_calls_is_kept(samples):
    """A routine the application calls (``CALL p(...)``, ``SELECT f(...)``) is kept even
    when its body touches no in-scope table."""
    wordpress = samples["wordpress"]
    base = next(
        q
        for q in wordpress["queries"]["query_patterns"]
        if q["tables_accessed"] == ["wordpress.wp_options"]
    )
    calls = [
        {**base, "query_id": "q-call", "query_text": "CALL wp_bulk_import_mode(1)"},
        {**base, "query_id": "q-fn", "query_text": "SELECT `wp_post_comment_count`(42)"},
    ]
    view = _view(_scoped_request(wordpress, {"wordpress.wp_options"}, calls), "documentdb")
    assert {"wp_bulk_import_mode", "wp_post_comment_count"} <= _kept(
        view, "procedures", "procedure_id"
    )
    view = _view(_scoped_request(wordpress, {"wordpress.wp_options"}, calls[:1]), "documentdb")
    assert "wp_post_comment_count" not in _kept(view, "procedures", "procedure_id")


def test_discourse_routines_on_its_tables_are_kept_from_their_bodies(samples):
    """``topic_visible_post_count`` reads ``posts`` and ``prune_read_notifications``
    deletes from ``notifications`` (``depends_on: []``); ``posts_bump_topic`` is the
    function of the trigger on ``posts``. ``begin_upload_snapshot`` touches no table and
    the compiled extension functions have no body to read: both dropped. An SQL or
    PL/pgSQL routine with no body collected is kept: what cannot be read is not dropped."""
    discourse = samples["discourse"]
    request = _scoped_request(discourse, {"public.posts", "public.notifications"})
    view = _view(request, "documentdb")
    kept = _kept(view, "procedures", "procedure_id")
    bodied = {"topic_visible_post_count", "prune_read_notifications", "posts_bump_topic"}
    assert bodied <= kept and "begin_upload_snapshot" not in kept
    routines = {p["procedure_id"]: p for p in discourse["database_schema"]["procedures"]}
    unread = {
        rid
        for rid, p in routines.items()
        if not p.get("definition") and (p.get("language") or "").lower() in ("sql", "plpgsql")
    }
    called = {
        rid
        for rid in kept - bodied - unread
        if any(
            re.search(rf"\b{re.escape(rid)}\s*\(", q["query_text"], re.I)
            for q in request["collector_output"]["queries"]["query_patterns"]
        )
    }
    # The rest are the routines an in-scope query calls by name (``max(...)``).
    assert unread and kept == bodied | unread | called
    assert not any(routines[r].get("language") in ("C", "INTERNAL") for r in kept - called)
    # depends_on: [] is kept as the contract's "none recorded".
    kept_rows = view["collector_output"]["database_schema"]["procedures"]
    assert next(p for p in kept_rows if p["procedure_id"] == "posts_bump_topic")["depends_on"] == []


def test_a_trigger_is_kept_when_its_body_writes_an_in_scope_table(samples):
    """``posts_bump_topic`` sits on ``posts`` and its function updates ``topics``: with
    ``topics`` in scope and ``posts`` not, the trigger and its function are kept."""
    discourse = samples["discourse"]
    view = _view(_scoped_request(discourse, {"public.topics"}), "documentdb")
    assert "posts_bump_topic" in _kept(view, "triggers", "trigger_id")
    assert "posts_bump_topic" in _kept(view, "procedures", "procedure_id")
    # OpenSearch reads no routine, but still meets the trigger that writes its table.
    view = _view(_scoped_request(discourse, {"public.topics"}), "opensearch")
    assert "posts_bump_topic" in _kept(view, "triggers", "trigger_id")
    assert _kept(view, "procedures", "procedure_id") == set()


def test_a_view_defined_on_an_in_scope_table_is_kept(samples):
    """``badge_posts`` reads ``posts``, ``topics`` and ``categories``: a design of any of
    them meets it, whether or not an in-scope query reads it."""
    discourse = samples["discourse"]
    view = _view(_scoped_request(discourse, {"public.categories"}, []), "opensearch")
    assert _kept(view, "views", "view_id") == {"badge_posts"}


def test_a_routine_whose_body_cannot_be_read_is_kept(samples):
    """Fail safe: a body the parser refuses, or no body at all, is never dropped."""
    wordpress = json.loads(json.dumps(samples["wordpress"]))
    procs = wordpress["database_schema"]["procedures"]
    procs.append({**procs[0], "procedure_id": "wp_broken", "procedure_name": "wp_broken"})
    procs[-1]["definition"] = "BEGIN\n  UPDATE (((( wp_users;\nEND"
    procs.append({**procs[0], "procedure_id": "wp_unread", "procedure_name": "wp_unread"})
    procs[-1] |= {"definition": None, "body_status": "not_readable"}
    view = _view(_scoped_request(wordpress, {"wordpress.wp_options"}), "documentdb")
    assert {"wp_broken", "wp_unread"} <= _kept(view, "procedures", "procedure_id")
    assert "wp_bulk_import_mode" not in _kept(view, "procedures", "procedure_id")
    # The same for a trigger on another table: its body may write an in-scope one.
    assert _kept(view, "triggers", "trigger_id") == set()
    triggers = wordpress["database_schema"]["triggers"]
    triggers.append({**triggers[0], "trigger_id": "wp_broken_trg", "trigger_name": "x"})
    triggers[-1]["definition"] = "UPDATE (((( wp_options"
    view = _view(_scoped_request(wordpress, {"wordpress.wp_options"}), "opensearch")
    assert _kept(view, "triggers", "trigger_id") == {"wp_broken_trg"}


def test_an_empty_scope_keeps_no_unreadable_routine_or_trigger(samples):
    """With no in-scope table there is nothing a routine or trigger could write: the
    bodiless discourse routines and the read-only triggers (whose functions have no body)
    stay out of a request that designs nothing."""
    view = _view(_scoped_request(samples["discourse"], set(), []), "documentdb")
    assert _kept(view, "procedures", "procedure_id") == set()
    assert _kept(view, "triggers", "trigger_id") == set()
    view = _view(_scoped_request(samples["discourse"], {"public.users"}), "documentdb")
    assert "topic_timers_topic_id_readonly" in _kept(view, "triggers", "trigger_id")


def test_a_postgresql_routine_that_executes_a_built_statement_is_kept(samples):
    """``EXECUTE format(...)`` (and MySQL's ``PREPARE ... FROM @s``, next test) parses, but
    the table it writes is built at run time: the routine is unreadable, so it is kept."""
    discourse = json.loads(json.dumps(samples["discourse"]))
    procs = discourse["database_schema"]["procedures"]
    procs.append(
        {
            **procs[0],
            "procedure_id": "public.prune_dynamic",
            "procedure_name": "prune_dynamic",
            "language": "PLPGSQL",
            "definition": _pg_function(
                "prune_dynamic", "BEGIN\n  EXECUTE format('DELETE FROM %I', 'notifications');\nEND"
            ),
        }
    )
    view = _view(_scoped_request(discourse, {"public.categories"}), "documentdb")
    assert "public.prune_dynamic" in _kept(view, "procedures", "procedure_id")


def test_a_mysql_routine_that_prepares_a_built_statement_is_kept(samples):
    wordpress = json.loads(json.dumps(samples["wordpress"]))
    procs = wordpress["database_schema"]["procedures"]
    procs.append({**procs[0], "procedure_id": "wp_dynamic", "procedure_name": "wp_dynamic"})
    procs[-1]["definition"] = (
        "BEGIN\n  SET @s = CONCAT('UPDATE ', 'wp_posts', ' SET x = 1');\n"
        "  PREPARE st FROM @s;\n  EXECUTE st;\nEND"
    )
    view = _view(_scoped_request(wordpress, {"wordpress.wp_options"}), "elasticache")
    assert "wp_dynamic" in _kept(view, "procedures", "procedure_id")
    assert "wp_bulk_import_mode" not in _kept(view, "procedures", "procedure_id")


def test_no_request_the_view_builds_holds_the_placeholder_create_header(samples):
    wordpress = _scoped_request(samples["wordpress"], {"wordpress.wp_posts"})
    for engine in sorted(ROUTINE_READERS):
        text = json.dumps(_view(wordpress, engine))
        assert "__trigger_table__" not in text
        assert not re.search(r"CREATE (PROCEDURE|FUNCTION|TRIGGER) `[rt]`", text)
