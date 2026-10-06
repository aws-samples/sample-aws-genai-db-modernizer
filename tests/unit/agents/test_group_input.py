"""The compact per-group DynamoDB schema design input (issue #272)."""

from __future__ import annotations

import json

from src.agents.schema_design.group_input import (
    LINE_WIDTH,
    QUERY_TEXT_LINE,
    READ_CHARS_PER_TOKEN,
    READ_PAGE_CHARS,
    READ_PAGE_LINES,
    READ_TOKEN_LIMIT,
    _split_text,
    build_group_input,
    read_pages,
    render_group_input,
)
from src.agents.schema_design.group_splitter import (
    MAX_GROUP_INPUT_CHARS,
    MAX_GROUP_INPUT_PAGES,
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
    # No procedures/views, no triggers on other tables, no duplicate top-level tables.
    assert set(co["database_schema"]) == {"tables"}
    assert "tables" not in co
    # total_queries_analyzed counts the whole database: as the denominator of
    # the skill's coverage check it would flag every group as incomplete.
    assert "total_queries_analyzed" not in co["queries"]
    assert co["queries"]["_filtered_count"] == len(co["queries"]["query_patterns"])
    assert co["queries"]["query_log_source"] == collector["queries"]["query_log_source"]

    group_qids = {q["query_id"] for q in co["queries"]["query_patterns"]}
    patterns = an["workload_analysis"]["patterns_detected"]
    assert [p["pattern_id"] for p in patterns] == ["dynamodb-01"]  # unrelated one dropped
    assert set(patterns[0]["query_ids"]) == group_qids
    assert patterns[0]["table_ids"] == [PRODUCTS]
    anti = an["workload_analysis"]["anti_patterns_detected"]
    assert set(anti[0]["query_ids"]) == group_qids and anti[0]["table_ids"] == [PRODUCTS]
    assert [a["root_table"] for a in an["aggregate_recommendations"]] == [PRODUCTS]
    assert [r["table_id"] for r in an["table_recommendations"]] == [PRODUCTS]


def test_group_input_keeps_the_triggers_on_its_tables() -> None:
    collector = get_ecommerce_collector_output()
    triggers = collector["database_schema"]["triggers"]
    triggers.append(
        {
            **triggers[0],
            "trigger_id": "ecommerce.trg_products_update",
            "trigger_name": "trg_products_update",
            "table_id": PRODUCTS,
            "is_enabled": None,
        }
    )
    # Some collectors give a trigger's table as the bare name (discourse).
    triggers.append(
        {
            **triggers[0],
            "trigger_id": "trg_products_bare",
            "trigger_name": "trg_products_bare",
            "table_id": "products",
        }
    )
    data = _products_group(collector, _analysis(collector))
    kept = data["collector_output"]["database_schema"]["triggers"]
    assert [t["trigger_id"] for t in kept] == [
        "ecommerce.trg_products_update",
        "trg_products_bare",
    ]
    assert kept[0]["definition"] == triggers[0]["definition"]
    assert "is_enabled" not in kept[0]


def test_group_input_drops_nulls_and_fields_the_design_does_not_read() -> None:
    collector = get_ecommerce_collector_output()
    data = _products_group(collector, _analysis(collector))
    co, an = data["collector_output"], data["analysis_output"]
    table = co["database_schema"]["tables"][0]
    assert "sample_data" not in table
    for col in table["columns"]:
        assert "data_type" in col  # the native type stays
        # Kept: the list order is not always the ordinal, and the skill sorts by it.
        assert "ordinal_position" in col
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
    q["query_text"] = (
        "SELECT "  # nosec B608 -- SQL text is test fixture data, never executed
        + ", ".join(f"col_{i}" for i in range(400))
        + " FROM products"
    )
    q["tables_accessed"] = [PRODUCTS]
    data = _products_group(collector, _analysis(collector))
    slim = next(
        x
        for x in data["collector_output"]["queries"]["query_patterns"]
        if x["query_id"] == q["query_id"]
    )
    assert slim["query_text"] == q["query_text"]
    # Whitespace is collapsed, so the lines join to the normalised text.
    assert " ".join(slim["query_text_lines"]) == " ".join(q["query_text"].split())
    assert max(len(ln) for ln in slim["query_text_lines"]) <= QUERY_TEXT_LINE
    text = render_group_input(data)
    long_lines = [ln for ln in text.splitlines() if len(ln) > LINE_WIDTH + 40]
    assert len(long_lines) == 1 and '"query_text":' in long_lines[0]


def test_query_text_lines_hard_wrap_long_words() -> None:
    word = "x" * (QUERY_TEXT_LINE * 2 + 7)
    lines = _split_text(
        f"SELECT {word} FROM t"  # nosec B608 -- SQL text is test fixture data, never executed
    )
    assert max(len(ln) for ln in lines) <= QUERY_TEXT_LINE
    assert "".join(lines).replace(" ", "") == f"SELECT{word}FROMt"


def test_query_text_threshold_counts_json_escapes() -> None:
    # Under LINE_WIDTH characters, but over it once newlines and quotes are escaped.
    text = 'SELECT "a"\n' * (LINE_WIDTH // 11 - 1)
    assert len(text) < LINE_WIDTH < len(json.dumps(text))
    collector = get_ecommerce_collector_output()
    q = collector["queries"]["query_patterns"][0]
    q["query_text"], q["tables_accessed"] = text, [PRODUCTS]
    data = _products_group(collector, _analysis(collector))
    slim = next(
        x
        for x in data["collector_output"]["queries"]["query_patterns"]
        if x["query_id"] == q["query_id"]
    )
    assert "query_text_lines" in slim


def test_read_page_stays_under_the_read_token_limit() -> None:
    # Read estimates ~1 token per 2 characters and refuses over 25,000 tokens;
    # keep a page at most 80% of that.
    assert READ_TOKEN_LIMIT == 25_000 and READ_CHARS_PER_TOKEN == 2
    assert READ_PAGE_CHARS / READ_CHARS_PER_TOKEN <= 0.8 * READ_TOKEN_LIMIT
    assert MAX_GROUP_INPUT_CHARS <= 3 * READ_PAGE_CHARS


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
        assert 1 <= len(g.input_pages) <= MAX_GROUP_INPUT_PAGES


def test_split_resolves_qualifier_mismatch_above_max_group_size(tmp_path) -> None:
    """#367 review finding 1: ``filter_collector_for_assignment`` resolves the
    #116 qualifier mismatch, but ``tables_for_queries`` in group_splitter.py
    did not use the same resolver, so ``--split`` wrote zero-table group parts
    for any engine with more than ``MAX_GROUP_SIZE`` queries once the
    collector's table_id qualifier (customer-entered database label)
    diverged from the SQL schema qualifier a query's ``tables_accessed``
    carries. 25 queries forces the sub-split that single-group runs (and the
    PR's original regression test) never exercised.
    """
    collector = get_ecommerce_collector_output()
    for t in collector["database_schema"]["tables"]:
        if t["table_id"] == PRODUCTS:
            # The customer-entered database label ("ecommerc3") diverges from
            # the real SQL schema ("ecommerce") a query's tables_accessed carries.
            t["table_id"] = "ecommerc3.products"
    queries = [
        {
            "query_id": f"q-prod-{i}",
            "query_text": "SELECT product_id FROM products WHERE product_id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 10.0,
            "calls_per_second": 1.0,
            "tables_accessed": [PRODUCTS],
        }
        for i in range(25)
    ]
    analysis = _analysis(collector)
    store = LocalArtifactStore(str(tmp_path))

    manifest = split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=analysis,
        queries=queries,
        store=store,
    )

    # 25 queries, all on one table, over MAX_GROUP_SIZE=20: the cluster is
    # sub-split into two chunks (20 + 5), so no group has zero tables and no
    # query is excluded (none of these queries is pseudo-table-only).
    assert manifest.excluded_queries == []
    assert manifest.total_groups >= 2
    assert sum(g.query_count for g in manifest.groups) == 25
    for g in manifest.groups:
        assert g.table_count > 0, f"group {g.group_name!r} has no tables"
        data = json.loads((tmp_path / "ecommerce/j1/schema-dynamodb/v1" / g.input_file).read_text())
        CollectorOutputContract.model_validate(data["collector_output"])


def _catalog_queries(n: int, start: int = 0) -> list[dict]:
    """``n`` queries whose only table is the pseudo table ``unknown`` (#276):
    catalog/utility statements like ``SELECT obj_description(...)``."""
    return [
        {
            "query_id": f"q-catalog-{start + i}",
            "query_text": "SELECT obj_description($1::regclass::oid, $2)",
            "query_type": "SELECT",
            "tables_accessed": ["unknown"],
            "frequency_per_hour": 36.0,
            "calls_per_second": 0.01,
        }
        for i in range(n)
    ]


def test_split_excludes_no_source_table_queries_instead_of_grouping_them(tmp_path) -> None:
    """#276/#369: catalog/utility queries with ``tables_accessed: ["unknown"]``
    must not form their own group with zero real tables, and must not be
    folded into a real table's group either (that just asks that table's
    design to invent access patterns for a catalog query it cannot serve).
    They are left out of every group and listed in the manifest instead.

    Not asserted: an exact ``total_groups`` count. #367's better clustering
    (table-name resolution before FK/co-dependency clustering) can legitimately
    change how many groups the fixture's own, non-catalog queries land in; what
    this test cares about is the three invariants #276 is actually about.
    """
    collector = get_ecommerce_collector_output()
    catalog_queries = _catalog_queries(6)
    queries = collector["queries"]["query_patterns"] + catalog_queries
    analysis = _analysis(collector)
    store = LocalArtifactStore(str(tmp_path))

    manifest = split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=analysis,
        queries=queries,
        store=store,
    )

    assert sum(g.query_count for g in manifest.groups) + len(manifest.excluded_queries) == len(
        queries
    )
    assert {e.query_id for e in manifest.excluded_queries} == {
        q["query_id"] for q in catalog_queries
    }
    assert all(e.reason == "not designed: no source table" for e in manifest.excluded_queries)
    # No group was named after, or contains, the pseudo table.
    assert all("unknown" not in g.primary_tables for g in manifest.groups)
    for g in manifest.groups:
        assert g.table_count > 0, f"group {g.group_name!r} has no tables"
        data = json.loads((tmp_path / "ecommerce/j1/schema-dynamodb/v1" / g.input_file).read_text())
        # Must validate against the full contract the Bedrock path checks.
        CollectorOutputContract.model_validate(data["collector_output"])


def test_split_excludes_catalog_queries_even_when_the_largest_group_is_full(
    tmp_path,
) -> None:
    """#367 review finding 1 (critical): folding tableless queries into the
    target group used to re-chunk it once the merge pushed it over
    MAX_GROUP_SIZE, so a chunk after the first held only catalog queries --
    a zero-table group again, just one layer removed. Excluding catalog
    queries before grouping at all means the real group's size is never
    inflated by them in the first place, so this can't happen regardless of
    how full the real group already is.
    """
    collector = get_ecommerce_collector_output()
    base = collector["queries"]["query_patterns"][0]
    real_queries = [
        {**base, "query_id": f"q-prod-{i}", "tables_accessed": [PRODUCTS]}
        for i in range(MAX_GROUP_SIZE)  # the largest group already has exactly 20 queries
    ]
    catalog_queries = _catalog_queries(6)
    queries = real_queries + catalog_queries
    analysis = _analysis(collector)
    store = LocalArtifactStore(str(tmp_path))

    manifest = split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=analysis,
        queries=queries,
        store=store,
    )

    assert len(manifest.excluded_queries) == 6
    assert {e.query_id for e in manifest.excluded_queries} == {
        q["query_id"] for q in catalog_queries
    }
    for g in manifest.groups:
        assert g.table_count > 0, f"group {g.group_name!r} has no tables"
        assert g.query_count <= MAX_GROUP_SIZE
        data = json.loads((tmp_path / "ecommerce/j1/schema-dynamodb/v1" / g.input_file).read_text())
        CollectorOutputContract.model_validate(data["collector_output"])
    # The real queries were never inflated by the catalog queries riding along.
    assert sum(g.query_count for g in manifest.groups) == MAX_GROUP_SIZE


def test_split_leaves_a_genuine_resolution_failure_in_its_own_group(tmp_path) -> None:
    """#367 review finding 369-2: "no matching table" must not fold a real,
    just-unresolved table name together with catalog queries. A query naming
    a real table the collector's schema does not have (a spelling mismatch,
    not a pseudo table) stays in normal grouping -- visible if it still
    can't be designed, rather than silently merged away with the catalog
    queries. (This case is a genuine resolution failure that #367's resolver
    would fix at the source; left loud here rather than papered over.)
    """
    collector = get_ecommerce_collector_output()
    mismatched_queries = [
        {
            "query_id": f"q-mismatch-{i}",
            "query_text": "SELECT * FROM wp_users",
            "query_type": "SELECT",
            "tables_accessed": ["wp_users"],  # not a pseudo table, but unknown to the collector
            "frequency_per_hour": 10.0,
            "calls_per_second": 1.0,
        }
        for i in range(6)
    ]
    queries = collector["queries"]["query_patterns"] + mismatched_queries
    analysis = _analysis(collector)
    store = LocalArtifactStore(str(tmp_path))

    manifest = split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=analysis,
        queries=queries,
        store=store,
    )

    # Not excluded: these queries name a real (if unresolved) table, not a
    # pseudo one, so they are not "no source table" and are not silently
    # dropped from the manifest's accounting either way.
    assert manifest.excluded_queries == []
    mismatch_group = next(g for g in manifest.groups if g.group_name == "wp_users")
    assert mismatch_group.query_count == 6
    assert mismatch_group.table_count == 0  # genuine resolution failure stays visible


def test_split_then_merge_when_every_query_is_pseudo_only(tmp_path) -> None:
    """#276/#369 (reviewer suggestion 1), end to end: an engine assigned only
    catalog/utility queries has zero groups to split into, so --merge must
    write a skipped schema_output.json carrying excluded_queries instead of
    raising "No group drafts found" for an engine that was never broken."""
    from src.agents.schema_design.group_merger import merge_schema_groups

    collector = get_ecommerce_collector_output()
    catalog_queries = _catalog_queries(6)
    analysis = _analysis(collector)
    store = LocalArtifactStore(str(tmp_path))

    manifest = split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=analysis,
        queries=catalog_queries,  # only pseudo-table-only queries assigned
        store=store,
    )

    assert manifest.total_groups == 0
    assert len(manifest.excluded_queries) == 6

    merged = merge_schema_groups("j1", "ecommerce", "dynamodb", store)

    assert merged["status"] == "skipped"
    assert len(merged["excluded_queries"]) == 6
