"""The compact per-group DynamoDB schema design input (issue #272)."""

from __future__ import annotations

import json

from src.agents.schema_design.group_input import (
    LINE_WIDTH,
    READ_PAGE_CHARS,
    READ_PAGE_LINES,
    build_group_input,
    read_pages,
    render_group_input,
)
from src.agents.schema_design.group_splitter import (
    MAX_GROUP_INPUT_CHARS,
    MAX_GROUP_SIZE,
    build_groups,
    fit_groups_to_budget,
    split_schema_input,
    tables_for_queries,
)
from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.schema_design_input import project_schema_design_input
from src.storage.local_store import LocalArtifactStore
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

PRODUCTS = "ecommerce.products"


def _analysis(collector: dict) -> dict:
    tables = [t["table_id"] for t in collector["database_schema"]["tables"]]
    qids = [q["query_id"] for q in collector["queries"]["query_patterns"]]
    return {
        "contract_version": "2.1",
        "agent_metadata": {
            "agent_name": "dynamodb-analysis-agent",
            "agent_version": "1.0.0",
            "target_database": "dynamodb",
            "analysis_timestamp": "2026-10-04T00:00:00",
        },
        "table_recommendations": [
            {
                "table_id": t,
                "confidence_score": 80,
                "rationale": None,
                "score_breakdown": {
                    "pattern_match_score": 1,
                    "complexity_score": 1,
                    "performance_score": 1,
                    "cost_score": 1,
                },
                "concerns": [f"concern for {t}"],
            }
            for t in tables
        ],
        "workload_analysis": {
            "patterns_detected": [
                {
                    "pattern_id": "dynamodb-01",
                    "pattern_type": "key-value-lookup",
                    "confidence": "HIGH",
                    "description": "every table",
                    "query_ids": qids,
                    "table_ids": tables,
                    "frequency_percent": None,
                },
                {
                    "pattern_id": "dynamodb-99",
                    "pattern_type": "unrelated",
                    "confidence": "LOW",
                    "query_ids": ["not-a-query"],
                    "table_ids": ["ecommerce.nowhere"],
                },
            ],
            "anti_patterns_detected": [
                {
                    "anti_pattern_id": "dynamodb-ap-01",
                    "anti_pattern_type": "frequent-full-scan",
                    "severity_weight": 0.5,
                    "query_ids": qids,
                    "table_ids": tables,
                }
            ],
        },
        "cost_estimate": {"monthly_cost_usd": 1.0, "cost_components": {}},
        "aggregate_recommendations": [
            {
                "aggregate_id": f"agg-{t}",
                "root_table": t,
                "member_tables": [t],
                "co_access_confidence": 40,
                "combined_migration_complexity": "LOW",
                "partition_key": None,
            }
            for t in tables
        ],
    }


def _products_group(collector: dict, analysis: dict) -> dict:
    queries = [
        q for q in collector["queries"]["query_patterns"] if q["tables_accessed"] == [PRODUCTS]
    ]
    assert queries, "fixture has product queries"
    tables = tables_for_queries(queries, collector["database_schema"]["tables"])
    return build_group_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        group_index=0,
        group_name="products",
        primary_tables=[PRODUCTS],
        group_queries=queries,
        group_tables=tables,
        collector_output=collector,
        analysis_output=analysis,
        total_queries=len(collector["queries"]["query_patterns"]),
    )


def test_group_input_holds_only_the_groups_tables_and_signals() -> None:
    collector = get_ecommerce_collector_output()
    data = _products_group(collector, _analysis(collector))
    co, an = data["collector_output"], data["analysis_output"]

    assert [t["table_id"] for t in co["database_schema"]["tables"]] == [PRODUCTS]
    # No procedures/views/triggers and no duplicate top-level tables.
    assert set(co["database_schema"]) == {"tables"}
    assert "tables" not in co
    # The source queries header (coverage denominator) is kept.
    assert co["queries"]["total_queries_analyzed"] == collector["queries"]["total_queries_analyzed"]

    group_qids = {q["query_id"] for q in co["queries"]["query_patterns"]}
    patterns = an["workload_analysis"]["patterns_detected"]
    assert [p["pattern_id"] for p in patterns] == ["dynamodb-01"]  # unrelated one dropped
    assert set(patterns[0]["query_ids"]) == group_qids
    assert patterns[0]["table_ids"] == [PRODUCTS]
    anti = an["workload_analysis"]["anti_patterns_detected"]
    assert set(anti[0]["query_ids"]) == group_qids and anti[0]["table_ids"] == [PRODUCTS]
    assert [a["root_table"] for a in an["aggregate_recommendations"]] == [PRODUCTS]
    assert [r["table_id"] for r in an["table_recommendations"]] == [PRODUCTS]


def test_group_input_drops_nulls_and_fields_the_design_does_not_read() -> None:
    collector = get_ecommerce_collector_output()
    data = _products_group(collector, _analysis(collector))
    co, an = data["collector_output"], data["analysis_output"]
    table = co["database_schema"]["tables"][0]
    assert "sample_data" not in table
    for col in table["columns"]:
        assert "data_type" in col  # the native type stays
        assert "ordinal_position" not in col
        assert all(v is not None for v in col.values())
    for q in co["queries"]["query_patterns"]:
        assert all(v is not None for v in q.values())
        assert not {
            "shared_blks_hit",
            "io_read_time_ms",
            "wait_events",
            "cache_hit_ratio_pct",
        } & set(q)
    assert "rationale" not in an["table_recommendations"][0]
    assert "partition_key" not in an["aggregate_recommendations"][0]


def test_group_input_still_validates_and_projects_for_the_bedrock_path() -> None:
    collector = get_ecommerce_collector_output()
    data = json.loads(render_group_input(_products_group(collector, _analysis(collector))))
    c = CollectorOutputContract.model_validate(data["collector_output"])
    a = AnalysisOutputContract.model_validate(data["analysis_output"])
    agent_collector, agent_analysis, _ = project_schema_design_input(c, a)
    assert [t.table_id for t in agent_collector.tables] == [PRODUCTS]
    assert agent_analysis.patterns_detected[0].pattern_id == "dynamodb-01"


def test_render_puts_one_record_per_line() -> None:
    collector = get_ecommerce_collector_output()
    data = _products_group(collector, _analysis(collector))
    text = render_group_input(data)
    assert json.loads(text) == json.loads(json.dumps(data, default=str))
    lines = text.splitlines()
    for col in data["collector_output"]["database_schema"]["tables"][0]["columns"]:
        # Each column sits whole on one line (alone, or inside its table's line).
        assert any(json.dumps(col, ensure_ascii=False) in ln for ln in lines), col["column_name"]
    assert max(len(ln) for ln in lines) <= LINE_WIDTH + 40
    # Far fewer lines than one field per line.
    assert len(lines) * 4 < len(json.dumps(data, indent=2).splitlines())


def test_long_query_text_is_also_given_in_short_lines() -> None:
    collector = get_ecommerce_collector_output()
    q = collector["queries"]["query_patterns"][0]
    q["query_text"] = "SELECT " + ", ".join(f"col_{i}" for i in range(400)) + " FROM products"
    q["tables_accessed"] = [PRODUCTS]
    data = _products_group(collector, _analysis(collector))
    slim = next(
        x
        for x in data["collector_output"]["queries"]["query_patterns"]
        if x["query_id"] == q["query_id"]
    )
    assert slim["query_text"] == q["query_text"]
    assert " ".join(slim["query_text_lines"]) == " ".join(q["query_text"].split())
    assert max(len(ln) for ln in slim["query_text_lines"]) < 200
    text = render_group_input(data)
    long_lines = [ln for ln in text.splitlines() if len(ln) > LINE_WIDTH + 40]
    assert len(long_lines) == 1 and '"query_text":' in long_lines[0]


def test_read_pages_cover_the_file_in_read_sized_pages() -> None:
    text = "".join(f'{{"n": {i}, "pad": "{"x" * (i % 300)}"}}\n' for i in range(5000))
    pages = read_pages(text)
    assert pages[0]["offset"] == 1
    lines = text.splitlines()
    covered = 0
    for page in pages:
        assert page["offset"] == covered + 1
        assert page["limit"] <= READ_PAGE_LINES
        chunk = lines[page["offset"] - 1 : page["offset"] - 1 + page["limit"]]
        assert sum(len(ln) + 1 for ln in chunk) <= READ_PAGE_CHARS
        covered += page["limit"]
    assert covered == len(lines)


def test_misc_batch_never_exceeds_the_group_cap() -> None:
    # Clusters of 3 queries each (under SMALL_GROUP_THRESHOLD) used to push a
    # misc batch to 21 queries before the size check.
    queries = [
        {"query_id": f"q{t}_{i}", "tables_accessed": [f"db.t{t}"]}
        for t in range(10)
        for i in range(3)
    ]
    groups = build_groups(queries, "db")
    assert max(len(g["queries"]) for g in groups) <= MAX_GROUP_SIZE
    assert sum(len(g["queries"]) for g in groups) == len(queries)


def test_fit_groups_to_budget_halves_oversized_groups() -> None:
    group = {"group_name": "big", "primary_tables": ["db.t"], "queries": list(range(20))}

    def measure(g: dict) -> int:
        return 1000 * len(g["queries"])

    fitted = fit_groups_to_budget([group], measure, max_chars=6000)
    assert [len(g["queries"]) for g in fitted] == [5, 5, 5, 5]
    assert [g["group_name"] for g in fitted] == ["big_s1", "big_s2", "big_s3", "big_s4"]
    assert all(g["primary_tables"] == ["db.t"] for g in fitted)
    assert sum((g["queries"] for g in fitted), []) == group["queries"]
    # A single query is kept whatever its size; a group that fits is untouched.
    one = {"group_name": "one", "primary_tables": [], "queries": [0]}
    assert fit_groups_to_budget([one], measure, max_chars=10) == [one]


def test_split_writes_bounded_inputs_with_read_pages(tmp_path) -> None:
    collector = get_ecommerce_collector_output()
    # Inflate the workload: many wide queries on one table.
    base = collector["queries"]["query_patterns"][0]
    collector["queries"]["query_patterns"] = [
        {
            **base,
            "query_id": f"q{i}",
            "tables_accessed": [PRODUCTS],
            "query_text": f"SELECT /* {i} */ " + "x, " * 1500 + "y FROM products",
        }
        for i in range(20)
    ]
    analysis = _analysis(collector)
    store = LocalArtifactStore(str(tmp_path))
    manifest = split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=analysis,
        queries=collector["queries"]["query_patterns"],
        store=store,
    )
    assert manifest.total_groups > 1  # the 20-query group went over the budget
    assert manifest.total_groups == len(manifest.groups)
    assert sum(g.query_count for g in manifest.groups) == 20
    for g in manifest.groups:
        text = (tmp_path / "ecommerce/j1/schema-dynamodb/v1" / g.input_file).read_text()
        assert len(text) <= MAX_GROUP_INPUT_CHARS
        assert g.input_pages == read_pages(text)
        assert 1 <= len(g.input_pages) <= 3
