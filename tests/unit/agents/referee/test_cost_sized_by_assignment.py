"""Each kept engine is priced for the queries the final assignment gives it (#1195).

The analysis estimates are made before routing, so each one sizes its engine for the
whole collection. The TCO re-runs each kept engine's own estimator on the queries the
effective assignment gives it (in scope only) and the tables those queries touch; the
cache layer is sized from the reads its overlay fronts. Each row states that basis
(``sizing``) and the price table it was priced from (``price_list``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_tco_analysis
from src.shared.catalog.prices import price_basis
from src.tools.analysis.aurora_common_analysis_tools import estimate_aurora_costs
from src.tools.analysis.dynamodb_analysis_tools import estimate_dynamodb_costs
from src.tools.analysis.redis_analysis_tools import estimate_redis_costs

TABLES = [
    {"table_id": "shop.orders", "size_mb": 10.0},
    {"table_id": "shop.carts", "size_mb": 5.0},
    # 30 GB that no in-scope query reads: wave 1's Aurora engine stores it (#1378).
    {"table_id": "shop.audit", "size_mb": 30720.0},
]


def _q(qid: str, qtype: str, cps: float, table: str) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_type": qtype,
        "calls_per_second": cps,
        "tables_accessed": [table],
    }


QUERIES = [
    _q("q1", "SELECT", 60.0, "shop.orders"),
    _q("q2", "SELECT", 40.0, "shop.carts"),
    _q("q3", "UPDATE", 10.0, "shop.carts"),
    _q("q4", "INSERT", 30.0, "shop.orders"),
    _q("q5", "SELECT", 50.0, "shop.audit"),
]
COLLECTOR = {"database_schema": {"tables": TABLES}, "queries": {"query_patterns": QUERIES}}


def _qa(qid: str, engine: str, table: str, **extra: Any) -> dict[str, Any]:
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "source_tables": [table],
        "assignment_reason": "test",
        **extra,
    }


ASSIGNMENT = {
    "query_assignments": [
        _qa("q1", "aurora_mysql", "shop.orders"),
        _qa("q2", "dynamodb", "shop.carts", cache_engine="elasticache"),
        _qa("q3", "dynamodb", "shop.carts"),
        _qa("q4", "aurora_mysql", "shop.orders"),
        # The customer took this one out of the iteration: no engine serves it yet.
        _qa("q5", "aurora_mysql", "shop.audit", in_scope=False),
    ]
}


def _whole(engine: str) -> dict[str, Any]:
    """An analysis estimate made before routing: the whole collection."""
    if engine == "aurora_mysql":
        est = estimate_aurora_costs(COLLECTOR, "us-east-1", engine="mysql")
    elif engine == "dynamodb":
        est = estimate_dynamodb_costs(COLLECTOR)
    else:
        est = estimate_redis_costs(COLLECTOR, "us-east-1", None)
    return {"cost_estimate": est.model_dump()}


def _data(assignment: dict[str, Any] | None = ASSIGNMENT) -> SynthesisData:
    return SynthesisData(
        job_id="j",
        database_name="shop",
        collector=COLLECTOR,
        engines={
            e: EngineArtifacts(engine=e, analysis=_whole(e))
            for e in ("aurora_mysql", "dynamodb", "elasticache")
        },
        assignment=assignment,
    )


def _subset(qids: set[str], tables: set[str]) -> dict[str, Any]:
    return {
        "database_schema": {"tables": [t for t in TABLES if t["table_id"] in tables]},
        "queries": {"query_patterns": [q for q in QUERIES if q["query_id"] in qids]},
    }


def _rows(tco: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["database"]: r for r in tco["cost_breakdown"]}


def test_each_engine_is_priced_by_its_own_estimator_on_its_assigned_queries() -> None:
    rows = _rows(build_tco_analysis(_data()))
    aurora = estimate_aurora_costs(
        _subset({"q1", "q4"}, {"shop.orders"}), "us-east-1", engine="mysql"
    )
    dynamo = estimate_dynamodb_costs(_subset({"q2", "q3"}, {"shop.carts"}))
    cache = estimate_redis_costs(_subset({"q2"}, {"shop.carts"}), "us-east-1", None)
    assert rows["aurora_mysql"]["monthly_cost_usd"] == aurora.monthly_cost_usd
    assert rows["dynamodb"]["monthly_cost_usd"] == dynamo.monthly_cost_usd
    assert rows["elasticache"]["monthly_cost_usd"] == cache.monthly_cost_usd


def test_aurora_drops_to_the_smaller_instance_its_own_load_needs() -> None:
    """The whole collection is 190 calls/s (db.r6g.xlarge); Aurora's in-scope queries are
    90 calls/s, under the 100 calls/s threshold (db.r6g.large)."""
    whole = _whole("aurora_mysql")["cost_estimate"]
    assert whole["cost_components"]["provisioned"]["instance_type"] == "db.r6g.xlarge"
    rows = _rows(build_tco_analysis(_data()))
    assert rows["aurora_mysql"]["monthly_cost_usd"] < whole["monthly_cost_usd"]
    assert rows["aurora_mysql"]["sizing"] == {
        "basis": "assigned_queries",
        "sized_query_count": 2,
        "calls_per_second": 90.0,
        "read_calls_per_second": 60.0,
        "write_calls_per_second": 30.0,
        "sized_table_count": 1,
        "unqueried_table_count": 0,
        "storage_gb": round(10.0 / 1024, 4),
    }


def test_dynamodb_is_billed_only_for_its_own_reads_and_writes() -> None:
    rows = _rows(build_tco_analysis(_data()))
    assert rows["dynamodb"]["sizing"] == {
        "basis": "assigned_queries",
        "sized_query_count": 2,
        "calls_per_second": 50.0,
        "read_calls_per_second": 40.0,
        "write_calls_per_second": 10.0,
        "sized_table_count": 1,
        "unqueried_table_count": 0,
        "storage_gb": round(5.0 / 1024, 4),
    }
    assert (
        rows["dynamodb"]["monthly_cost_usd"]
        < _whole("dynamodb")["cost_estimate"]["monthly_cost_usd"]
    )


def test_the_cache_layer_is_sized_from_the_reads_its_overlay_fronts() -> None:
    rows = _rows(build_tco_analysis(_data()))
    assert rows["elasticache"]["sizing"] == {
        "basis": "cache_overlay",
        "sized_query_count": 1,
        "calls_per_second": 40.0,
        "read_calls_per_second": 40.0,
        "write_calls_per_second": 0.0,
        "sized_table_count": 1,
        "unqueried_table_count": 0,
        "storage_gb": round(5.0 / 1024, 4),
    }


def test_a_dropped_overlay_is_not_cache_load() -> None:
    assignment = {
        "query_assignments": [
            {**qa, "cache_dropped": True} if qa["query_id"] == "q2" else qa
            for qa in ASSIGNMENT["query_assignments"]
        ]
    }
    rows = _rows(build_tco_analysis(_data(assignment)))
    assert rows["elasticache"]["sizing"]["sized_query_count"] == 0
    # An engine with nothing to serve costs its estimator's floor, never a negative or a gap.
    floor = estimate_redis_costs(_subset(set(), set()), "us-east-1", None)
    assert rows["elasticache"]["monthly_cost_usd"] == floor.monthly_cost_usd


def test_the_total_is_the_sum_of_the_sized_rows() -> None:
    tco = build_tco_analysis(_data())
    total = sum(r["monthly_cost_usd"] for r in tco["cost_breakdown"])
    assert tco["projected_monthly_cost"] == round(total, 2)
    whole = sum(_whole(e)["cost_estimate"]["monthly_cost_usd"] for e in _rows(tco))
    assert tco["projected_monthly_cost"] < whole


def test_each_row_records_the_price_table_it_was_priced_from() -> None:
    for row in build_tco_analysis(_data())["cost_breakdown"]:
        assert row["price_list"] == price_basis()


def test_without_an_assignment_the_analysis_estimates_stand() -> None:
    rows = _rows(build_tco_analysis(_data(None)))
    for engine, row in rows.items():
        assert row["monthly_cost_usd"] == _whole(engine)["cost_estimate"]["monthly_cost_usd"]
        assert row["sizing"] is None


def test_an_analysis_that_priced_nothing_keeps_its_figure() -> None:
    """Cost estimation turned off (or an estimate with no components) is not re-priced."""
    data = _data()
    data.engines["dynamodb"].analysis = {"cost_estimate": {"monthly_cost_usd": 0.0}}
    rows = _rows(build_tco_analysis(data))
    assert rows["dynamodb"]["monthly_cost_usd"] == 0.0
    assert rows["dynamodb"]["sizing"] is None


def _moved(qid: str, engine: str) -> dict[str, Any]:
    return {
        "query_assignments": [
            {**qa, "assigned_engine": engine} if qa["query_id"] == qid else qa
            for qa in ASSIGNMENT["query_assignments"]
        ]
    }


def test_a_move_between_two_owners_leaves_the_cache_layer_alone() -> None:
    base = _rows(build_tco_analysis(_data()))
    after = _rows(build_tco_analysis(_data(_moved("q1", "dynamodb"))))
    assert after["elasticache"] == base["elasticache"]
    assert after["dynamodb"]["sizing"]["calls_per_second"] == 110.0
    assert after["aurora_mysql"]["sizing"]["calls_per_second"] == 30.0


@pytest.mark.passes_on_main  # main sizes nothing per query, so this holds there trivially
def test_an_out_of_scope_query_sizes_no_engine() -> None:
    assert _rows(build_tco_analysis(_data(_moved("q5", "dynamodb")))) == _rows(
        build_tco_analysis(_data())
    )


# Every name a reader of the sized costs would use: the sizing module, its entry
# point, the estimator table it calls, the TCO row's ``sizing`` and the facts' copy.
_SIZED_COST_NAMES = re.compile(
    r"\b(assigned_cost|sized_cost_estimate|COST_ESTIMATORS|cost_sizing|engine_cost_sizing|"
    r"sized_query_count|sized_table_count|CACHE_MISS_ASSUMPTION)\b|[\"']sizing[\"']"
)
# The TCO and synthesis code, the facts layer that states the basis, and the sizing
# module itself: the only places allowed to read it.
_SIZED_COST_READERS = {
    "src/agents/referee/assigned_cost.py",
    "src/agents/referee/synthesis_report.py",
    "src/agents/referee/synthesis_handler.py",
    "src/shared/report_facts.py",
}


def test_routing_never_reads_the_sized_costs() -> None:
    """ADR-031 Decision 5: routing's cost dimension is its own per-query model, so the
    sized TCO is read only by the TCO and synthesis code. Every module of the referee
    (recursively), the orchestrator, the shared layer and the scripts is scanned."""
    root = Path(__file__).resolve().parents[4]
    scanned = [
        p
        for d in ("src/agents/referee", "src/orchestrator", "src/shared", "scripts")
        for p in sorted((root / d).rglob("*.py"))
    ]
    assert len(scanned) > 100
    readers = {
        p.relative_to(root).as_posix()
        for p in scanned
        if _SIZED_COST_NAMES.search(p.read_text(encoding="utf-8"))
    }
    assert readers - _SIZED_COST_READERS == set()
    assert {"src/agents/referee/synthesis_report.py", "src/shared/report_facts.py"} <= readers


def test_the_guard_catches_a_reader() -> None:
    assert _SIZED_COST_NAMES.search('cost = row["sizing"]["calls_per_second"]')
    assert _SIZED_COST_NAMES.search("from src.agents.referee.assigned_cost import x")
    assert not _SIZED_COST_NAMES.search("load_test_sizing = derive()")


def test_the_summary_model_sees_each_kept_engines_tco_figure(tmp_path) -> None:
    """The ranking the summary model reads carries each kept engine's TCO row figure,
    never the whole-collection analysis estimate next to it (#1195)."""
    from src.agents.referee.synthesis_handler import (
        prepare_synthesis_llm_input,
        run_synthesis_deterministic,
    )
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    db, job = "shop", "j"
    store.write_json(
        f"{db}/{job}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in ("aurora_mysql", "dynamodb")]},
    )
    store.write_json(f"{db}/{job}/collector/output.json", COLLECTOR)
    for engine in ("aurora_mysql", "dynamodb"):
        store.write_json(
            f"{db}/{job}/analysis-{engine}/analysis.json",
            {
                "table_recommendations": [{"table_id": "shop.orders", "confidence_score": 80}],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
                **_whole(engine),
            },
        )
    store.write_json(
        f"{db}/{job}/assignment/v1/assignment.json",
        {
            "job_id": job,
            "version": 1,
            "query_assignments": [
                {k: v for k, v in qa.items() if k != "cache_engine"}
                for qa in ASSIGNMENT["query_assignments"]
            ],
            "table_assignments": [],
        },
    )
    result = run_synthesis_deterministic(job, db, store, assignment_version=1)
    tco = {r["database"]: r["monthly_cost_usd"] for r in result["tco_analysis"]["cost_breakdown"]}
    assert tco["aurora_mysql"] != _whole("aurora_mysql")["cost_estimate"]["monthly_cost_usd"]
    # report.json's ranking states each kept engine's TCO figure, so every cost agrees.
    ranked = {r["target"]: r["monthly_cost_usd"] for r in result["ranking"] if r["target"] in tco}
    assert ranked == tco
    payload = prepare_synthesis_llm_input(result)
    # The model's per-engine costs are the TCO rows; any ranking cost it gets is the same.
    model_tco = {
        r["database"]: r["monthly_cost_usd"] for r in payload["tco_analysis"]["cost_breakdown"]
    }
    assert model_tco == tco
    for r in payload["ranking"]:
        if r.get("target") in tco and "monthly_cost_usd" in r:
            assert r["monthly_cost_usd"] == tco[r["target"]]


def test_with_every_query_out_of_scope_no_engine_falls_back_to_the_whole_collection() -> None:
    """The customer took every query out of the iteration: each engine is sized to
    nothing ($0, basis ``no_in_scope_queries``), not billed its analysis figure."""
    assignment = {
        "query_assignments": [{**qa, "in_scope": False} for qa in ASSIGNMENT["query_assignments"]]
    }
    tco = build_tco_analysis(_data(assignment))
    for row in tco["cost_breakdown"]:
        assert row["monthly_cost_usd"] == 0.0, row["database"]
        assert row["sizing"]["basis"] == "no_in_scope_queries"
        assert row["sizing"]["sized_query_count"] == 0
    assert tco["projected_monthly_cost"] == 0.0


def test_the_cache_miss_assumption_is_stated_only_with_a_cache_layer() -> None:
    from src.agents.referee.assigned_cost import CACHE_MISS_ASSUMPTION

    assert CACHE_MISS_ASSUMPTION in build_tco_analysis(_data())["assumptions"]
    no_cache = {
        "query_assignments": [
            {k: v for k, v in qa.items() if k != "cache_engine"}
            for qa in ASSIGNMENT["query_assignments"]
        ]
    }
    assert CACHE_MISS_ASSUMPTION not in build_tco_analysis(_data(no_cache))["assumptions"]
    assert CACHE_MISS_ASSUMPTION not in build_tco_analysis(_data(None))["assumptions"]


# --------------------------------------------------------------------------
# #1378: the tables no in-scope query reads are stored on wave 1's Aurora engine
# --------------------------------------------------------------------------

AUDIT_GB = round(30720.0 / 1024, 4)


def test_wave_one_engine_stores_the_tables_no_in_scope_query_reads() -> None:
    """Wave 1 moves the whole database, so shop.audit (read only by q5, out of scope)
    lands on Aurora MySQL: its storage is in Aurora's figure and sizing, not its count of
    tables its queries touch."""
    tco = build_tco_analysis(_data(), wave_one_engine="aurora_mysql")
    rows = _rows(tco)
    aurora = estimate_aurora_costs(
        _subset({"q1", "q4"}, {"shop.orders", "shop.audit"}), "us-east-1", engine="mysql"
    )
    assert rows["aurora_mysql"]["monthly_cost_usd"] == aurora.monthly_cost_usd
    assert rows["aurora_mysql"]["sizing"]["sized_table_count"] == 1
    assert rows["aurora_mysql"]["sizing"]["unqueried_table_count"] == 1
    assert rows["aurora_mysql"]["sizing"]["storage_gb"] == round((10.0 + 30720.0) / 1024, 4)
    for engine in ("dynamodb", "elasticache"):
        assert rows[engine] == _rows(build_tco_analysis(_data()))[engine]
        assert rows[engine]["sizing"]["unqueried_table_count"] == 0
    assert tco["unqueried_tables"] == {
        "table_count": 1,
        "storage_gb": AUDIT_GB,
        "stored_on": "aurora_mysql",
    }


def test_the_projected_cost_includes_that_storage() -> None:
    without = build_tco_analysis(_data())
    with_storage = build_tco_analysis(_data(), wave_one_engine="aurora_mysql")
    assert with_storage["projected_monthly_cost"] > without["projected_monthly_cost"]
    total = sum(r["monthly_cost_usd"] for r in with_storage["cost_breakdown"])
    assert with_storage["projected_monthly_cost"] == round(total, 2)


def test_the_assumptions_name_the_engine_that_stores_them() -> None:
    lines = build_tco_analysis(_data(), wave_one_engine="aurora_mysql")["assumptions"]
    assert (
        "1 source table no in-scope query reads (30 GB) is stored on Aurora MySQL, "
        "where wave 1 moves it"
    ) in lines


@pytest.mark.parametrize("engine", [None, "aurora_postgresql"])
def test_with_no_wave_one_engine_in_the_tco_no_engine_stores_them(engine: str | None) -> None:
    """No Aurora engine in wave 1 (a scope that leaves Aurora out), or one the TCO has no
    row for: no figure grows, and the TCO says no engine stores the tables."""
    tco = build_tco_analysis(_data(), wave_one_engine=engine)
    assert {e: r["monthly_cost_usd"] for e, r in _rows(tco).items()} == {
        e: r["monthly_cost_usd"] for e, r in _rows(build_tco_analysis(_data())).items()
    }
    assert all(r["sizing"]["unqueried_table_count"] == 0 for r in tco["cost_breakdown"])
    assert tco["unqueried_tables"] == {"table_count": 1, "storage_gb": AUDIT_GB, "stored_on": None}
    assert (
        "1 source table no in-scope query reads (30 GB) is stored on no engine in this "
        "estimate: wave 1 moves it to no engine the estimate prices"
    ) in tco["assumptions"]


def test_with_every_table_read_nothing_is_added() -> None:
    every = {
        "query_assignments": [
            {**qa, "in_scope": True} if qa["query_id"] == "q5" else qa
            for qa in ASSIGNMENT["query_assignments"]
        ]
    }
    tco = build_tco_analysis(_data(every), wave_one_engine="aurora_mysql")
    assert tco["unqueried_tables"] is None
    assert not [line for line in tco["assumptions"] if "no in-scope query reads" in line]
    assert tco["cost_breakdown"] == build_tco_analysis(_data(every))["cost_breakdown"]


def test_engine_system_objects_are_not_stored() -> None:
    """Wave 1 leaves engine-internal objects behind (``is_engine_system_object``)."""
    data = _data()
    data.collector = {
        **COLLECTOR,
        "database_schema": {
            "tables": [*TABLES, {"table_id": "information_schema.tables", "size_mb": 1.0}]
        },
    }
    assert build_tco_analysis(data, wave_one_engine="aurora_mysql")["unqueried_tables"] == {
        "table_count": 1,
        "storage_gb": AUDIT_GB,
        "stored_on": "aurora_mysql",
    }


def test_with_every_query_out_of_scope_nothing_is_stored_either() -> None:
    assignment = {
        "query_assignments": [{**qa, "in_scope": False} for qa in ASSIGNMENT["query_assignments"]]
    }
    tco = build_tco_analysis(_data(assignment), wave_one_engine="aurora_mysql")
    assert tco["projected_monthly_cost"] == 0.0
    assert tco.get("unqueried_tables") is None


def test_without_an_assignment_nothing_is_added() -> None:
    tco = build_tco_analysis(_data(None), wave_one_engine="aurora_mysql")
    assert tco.get("unqueried_tables") is None
    for engine, row in _rows(tco).items():
        assert row["monthly_cost_usd"] == _whole(engine)["cost_estimate"]["monthly_cost_usd"]


def test_synthesis_stores_them_on_wave_ones_engine(tmp_path) -> None:
    """End to end through synthesis: wave 1 moves the MySQL source to Aurora MySQL, the
    TCO stores the unread table there, and the ranking states the same figure."""
    from src.agents.referee.synthesis_handler import run_synthesis_deterministic
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    db, job = "shop", "j"
    collector = {**COLLECTOR, "metadata": {"source_database": {"engine": "mysql"}}}
    store.write_json(
        f"{db}/{job}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in ("aurora_mysql", "dynamodb")]},
    )
    store.write_json(f"{db}/{job}/collector/output.json", collector)
    for engine in ("aurora_mysql", "dynamodb"):
        store.write_json(
            f"{db}/{job}/analysis-{engine}/analysis.json",
            {
                "table_recommendations": [{"table_id": "shop.orders", "confidence_score": 80}],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
                **_whole(engine),
            },
        )
    store.write_json(
        f"{db}/{job}/assignment/v1/assignment.json",
        {
            "job_id": job,
            "version": 1,
            "query_assignments": [
                {k: v for k, v in qa.items() if k != "cache_engine"}
                for qa in ASSIGNMENT["query_assignments"]
            ],
            "table_assignments": [],
        },
    )
    result = run_synthesis_deterministic(job, db, store, assignment_version=1)
    assert result["migration_waves"][0]["engines"] == ["aurora_mysql"]
    tco = result["tco_analysis"]
    assert tco["unqueried_tables"]["stored_on"] == "aurora_mysql"
    rows = _rows(tco)
    assert rows["aurora_mysql"]["sizing"]["unqueried_table_count"] == 1
    ranked = {r["target"]: r["monthly_cost_usd"] for r in result["ranking"] if r["target"] in rows}
    assert ranked == {e: r["monthly_cost_usd"] for e, r in rows.items()}
