"""The stable additions to the ``assess()`` result before contract 1.0 ships (#1390).

``summary.source_engine``, ``assignment_version``, ``engines[]``, ``consolidations[]``
and ``tables[].reason``; ``statements[].query_type``, ``join_count`` and
``matched_patterns`` by name; the result ``label``. Each is checked against the
artifact or facts-layer figure it is read from, on the three samples and the trimmed
AdventureWorks collection (SQL Server), never against a recount of its own.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from src.agents.referee.aurora_choice import source_database_engine
from src.assessment import AssessmentResult
from src.assessment.models import LABEL_BY_PHASE, ResultLabel, StatementType
from src.shared.catalog.patterns import engine_pattern, patterns
from src.shared.report_facts import (
    assignment_table_usage,
    table_owner_line,
    table_owner_reason,
)
from src.storage.assignment_versioning import resolve_effective_assignment_version
from tests.unit.assessment.conftest import COLLECTIONS, PLACED_JOB, assessed, placed_run

SOURCE_ENGINES = {
    "wordpress": "mysql",
    "discourse": "postgresql",
    "trailgear": "sqlserver",
    "adventureworks": "sqlserver",
}


@pytest.fixture(params=COLLECTIONS)
def placed(request: pytest.FixtureRequest) -> tuple[str, Any, Any, Any]:
    result, state, store = placed_run(request.param)
    return request.param, result, state, store


# --------------------------------------------------------------------------- summary


@pytest.mark.parametrize("name", COLLECTIONS)
def test_source_engine_is_the_collections(name: str) -> None:
    result = assessed(name, "analysis")
    assert result.summary.source_engine == SOURCE_ENGINES[name]
    assert result.summary.assignment_version is None


def test_assignment_version_is_the_effective_one(placed: Any) -> None:
    name, result, state, store = placed
    assert result.summary.source_engine == source_database_engine(state.collector)
    assert result.summary.source_engine == SOURCE_ENGINES[name]
    effective = resolve_effective_assignment_version(store, name, PLACED_JOB)
    assert result.summary.assignment_version == effective == state.assignment["version"]
    # The reality check wrote a new version on top of the first assignment.
    assert result.summary.assignment_version > 1


@pytest.mark.parametrize("name", COLLECTIONS)
def test_nothing_is_placed_at_analysis(name: str) -> None:
    summary = assessed(name, "analysis").summary
    assert summary.engines == [] and summary.consolidations == []


def test_one_engine_line_per_analysed_engine(placed: Any) -> None:
    _, result, state, _ = placed
    assert [e.engine for e in result.summary.engines] == list(state.analyses)
    for e in result.summary.engines:
        assert e.statement_count == result.summary.statements_per_engine.get(e.engine, 0)
        assert e.reason.strip()


def test_engine_roles_match_the_assignment(placed: Any) -> None:
    _, result, state, _ = placed
    placed_on = {s.engine for s in result.statements if s.engine}
    cache = (state.assignment.get("cache_overlay") or {}).get("engine")
    rc = state.reality_check
    full = {c["from_engine"] for c in rc["consolidations"] if c.get("action") == "full"}
    roles = {e.engine: e.role for e in result.summary.engines}
    assert {e for e, r in roles.items() if r == "owner"} == placed_on
    assert {e for e, r in roles.items() if r == "cache_layer"} == ({cache} if cache else set())
    # Every engine a full consolidation emptied, and only those.
    assert {e for e, r in roles.items() if r == "consolidated"} == full - placed_on
    for e in result.summary.engines:
        if e.role == "consolidated":
            # The engine that took most of its statements.
            moves = [c for c in rc["consolidations"] if c["from_engine"] == e.engine]
            most = max(c["query_count"] for c in moves)
            assert e.consolidated_into in {
                c["to_engine"] for c in moves if c["query_count"] == most
            }
            assert e.statement_count == 0
        else:
            assert e.consolidated_into is None
        if e.role == "not_selected":
            assert e.statement_count == 0 and e.engine not in full


def test_engine_unique_value_is_the_reality_checks(placed: Any) -> None:
    _, result, state, _ = placed
    uva = state.reality_check["unique_value_assessment"]
    for e in result.summary.engines:
        if e.engine in uva:
            assert e.unique_queries == len(uva[e.engine]["unique_queries"])
            assert e.avg_delta == uva[e.engine]["avg_delta"]
        else:
            assert e.unique_queries is None and e.avg_delta is None


def test_engine_reasons_are_the_executive_reports(placed: Any) -> None:
    """An owner's and the cache's reason is the executive report's "why" text for it, as
    a deterministic synthesis of the same job states it in ``facts.engines``; a
    consolidated engine's is the reality check's reason for its first consolidation."""
    from src.agents.referee.synthesis_handler import run_synthesis

    name, result, state, store = placed
    version = result.summary.assignment_version
    run_synthesis(PLACED_JOB, name, store, assignment_version=version, llm_mode="none")
    report = store.read_json(f"{name}/{PLACED_JOB}/synthesis/v{version}/report.json")
    why = {e["engine"]: e["rationale"] for e in report["facts"]["engines"]}
    first: dict[str, str] = {}
    for c in state.reality_check["consolidations"]:
        first.setdefault(c["from_engine"], c["reason"])
    kept = [e for e in result.summary.engines if e.role in ("owner", "cache_layer")]
    assert {e.engine for e in kept} == set(why)
    for e in result.summary.engines:
        if e.role in ("owner", "cache_layer"):
            assert e.reason == why[e.engine]
        elif e.role == "consolidated":
            assert e.reason == first[e.engine]


def test_an_aurora_engine_not_chosen_names_the_choice() -> None:
    """A heterogeneous source keeps one Aurora engine; the other is not selected, for
    the reason the choice recorded."""
    for name in ("trailgear", "adventureworks"):
        result, state, _ = placed_run(name)
        chosen = state.assignment["aurora_engine_choice"]["engine"]
        other = ({"aurora_mysql", "aurora_postgresql"} - {chosen}).pop()
        line = next(e for e in result.summary.engines if e.engine == other)
        assert line.role == "not_selected"
        assert result.summary.relational_engine.reason.rstrip(".") in line.reason


def test_consolidations_are_the_reality_checks(placed: Any) -> None:
    _, result, state, _ = placed
    expected = [
        {k: c[k] for k in ("from_engine", "to_engine", "query_count", "reason")}
        for c in state.reality_check["consolidations"]
    ]
    assert [c.model_dump() for c in result.summary.consolidations] == expected


def test_consolidated_statements_have_a_consolidation(placed: Any) -> None:
    _, result, _, _ = placed
    pairs = {(c.from_engine, c.to_engine) for c in result.summary.consolidations}
    for s in result.statements:
        if s.consolidated_from:
            assert (s.consolidated_from, s.engine) in pairs


def test_table_reasons_are_the_facts_layers(placed: Any) -> None:
    _, result, state, _ = placed
    usage = assignment_table_usage(state.assignment)
    retained = result.summary.relational_engine.engine
    for t in result.summary.tables:
        u = usage.get(t.table_id) or {}
        writes, queries = u.get("writes") or {}, u.get("queries") or {}
        line = table_owner_line(
            writes, queries, u.get("cached_by") or [], (), retained_engine=retained
        )
        assert t.reason == table_owner_reason(line, writes, queries, retained)
        assert t.reason, t.table_id


# --------------------------------------------------------------------------- table reason


def _reason(writes: dict[str, int], queries: dict[str, int], retained: str | None = None) -> str:
    line = table_owner_line(writes, queries, [], (), retained_engine=retained)
    reason = table_owner_reason(line, writes, queries, retained)
    assert reason is not None
    return reason


def test_table_reason_by_writes() -> None:
    assert _reason({"dynamodb": 2}, {"dynamodb": 2, "aurora_mysql": 1}) == (
        "DynamoDB takes every statement that writes it (2 statements); Aurora MySQL reads "
        "a copy kept in sync from DynamoDB via CDC."
    )


def test_table_reason_by_most_queries() -> None:
    assert _reason({}, {"dynamodb": 1}) == "No statement writes it; DynamoDB serves its only query."
    assert _reason({}, {"dynamodb": 3, "aurora_mysql": 1}) == (
        "No statement writes it; DynamoDB serves 3 of its 4 queries, more than any other "
        "engine that can own a table; Aurora MySQL reads a copy kept in sync from DynamoDB "
        "via CDC."
    )


def test_table_reason_names_how_a_tie_was_settled() -> None:
    to_retained = _reason({}, {"dynamodb": 1, "aurora_mysql": 1}, retained="aurora_mysql")
    assert "Aurora MySQL serves 1 of its 2 queries, tied with DynamoDB; a tie goes to the " in (
        to_retained
    )
    by_id = _reason({}, {"dynamodb": 1, "documentdb": 1})
    assert (
        "DocumentDB serves 1 of its 2 queries, tied with DynamoDB; a tie goes to the engine "
        in (by_id)
    )
    assert "first in alphabetical order" in by_id


def test_the_tie_rule_wording_matches_the_id_order() -> None:
    """``primary_owner_engine`` settles a tie by engine id; the sentence says
    "alphabetical order", true only while ids sort as their display names do."""
    from src.shared import catalog
    from src.shared.engine_names import display_engine

    ids = sorted(catalog.engine_ids(status=catalog.ACTIVE))
    assert ids == sorted(ids, key=lambda e: display_engine(e).lower())


def test_table_reason_when_the_owner_serves_every_query() -> None:
    assert (
        _reason({}, {"dynamodb": 3})
        == "No statement writes it; DynamoDB serves all 3 of its queries."
    )


def test_table_reason_with_split_writes() -> None:
    reason = _reason({"dynamodb": 1, "aurora_mysql": 2}, {"dynamodb": 3, "aurora_mysql": 2})
    assert reason.startswith("Its writes are on Aurora MySQL and DynamoDB, so the owner is ")
    assert "DynamoDB serves 3 of its 5 queries" in reason


def test_table_reason_retained_and_none() -> None:
    assert _reason({}, {}, retained="aurora_mysql") == (
        "No engine that can own a table queries it, so it stays on Aurora MySQL, the "
        "retained relational engine."
    )
    line = table_owner_line({}, {"elasticache": 2}, ["elasticache"], ())
    assert line["owner"] is None
    assert table_owner_reason(line, {}, {"elasticache": 2}) is None


# --------------------------------------------------------------------------- statements


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
@pytest.mark.parametrize("name", COLLECTIONS)
def test_query_type_and_joins_are_the_parsers(name: str, run_through: str) -> None:
    result = assessed(name, run_through)
    _, state, _ = placed_run(name)
    by_id = {q["query_id"]: q for q in state.query_patterns}
    for s in result.statements:
        assert s.query_type == by_id[s.query_id]["query_type"]
        assert s.join_count == by_id[s.query_id]["join_count"]
    assert all(s.query_type for s in result.statements)


def test_statement_types_are_the_parsers() -> None:
    from src.contracts.collector_output import QueryType

    assert set(get_args(StatementType)) == {t.value for t in QueryType}


def test_matched_patterns_are_the_assigned_engines_by_name(placed: Any) -> None:
    _, result, state, _ = placed
    names = {p.id: p.name for p in patterns()}
    for s in result.statements:
        match: dict[str, Any] = next(
            (
                m
                for m in state.decision_traces[s.engine]["query_matches"]
                if m["query_id"] == s.query_id
            ),
            {},
        )
        ids = [engine_pattern(s.engine, cid).id for cid in match.get("matched_patterns") or []]
        assert [p.id for p in s.matched_patterns] == list(dict.fromkeys(ids))
        for p in s.matched_patterns:
            assert names[p.id] == p.name
    assert any(s.matched_patterns for s in result.statements)


@pytest.mark.parametrize("name", COLLECTIONS)
def test_no_matched_patterns_before_placement(name: str) -> None:
    assert all(s.matched_patterns == [] for s in assessed(name, "analysis").statements)


def test_an_unservable_statement_matches_no_pattern() -> None:
    from src.assessment import assess
    from tests.unit.assessment.conftest import sample_collection

    result = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"target_engines": ["dynamodb"]},
        }
    )
    unservable = [s for s in result.statements if s.reason_code == "unservable"]
    assert unservable
    assert all(s.matched_patterns == [] for s in unservable)
    roles = {e.engine: e.role for e in result.summary.engines}
    assert roles == {"dynamodb": "owner"}


def test_every_catalog_pattern_has_a_name() -> None:
    entries = patterns()
    assert all(p.name.strip() == p.name and p.name for p in entries)
    assert len({p.name for p in entries}) == len(entries)


def test_an_unknown_catalog_id_is_refused() -> None:
    from src.assessment.result_builder import _named_patterns

    assert engine_pattern("dynamodb", "no-such-id") is None
    with pytest.raises(ValueError, match="no pattern file"):
        _named_patterns("dynamodb", ["no-such-id"])
    assert _named_patterns(None, ["dynamodb-01"]) == []


# --------------------------------------------------------------------------- label


@pytest.mark.parametrize("name", COLLECTIONS)
def test_label_follows_the_phase(name: str) -> None:
    assert assessed(name, "analysis").label == "analysis_only"
    assert assessed(name, "reality_check").label == "recommendation"


def test_label_values() -> None:
    assert set(get_args(ResultLabel)) == {"analysis_only", "recommendation", "designed"}
    assert LABEL_BY_PHASE == {
        "analysis": "analysis_only",
        "reality_check": "recommendation",
        "schema_design": "designed",
        "synthesis": "designed",
    }


@pytest.mark.parametrize(
    ("run_through", "wrong"),
    [
        ("analysis", "recommendation"),
        ("analysis", "designed"),
        ("reality_check", "analysis_only"),
        ("reality_check", "designed"),
    ],
)
def test_a_label_that_does_not_match_the_phase_is_refused(run_through: str, wrong: str) -> None:
    dumped = assessed("wordpress", run_through).model_dump(mode="json")
    dumped["label"] = wrong
    with pytest.raises(ValidationError, match="label must be"):
        AssessmentResult.model_validate(dumped)


def test_designed_only_with_a_schema_design() -> None:
    dumped = assessed("wordpress", "reality_check").model_dump(mode="json")
    for phase in ("schema_design", "synthesis"):
        dumped["summary"]["run_through_reached"] = phase
        dumped["summary"]["designed_engines"] = ["dynamodb"]
        dumped["label"] = "designed"
        assert AssessmentResult.model_validate(dumped).label == "designed"
        dumped["label"] = "recommendation"
        with pytest.raises(ValidationError, match="label must be 'designed'"):
            AssessmentResult.model_validate(dumped)
        # No engine designed: the result carries no data model, so it stays a
        # recommendation (#1387 review).
        dumped["summary"]["designed_engines"] = []
        assert AssessmentResult.model_validate(dumped).label == "recommendation"
        dumped["label"] = "designed"
        with pytest.raises(ValidationError, match="label must be 'recommendation'"):
            AssessmentResult.model_validate(dumped)


def test_new_reasons_have_no_internal_wording(placed: Any) -> None:
    """The customer-facing sentences this release adds name engines by their display
    names and carry no internal wording (the deliverables' own guard)."""
    from src.tools.validation.customer_text_guard import scan_text

    _, result, _, _ = placed
    texts = [(f"engines.{e.engine}", e.reason) for e in result.summary.engines]
    texts += [(f"consolidations.{c.from_engine}", c.reason) for c in result.summary.consolidations]
    texts += [(f"tables.{t.table_id}", t.reason or "") for t in result.summary.tables]
    findings = [f for path, text in texts for f in scan_text(text, path)]
    assert findings == [], [(f.kind, f.path, f.excerpt) for f in findings]


def test_a_reality_check_run_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A normal run warns about nothing: schema design has not run, so its outputs are
    never reported missing (#1390 review)."""
    import logging

    from src.assessment import assess
    from tests.unit.assessment.conftest import collection

    with caplog.at_level(logging.WARNING):
        assess({"database_name": "trailgear", "collection": collection("trailgear")})
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_synthesis_still_warns_about_a_missing_design(caplog: pytest.LogCaptureFixture) -> None:
    """Synthesis itself, which expects schema design, keeps the warning."""
    import logging

    from src.agents.referee.synthesis_data import load_synthesis_data

    result, _, store = placed_run("trailgear")
    version = result.summary.assignment_version
    with caplog.at_level(logging.WARNING):
        load_synthesis_data(store, PLACED_JOB, "trailgear", version)
    assert any("schema_output.json" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        load_synthesis_data(store, PLACED_JOB, "trailgear", version, schema_design_expected=False)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
