"""The fields contract 1.2 adds to the ``assess()`` result (#1603), each checked against
the artifact it is read from, never against a recount of its own:

- ``statements[].query_text`` and ``query_tags``: the collector output's;
- ``statements[].matched_anti_patterns``: the findings routing recorded next to the
  assignment (``access_patterns.json``), labelled as it labelled them;
- ``statements[].fit_breakdown`` on a placed result: past a gate routing lifted, and
  with the ladder's gate otherwise;
- ``summary.table_facts``: the collector output's tables, at every phase;
- ``summary.consolidations[].action`` and ``queries_retained``, and
  ``summary.engines[].unique_query_ids``: the reality check's;
- the DynamoDB design entry's ``aggregate_pattern`` and ``entities``: the design's.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.assessment.models import DynamoDBDesign
from src.assessment.result_builder import (
    RunState,
    _Scorer,
    build_consolidations,
    build_designs,
    build_engine_lines,
    build_table_facts,
)
from src.shared.catalog.patterns import penalty_pattern
from src.storage.assignment_versioning import resolve_effective_assignment_version
from tests.unit.assessment.conftest import PLACED_JOB, SAMPLES, assessed, placed_run


@pytest.fixture(params=SAMPLES)
def placed(request: pytest.FixtureRequest) -> tuple[str, Any, Any, Any]:
    result, state, store = placed_run(request.param)
    return request.param, result, state, store


# --------------------------------------------------------------------------- statements


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
@pytest.mark.parametrize("sample", SAMPLES)
def test_query_text_and_tags_are_the_collectors(sample: str, run_through: str) -> None:
    _, state, _ = placed_run(sample)
    by_id = {q["query_id"]: q for q in state.query_patterns}
    result = assessed(sample, run_through)
    for s in result.statements:
        assert s.query_text == by_id[s.query_id]["query_text"], s.query_id
        assert s.query_text
        assert s.query_tags == by_id[s.query_id]["query_tags"], s.query_id
    if sample == "discourse":
        # The PostgreSQL sample's leading comments reach the result.
        assert any(s.query_tags for s in result.statements)


def test_matched_anti_patterns_are_the_ones_routing_recorded(placed: Any) -> None:
    name, result, _, store = placed
    version = resolve_effective_assignment_version(store, name, PLACED_JOB)
    record = store.read_json(f"{name}/{PLACED_JOB}/assignment/v{version}/access_patterns.json")
    recorded: dict[str, dict[str, bool]] = {}
    for row in record["queries"]:
        for finding in row["anti_pattern_findings"]:
            entry = penalty_pattern(finding["pattern"])
            assert entry is not None, finding
            recorded.setdefault(row["query_id"], {})[entry.id] = finding["kind"] == "intrinsic"
    found = 0
    for s in result.statements:
        ours = {p.id: p.intrinsic for p in s.matched_anti_patterns}
        assert ours == recorded.get(s.query_id, {}), s.query_id
        assert [p.id for p in s.matched_anti_patterns] == sorted(ours)
        for p in s.matched_anti_patterns:
            entry = penalty_pattern(p.id)
            assert entry is not None and p.name == entry.name
        found += bool(ours)
    assert found


def test_an_engine_routing_scored_past_a_gate_keeps_its_score(placed: Any) -> None:
    """A capability waiver lifts a gate the plain scorer applies; the breakdown of such
    an engine is the arithmetic past the gate, with the ladder's score."""
    from src.agents.referee.query_scoring import score_query_for_engine

    name, result, state, _ = placed
    scorer = _Scorer(state)
    lifted = 0
    for s in result.statements:
        query = scorer.queries[s.query_id]
        for engine, score in s.fit.items():
            if score_query_for_engine(query, engine, scorer.ctx).score is None:
                lifted += 1
                entry = s.fit_breakdown[engine]
                assert entry.gate is None and entry.fit == score, (s.query_id, engine)
    if name in ("wordpress", "discourse"):
        assert lifted


def test_a_gated_engine_on_the_ladder_keeps_the_ladders_gate(placed: Any) -> None:
    _, result, state, _ = placed
    scorer = _Scorer(state)
    s = next(s for s in result.statements if s.fit)
    engine = sorted(s.fit)[0]
    flagged = scorer.explain(s.query_id, {engine: "model_flag"}, {})[engine].gate
    assert flagged is not None and flagged.kind == "model_flag"
    assert flagged.reason.endswith("was flagged as unable to serve this query")
    other = scorer.explain(s.query_id, {engine: "write_gate"}, {})[engine].gate
    assert other is not None and other.kind == "write_gate"
    assert other.reason.endswith("was ruled out for this statement.")
    assert scorer.explain("no-such-statement", {engine: None}, {engine: 1}) == {}


# --------------------------------------------------------------------------- table facts


@pytest.mark.parametrize("sample", SAMPLES)
def test_table_facts_are_the_collected_tables_at_every_phase(sample: str) -> None:
    _, state, _ = placed_run(sample)
    tables = {t["table_id"]: t for t in state.collector["database_schema"]["tables"]}
    analysis = assessed(sample, "analysis").summary.table_facts
    assert analysis == assessed(sample, "reality_check").summary.table_facts
    assert [f.table_id for f in analysis] == sorted(tables)
    ids = set(tables)
    fks = 0
    for facts in analysis:
        t = tables[facts.table_id]
        assert (facts.row_count, facts.size_mb) == (t["row_count"], t.get("size_mb"))
        assert facts.primary_key == list(t.get("primary_key") or [])
        positions = [c.get("ordinal_position") for c in t["columns"]]
        assert sorted(c.name for c in facts.columns) == sorted(
            c["column_name"] for c in t["columns"]
        )
        by_name = {c["column_name"]: c for c in t["columns"]}
        assert [by_name[c.name].get("ordinal_position") for c in facts.columns] == sorted(
            positions, key=lambda p: (p is None, p or 0)
        )
        for c in facts.columns:
            assert (c.data_type, c.nullable) == (
                by_name[c.name]["data_type"],
                by_name[c.name]["nullable"],
            )
        assert [fk.constraint_name for fk in facts.foreign_keys] == [
            fk["constraint_name"] for fk in t.get("foreign_keys") or []
        ]
        for fk, raw in zip(facts.foreign_keys, t.get("foreign_keys") or [], strict=True):
            assert (fk.columns, fk.referenced_columns) == (
                raw["columns"],
                raw["referenced_columns"],
            )
            assert fk.referenced_table in ids
            assert fk.referenced_table.rsplit(".", 1)[-1] == raw["referenced_table"]
            fks += 1
    if sample != "wordpress":
        assert fks


def _state(**collector: Any) -> RunState:
    return RunState(run_through="analysis", collector=collector, triage={})


def test_a_foreign_key_resolves_to_one_table_id_or_none() -> None:
    def table(table_id: str, schema: str, fks: list[dict[str, Any]] = ()) -> dict[str, Any]:  # type: ignore[assignment]
        return {
            "table_id": table_id,
            "table_name": table_id.rsplit(".", 1)[-1],
            "schema_name": schema,
            "row_count": 1,
            "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
            "foreign_keys": list(fks),
        }

    def fk(ref: str) -> dict[str, Any]:
        return {
            "constraint_name": f"fk_{ref}",
            "columns": ["x"],
            "referenced_table": ref,
            "referenced_columns": ["id"],
        }

    tables = [
        table(
            "sales.Orders", "sales", [fk("Customers"), fk("Lines"), fk("Missing"), fk("hr.Lines")]
        ),
        table("sales.Customers", "sales"),
        table("hr.Customers", "hr"),
        table("hr.Lines", "hr"),
        table("ops.Lines", "ops"),
    ]
    (orders,) = [
        f
        for f in build_table_facts(_state(database_schema={"tables": tables}))
        if f.table_id == "sales.Orders"
    ]
    assert [f.referenced_table for f in orders.foreign_keys] == [
        "sales.Customers",  # the referencing table's schema first
        None,  # two schemas hold a Lines, neither is sales: ambiguous
        None,  # not collected
        "hr.Lines",  # already a table id
    ]
    assert build_table_facts(_state()) == []


# --------------------------------------------------------------------------- reality check


def test_unique_query_ids_are_the_reality_checks_first_pass_set(placed: Any) -> None:
    _, result, state, _ = placed
    unique = state.reality_check["unique_value_assessment"]
    for line in result.summary.engines:
        uva = unique.get(line.engine)
        if not isinstance(uva, dict):
            assert line.unique_query_ids is None and line.unique_queries is None
            continue
        assert line.unique_query_ids == uva["unique_queries"]
        assert line.unique_queries == len(line.unique_query_ids)


def test_a_partial_consolidation_names_the_statements_it_left() -> None:
    state = RunState(
        run_through="reality_check",
        collector={},
        triage={},
        reality_check={
            "consolidations": [
                {
                    "from_engine": "documentdb",
                    "to_engine": "dynamodb",
                    "query_count": 2,
                    "reason": "r",
                    "action": "partial",
                    "queries_retained": ["q3"],
                },
                # A record written before the field: the record's default, full.
                {"from_engine": "opensearch", "to_engine": "aurora_mysql", "query_count": 1},
            ]
        },
    )
    partial, legacy = build_consolidations(state)
    assert (partial.action, partial.queries_retained) == ("partial", ["q3"])
    assert (legacy.action, legacy.queries_retained) == ("full", [])
    assert build_engine_lines(state, {}, [partial, legacy], None) == []


# --------------------------------------------------------------------------- DynamoDB


def _key(name: str) -> dict[str, str]:
    return {"attribute_name": name, "attribute_type": "S"}


def test_the_dynamodb_entry_copies_the_aggregate_pattern_and_entities() -> None:
    design = {
        "validation_passed": True,
        "table_definitions": [
            {
                "table_name": "Shop",
                "aggregate_pattern": "item_collection",
                "source_tables": ["shop.users", "shop.orders"],
                "partition_key": _key("PK"),
                "sort_key": _key("SK"),
                "entities": [
                    {
                        "entity_type": "USER",
                        "source_table": "shop.users",
                        "pk_template": "USER#{id}",
                        "sk_template": "PROFILE",
                    },
                    {
                        "entity_type": "ORDER",
                        "source_table": "shop.orders",
                        "pk_template": "USER#{user_id}",
                        "sk_template": "ORDER#{id}",
                    },
                    {"entity_type": "X", "pk_template": "X#{id}", "sk_template": "X"},
                ],
            },
            {"table_name": "Logs", "aggregate_pattern": "separate", "partition_key": _key("id")},
            {"table_name": "Old", "partition_key": _key("id")},
            {
                "table_name": "Odd",
                "aggregate_pattern": "something_new",
                "partition_key": _key("id"),
            },
        ],
    }
    state = RunState(
        run_through="schema_design", collector={}, triage={}, schema_designs={"dynamodb": design}
    )
    (entry,) = build_designs(state, ["dynamodb"])
    assert isinstance(entry, DynamoDBDesign)
    shop, logs, old, odd = entry.tables
    assert shop.aggregate_pattern == "item_collection"
    assert [(e.source_table, e.pk_template, e.sk_template) for e in shop.entities] == [
        ("shop.users", "USER#{id}", "PROFILE"),
        ("shop.orders", "USER#{user_id}", "ORDER#{id}"),
    ]
    assert (logs.aggregate_pattern, logs.entities) == ("separate", [])
    # An older design, or a value the contract does not name: not stated.
    assert old.aggregate_pattern is None and odd.aggregate_pattern is None
