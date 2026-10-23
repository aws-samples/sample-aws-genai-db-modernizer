"""``assess()`` on the committed samples, at both ``run_through`` values (#1182).

The result is read from the internal contracts the run wrote, so each check compares
it with those contracts or with the facts layer, never with a recount of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.assessment import AssessmentResult, assess
from src.assessment.result_builder import load_run_state
from src.shared.report_facts import assignment_table_usage, table_owner_line
from src.storage.local_store import LocalArtifactStore
from tests.unit.assessment.conftest import SAMPLES, assessed, placed_run, sample_collection

JOB = "assess01"


@pytest.fixture(params=SAMPLES)
def placed(request: pytest.FixtureRequest) -> Any:
    """A ``run_through="reality_check"`` run that keeps its artifacts, per sample."""
    result, state, _ = placed_run(request.param)
    return request.param, result, state


@pytest.mark.parametrize("name", SAMPLES)
def test_analysis_places_nothing_and_scores_every_statement(name: str) -> None:
    result = assessed(name, "analysis")
    collected = sample_collection(name)
    assert result.summary.run_through_reached == "analysis"
    assert result.summary.statement_count == len(result.statements) > 0
    assert result.summary.statements_per_engine == {}
    assert result.summary.tables == []
    assert all(s.engine is None and s.confidence is None for s in result.statements)
    assert all(s.reason_code is None and s.reason_text is None for s in result.statements)
    assert all(s.fit for s in result.statements)
    # Every collected statement shape is there once (rows sharing a digest merge).
    assert len({s.query_id for s in result.statements}) == len(result.statements)
    assert len(result.statements) <= len(collected["queries"])
    assert all(g.engine is None for g in result.groups)
    assert result.artifacts is None


def test_reality_check_places_every_statement(placed: Any) -> None:
    name, result, state = placed
    assert result.summary.run_through_reached == "reality_check"
    assert all(s.engine and s.reason_code for s in result.statements)
    assert sum(result.summary.statements_per_engine.values()) == len(result.statements)
    # With no engine scope, nothing is unservable.
    assert result.summary.unservable_count == 0


def test_statement_count_is_placed_plus_unservable(placed: Any) -> None:
    _, result, state = placed
    in_scope = [qa for qa in state.assignment["query_assignments"] if qa.get("in_scope", True)]
    unservable = state.assignment.get("unservable_queries") or []
    assert result.summary.statement_count == len(in_scope) + len(unservable)
    assert result.summary.unservable_count == len(unservable)


def test_statements_carry_the_assignment(placed: Any) -> None:
    _, result, state = placed
    by_id = {qa["query_id"]: qa for qa in state.assignment["query_assignments"]}
    for s in result.statements:
        qa = by_id[s.query_id]
        assert s.engine == qa["assigned_engine"]
        assert s.confidence == qa["confidence"]
        assert s.tables == qa["source_tables"]
        assert s.tied_engines == qa["tied_engines"]
        assert s.reason_text == qa["assignment_reason"]


def test_fit_is_the_routing_ladder(placed: Any) -> None:
    """``fit`` is the scorer's score routing recorded on each statement's first Move."""
    _, result, state = placed
    first: dict[str, dict[str, int]] = {}
    for move in state.lineage[0]["moves"]:
        if move["query_id"] not in first and move["evidence"]["candidates"]:
            first[move["query_id"]] = {
                c["engine"]: int(c["score"])
                for c in move["evidence"]["candidates"]
                if c["score"] is not None
            }
    for s in result.statements:
        assert s.fit == first[s.query_id]
        assert s.engine in s.fit


def test_analysis_fit_agrees_with_routing(placed: Any) -> None:
    """Before placement the same scorer gives the same scores. Placement can only add
    an engine (a capability waiver that needs the table's writes placed) or drop one
    (the Aurora engine a heterogeneous source does not keep)."""
    from src.agents.referee.aurora_choice import AURORA_ENGINES
    from src.agents.referee.query_scoring import ScoringContext

    name, result, state = placed
    before = {s.query_id: s.fit for s in assessed(name, "analysis").statements}
    assert set(before) == {s.query_id for s in result.statements}
    # The waivers read each written table's source of truth: its owner by its writes.
    sources = {
        t.table_id: t.owner
        for t in result.summary.tables
        if t.owner and t.owner_basis in ("writes", "split_writes")
    }
    ctx = ScoringContext.from_pipeline(
        state.analyses, state.triage, state.collector
    ).with_sources_of_truth(sources)
    patterns = {q["query_id"]: q for q in state.query_patterns}
    choice = state.assignment.get("aurora_engine_choice") or {}
    not_kept = (AURORA_ENGINES - {choice["engine"]}) if choice else set()
    for s in result.statements:
        shared = set(before[s.query_id]) & set(s.fit)
        assert {e: before[s.query_id][e] for e in shared} == {e: s.fit[e] for e in shared}
        for engine in set(s.fit) - set(before[s.query_id]):
            # Added: a catalog waiver lifts the engine's gate once the writes are placed.
            assert ctx.capability_waivers(patterns[s.query_id], engine), (s.query_id, engine)
        # Dropped: only the Aurora engine the heterogeneous choice did not keep.
        assert set(before[s.query_id]) - set(s.fit) <= not_kept, s.query_id


def test_groups_are_the_assignments(placed: Any) -> None:
    name, result, state = placed
    co_dep = [g for g in result.groups if g.kind == "co_dependency"]
    writes = [g for g in result.groups if g.kind == "write_group"]
    assert [g.query_ids for g in co_dep] == state.assignment["co_dependency_groups"]
    expected = {t: m for t, m in state.assignment["write_groups"].items() if len(m) >= 2}
    assert {g.tables[0]: g.query_ids for g in writes} == expected
    engines = {s.query_id: s.engine for s in result.statements}
    for g in result.groups:
        # A group is placed as one unit.
        assert g.engine is not None
        assert {engines[q] for q in g.query_ids} == {g.engine}
    # The same groups before anything is placed, from the same builders.
    before = assessed(name, "analysis").groups
    assert [(g.group_id, g.query_ids) for g in before] == [
        (g.group_id, g.query_ids) for g in result.groups
    ]


def test_group_reason_codes_name_a_group(placed: Any) -> None:
    """A statement whose reason is its group is a member of a group of that kind."""
    _, result, _ = placed
    members = {
        kind: {q for g in result.groups if g.kind == kind for q in g.query_ids}
        for kind in ("write_group", "co_dependency")
    }
    for s in result.statements:
        if s.reason_code in members:
            assert s.query_id in members[s.reason_code], (s.query_id, s.reason_code)


@pytest.mark.passes_on_main
def test_summary_tables_are_the_facts_layer_lines(placed: Any) -> None:
    _, result, state = placed
    usage = assignment_table_usage(state.assignment)
    retained = result.summary.relational_engine.engine
    expected = []
    for table in sorted({t["table_id"] for t in state.assignment["table_assignments"]}):
        u = usage.get(table) or {}
        line = table_owner_line(
            u.get("writes") or {},
            u.get("queries") or {},
            u.get("cached_by") or [],
            (),
            retained_engine=retained,
        )
        expected.append({"table_id": table, **line})
    # The reason sentence is checked in test_assess_additions.py.
    assert [t.model_dump(exclude={"reason"}) for t in result.summary.tables] == expected
    assert result.summary.tables


def test_relational_engine(placed: Any) -> None:
    name, result, state = placed
    chosen = {"wordpress": "aurora_mysql", "discourse": "aurora_postgresql"}
    if name in chosen:
        # A homogeneous source keeps its own Aurora engine, at both phases.
        assert result.summary.relational_engine.engine == chosen[name]
        assert assessed(name, "analysis").summary.relational_engine == (
            result.summary.relational_engine
        )
    else:
        # A heterogeneous source: routing's recorded choice; none before placement.
        choice = state.assignment["aurora_engine_choice"]
        assert result.summary.relational_engine.engine == choice["engine"]
        assert result.summary.relational_engine.reason == choice["reason"]
        assert assessed(name, "analysis").summary.relational_engine is None


def test_consolidated_statements_name_the_removed_engine(placed: Any) -> None:
    _, result, state = placed
    moved = {
        (c["from"] if "from" in c else c.get("from_engine"))
        for c in (state.reality_check or {}).get("consolidations") or []
    }
    consolidated = [s for s in result.statements if s.reason_code == "consolidated"]
    for s in consolidated:
        assert s.consolidated_from in moved
        assert s.consolidated_from != s.engine
    assert all(s.consolidated_from is None for s in result.statements if s not in consolidated)


def test_signals_and_patterns_come_from_triage_and_traces(placed: Any) -> None:
    _, result, state = placed
    by_id = {s.query_id: s for s in result.statements}
    for signal in state.triage["signals"]:
        for qid in signal.get("query_ids") or []:
            if qid in by_id:
                assert signal["signal"] in by_id[qid].signals
    from src.shared.catalog.patterns import engine_pattern

    for s in result.statements:
        trace = state.decision_traces[s.engine]
        match: dict[str, Any] = next(
            (m for m in trace["query_matches"] if m["query_id"] == s.query_id), {}
        )
        expected = [engine_pattern(s.engine, cid).id for cid in match.get("matched_patterns") or []]
        assert [p.id for p in s.matched_patterns] == list(dict.fromkeys(expected))


def test_unique_delta_threshold_is_the_reality_checks(placed: Any) -> None:
    from src.agents.referee.reality_check_rules import UNIQUE_DELTA_THRESHOLD

    _, result, _ = placed
    assert result.summary.unique_delta_threshold == UNIQUE_DELTA_THRESHOLD


def test_versions_are_reported(placed: Any) -> None:
    from src.shared.package_version import get_package_version

    _, result, _ = placed
    assert result.contract_version == "1.0"
    assert result.library_version == get_package_version()
    assert result.ruleset_version == result.library_version
    assert result.ruleset_hash.startswith("sha256:") and len(result.ruleset_hash) == 71


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_same_input_same_result(run_through: str) -> None:
    """With no model, the same request on the same library gives the same result."""
    first = assessed("wordpress", run_through)
    again = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"run_through": run_through},
        }
    )
    assert again.model_dump() == first.model_dump()


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_json_round_trip(run_through: str) -> None:
    result = assessed("trailgear", run_through)
    text = result.model_dump_json()
    assert AssessmentResult.model_validate_json(text) == result


def test_artifacts_only_on_request(tmp_path: Path) -> None:
    store = LocalArtifactStore(str(tmp_path))
    result = assess(
        {
            "database_name": "trailgear",
            "collection": sample_collection("trailgear"),
            "options": {"include_artifacts": True},
        },
        store=store,
        job_id=JOB,
    )
    state = load_run_state(store, "trailgear", JOB, "reality_check")
    art = result.artifacts
    assert art is not None
    assert art.compatibility == "none: internal contracts, may change in any release"
    dumped = art.model_dump()
    # Verbatim, each with its own internal contract version.
    assert dumped["collector_output"] == state.collector
    assert dumped["triage"] == state.triage
    assert dumped["analyses"] == state.analyses
    assert dumped["decision_traces"] == state.decision_traces
    assert dumped["assignment"] == state.assignment
    assert dumped["reality_check"] == state.reality_check
    assert all("contract_version" in a for a in dumped["analyses"].values())
    assert AssessmentResult.model_validate_json(result.model_dump_json()) == result
    assert assessed("trailgear", "reality_check").artifacts is None


def test_analysis_artifacts_have_no_assignment() -> None:
    result = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"run_through": "analysis", "include_artifacts": True},
        }
    )
    dumped = result.artifacts.model_dump()
    assert {"collector_output", "triage", "analyses"} <= set(dumped)
    assert "assignment" not in dumped and "reality_check" not in dumped


def test_rewritten_statements_are_marked() -> None:
    first = assessed("wordpress", "analysis").statements[0].query_id
    result = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"run_through": "analysis"},
            "scope": {"tables": ["wordpress.wp_posts"], "rewritten_query_ids": [first]},
        }
    )
    assert [s.query_id for s in result.statements if s.rewritten_by_caller] == [first]
    # Echoed unchanged; no rule reads it.
    assert result.scope.tables == ["wordpress.wp_posts"]
    assert [s.fit for s in result.statements] == [
        s.fit for s in assessed("wordpress", "analysis").statements
    ]


def test_engine_scope_limits_every_statement() -> None:
    result = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"target_engines": ["DynamoDB ", "aurora_mysql", "dynamodb"]},
        }
    )
    assert result.options.target_engines == ["aurora_mysql", "dynamodb"]
    assert {s.engine for s in result.statements} <= {"aurora_mysql", "dynamodb"}
    for s in result.statements:
        assert set(s.fit) <= {"aurora_mysql", "dynamodb"}
        # Engines outside the scope are ruled out for every statement.
        assert {"documentdb", "elasticache", "opensearch"} <= set(s.excluded)
    assert {t.owner for t in result.summary.tables} <= {"aurora_mysql", "dynamodb"}


def test_unservable_statements_are_placed_nowhere(tmp_path: Path) -> None:
    """A scope no engine of which can serve a statement leaves it unservable: no engine,
    its reason, never counted against an engine (ADR-031 Decision 2 Amendment 15)."""
    store = LocalArtifactStore(str(tmp_path))
    result = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"target_engines": ["dynamodb"]},
        },
        store=store,
        job_id=JOB,
    )
    state = load_run_state(store, "wordpress", JOB, "reality_check")
    recorded = {u["query_id"]: u for u in state.assignment["unservable_queries"]}
    assert recorded
    unservable = [s for s in result.statements if s.reason_code == "unservable"]
    assert {s.query_id for s in unservable} == set(recorded)
    for s in unservable:
        uq = recorded[s.query_id]
        assert s.engine is None and s.confidence is None and s.tied_engines == []
        assert s.unservable_reason == uq["reason"]
        assert s.tables == uq["source_tables"]
        assert {g["engine"] for g in uq["gates"]} <= set(s.excluded)
    placed = [s for s in result.statements if s.reason_code != "unservable"]
    assert all(s.engine == "dynamodb" and s.unservable_reason is None for s in placed)
    assert result.summary.unservable_count == len(recorded)
    assert result.summary.statements_per_engine == {"dynamodb": len(placed)}
    assert result.summary.statement_count == len(placed) + len(recorded)
    # No group holds a statement that is placed nowhere next to one that is placed.
    engines = {s.query_id: s.engine for s in result.statements}
    for g in result.groups:
        assert len({engines[q] for q in g.query_ids}) == 1


def test_reality_check_record_follows_the_lineage(tmp_path: Path) -> None:
    """The result carries the reality-check run the effective assignment descends from:
    after the assignment is resolved again (a new lineage), the earlier run's record is
    not reported, and the new lineage's run is once it has one."""
    from src.atx_orchestrator.core import run_assignment_core, run_reality_check_core

    store = LocalArtifactStore(str(tmp_path))
    first = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"include_artifacts": True},
        },
        store=store,
        job_id=JOB,
    )
    first_run = first.artifacts.model_dump()["reality_check"]
    assert first_run["consolidations"]

    # A second resolution starts a new lineage, with no reality-check run of its own yet.
    run_assignment_core(JOB, "wordpress", store=store)
    state = load_run_state(store, "wordpress", JOB, "reality_check")
    assert state.lineage[0]["previous_version"] is None and len(state.lineage) == 1
    assert state.reality_check is None

    run_reality_check_core(JOB, "wordpress", store=store, llm_mode="none")
    state = load_run_state(store, "wordpress", JOB, "reality_check")
    assert state.reality_check is not None
    assert state.reality_check["output_assignment_version"] == state.assignment["version"]
    assert state.reality_check["output_assignment_version"] != (
        first_run["output_assignment_version"]
    )
