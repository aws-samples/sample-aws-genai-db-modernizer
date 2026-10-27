"""DynamoDB schema design works per access pattern, not per statement (#1602).

``--split`` builds DynamoDB's design parts from query patterns (#1146): a part holds at
most ``MAX_PATTERNS_PER_PART`` patterns, its input lists one entry per pattern (a
representative statement with the pattern's summed traffic) and every member's query
id, and everything keyed by query id still reads every member. The design must still
name every member statement: ``design_metrics`` gives a pattern one row with its
members, the numbers check asks an access pattern serving one member to name them all,
``--merge`` adds a member no access pattern names to the one serving its pattern, and
the PE review request names every member.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.schema_design.group_merger import merge_schema_groups
from src.agents.schema_design.group_splitter import MAX_PATTERNS_PER_PART, split_schema_input
from src.agents.schema_design.pe_review_external import (
    prepare_pe_review_llm_input,
    request_key,
)
from src.agents.schema_design.scope import covered_query_ids
from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.schema_design_input import (
    SchemaDesignGroupsManifest,
    project_schema_design_input,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.design_metrics import (
    check_design_numbers,
    design_metrics_from_projection,
    design_rps,
)
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.unit.agents.schema_design.valid_designs import (
    ddb_attr,
    ddb_design,
    ddb_pattern,
    ddb_table,
)

PRODUCTS = "ecommerce.products"
BASE = "ecommerce/j1/schema-dynamodb/v1"


def _q(qid: str, text: str, cps: float, **extra: object) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "frequency_per_hour": cps * 3600,
        "calls_per_second": cps,
        "tables_accessed": [PRODUCTS],
        **extra,
    }


def _members(pattern: int, n: int, cps: float = 1.0) -> list[dict]:
    """``n`` statements of one access pattern: one filter column of the pattern's own,
    different IN-list lengths and returned columns."""
    return [
        _q(
            f"p{pattern}m{i}",
            f"SELECT name{', price' * (i % 2)} FROM products "
            f"WHERE c{pattern} IN ({', '.join(['?'] * (i + 1))})",
            cps,
        )
        for i in range(n)
    ]


def _analysis(query_ids: list[str]) -> dict:
    return {
        "contract_version": "2.1",
        "agent_metadata": {
            "agent_name": "dynamodb-analysis-agent",
            "agent_version": "1.0.0",
            "target_database": "dynamodb",
            "analysis_timestamp": "2026-10-27T00:00:00",
        },
        "table_recommendations": [
            {
                "table_id": PRODUCTS,
                "confidence_score": 80,
                "score_breakdown": {
                    "pattern_match_score": 1,
                    "complexity_score": 1,
                    "performance_score": 1,
                    "cost_score": 1,
                },
            }
        ],
        "workload_analysis": {
            "patterns_detected": [
                {
                    "pattern_id": "dynamodb-01",
                    "pattern_type": "key-value-lookup",
                    "confidence": "HIGH",
                    "query_ids": query_ids,
                    "table_ids": [PRODUCTS],
                }
            ]
        },
        "cost_estimate": {"monthly_cost_usd": 1.0, "cost_components": {}},
        "key_scoped_writes": [
            {
                "query_id": qid,
                "table_id": PRODUCTS,
                "columns": ["category_id"],
                "schema_note": f"note for {qid}",
            }
            for qid in query_ids
        ],
        "key_scoped_counts": [
            {
                "query_id": qid,
                "table_id": PRODUCTS,
                "columns": ["category_id"],
                "schema_note": f"count for {qid}",
            }
            for qid in query_ids
        ],
        "redesign_requirements": [{"query_id": qid} for qid in query_ids],
        "carried_writes": [
            {
                "query_id": qid,
                "table_id": PRODUCTS,
                "engine": "dynamodb",
                "score": 0.1,
                "floor": 0.5,
                "group_size": 2,
                "intent": "i",
                "intent_name": "n",
                "intent_summary": "s",
                "approach": "a",
            }
            for qid in query_ids
        ],
    }


def _split(tmp_path: Path, queries: list[dict]) -> tuple[LocalArtifactStore, dict, dict]:
    collector = get_ecommerce_collector_output()
    collector["queries"]["query_patterns"] = queries
    store = LocalArtifactStore(str(tmp_path))
    split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=_analysis([q["query_id"] for q in queries]),
        queries=queries,
        store=store,
    )
    manifest = store.read_json(f"{BASE}/groups_manifest.json")
    inputs = {
        g["group_index"]: store.read_json(f"{BASE}/{g['input_file']}") for g in manifest["groups"]
    }
    return store, manifest, inputs


def _entries(data: dict) -> list[dict]:
    return list(data["collector_output"]["queries"]["query_patterns"])


def _listing(data: dict) -> list[dict]:
    return data["collector_output"]["queries"].get("query_pattern_groups") or []


def _statements(data: dict) -> list[str]:
    members = {i: g["query_ids"] for g in _listing(data) for i in g["query_ids"]}
    return [m for q in _entries(data) for m in members.get(q["query_id"], [q["query_id"]])]


def test_a_pattern_with_many_members_is_one_entry(tmp_path: Path) -> None:
    queries = [*_members(1, 12), *_members(2, 1), *_members(3, 1)]
    _, manifest, inputs = _split(tmp_path, queries)
    [data] = inputs.values()
    entries = _entries(data)
    assert len(entries) == 3
    [listed] = _listing(data)
    assert listed["query_ids"] == [f"p1m{i}" for i in range(12)]
    pattern = [e for e in entries if e.get("query_pattern_id")]
    assert [e["query_pattern_id"] for e in pattern] == [listed["pattern_id"]]
    assert pattern[0]["query_id"] in listed["query_ids"]
    assert listed["returned_columns"] == ["name", "price"]
    # Every member is still read by what is keyed by query id.
    writes = {w["query_id"] for w in data["analysis_output"]["key_scoped_writes"]}
    assert writes == {q["query_id"] for q in queries}
    [group] = manifest["groups"]
    assert (group["query_count"], group["pattern_count"]) == (14, 3)


def test_a_patterns_traffic_is_summed(tmp_path: Path) -> None:
    rates = [0.5, 2.0, 1.25]
    queries = [
        {**q, "calls_per_second": r, "frequency_per_hour": r * 3600, **extra}
        for q, r, extra in zip(
            _members(1, 3),
            rates,
            [
                {"execution_time_ms_p95": 4.0, "total_time_ms": 10.0, "rows_returned_avg": 1.0},
                {"execution_time_ms_p95": 9.0, "total_time_ms": 20.0, "rows_returned_avg": 4.0},
                {"execution_time_ms_p95": 2.0, "total_time_ms": 5.0, "rows_returned_avg": 2.0},
            ],
            strict=True,
        )
    ]
    # A member with no calls_per_second counts its frequency_per_hour / 3600.
    queries.append({**_members(1, 4)[3], "calls_per_second": None, "frequency_per_hour": 1800.0})
    _, _, inputs = _split(tmp_path, queries)
    [data] = inputs.values()
    [entry] = _entries(data)
    total = sum(rates) + 0.5
    assert entry["query_id"] == "p1m1"  # the busiest member stands for the pattern
    assert entry["calls_per_second"] == total
    assert entry["frequency_per_hour"] == sum(rates) * 3600 + 1800.0
    assert entry["execution_time_ms_p95"] == 9.0
    assert entry["total_time_ms"] == 35.0
    weighted = (1.0 * 0.5 + 4.0 * 2.0 + 2.0 * 1.25) / sum(rates)
    assert entry["rows_returned_avg"] == round(weighted, 6)
    [row] = data["design_metrics"]["queries"]
    assert row["query_id"] == "p1m1"
    assert row["design_rps"] == design_rps(total, None)
    assert row["member_query_ids"] == [q["query_id"] for q in queries]


def test_parts_are_capped_by_patterns(tmp_path: Path) -> None:
    queries = [q for p in range(30) for q in _members(p, 3)]
    _, manifest, inputs = _split(tmp_path, queries)
    assert [g["pattern_count"] for g in manifest["groups"]] == [20, 10]
    assert [g["query_count"] for g in manifest["groups"]] == [60, 30]
    for g in manifest["groups"]:
        assert (
            len(_entries(inputs[g["group_index"]])) == g["pattern_count"] <= MAX_PATTERNS_PER_PART
        )
    placed = [qid for data in inputs.values() for qid in _statements(data)]
    assert sorted(placed) == sorted(q["query_id"] for q in queries)


def test_a_pattern_of_more_statements_than_the_cap_stays_one_entry(tmp_path: Path) -> None:
    _, manifest, inputs = _split(tmp_path, _members(1, MAX_PATTERNS_PER_PART + 5))
    assert [(g["pattern_count"], g["query_count"]) for g in manifest["groups"]] == [
        (1, MAX_PATTERNS_PER_PART + 5)
    ]
    assert len(_entries(inputs[0])) == 1


def test_a_pattern_too_large_for_a_part_is_split_deterministically(tmp_path: Path) -> None:
    """One pattern whose members alone are over a part's input budget (long query ids,
    each named in the listing and in the key-scoped writes) is cut between its members,
    first half first; two runs write the same parts."""
    queries = [{**q, "query_id": f"{q['query_id']}-{'x' * 600}"} for q in _members(1, 300)]
    first = _split(tmp_path / "a", queries)
    second = _split(tmp_path / "b", queries)
    _, manifest, inputs = first
    assert manifest == second[1] and inputs == second[2]
    assert len(manifest["groups"]) > 1
    assert all(g["pattern_count"] == 1 for g in manifest["groups"])
    parts = [_statements(inputs[g["group_index"]]) for g in manifest["groups"]]
    assert [qid for part in parts for qid in part] == [q["query_id"] for q in queries]


def _draft_for(data: dict, members_too: bool) -> dict:
    """One access pattern per input entry; ``members_too`` lists its pattern's members."""
    members = {i: g["query_ids"] for g in _listing(data) for i in g["query_ids"]}
    table = ddb_table(
        "Products", PRODUCTS, pk="product_id", attributes=[ddb_attr("product_id", PRODUCTS)]
    )
    patterns = []
    for n, entry in enumerate(_entries(data), start=1):
        ap = ddb_pattern(f"DDB-AP-{n}", "Products", PRODUCTS, entry["query_id"])
        if members_too:
            ap["query_ids"] = members.get(entry["query_id"], [entry["query_id"]])
        patterns.append(ap)
    return ddb_design([table], patterns)


def test_member_ids_survive_the_merge(tmp_path: Path) -> None:
    """A draft naming every member keeps them all; a draft naming only the entry gets
    its pattern's other members added by --merge, with a note in the merge trace."""
    queries = [q for p in range(24) for q in _members(p, 3)]
    store, manifest, inputs = _split(tmp_path, queries)
    for n, data in inputs.items():
        store.write_json(
            f"{BASE}/schema_draft_group_{n}.json", _draft_for(data, members_too=n == 0)
        )
    merged = merge_schema_groups("j1", "ecommerce", "dynamodb", store, schema_version=1)
    assert covered_query_ids("dynamodb", merged) == {q["query_id"] for q in queries}
    for ap in merged["access_patterns"]:
        assert len(ap["query_ids"]) == 3, ap["pattern_id"]
    trace = store.read_json(f"{BASE}/design_trace.json")
    added = [n for n in trace["merge_reconciliation"] if "member statement" in n]
    assert len(added) == len(_entries(inputs[1]))


@pytest.mark.passes_on_main  # main's input lists every statement; per-pattern input must keep it
def test_the_pe_review_names_every_member(tmp_path: Path) -> None:
    queries = [q for p in range(3) for q in _members(p, 4)]
    store, _, inputs = _split(tmp_path, queries)
    store.write_json(f"{BASE}/schema_draft_group_0.json", _draft_for(inputs[0], members_too=True))
    prepared = prepare_pe_review_llm_input(store, "ecommerce", "j1", 1, engines=["dynamodb"])
    assert [r["group"] for r in prepared["reviews"]] == [0]
    request = store.read_json(request_key("ecommerce", "j1", "dynamodb", 0))
    assert request["query_ids"] == sorted(q["query_id"] for q in queries)
    assert request["source_summary"]["query_count"] == len(queries)


def _metrics(rows: list[dict]) -> dict:
    return {"engine": "dynamodb", "queries": rows}


def test_an_access_pattern_serving_a_pattern_names_every_member() -> None:
    row = {
        "query_id": "a",
        "base_rps": 3.0,
        "design_rps": 90.0,
        "member_query_ids": ["a", "b", "c"],
    }
    other = {"query_id": "d", "base_rps": 1.0, "design_rps": 30.0}
    design = {
        "access_patterns": [
            {"pattern_id": "DDB-AP-1", "query_ids": ["a"], "design_rps": 90.0},
            {"pattern_id": "DDB-AP-2", "query_ids": ["a", "b", "c", "d"], "design_rps": 120.0},
        ],
        "hot_partition_analysis": [],
    }
    errors = check_design_numbers("dynamodb", design, _metrics([row, other]))
    assert len(errors) == 1
    assert errors[0].startswith("access pattern DDB-AP-1: serves the query pattern of a")
    assert "b, c" in errors[0]
    # The pattern's rate is counted once, however many members are listed.
    wrong = {
        **design,
        "access_patterns": [{"pattern_id": "X", "query_ids": ["a", "b", "c"], "design_rps": 270.0}],
    }
    assert any(
        "design_rps 270" in e for e in check_design_numbers("dynamodb", wrong, _metrics([row]))
    )


def test_the_bedrock_projection_reads_a_per_pattern_input(tmp_path: Path) -> None:
    queries = [*_members(1, 5), *_members(2, 1)]
    _, _, inputs = _split(tmp_path, queries)
    [data] = inputs.values()
    collector = CollectorOutputContract.model_validate(data["collector_output"])
    analysis = AnalysisOutputContract.model_validate(data["analysis_output"])
    agent_collector, agent_analysis, _ = project_schema_design_input(
        collector, analysis, query_pattern_groups=_listing(data)
    )
    queries_in = agent_collector.queries
    [group] = queries_in.query_pattern_groups or []
    assert group.query_ids == [f"p1m{i}" for i in range(5)]
    tagged = [q for q in queries_in.query_patterns if q.query_pattern_id]
    assert [q.query_pattern_id for q in tagged] == [group.pattern_id]
    every = {q["query_id"] for q in queries}
    assert {w.query_id for w in agent_analysis.key_scoped_writes or []} == every
    assert {c.query_id for c in agent_analysis.key_scoped_counts or []} == every
    assert {r.query_id for r in agent_analysis.redesign_requirements or []} == every
    assert {c.query_id for c in agent_analysis.carried_writes or []} == every
    projected = {"collector": agent_collector.model_dump(mode="json"), "context": {}}
    rows = design_metrics_from_projection("dynamodb", projected)["queries"]
    assert [r.get("member_query_ids") for r in rows if r["query_id"] == tagged[0].query_id] == [
        group.query_ids
    ]


def test_an_old_manifest_and_input_still_load(tmp_path: Path) -> None:
    """A manifest written before #1602 (1.3, no pattern_count) loads; its input lists
    every statement, so the projection keeps its pattern listing, the metrics have one
    row per statement, and --merge adds nothing."""
    old = {
        "contract_version": "1.3",
        "job_id": "j1",
        "database_name": "ecommerce",
        "target_engine": "dynamodb",
        "total_queries": 3,
        "total_groups": 1,
        "groups": [
            {
                "group_index": 0,
                "group_name": "products",
                "primary_tables": [PRODUCTS],
                "context_tables": [],
                "query_count": 3,
                "table_count": 1,
                "input_file": "input_group_0.json",
                "input_pages": [{"offset": 1, "limit": 10}],
            }
        ],
    }
    loaded = SchemaDesignGroupsManifest.model_validate(old)
    assert loaded.groups[0].pattern_count is None
    queries = _members(1, 3)
    listing = [
        {
            "pattern_id": "PAT-1",
            "query_ids": [q["query_id"] for q in queries],
            "frequency_per_hour": 3 * 3600.0,
            "calls_per_second": 3.0,
            "operation": "select",
            "tables": [PRODUCTS],
            "returned_columns": ["name", "price"],
        }
    ]
    collector = get_ecommerce_collector_output()
    collector["queries"]["query_patterns"] = queries
    agent_collector, _, _ = project_schema_design_input(
        CollectorOutputContract.model_validate(collector),
        AnalysisOutputContract.model_validate(_analysis([q["query_id"] for q in queries])),
        query_pattern_groups=listing,
    )
    assert [q.query_pattern_id for q in agent_collector.queries.query_patterns] == [None] * 3
    rows = design_metrics_from_projection(
        "dynamodb", {"collector": agent_collector.model_dump(mode="json"), "context": {}}
    )["queries"]
    assert len(rows) == 3 and not any("member_query_ids" in r for r in rows)
    store = LocalArtifactStore(str(tmp_path))
    store.write_json(f"{BASE}/groups_manifest.json", old)
    store.write_json(
        f"{BASE}/input_group_0.json",
        {
            "collector_output": {
                "queries": {"query_patterns": queries, "query_pattern_groups": listing}
            }
        },
    )
    table = ddb_table(
        "Products", PRODUCTS, pk="product_id", attributes=[ddb_attr("product_id", PRODUCTS)]
    )
    draft = ddb_design(
        [table],
        [
            ddb_pattern(f"DDB-AP-{n}", "Products", PRODUCTS, q["query_id"])
            for n, q in enumerate(queries, 1)
        ],
    )
    store.write_json(f"{BASE}/schema_draft_group_0.json", draft)
    merged = merge_schema_groups("j1", "ecommerce", "dynamodb", store, schema_version=1)
    assert [ap["query_ids"] for ap in merged["access_patterns"]] == [
        [q["query_id"]] for q in queries
    ]


def test_the_split_status_counts_patterns_and_statements(tmp_path: Path) -> None:
    from scripts.run_schema_design import _group_entries

    queries = [q for p in range(30) for q in _members(p, 3)]
    store, manifest, _ = _split(tmp_path, queries)
    entries = _group_entries(store, BASE, manifest)
    assert [(e["query_count"], e["pattern_count"]) for e in entries] == [(60, 20), (30, 10)]


def test_table_ownership_still_counts_statements(tmp_path: Path) -> None:
    """products' 23 patterns need two parts. The first holds 20 one-statement patterns,
    the second 2 more and one pattern of 30 statements: the second has more of
    products' statements (32 to 20), so it owns the table (#1243's rule), though the
    first has more of its patterns."""
    singles = [q for p in range(22) for q in _members(p, 1)]
    queries = [*singles, *_members(99, 30)]
    _, manifest, _ = _split(tmp_path, queries)
    assert [(g["pattern_count"], g["query_count"]) for g in manifest["groups"]] == [
        (20, 20),
        (3, 32),
    ]
    assert [g["primary_tables"] for g in manifest["groups"]] == [[], [PRODUCTS]]


ANALYTICS = "ecommerce.daily_analytics"


@pytest.mark.passes_on_main  # main clusters from every statement; per-pattern units must keep it
def test_a_co_dependency_group_naming_a_later_member_still_clusters(tmp_path: Path) -> None:
    """A co-dependency group names the second member of a products pattern and a
    daily_analytics query (no foreign key links the tables): the two tables are designed
    in one part, as when every statement was an entry."""
    products = [q for p in range(5) for q in _members(p, 2)]
    analytics = [
        {
            **_q(f"a{i}", f"SELECT total_orders FROM daily_analytics WHERE c{i} = ?", 1.0),
            "tables_accessed": [ANALYTICS],
        }
        for i in range(5)
    ]
    queries = [*products, *analytics]
    collector = get_ecommerce_collector_output()
    collector["queries"]["query_patterns"] = queries
    store = LocalArtifactStore(str(tmp_path))
    manifest = split_schema_input(
        job_id="j1",
        database_name="ecommerce",
        engine="dynamodb",
        collector_output=collector,
        analysis_output=_analysis([q["query_id"] for q in queries]),
        queries=queries,
        store=store,
        co_dependency_groups=[["p0m1", "a0"]],
    )
    assert [sorted(g.primary_tables) for g in manifest.groups] == [sorted([PRODUCTS, ANALYTICS])]
