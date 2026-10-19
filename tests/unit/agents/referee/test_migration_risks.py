"""#950: migration risks raised from the facts synthesis already computes.

One positive and one negative case per rule in ``migration_risks.RULES`` (and
per severity of the rules with more than one), the id numbering, the wave-gate
strategy lines and the plain wording.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

import src.agents.referee.migration_risks as migration_risks
from src.agents.referee.migration_risks import (
    GATE_IDS_NAMED,
    HOT_PARTITION_HIGH_PCT,
    PENDING_FACT_HOOKS,
    RULES,
    add_migration_risks,
    build_migration_risks,
    last_risk_number,
    wave_gate_strategies,
)
from src.shared.prose_style import find_style_hits
from src.shared.report_facts import CLOSE_CALL_MARGIN

AURORA = "aurora_mysql"
DDB = "dynamodb"
DOC = "documentdb"
CACHE = "elasticache"

WAVE_1 = {
    "wave": 1,
    "engines": [AURORA],
    "homogeneity": "homogeneous",
    "moves_from": ["mysql"],
    "tables": ["db.a", "db.b", "db.c", "db.d"],
    "table_count": 4,
}
WAVE_2 = {"wave": 2, "engines": [CACHE], "moves_from": [], "tables": [], "table_count": 0}


def _wave_3(owners: list[dict[str, str]], tables: list[str], engines: list[str] | None = None):
    return {
        "wave": 3,
        "engines": engines or [DDB],
        "moves_from": [AURORA],
        "tables": tables,
        "table_count": len(tables),
        "table_owners": owners,
    }


WAVES = [WAVE_1, WAVE_2, _wave_3([], [])]


def _qa(qid: str, engine: str, tables: list[str], **extra: Any) -> dict[str, Any]:
    return {"query_id": qid, "assigned_engine": engine, "source_tables": tables, **extra}


def _q(qid: str, query_type: str, tables: list[str]) -> dict[str, Any]:
    return {"query_id": qid, "query_type": query_type, "tables_accessed": tables}


def _build(**kw: Any) -> list[dict[str, Any]]:
    kw.setdefault("migration_waves", [WAVE_1, WAVE_2])
    kw.setdefault("query_assignments", [])
    kw.setdefault("source_queries", [])
    kw.setdefault("source_engine", "mysql")
    return build_migration_risks(**kw)


def _rules(risks: list[dict[str, Any]]) -> list[str]:
    return [r["rule"] for r in risks]


def _one(risks: list[dict[str, Any]], rule: str) -> dict[str, Any]:
    found = [r for r in risks if r["rule"] == rule]
    assert len(found) == 1, (rule, _rules(risks))
    return found[0]


# ---------------------------------------------------------------------------
# Tables kept in sync: one risk per wave
# ---------------------------------------------------------------------------


def _source(table: str, source: str | None, replicas: dict[str, list[str]], writers=None):
    return {
        "table": table,
        "source_of_truth": source,
        "write_engines": writers or ([source] if source else []),
        "write_query_ids": [f"w-{table}"],
        "replicas": [{"engine": e, "read_query_ids": q} for e, q in replicas.items()],
    }


def test_replicated_tables_are_one_medium_sync_risk_for_their_wave() -> None:
    risks = _build(
        migration_waves=[WAVE_1, WAVE_2, _wave_3([], ["db.a", "db.b"], [DDB, DOC])],
        table_sources=[
            _source("db.a", DDB, {AURORA: ["r1", "r2"]}),
            _source("db.b", DOC, {DDB: ["r3"]}),
            _source("db.c", AURORA, {}),
        ],
    )
    risk = _one(risks, "sync.wave")
    assert risk["severity"] == "MEDIUM"
    assert risk["affected_tables"] == ["db.a", "db.b"]
    assert risk["query_ids"] == ["r1", "r2", "r3"]
    # The later of the two engines' waves starts the replication.
    assert risk["wave"] == 3
    # A merged wave's risk is filed under no single engine.
    assert risk["description"] == (
        "Wave 3: 3 reads run on a copy of 2 tables that CDC keeps in sync from DocumentDB "
        "and DynamoDB across 2 engine pairs, so a read there can miss a recent write."
    )
    assert "sources of truth section" in risk["mitigation"]
    assert "Wave 3 gate" in risk["mitigation"]


def test_a_single_engine_table_raises_no_sync_risk() -> None:
    risks = _build(
        migration_waves=[WAVE_1, WAVE_2, _wave_3([], ["db.a"])],
        table_sources=[_source("db.a", DDB, {})],
    )
    assert risks == []


def test_a_table_written_on_several_engines_makes_the_wave_sync_risk_high() -> None:
    risks = _build(
        migration_waves=WAVES,
        table_sources=[
            _source("db.a", None, {}, writers=[AURORA, DDB]),
            _source("db.b", DDB, {AURORA: ["r1"]}),
        ],
    )
    risk = _one(risks, "sync.wave")
    assert risk["severity"] == "HIGH"
    assert risk["affected_tables"] == ["db.a", "db.b"]
    assert "w-db.a" in risk["query_ids"]
    assert risk["description"].startswith(
        "[dynamodb] Wave 3: 1 table takes writes on Aurora MySQL and DynamoDB"
    )


KEPT = [{"table": "db.c", "owner": AURORA, "sync": "CDC"}]


def test_tables_no_captured_query_writes_are_a_low_one_time_copy() -> None:
    risks = _build(
        migration_waves=[WAVE_1, WAVE_2, _wave_3(KEPT, ["db.c"])],
        query_assignments=[_qa("r1", DDB, ["db.c"])],
        source_queries=[_q("r1", "SELECT", ["db.c"])],
        table_sources=[],
    )
    risk = _one(risks, "sync.wave")
    assert risk["severity"] == "LOW"
    assert risk["affected_tables"] == ["db.c"]
    assert risk["query_ids"] == ["r1"]
    assert "1 table is read here and written by no captured query" in risk["description"]
    assert "copy the read-only tables once" in risk["mitigation"]


def test_a_one_time_copy_folds_into_the_waves_cdc_risk() -> None:
    risks = _build(
        migration_waves=[WAVE_1, WAVE_2, _wave_3(KEPT, ["db.a", "db.c"])],
        query_assignments=[_qa("r1", DDB, ["db.c"])],
        source_queries=[_q("r1", "SELECT", ["db.c"])],
        table_sources=[_source("db.a", DDB, {AURORA: ["r2"]})],
    )
    risk = _one(risks, "sync.wave")
    assert risk["severity"] == "MEDIUM"
    assert risk["affected_tables"] == ["db.a", "db.c"]
    assert "1 more table is read here and written by no captured query" in risk["description"]


def test_an_older_assignment_reads_the_waves_kept_tables() -> None:
    """No ``table_sources`` (written before write groups): the wave's kept tables
    are CDC copies when a captured query writes them, a one-time copy otherwise."""
    owners = [*KEPT, {"table": "db.a", "owner": AURORA, "sync": "CDC"}]
    wave = _wave_3(owners, ["db.a", "db.c"])
    written = _build(
        migration_waves=[WAVE_1, WAVE_2, wave],
        query_assignments=[_qa("w1", AURORA, ["db.a"]), _qa("r1", DDB, ["db.a", "db.c"])],
        source_queries=[_q("w1", "UPDATE", ["db.a"]), _q("r1", "SELECT", ["db.a", "db.c"])],
    )
    risk = _one(written, "sync.wave")
    assert risk["severity"] == "MEDIUM"
    assert "1 table the wave uses stays owned outside it" in risk["description"]
    assert "1 more table is read here" in risk["description"]
    read_only = _build(
        migration_waves=[WAVE_1, WAVE_2, wave],
        query_assignments=[_qa("r1", DDB, ["db.a", "db.c"])],
        source_queries=[_q("r1", "SELECT", ["db.a", "db.c"])],
    )
    assert _one(read_only, "sync.wave")["severity"] == "LOW"


def test_wave_1_and_the_cache_wave_raise_no_sync_risk() -> None:
    cache_wave = {**WAVE_2, "table_owners": KEPT, "tables": ["db.c"], "table_count": 1}
    risks = _build(
        migration_waves=[{**WAVE_1, "table_owners": KEPT}, cache_wave],
        query_assignments=[_qa("w1", AURORA, ["db.c"])],
        source_queries=[_q("w1", "UPDATE", ["db.c"])],
    )
    assert risks == []


def test_a_wave_with_no_number_is_left_out() -> None:
    """``Risk.wave`` is at least 1: a wave without a number names no gate."""
    unnumbered = {**_wave_3(KEPT, ["db.c"]), "wave": None}
    risks = _build(
        migration_waves=[WAVE_1, WAVE_2, unnumbered],
        query_assignments=[_qa("r1", DDB, ["db.c"])],
        source_queries=[_q("r1", "SELECT", ["db.c"])],
        query_traces={"close_calls": [_close("r1", DDB, AURORA, 1.0)]},
    )
    assert "sync.wave" not in _rules(risks)
    assert all(r["wave"] is None or r["wave"] >= 1 for r in risks)


# ---------------------------------------------------------------------------
# Engine choices
# ---------------------------------------------------------------------------


def _close(qid: str, engine: str, alternative: str, gap: float, settled: bool = False):
    return {
        "query_id": qid,
        "engine": engine,
        "alternative": alternative,
        "gap": gap,
        "basis": "query",
        "settled_tie": settled,
    }


def test_close_calls_are_one_risk_per_wave_medium_when_a_query_moves() -> None:
    risks = _build(
        migration_waves=WAVES,
        query_assignments=[_qa("q1", DDB, ["db.a"]), _qa("q2", AURORA, ["db.b"])],
        query_traces={
            "close_calls": [
                _close("q1", DDB, AURORA, 2.0),
                _close("q2", AURORA, DDB, 1.0),
                _close("q3", AURORA, DDB, 0.0, settled=True),
            ]
        },
    )
    risk = _one(risks, "routing.close_calls")
    assert risk["severity"] == "MEDIUM"
    assert risk["wave"] == 3
    assert risk["query_ids"] == ["q1", "q2", "q3"]
    assert risk["affected_tables"] == ["db.a", "db.b"]
    assert risk["description"] == (
        f"[dynamodb] Wave 3: 3 queries were decided by less than {CLOSE_CALL_MARGIN:g} points "
        "(1 exact tie a routing rule settled): 2 stay on Aurora MySQL over DynamoDB and 1 "
        "goes to DynamoDB over Aurora MySQL. A small change in the scores moves them."
    )


def test_close_calls_that_all_stay_on_the_source_compatible_engine_are_low() -> None:
    risks = _build(
        migration_waves=WAVES,
        query_traces={"close_calls": [_close("q1", AURORA, DDB, 1.0)]},
    )
    risk = _one(risks, "routing.close_calls")
    assert risk["severity"] == "LOW"
    # The choice matters when DynamoDB's wave would move it.
    assert risk["wave"] == 3
    assert "1 query was decided" in risk["description"]
    assert "Confirm the engine for this query" in risk["mitigation"]


def test_no_close_call_risk_without_close_calls() -> None:
    assert _build(migration_waves=WAVES, query_traces={"close_calls": []}) == []
    assert _build(migration_waves=WAVES, query_traces=None) == []


def test_an_open_tie_is_a_pending_choice_medium() -> None:
    risks = _build(
        migration_waves=WAVES,
        tied_queries=[
            {"query_id": "t1", "assigned_engine": DDB, "tied_engines": [DOC, DDB]},
            {"query_id": "t2", "assigned_engine": DDB, "tied_engines": [DDB, DOC]},
        ],
    )
    risk = _one(risks, "routing.tie_open")
    assert risk["severity"] == "MEDIUM"
    assert risk["query_ids"] == ["t1", "t2"]
    assert "2 queries score exactly the same on DocumentDB and DynamoDB" in risk["description"]
    assert "Choose DocumentDB or DynamoDB for these queries" in risk["mitigation"]


def test_one_tied_query_reads_in_the_singular() -> None:
    risks = _build(
        migration_waves=WAVES,
        tied_queries=[{"query_id": "t1", "assigned_engine": DDB, "tied_engines": [DOC, DDB]}],
    )
    assert (
        "for this query in the assignment review" in _one(risks, "routing.tie_open")["mitigation"]
    )


def test_no_tie_risk_without_open_ties() -> None:
    assert _build(migration_waves=WAVES, tied_queries=[]) == []
    assert _build(migration_waves=WAVES, tied_queries=[{"query_id": "x", "tied_engines": []}]) == []


def test_a_query_no_engine_fits_is_high() -> None:
    risks = _build(
        migration_waves=WAVES,
        query_traces={"unservable": [{"query_id": "u1", "engine": AURORA, "kind": "fallback"}]},
    )
    risk = _one(risks, "routing.unservable")
    assert risk["severity"] == "HIGH"
    assert risk["query_ids"] == ["u1"]
    assert risk["description"].startswith("[aurora_mysql] 1 query fits no analyzed engine")


def test_no_unservable_risk_without_unservable_queries() -> None:
    assert _build(migration_waves=WAVES, query_traces={"unservable": []}) == []


# ---------------------------------------------------------------------------
# The cache layer
# ---------------------------------------------------------------------------


def test_dropped_cache_reads_are_medium() -> None:
    risks = _build(
        migration_waves=WAVES,
        query_assignments=[_qa("c1", AURORA, ["db.a"])],
        source_queries=[_q("c1", "SELECT", ["db.a"])],
        cache_overlay={"engine": CACHE, "dropped_query_ids": ["c1"]},
    )
    risk = _one(risks, "cache.dropped")
    assert risk["severity"] == "MEDIUM"
    assert risk["wave"] == 2
    assert risk["affected_tables"] == ["db.a"]
    assert "go to Aurora MySQL uncached" in risk["description"].replace("goes", "go")


def test_no_dropped_cache_risk_when_nothing_was_dropped() -> None:
    assert _build(migration_waves=WAVES, cache_overlay={"engine": CACHE}) == []


UNSUPPORTED: dict[str, Any] = {
    "source_query_ids": ["c2"],
    "reason": "A range read over a sorted set the design does not keep.",
    "workaround": "Serve the range from the owner",
}


def test_a_cache_layer_unsupported_pattern_is_medium() -> None:
    risks = _build(
        migration_waves=WAVES,
        query_assignments=[_qa("c2", AURORA, ["db.b"])],
        source_queries=[_q("c2", "SELECT", ["db.b"])],
        schema_designs={CACHE: {"unsupported_patterns": [UNSUPPORTED]}},
        cache_only_engines={CACHE},
    )
    risk = _one(risks, "cache.unsupported")
    assert risk["severity"] == "MEDIUM"
    assert risk["query_ids"] == ["c2"]
    assert risk["affected_tables"] == ["db.b"]
    assert risk["description"] == (
        "[elasticache] The cache design marks 1 pattern unsupported; each one's workaround "
        "is in the engineering report."
    )
    assert risk["details"] == [
        "unsupported pattern: A range read over a sorted set the design does not keep. "
        "Workaround: Serve the range from the owner"
    ]
    assert risk["mitigation"].startswith("Serve this pattern from its owner engine")


def test_an_engine_that_owns_queries_is_left_to_the_per_engine_loop() -> None:
    """Its unsupported patterns are already risks (build_risk_assessment)."""
    risks = _build(
        migration_waves=WAVES,
        schema_designs={CACHE: {"unsupported_patterns": [UNSUPPORTED]}},
        cache_only_engines=set(),
    )
    assert risks == []


# ---------------------------------------------------------------------------
# Schema designs
# ---------------------------------------------------------------------------


def test_an_app_layer_note_is_medium_and_names_its_object_and_table() -> None:
    risks = _build(
        migration_waves=WAVES,
        schema_designs={
            AURORA: {
                "app_layer_notes": [
                    {"feature": "trigger", "source_object": "t_after_insert", "recommendation": "x"}
                ]
            }
        },
        trigger_tables={"t_after_insert": "db.a"},
    )
    risk = _one(risks, "design.app_layer")
    assert risk["severity"] == "MEDIUM"
    assert risk["risk_type"] == "OPERATIONAL_RISK"
    assert risk["affected_tables"] == ["db.a"]
    assert risk["wave"] == 1
    assert risk["description"] == (
        "[aurora_mysql] 1 source object needs application code on Aurora MySQL "
        "(t_after_insert): 1 trigger. The schema carries it over without the database logic."
    )


def test_no_app_layer_risk_without_notes() -> None:
    assert _build(migration_waves=WAVES, schema_designs={AURORA: {"app_layer_notes": []}}) == []


def _hot(pct: float, at_risk: bool = True) -> dict[str, Any]:
    return {
        "table_name": "Orders",
        "operation": "write",
        "utilization_pct": pct,
        "at_risk": at_risk,
        "contributing_patterns": ["h1"],
        "mitigation": "Shard the key",
    }


DESIGN_TABLES = [{"table_name": "Orders", "source_tables": ["db.b"]}]


@pytest.mark.parametrize(
    ("pct", "severity"), [(85.0, "MEDIUM"), (HOT_PARTITION_HIGH_PCT, "HIGH"), (100.0, "HIGH")]
)
def test_a_hot_partition_is_medium_and_high_at_the_threshold(pct: float, severity: str) -> None:
    risks = _build(
        migration_waves=WAVES,
        schema_designs={
            DDB: {"hot_partition_analysis": [_hot(pct)], "table_definitions": DESIGN_TABLES}
        },
    )
    risk = _one(risks, "design.hot_partition")
    assert risk["severity"] == severity
    assert risk["affected_tables"] == ["db.b"]
    assert risk["query_ids"] == ["h1"]
    assert risk["wave"] == 3


def test_a_partition_not_at_risk_raises_nothing() -> None:
    designs = {DDB: {"hot_partition_analysis": [_hot(60.0, at_risk=False)]}}
    assert _build(migration_waves=WAVES, schema_designs=designs) == []


def test_a_model_written_object_name_stays_out_of_the_text() -> None:
    """Only a collected trigger is named: the executive report strips the
    collected names, never a name the model wrote."""
    risks = _build(
        migration_waves=WAVES,
        schema_designs={
            AURORA: {"app_layer_notes": [{"feature": "trigger", "source_object": "made_up"}]}
        },
        trigger_tables={},
    )
    risk = _one(risks, "design.app_layer")
    assert "made_up" not in risk["description"]
    assert risk["description"].startswith(
        "[aurora_mysql] 1 source object needs application code on Aurora MySQL: 1 trigger."
    )


# ---------------------------------------------------------------------------
# Design trade-offs: one review risk per engine, never a severity from the words
# ---------------------------------------------------------------------------

# The review's examples: each says the opposite of what a keyword match reads.
REVIEW_EXAMPLES = [
    "Order lines are split across two item collections by order id",
    "Wrapped in one TransactWriteItems call, so the write stays atomic across separate writes",
    "Strongly consistent reads, so no stale data is returned",
    "The partition key preserves uniqueness of the email address",
    "Keeps the source auto-increment ids unchanged",
]


@pytest.mark.parametrize("text", REVIEW_EXAMPLES)
def test_trade_off_words_never_raise_a_specific_or_high_risk(text: str) -> None:
    risks = _build(
        migration_waves=WAVES,
        trade_offs=[{"description": text, "engine": DDB, "query_ids": ["d1"]}],
    )
    assert _rules(risks) == ["design.trade_offs"]
    assert risks[0]["severity"] == "MEDIUM"
    assert risks[0]["details"] == [text.rstrip(".")]
    assert text not in risks[0]["description"]


def test_an_engines_trade_offs_are_one_review_risk_listing_them() -> None:
    risks = _build(
        migration_waves=WAVES,
        trade_offs=[
            {
                "description": "GSI reads are eventually consistent.",
                "engine": DDB,
                "query_ids": ["1"],
            },
            {"description": "Single table per entity", "engine": DDB, "source_tables": ["db.c"]},
            {"description": "GSI reads are eventually consistent.", "engine": DDB},
            {"description": "Cache-aside with a short TTL", "engine": CACHE},
        ],
    )
    assert _rules(risks) == ["design.trade_offs", "design.trade_offs"]
    ddb = next(r for r in risks if r["description"].startswith("[dynamodb]"))
    assert ddb["description"] == (
        "[dynamodb] The DynamoDB schema design records 2 trade-offs to review; each one is in "
        "the engineering report."
    )
    assert ddb["details"] == ["GSI reads are eventually consistent", "Single table per entity"]
    assert ddb["affected_tables"] == ["db.c"]
    assert ddb["query_ids"] == ["1"]
    assert ddb["wave"] == 3
    cache = next(r for r in risks if r["description"].startswith("[elasticache]"))
    assert cache["wave"] == 2 and "1 trade-off to review" in cache["description"]


def test_reality_check_entries_and_empty_titles_raise_no_trade_off_risk() -> None:
    routed = {"description": "Recommended pattern: CQRS.", "engine": "reality-check"}
    empty = {"description": "  ", "engine": DDB}
    assert _build(migration_waves=WAVES, trade_offs=[routed, empty]) == []


def test_a_long_trade_off_text_is_kept_whole_in_the_details() -> None:
    """No clipped quote: the text says the count, the details carry each text in full."""
    title = "word " * 40
    risks = _build(migration_waves=WAVES, trade_offs=[{"description": title, "engine": DDB}])
    assert risks[0]["details"] == [title.strip()]
    assert "…" not in risks[0]["description"] and "word" not in risks[0]["description"]


# ---------------------------------------------------------------------------
# Numbering, strategies, wording
# ---------------------------------------------------------------------------


def _all_kinds() -> list[dict[str, Any]]:
    wave = _wave_3(KEPT, ["db.a", "db.c"])
    return _build(
        migration_waves=[WAVE_1, WAVE_2, wave],
        query_assignments=[
            _qa("w1", DDB, ["db.a"]),
            _qa("r1", DDB, ["db.c"]),
            _qa("a1", AURORA, ["db.a"]),
        ],
        source_queries=[
            _q("w1", "UPDATE", ["db.a"]),
            _q("r1", "SELECT", ["db.c"]),
            _q("a1", "SELECT", ["db.a"]),
        ],
        table_sources=[_source("db.a", DDB, {AURORA: ["a1"]})],
        query_traces={
            "close_calls": [_close("a1", AURORA, DDB, 1.0), _close("w1", DDB, AURORA, 3.0)],
            "unservable": [{"query_id": "a1", "engine": AURORA}],
        },
        tied_queries=[{"query_id": "t1", "assigned_engine": DDB, "tied_engines": [DDB, DOC]}],
        cache_overlay={"engine": CACHE, "dropped_query_ids": ["a1"]},
        schema_designs={
            AURORA: {"app_layer_notes": [{"feature": "stored_procedure", "source_object": "p"}]},
            DDB: {"hot_partition_analysis": [_hot(90.0)], "table_definitions": DESIGN_TABLES},
            CACHE: {"unsupported_patterns": [UNSUPPORTED]},
        },
        trade_offs=[
            {"description": t, "engine": DDB, "query_ids": ["w1"]} for t in REVIEW_EXAMPLES
        ],
        cache_only_engines={CACHE},
        trigger_tables={"p": "db.a"},
        flagged_unservable=[
            {
                "query_id": "r1",
                "engine": DDB,
                "cause": "no_kept_engine",
                "reason_category": "full_scan",
            }
        ],
        relational_gaps={
            "target_engine": AURORA,
            "gaps": [
                {
                    "feature": "TOP row limits",
                    "impact": "needs_rewrite",
                    "reason": "TOP becomes LIMIT.",
                    "tables": ["db.a"],
                    "queries": ["a1"],
                }
            ],
        },
        start_id=7,
    )


def test_ids_continue_after_the_analysis_risks() -> None:
    risks = _all_kinds()
    assert [r["risk_id"] for r in risks] == [f"RISK-{i:03d}" for i in range(8, 8 + len(risks))]
    assert last_risk_number([{"risk_id": "RISK-007"}, {"risk_id": "RISK-003"}, {}]) == 7
    assert last_risk_number([]) == 0


def test_every_rule_is_raised_by_the_fixture_and_documented() -> None:
    risks = _all_kinds()
    assert set(_rules(risks)) == set(RULES)
    assert len(_rules(risks)) == len(RULES)
    for rule, spec in RULES.items():
        assert spec.severity in ("HIGH", "MEDIUM", "LOW"), rule
        assert f"``{rule}``" in (migration_risks.__doc__ or ""), rule


def test_every_risk_has_evidence_a_mitigation_and_a_wave() -> None:
    for r in _all_kinds():
        assert r["mitigation"], r["rule"]
        assert r["affected_tables"] or r["query_ids"], r
        assert r["wave"] in (1, 2, 3), r


def test_the_strategies_name_each_risk_by_wave_gate() -> None:
    risks = _all_kinds()
    lines = wave_gate_strategies(risks)
    assert [line.split(",")[0] for line in lines] == [
        "Before the Wave 1 gate",
        "Before the Wave 2 gate",
        "Before the Wave 3 gate",
    ]
    joined = " ".join(lines)
    for r in risks:
        if sum(1 for x in risks if x["wave"] == r["wave"]) <= GATE_IDS_NAMED:
            assert re.search(rf"\b{r['risk_id']}\b", joined), r["risk_id"]


def test_a_wave_with_many_risks_is_counted_not_listed() -> None:
    risks = [
        {"risk_id": f"RISK-{i:03d}", "rule": "design.trade_offs", "wave": 3}
        for i in range(1, GATE_IDS_NAMED + 2)
    ]
    assert wave_gate_strategies(risks) == [
        f"Before the Wave 3 gate, close the {GATE_IDS_NAMED + 1} Wave 3 risks (design trade-offs)."
    ]
    assert "RISK-001" in wave_gate_strategies(risks[:GATE_IDS_NAMED])[0]


def test_adding_the_risks_raises_the_overall_level_and_keeps_the_existing_strategies() -> None:
    assessment: dict[str, Any] = {
        "overall_risk_level": "LOW",
        "risks": [{"risk_id": "RISK-001", "severity": "LOW", "description": "[x] y"}],
        "mitigation_strategies": ["Existing line."],
    }
    risks = _all_kinds()
    add_migration_risks(assessment, risks)
    assert assessment["overall_risk_level"] == "HIGH"
    assert assessment["risks"][0]["risk_id"] == "RISK-001"
    assert len(assessment["risks"]) == 1 + len(risks)
    assert assessment["mitigation_strategies"][0] == "Existing line."
    untouched: dict[str, Any] = {
        "overall_risk_level": "LOW",
        "risks": [],
        "mitigation_strategies": [],
    }
    add_migration_risks(untouched, [])
    assert untouched == {"overall_risk_level": "LOW", "risks": [], "mitigation_strategies": []}


def test_the_wording_is_plain() -> None:
    risks = _all_kinds()
    texts = [r["description"] for r in risks] + [r["mitigation"] for r in risks]
    texts += wave_gate_strategies(risks)
    for text in texts:
        body = re.sub(r"^\[[a-z_]+\] ", "", text)
        # Model text quoted as data (a cache pattern's reason, trade-off titles) is not ours.
        body = body.replace(UNSUPPORTED["reason"], "")
        for example in REVIEW_EXAMPLES:
            body = body.replace(example, "")
        assert not find_style_hits(body), (text, find_style_hits(body))


def test_every_pending_fact_hook_has_its_rule() -> None:
    assert PENDING_FACT_HOOKS == {}


# ---------------------------------------------------------------------------
# The Bedrock smoke's real shapes (designed wordpress, #1095)
# ---------------------------------------------------------------------------


def test_mixed_engine_ids_are_one_risk_per_engine_in_its_wave() -> None:
    """The Aurora MySQL designer wrote 4 trade-offs as ``aurora-mysql`` and 6 as
    ``aurora_mysql``: one risk, in Wave 1, filed under Aurora MySQL."""
    trade_offs = [
        {"description": f"Added composite index {i}", "engine": "aurora-mysql"} for i in range(4)
    ] + [
        {"description": f"[PE note] After cutover, run EXPLAIN on query {i}.", "engine": AURORA}
        for i in range(6)
    ]
    risks = _build(migration_waves=WAVES, trade_offs=trade_offs)
    risk = _one(risks, "design.trade_offs")
    assert risk["wave"] == 1
    assert risk["description"].startswith(
        "[aurora_mysql] The Aurora MySQL schema design records 10 trade-offs to review; "
    )
    assert len(risk["details"]) == 10
    assert not any("[PE note]" in d for d in risk["details"])


def test_several_app_layer_notes_and_cache_patterns_are_one_risk_per_engine() -> None:
    notes = [
        {"feature": "deprecated_sql", "source_object": "a"},
        {"feature": "eav_reporting", "source_object": "b"},
        {"feature": "zero_date_defaults", "source_object": "c"},
    ]
    patterns = [
        {"source_query_ids": ["c1"], "reason": "meta_id lookups"},
        {"source_query_ids": ["c2"], "reason": "slug-uniqueness check"},
    ]
    risks = _build(
        migration_waves=WAVES,
        schema_designs={
            "aurora-mysql": {"app_layer_notes": notes},
            CACHE: {"unsupported_patterns": patterns},
        },
        cache_only_engines={CACHE},
    )
    assert sorted(_rules(risks)) == ["cache.unsupported", "design.app_layer"]
    app = _one(risks, "design.app_layer")
    assert app["wave"] == 1
    assert app["description"] == (
        "[aurora_mysql] 3 source objects need application code on Aurora MySQL: "
        "1 deprecated sql; 1 eav reporting; 1 zero date defaults. The schema carries them "
        "over without the database logic."
    )
    cache = _one(risks, "cache.unsupported")
    assert cache["query_ids"] == ["c1", "c2"]
    assert "marks 2 patterns unsupported" in cache["description"]
    assert "slug-uniqueness check" not in cache["description"]
    assert any("slug-uniqueness check" in d for d in cache["details"])


def test_hot_partitions_on_several_tables_are_one_risk() -> None:
    hot = [_hot(85.0), {**_hot(90.0), "table_name": "Posts"}]
    risks = _build(migration_waves=WAVES, schema_designs={DDB: {"hot_partition_analysis": hot}})
    risk = _one(risks, "design.hot_partition")
    assert "2 design tables have a partition above 80%" in risk["description"]


def test_a_detail_drops_its_review_marker_and_keeps_its_text() -> None:
    text = "[PE note] " + "consistency " * 20
    risks = _build(migration_waves=WAVES, trade_offs=[{"description": text, "engine": DDB}])
    assert risks[0]["details"] == [("consistency " * 20).strip()]


def test_the_engine_id_normalizer_maps_variants_once() -> None:
    from src.shared.engine_names import canonical_engine_id

    for raw in ("aurora-mysql", "Aurora MySQL", " AURORA_MYSQL ", "aurora_mysql"):
        assert canonical_engine_id(raw) == "aurora_mysql", raw
    assert canonical_engine_id("reality-check") == "reality-check"
    assert canonical_engine_id("") == ""
