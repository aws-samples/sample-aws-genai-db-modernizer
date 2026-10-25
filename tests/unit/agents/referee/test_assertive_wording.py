"""Fixed wording in the deliverables is assertive and agrees with the plan (#951).

The rubric judge, on a wordpress model run, cited fixed sentences that synthesis
and the reality check write: a hedge in the key-value wave's rationale ("where
possible"), a CQRS line saying one engine takes "all write operations" while the
sources of truth name another engine as the owner of tables, an "estimated
$X/month." fragment appended to every engine's reason (the cost has its own
table), a consolidation line that states its query count twice, per-query
reasons cut off mid-sentence with "…", and a replica risk that lists raw 64-char
query hashes. Each is fixed where the text is written, so every deliverable that
shows it reads the same thing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.agents.referee.migration_wave_builders import _kv_wave
from src.agents.referee.reality_check_patterns import (
    _build_recommendations,
    format_pattern_recommendation,
    patterns_for_assignment,
)
from src.agents.referee.reality_check_place import _place_settled_queries
from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_grounding import recompute_reality_check_patterns
from src.agents.referee.synthesis_report import _clean_relational_reason, build_ranking
from src.shared.report_facts import table_source_facts

_HASH = re.compile(r"\b[0-9a-f]{64}\b")


# ---------------------------------------------------------------------------
# Key-value wave rationale: no "where possible"
# ---------------------------------------------------------------------------


def test_the_key_value_wave_states_how_co_dependent_tables_move() -> None:
    wave = _kv_wave(
        "dynamodb",
        {"dynamodb": {"assigned_queries": 2, "workload_percent": 40.0}},
        [
            {"table_id": "db.a", "engines": ["dynamodb"]},
            {"table_id": "db.b", "engines": ["dynamodb"]},
        ],
        [
            {"query_id": "q1", "assigned_engine": "dynamodb", "source_tables": ["db.a", "db.b"]},
            {"query_id": "q2", "assigned_engine": "dynamodb", "source_tables": ["db.b"]},
        ],
        [["q1"]],
        "aurora_mysql",
        "mysql",
    )
    assert wave is not None
    assert "where possible" not in wave["rationale"]
    assert "co-dependent tables move together" in wave["rationale"]


# ---------------------------------------------------------------------------
# CQRS: the write engine comes from the sources of truth
# ---------------------------------------------------------------------------


def _qas(**counts: int) -> list[dict]:
    return [
        {"query_id": f"{engine}-{i}", "assigned_engine": engine, "in_scope": True}
        for engine, n in counts.items()
        for i in range(n)
    ]


def _lines(patterns: list[dict]) -> list[str]:
    return [format_pattern_recommendation(p) for p in patterns]


def test_cqrs_is_not_claimed_when_another_engine_owns_writes() -> None:
    """The wordpress evidence: Aurora MySQL carries most queries, DynamoDB owns
    the writes of some tables, so no engine takes "all write operations"."""
    qas = _qas(aurora_mysql=6, dynamodb=4)
    write_groups = {"db.posts": ["dynamodb-0"], "db.users": ["aurora_mysql-0"]}
    lines = _lines(patterns_for_assignment(qas, write_groups))
    assert not any("for all write operations" in line for line in lines), lines
    assert not any("CQRS" in line for line in lines), lines


def test_cqrs_names_the_one_engine_every_write_lands_on() -> None:
    qas = _qas(aurora_mysql=6, dynamodb=4)
    write_groups = {"db.posts": ["aurora_mysql-1"], "db.users": ["aurora_mysql-0"]}
    lines = _lines(patterns_for_assignment(qas, write_groups))
    assert any("Use Aurora MySQL for all write operations" in line for line in lines), lines


def test_cqrs_is_not_claimed_when_the_primary_writes_nothing() -> None:
    qas = _qas(aurora_mysql=6, dynamodb=4)
    write_groups = {"db.posts": ["dynamodb-0"]}
    lines = _lines(patterns_for_assignment(qas, write_groups))
    assert not any("for all write operations" in line for line in lines), lines


def test_no_written_table_claims_no_write_engine() -> None:
    """Write groups recorded but empty: nothing is written, so no engine takes
    "all write operations"."""
    lines = _lines(patterns_for_assignment(_qas(aurora_mysql=6, dynamodb=4), {}))
    assert not any("for all write operations" in line for line in lines), lines


def test_an_assignment_without_write_groups_keeps_the_earlier_detection() -> None:
    """A job written before write groups existed has no sources of truth to read."""
    lines = _lines(patterns_for_assignment(_qas(aurora_mysql=6, dynamodb=4)))
    assert any("Use Aurora MySQL for all write operations" in line for line in lines), lines


def test_synthesis_recomputes_cqrs_from_the_write_groups() -> None:
    summary = {
        "architectural_patterns": [],
        "recommendations": [
            "Recommended pattern: Command Query Responsibility Segregation (CQRS). Use "
            "Aurora MySQL for all write operations and DynamoDB for specialized reads."
        ],
    }
    out = recompute_reality_check_patterns(
        summary, _qas(aurora_mysql=6, dynamodb=4), {"db.posts": ["dynamodb-0"]}
    )
    assert not any("for all write operations" in r for r in out["recommendations"])


# ---------------------------------------------------------------------------
# Consolidation line: the count once
# ---------------------------------------------------------------------------


def _consolidations() -> list[dict]:
    """The reality check's Phase 2 on two DocumentDB queries Aurora MySQL can serve."""
    qas = [
        {"query_id": q, "assigned_engine": "documentdb", "assignment_reason": "t"}
        for q in ("q1", "q2")
    ]
    _, consolidations = _place_settled_queries(
        query_assignments=qas,
        committed_engines={"aurora_mysql"},
        mandatory_query_ids=set(),
        current_engine_by_qid={"q1": "documentdb", "q2": "documentdb"},
        query_signals={},
        query_map={},
        query_capabilities=None,
        trace_by_query={
            q: {"engines": {"documentdb": 80, "aurora_mysql": 70}} for q in ("q1", "q2")
        },
        source_compatible_engine=None,
        workload_justified={},
    )
    return consolidations


def test_a_consolidation_reason_does_not_restate_its_count() -> None:
    """The count is the record's own field; the reason is written without it."""
    (record,) = _consolidations()
    assert record["query_count"] == 2
    assert record["reason"] == "DocumentDB provides no unique capabilities over Aurora MySQL"


def test_a_consolidation_line_states_its_query_count_once() -> None:
    recs = _build_recommendations(_qas(aurora_mysql=2), _consolidations(), [], {}, {})
    line = next(r for r in recs if r.startswith("Consolidated"))
    assert line.startswith(
        "Consolidated 2 queries from DocumentDB → Aurora MySQL: DocumentDB provides no "
        "unique capabilities over Aurora MySQL."
    ), line
    assert line.count("2 queries") == 1, line
    assert "can be served by" not in line, line


def test_another_consolidation_reason_is_kept_as_written() -> None:
    reason = (
        "Sanity sweep: DocumentDB had only 3 queries, each with a serviceable "
        "committed engine; 3 scored best on DynamoDB"
    )
    consolidation = {
        "from_engine": "documentdb",
        "to_engine": "dynamodb",
        "query_count": 3,
        "reason": reason,
        "saved_cost_estimate": 0,
        "action": "full",
    }
    recs = _build_recommendations(_qas(dynamodb=3), [consolidation], [], {}, {})
    line = next(r for r in recs if r.startswith("Consolidated"))
    assert reason in line, line


# ---------------------------------------------------------------------------
# Engine reasons: no "estimated $X/month." fragment
# ---------------------------------------------------------------------------


def _ranking_data() -> SynthesisData:
    queries = [
        {"query_id": q, "query_type": "SELECT", "tables_accessed": ["t"], "calls_per_second": c}
        for q, c in (("c1", 3.0), ("c2", 3.0), ("c3", 3.8), ("other", 20.0))
    ]
    return SynthesisData(
        job_id="j",
        database_name="db",
        collector={"queries": {"query_patterns": queries}},
        engines={
            "aurora_postgresql": EngineArtifacts(
                "aurora_postgresql",
                analysis={"cost_estimate": {"monthly_cost_usd": 121.06}},
                schema_design={},
            ),
            "elasticache": EngineArtifacts(
                "elasticache",
                analysis={"cost_estimate": {"monthly_cost_usd": 165.10}},
                schema_design={},
            ),
        },
        assignment={
            "query_assignments": [
                {
                    "query_id": qid,
                    "assigned_engine": "aurora_postgresql",
                    "cache_engine": "elasticache",
                    "in_scope": True,
                }
                for qid in ("c1", "c2", "c3")
            ]
            + [{"query_id": "other", "assigned_engine": "aurora_postgresql", "in_scope": True}]
        },
    )


def test_no_engine_reason_carries_a_cost_fragment() -> None:
    ranking = build_ranking(_ranking_data())
    assert ranking
    for r in ranking:
        assert "estimated $" not in r["rationale"], r["rationale"]
        assert "/month" not in r["rationale"], r["rationale"]


def test_the_cache_reason_still_states_the_traffic_that_clears_the_floor() -> None:
    cache = next(r for r in build_ranking(_ranking_data()) if r.get("role") == "cache_layer")
    assert (
        "These reads total 9.8 calls/s, above the 1 calls/s hot-read floor." in cache["rationale"]
    ), cache["rationale"]


# ---------------------------------------------------------------------------
# Per-query reasons: whole clauses, never cut mid-sentence
# ---------------------------------------------------------------------------


def test_a_long_reason_drops_whole_clauses_without_an_ellipsis() -> None:
    reason = (
        "utility/metadata statement, kept on the source-compatible relational engine "
        "(Aurora MySQL) | DocumentDB lacks required capability: a utility or DDL statement; "
        "DynamoDB lacks required capability: a utility or DDL statement; ElastiCache lacks "
        "required capability: a utility or DDL statement; OpenSearch lacks required "
        "capability: a utility or DDL statement"
    )
    out = _clean_relational_reason(reason, max_chars=180)
    # #1220: the utility pin's label reads as a sentence.
    assert out == (
        "administration or metadata statement, kept on Aurora MySQL as the source-compatible "
        "engine | DocumentDB lacks required capability: a utility or DDL statement (+3 more)"
    ), out


def test_a_base_reason_alone_carries_no_count() -> None:
    """Not even one exclusion fits: the base reason stands alone, with no
    "(+N more)" that would read as more reasons for this engine."""
    base = "co-dependency group → Aurora MySQL"
    reason = (
        base
        + " | "
        + "; ".join(f"Engine{i} lacks required capability: " + "y" * 80 for i in range(4))
    )
    assert _clean_relational_reason(reason, max_chars=100) == base


def test_a_short_reason_is_unchanged() -> None:
    reason = "highest fit for Aurora MySQL | DynamoDB lacks required capability: aggregation"
    assert _clean_relational_reason(reason) == reason


def test_a_long_base_reason_is_kept_whole() -> None:
    """The base reason (before " | ") is never cut: it says why the query is here."""
    base = "highest fit for Aurora MySQL (scores tie with OpenSearch; " + "x" * 200 + ")"
    assert _clean_relational_reason(base, max_chars=180) == base


# ---------------------------------------------------------------------------
# Replica risk: counts, the mechanism and what to do, no query hashes
# ---------------------------------------------------------------------------


def test_the_replica_risk_names_no_query_hash_and_says_what_to_do() -> None:
    reads = [f"{i:064x}" for i in range(7)]
    facts = table_source_facts(
        {
            "table_sources": [
                {
                    "table": "db.posts",
                    "source_of_truth": "dynamodb",
                    "write_query_ids": ["w1"],
                    "replicas": [{"engine": "aurora_mysql", "read_query_ids": reads}],
                }
            ]
        }
    )
    assert facts is not None
    row = facts["replicated_tables"][0]
    assert row["replica_read_query_ids"] == sorted(reads)  # the ids stay in the data
    risk = row["risk"]
    assert not _HASH.search(risk), risk
    assert risk.startswith("Eventually consistent: 7 reads on Aurora MySQL")
    assert "in sync from DynamoDB via CDC" in risk
    assert "behind by the replication lag" in risk, risk
    assert "a read that must see its own write should read from DynamoDB" in risk, risk
    assert "can miss" not in risk


# ---------------------------------------------------------------------------
# The prompts that write customer prose teach the bound, not the hedge
# ---------------------------------------------------------------------------

_SKILLS = Path(__file__).resolve().parents[4] / "src" / "skills"
# The hedges the judge cited in model-written text, and the prompt examples that
# taught two of them ("a few seconds behind", "if the window matters").
_HEDGE_EXAMPLES = ("may be a few seconds behind", "if the window matters")


@pytest.mark.parametrize(
    "skill",
    ["executive-summary.md", "dynamodb-data-modeling.md", "elasticache-data-modeling.md"],
)
def test_the_skill_asks_for_the_mechanism_and_its_bound(skill: str) -> None:
    text = (_SKILLS / skill).read_text(encoding="utf-8")
    for example in _HEDGE_EXAMPLES:
        assert example not in text, (skill, example)
    assert "bound" in text, skill
    assert "TTL" in text or "strongly consistent" in text, skill


def test_the_synthesized_report_names_no_single_write_engine_when_two_own_writes(
    tmp_path,
) -> None:
    """End to end through synthesis: the assignment's write groups reach the
    pattern recompute, so the stale CQRS line the reality check wrote is replaced."""
    from src.agents.referee.synthesis_handler import run_synthesis_deterministic
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    db, job = "shop", "job-951"
    store.write_json(
        f"{db}/{job}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in ("aurora_mysql", "dynamodb")]},
    )
    store.write_json(
        f"{db}/{job}/collector/output.json",
        {
            "database_schema": {
                "tables": [{"table_id": "shop.orders"}, {"table_id": "shop.products"}]
            },
            "queries": {"query_patterns": []},
        },
    )
    for engine in ("aurora_mysql", "dynamodb"):
        store.write_json(
            f"{db}/{job}/analysis-{engine}/analysis.json",
            {"workload_analysis": {"anti_patterns_detected": []}},
        )
    queries = [
        {"query_id": q, "assigned_engine": e, "in_scope": True, "source_tables": [t]}
        for q, e, t in (
            ("q-a1", "aurora_mysql", "shop.products"),
            ("q-a2", "aurora_mysql", "shop.products"),
            ("q-a3", "aurora_mysql", "shop.orders"),
            ("q-d1", "dynamodb", "shop.orders"),
            ("q-d2", "dynamodb", "shop.orders"),
        )
    ]
    store.write_json(
        f"{db}/{job}/assignment/v2/assignment.json",
        {
            "version": 2,
            "status": "reality_checked",
            "query_assignments": queries,
            "write_groups": {"shop.orders": ["q-d1"], "shop.products": ["q-a1"]},
        },
    )
    store.write_json(
        f"{db}/{job}/reality-check/output.json",
        {
            "consolidations": [],
            "architectural_patterns": [],
            "recommendations": [
                "Recommended pattern: Command Query Responsibility Segregation (CQRS). Use "
                "Aurora MySQL for all write operations and DynamoDB for specialized reads."
            ],
            "before_distribution": {"aurora_mysql": 3, "dynamodb": 2},
            "after_distribution": {"aurora_mysql": 3, "dynamodb": 2},
        },
    )
    result = run_synthesis_deterministic(job, db, store, assignment_version=2)
    texts = [str(t.get("description")) for t in result["trade_offs"]]
    assert not any("for all write operations" in t for t in texts), texts
    assert any(t.startswith("Recommended pattern: ") for t in texts), texts
