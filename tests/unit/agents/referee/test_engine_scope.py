"""A customer's engine scope (ADR-031 Decision 2 Amendment 14, #1327).

The candidates are Decision 2 step 1's, limited to the scope. When the scope leaves
out a MySQL or PostgreSQL source's source-compatible Aurora engine and lists the other
one, that one is the relational candidate; a scope listing both keeps the
source-compatible engine as the one relational candidate. No scope routes exactly as
before, and no engine outside the scope is ever assigned.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.engine_scope import (
    SCOPE_SKIPPED_REASON,
    EngineScopeError,
    OutOfScopeAssignment,
    check_in_scope,
    normalize_engine_scope,
)
from src.agents.referee.migration_waves import build_migration_waves
from src.agents.referee.table_assignments import retained_engine_for
from src.agents.referee.triage import ANALYSIS_AGENTS, triage
from src.agents.referee.triage_handler import run_triage
from src.contracts.assignment_models import Assignment, AuroraEngineChoice
from src.contracts.triage_output import TriageOutputContract
from src.shared.catalog.gaps import gap_rules
from src.shared.report_facts import heterogeneous_wave_note
from src.storage.local_store import LocalArtifactStore
from src.tools.analysis.relational_gaps import detect_relational_gaps

DDB_PG = ["dynamodb", "aurora_postgresql"]


def _collector(engine: str | None, queries: list[dict], version: str = "8.0.36") -> dict:
    collector: dict = {
        "job_id": "t",
        "database_schema": {
            "tables": [
                {
                    "table_id": "app.orders",
                    "table_name": "orders",
                    "columns": [{"column_name": "id", "data_type": "int"}],
                    "foreign_keys": [],
                }
            ]
        },
        "queries": {"query_patterns": queries},
    }
    if engine is not None:
        collector["metadata"] = {"source_database": {"engine": engine, "version": version}}
    return collector


def _query(qid: str = "q1", text: str = "SELECT id, total FROM orders WHERE customer_ref = ?"):
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "tables_accessed": ["app.orders"],
        "join_count": 0,
        "has_joins": False,
        "calls_per_second": 1.0,
        "rows_returned_avg": 50,
    }


def _analysis(confidence: int = 70) -> dict:
    return {
        "table_recommendations": [{"table_id": "app.orders", "confidence_score": confidence}],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


def _triage_doc(result, scope: list[str] | None) -> dict:
    return {
        "selected_agents": [{"agent_type": e} for e in result.selected],
        "signals": [],
        "query_capabilities": result.query_capabilities,
        "engine_scope": scope,
    }


class TestTheScopeIsValidated:
    def test_no_scope_is_none(self):
        assert normalize_engine_scope(None) is None

    def test_a_scope_is_sorted(self):
        assert normalize_engine_scope(["dynamodb", "aurora_postgresql"]) == (
            "aurora_postgresql",
            "dynamodb",
        )

    @pytest.mark.parametrize(
        ("scope", "says"),
        [
            ([], "empty"),
            (["dynamodb", "cassandra"], "cassandra"),
            # The generic legacy id and a future placeholder are not routing targets.
            (["aurora"], "aurora"),
            (["neptune"], "neptune"),
        ],
    )
    def test_an_empty_or_unknown_scope_is_refused(self, scope, says):
        with pytest.raises(EngineScopeError, match=says):
            normalize_engine_scope(scope)

    def test_case_and_repeats_follow_the_one_setting_rule(self):
        # #1328: the library path uses the target_databases rule (one validation).
        assert normalize_engine_scope(["DynamoDB", "dynamodb", " aurora_postgresql"]) == (
            "aurora_postgresql",
            "dynamodb",
        )

    def test_the_library_and_the_setting_refuse_with_the_same_error(self):
        from src.shared.target_databases import TargetDatabasesError

        assert EngineScopeError is TargetDatabasesError

    def test_triage_refuses_an_invalid_scope(self):
        with pytest.raises(EngineScopeError):
            triage(_collector("mysql", [_query()]), engine_scope=["nope"])


class TestTriage:
    def test_no_scope_is_todays_selection(self):
        collector = _collector("mysql", [_query()])
        plain, unscoped = triage(collector), triage(collector, engine_scope=None)
        assert plain.selected == unscoped.selected
        assert plain.skipped == unscoped.skipped
        assert unscoped.engine_scope is None

    @pytest.mark.parametrize("source", ["mysql", "postgresql", "sqlserver", None])
    def test_a_scope_of_every_engine_selects_what_no_scope_selects(self, source):
        collector = _collector(source, [_query()])
        everything = triage(collector, engine_scope=sorted(ANALYSIS_AGENTS))
        plain = triage(collector)
        assert everything.selected == plain.selected
        assert everything.skipped == plain.skipped

    def test_a_mysql_source_takes_aurora_postgresql_when_the_scope_leaves_out_aurora_mysql(self):
        result = triage(_collector("mysql", [_query()]), engine_scope=DDB_PG)
        assert set(result.selected) == set(DDB_PG)
        assert result.engine_scope == sorted(DDB_PG)
        (reason,) = [r for r in result.selected["aurora_postgresql"] if "scope" in r]
        assert "Aurora MySQL" in reason and "source-compatible" in reason
        for engine in ANALYSIS_AGENTS - set(DDB_PG):
            assert result.skipped[engine] == SCOPE_SKIPPED_REASON

    def test_a_postgresql_source_keeps_its_own_engine(self):
        result = triage(_collector("postgresql", [_query()]), engine_scope=DDB_PG)
        assert set(result.selected) == set(DDB_PG)
        assert not any("scope" in r for r in result.selected["aurora_postgresql"])

    def test_a_postgresql_source_takes_aurora_mysql_when_the_scope_leaves_out_its_own(self):
        result = triage(
            _collector("postgresql", [_query()]), engine_scope=["aurora_mysql", "dynamodb"]
        )
        assert set(result.selected) == {"aurora_mysql", "dynamodb"}
        assert result.skipped["aurora_postgresql"] == SCOPE_SKIPPED_REASON

    def test_a_scope_listing_both_aurora_engines_keeps_the_source_compatible_one_only(self):
        both = ["aurora_mysql", "aurora_postgresql", "dynamodb"]
        result = triage(_collector("mysql", [_query()]), engine_scope=both)
        assert set(result.selected) == {"aurora_mysql", "dynamodb"}
        assert result.skipped["aurora_postgresql"] == (
            "Source engine does not match this Aurora variant"
        )

    def test_a_heterogeneous_source_analyses_only_the_aurora_engine_in_scope(self):
        result = triage(_collector("sqlserver", [_query()]), engine_scope=DDB_PG)
        assert set(result.selected) == set(DDB_PG)
        assert result.skipped["aurora_mysql"] == SCOPE_SKIPPED_REASON

    def test_a_scope_with_no_relational_engine_analyses_none(self):
        result = triage(_collector("mysql", [_query()]), engine_scope=["dynamodb"])
        assert set(result.selected) == {"dynamodb"}
        assert result.skipped["aurora_mysql"] == SCOPE_SKIPPED_REASON

    def test_elasticache_joins_only_in_scope_and_on_its_own_signal(self):
        hot = _query("q1", "SELECT id, total FROM orders WHERE id = ?")
        hot.update(calls_per_second=5000.0, rows_returned_avg=1)
        collector = _collector("mysql", [hot])
        signalled = "elasticache" in triage(collector).selected
        assert signalled, "the fixture must fire a cache signal"
        out = triage(collector, engine_scope=DDB_PG)
        assert out.skipped["elasticache"] == SCOPE_SKIPPED_REASON
        listed = triage(collector, engine_scope=[*DDB_PG, "elasticache"])
        assert "elasticache" in listed.selected
        quiet = triage(_collector("mysql", [_query()]), engine_scope=[*DDB_PG, "elasticache"])
        assert quiet.skipped["elasticache"] == "No matching workload signals detected"


class TestTheTriageRecord:
    def test_run_triage_records_the_scope(self, tmp_path):
        store = LocalArtifactStore(str(tmp_path))
        store.write_json("db/j/collector/output.json", _collector("mysql", [_query()]))
        run_triage("j", "db", store, engine_scope=DDB_PG)
        doc = store.read_json("db/j/referee-triage/triage.json")
        assert doc["engine_scope"] == sorted(DDB_PG)
        skipped = {s["agent_type"]: s["reason"] for s in doc["skipped_agents"]}
        assert skipped["aurora_mysql"] == SCOPE_SKIPPED_REASON

    def test_run_triage_without_a_scope_records_none(self, tmp_path):
        store = LocalArtifactStore(str(tmp_path))
        store.write_json("db/j/collector/output.json", _collector("mysql", [_query()]))
        run_triage("j", "db", store)
        assert store.read_json("db/j/referee-triage/triage.json")["engine_scope"] is None

    def test_a_triage_written_before_the_scope_loads_with_none(self):
        old = {
            "contract_version": "1.3",
            "job_id": "j",
            "database_name": "db",
            "selected_agents": [{"agent_type": "dynamodb", "reasons": ["x"]}],
            "skipped_agents": [],
            "signals": [],
            "confidence_score": 50,
            "timestamp": "2026-10-01T00:00:00Z",
        }
        assert TriageOutputContract.model_validate(old).engine_scope is None


@pytest.mark.usefixtures("table_confidence_scores")
class TestTheFirstAssignment:
    def _resolve(self, source: str | None, scope: list[str] | None, queries=None):
        collector = _collector(source, queries or [_query("q1")])
        result = triage(collector, engine_scope=scope)
        analyses = {e: _analysis() for e in result.selected}
        return AssignmentResolver().resolve(_triage_doc(result, scope), analyses, collector)

    def test_a_scope_swap_is_recorded_as_the_customers_aurora_choice(self):
        assignment = self._resolve("mysql", DDB_PG)
        choice = assignment.aurora_engine_choice
        assert choice is not None
        assert choice.engine == "aurora_postgresql"
        assert choice.basis == "engine_scope"
        assert "Aurora MySQL" in choice.reason and "scope" in choice.reason
        assert {qa.assigned_engine for qa in assignment.query_assignments} <= set(DDB_PG)

    def test_a_heterogeneous_source_with_one_aurora_engine_in_scope_records_it(self):
        assignment = self._resolve("oracle", DDB_PG)
        choice = assignment.aurora_engine_choice
        assert choice is not None and choice.engine == "aurora_postgresql"
        assert choice.basis == "engine_scope"

    @pytest.mark.parametrize("scope", [None, DDB_PG])
    def test_a_source_that_keeps_its_own_engine_records_no_choice(self, scope):
        assert self._resolve("postgresql", scope).aurora_engine_choice is None

    def test_no_scope_records_no_choice_for_a_homogeneous_source(self):
        assert self._resolve("mysql", None).aurora_engine_choice is None

    def test_the_utility_pin_lands_on_the_in_scope_aurora_engine_not_as_source_compatible(self):
        queries = [_query("q1"), _query("q2", "SHOW TABLES")]
        assignment = self._resolve("mysql", DDB_PG, queries)
        qa = next(q for q in assignment.query_assignments if q.query_id == "q2")
        assert qa.assigned_engine == "aurora_postgresql"
        assert "chosen relational engine" in qa.assignment_reason
        pin = next(
            m for m in assignment.moves if m.query_id == "q2" and m.to_engine == qa.assigned_engine
        )
        assert not pin.evidence.source_compatible

    def test_a_query_no_engine_in_scope_can_serve_stops_the_run_naming_it(self):
        # SHOW TABLES has no in-scope relational engine to be pinned to; the Aurora
        # fallback would put it on Aurora MySQL, outside the scope.
        with pytest.raises(OutOfScopeAssignment, match="q2") as raised:
            self._resolve(
                "mysql", ["dynamodb", "documentdb"], [_query("q1"), _query("q2", "SHOW TABLES")]
            )
        assert "q2: Aurora MySQL is outside the scope" in str(raised.value)

    def test_the_same_query_without_a_scope_still_falls_back(self):
        assignment = self._resolve(None, None, [_query("q2", "SHOW TABLES")])
        assert assignment.query_assignments


class TestAScopeOfEveryEngineRoutesAsNoScope:
    """A scope listing every engine, both Aurora engines included, keeps the
    source-compatible engine as the one relational candidate, so it routes exactly as
    no scope: the same analyses, the same engines, ties and Moves, and no choice."""

    @staticmethod
    def _routing(assignment) -> list:
        return [
            (qa.query_id, qa.assigned_engine, list(qa.tied_engines or []))
            for qa in assignment.query_assignments
        ] + [(m.query_id, m.to_engine, m.reason_code, m.score_after) for m in assignment.moves]

    @pytest.mark.usefixtures("table_confidence_scores")
    @pytest.mark.parametrize("source", ["mysql", "postgresql", "sqlserver", None])
    def test_the_first_assignment_is_the_same(self, source):
        queries = [
            _query("q1"),
            _query("q2", "SELECT id, total FROM orders WHERE id = ?"),
            _query("q3", "SHOW TABLES"),
        ]
        collector = _collector(source, queries)
        scores = {"aurora_mysql": 70, "aurora_postgresql": 70, "dynamodb": 70}
        out = {}
        for label, scope in (("none", None), ("every", sorted(ANALYSIS_AGENTS))):
            result = triage(collector, engine_scope=scope)
            analyses = {e: _analysis(scores.get(e, 60)) for e in result.selected}
            out[label] = AssignmentResolver().resolve(
                _triage_doc(result, scope), analyses, collector
            )
        assert self._routing(out["every"]) == self._routing(out["none"])
        assert out["every"].aurora_engine_choice == out["none"].aurora_engine_choice


@pytest.fixture(scope="module")
def discourse_collector() -> dict:
    """The bundled discourse sample (a PostgreSQL source), ingested as the collect
    step does, without running the pipeline."""
    import json
    import zipfile
    from pathlib import Path

    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.tools.database.offline_parser import parse_offline_collection

    zip_path = Path(__file__).resolve().parents[4] / "docs/examples/discourse/discourse.zip"
    with zipfile.ZipFile(zip_path) as z:
        raw = json.loads(z.read("discourse-collection.json"))
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="t", database_name="discourse", s3_key="t"
    )


DDB_MY = ["aurora_mysql", "dynamodb"]


class TestTheDiscourseCollection:
    """A PostgreSQL source whose scope leaves out Aurora PostgreSQL and lists Aurora
    MySQL, on the bundled discourse collection (moved out of the e2e suite, whose
    discourse shard is the slowest)."""

    def test_triage_takes_aurora_mysql_in_place_of_aurora_postgresql(self, discourse_collector):
        result = triage(discourse_collector, engine_scope=DDB_MY)
        assert set(result.selected) == set(DDB_MY)
        (reason,) = [r for r in result.selected["aurora_mysql"] if "scope" in r]
        assert "Aurora PostgreSQL" in reason and "source-compatible" in reason
        for engine in ANALYSIS_AGENTS - set(DDB_MY):
            assert result.skipped[engine] == SCOPE_SKIPPED_REASON

    def test_a_scope_of_every_engine_selects_what_no_scope_selects(self, discourse_collector):
        everything = triage(discourse_collector, engine_scope=sorted(ANALYSIS_AGENTS))
        plain = triage(discourse_collector)
        assert everything.selected == plain.selected
        assert everything.skipped == plain.skipped
        assert everything.query_capabilities == plain.query_capabilities

    def test_the_scope_chooses_aurora_mysql_and_wave_one_retains_it(self, discourse_collector):
        from src.agents.referee.engine_scope import scope_aurora_choice

        choice = scope_aurora_choice("postgresql", DDB_MY, frozenset(DDB_MY))
        assert choice is not None
        assert (choice.engine, choice.basis) == ("aurora_mysql", "engine_scope")
        assert retained_engine_for(discourse_collector, [], choice) == "aurora_mysql"
        assert retained_engine_for(discourse_collector, [], None) == "aurora_postgresql"

    def test_wave_one_lists_the_conversions_feature_gaps(self, discourse_collector):
        # #1330: the gap catalog checks PostgreSQL to Aurora MySQL, so wave 1 lists the
        # gaps it found instead of saying they were not checked.
        gaps = detect_relational_gaps(discourse_collector, "aurora_mysql")
        assert gaps is not None and gaps["feature_rules_checked"] > 0
        found = {g["rule_id"] for g in gaps["gaps"]}
        assert {"postgresql-returning-clause", "postgresql-on-conflict"} <= found
        wave = {"engines": ["aurora_mysql"], "moves_from": ["postgresql"]}
        note = heterogeneous_wave_note(wave, 311, gaps)
        assert "from PostgreSQL to Aurora MySQL" in note
        assert f"The relational gap check found {len(gaps['gaps'])} gaps" in note
        assert "no feature rules" not in note


class TestTheGuard:
    def test_no_scope_checks_nothing(self):
        check_in_scope([{"query_id": "q1", "assigned_engine": "aurora_mysql"}], None, "x")

    def test_an_in_scope_assignment_passes(self):
        check_in_scope(
            [{"query_id": "q1", "assigned_engine": "dynamodb", "tied_engines": []}], DDB_PG, "x"
        )

    def test_a_cache_outside_the_scope_is_refused_too(self):
        qa = {"query_id": "q1", "assigned_engine": "dynamodb", "cache_engine": "elasticache"}
        with pytest.raises(
            OutOfScopeAssignment, match="q1: ElastiCache, its cache, is outside the scope"
        ):
            check_in_scope([qa], DDB_PG, "customer gate")
        check_in_scope([qa], [*DDB_PG, "elasticache"], "customer gate")

    def test_a_tie_on_an_out_of_scope_engine_is_refused_too(self):
        with pytest.raises(OutOfScopeAssignment, match="q1"):
            check_in_scope(
                [
                    {
                        "query_id": "q1",
                        "assigned_engine": "dynamodb",
                        "tied_engines": ["dynamodb", "documentdb"],
                    }
                ],
                DDB_PG,
                "reality check",
            )


class TestTheRealityCheckRefusesTheScopeToo:
    def _result(self, engine: str) -> dict:
        qa = {"query_id": "q1", "assigned_engine": engine, "tied_engines": []}
        return {
            "triage": {"engine_scope": DDB_PG},
            "assignment": {"query_assignments": [dict(qa)]},
            "input_state": {"q1": (engine, [])},
            "revised_assignments": [qa],
        }

    @pytest.mark.passes_on_main  # a negative or old-artifact case main already meets
    def test_an_in_scope_revision_passes(self):
        from src.agents.referee.reality_check_handler import _check_replay

        _check_replay(self._result("dynamodb"), [])

    def test_a_revision_outside_the_scope_is_refused(self):
        from src.agents.referee.reality_check_handler import _check_replay

        with pytest.raises(
            OutOfScopeAssignment, match="reality check: 1 query would be placed outside"
        ):
            _check_replay(self._result("aurora_mysql"), [])

    @pytest.mark.passes_on_main  # a negative or old-artifact case main already meets
    def test_a_triage_without_a_scope_checks_nothing(self):
        from src.agents.referee.reality_check_handler import _check_replay

        result = self._result("aurora_mysql")
        result["triage"] = {}
        _check_replay(result, [])


class TestOldArtifacts:
    def test_an_aurora_choice_written_before_the_basis_reads_as_the_workload_choice(self):
        old = {"source_engine": "sqlserver", "engine": "aurora_postgresql", "reason": "r"}
        assert AuroraEngineChoice.model_validate(old).basis == "workload"

    def test_an_old_assignment_loads(self):
        old = Assignment(
            job_id="j",
            version=1,
            status="auto_generated",
            source="assignment_resolution",
            timestamp="2026-10-01T00:00:00Z",
            query_assignments=[],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
            aurora_engine_choice=AuroraEngineChoice(
                source_engine="oracle", engine="aurora_mysql", reason="r"
            ),
        ).model_dump(mode="json")
        del old["aurora_engine_choice"]["basis"]
        assert Assignment.model_validate(old).aurora_engine_choice.basis == "workload"


SCOPE_CHOICE = {
    "source_engine": "mysql",
    "engine": "aurora_postgresql",
    "basis": "engine_scope",
    "reason": "The customer's engine scope leaves out Aurora MySQL, the source-compatible engine",
}


class TestWaveOne:
    def test_the_retained_engine_is_the_scope_choice(self):
        collector = _collector("mysql", [])
        assert retained_engine_for(collector, [], SCOPE_CHOICE) == "aurora_postgresql"
        assert retained_engine_for(collector, [], None) == "aurora_mysql"

    def _waves(self, choice, ranking=None, all_on=None):
        ranking = ranking or [
            {"target": "aurora_postgresql", "assigned_queries": 6, "workload_percent": 60.0},
            {"target": "dynamodb", "assigned_queries": 4, "workload_percent": 40.0},
        ]
        tables = [
            {
                "table_id": "app.orders",
                "primary_engine": "aurora_postgresql",
                "engines": ["aurora_postgresql"],
            },
            {"table_id": "app.users", "primary_engine": "dynamodb", "engines": ["dynamodb"]},
        ]
        queries = [
            {"query_id": f"q{i}", "assigned_engine": all_on or e, "tables": [t]}
            for i, (e, t) in enumerate(
                [("aurora_postgresql", "app.orders")] * 6 + [("dynamodb", "app.users")] * 4
            )
        ]
        waves = build_migration_waves(
            ranking=ranking,
            table_assignments=tables,
            query_assignments=queries,
            co_dependency_groups=[],
            cache_overlay=None,
            source_engine="mysql",
            source_version="8.0.36",
            aurora_engine_choice=choice,
        )
        assert waves
        return waves

    def test_wave_one_is_a_cross_engine_move_to_the_scope_choice(self):
        waves = self._waves(SCOPE_CHOICE)
        wave1 = waves[0]
        assert wave1["engines"] == ["aurora_postgresql"]
        assert wave1["homogeneity"] == "cross_engine"
        assert "Aurora PostgreSQL" in wave1["title"] and "cross-engine" in wave1["title"]
        assert "customer's engine scope" in wave1["rationale"]
        assert "Aurora dialect of its own" not in wave1["rationale"]

    def test_the_customers_scope_wave_names_no_score_totals(self):
        # Totals a scope choice never carries, so a rationale that read them would show.
        choice = {**SCOPE_CHOICE, "totals": {"aurora_mysql": 0.0, "aurora_postgresql": 70.0}}
        assert "total adjusted" not in self._waves(choice)[0]["rationale"]

    def test_no_query_on_the_scope_engine_plans_no_conversion_to_it(self):
        # Every query ends on DynamoDB: wave 1 converts the source to no engine, and
        # never names the source-compatible engine the scope left out.
        ranking = [
            {"target": "aurora_postgresql", "assigned_queries": 0, "workload_percent": 0.0},
            {"target": "dynamodb", "assigned_queries": 4, "workload_percent": 100.0},
        ]
        wave1 = self._waves(SCOPE_CHOICE, ranking=ranking, all_on="dynamodb")[0]
        assert wave1["engines"] == []
        assert wave1["homogeneity"] == "heterogeneous"
        assert "Aurora PostgreSQL" not in wave1["title"]
        assert "Aurora MySQL" not in wave1["title"] + wave1["rationale"]


class TestTheGapCheck:
    @pytest.mark.passes_on_main  # #1327's note, kept working on a catalog without rules
    def test_a_conversion_the_catalog_has_no_feature_rules_for_says_so(self):
        # Every conversion has feature rules since #1330: a catalog of version rules
        # only stands in for one that has none.
        version_only = [r for r in gap_rules() if r.match.version is not None]
        gaps = detect_relational_gaps(
            _collector("mysql", []), "aurora_postgresql", rules=version_only
        )
        assert gaps is not None
        assert gaps["feature_rules_checked"] == 0
        wave = {"engines": ["aurora_postgresql"], "moves_from": ["mysql"]}
        note = heterogeneous_wave_note(wave, 2, gaps)
        assert "no feature rules" in note
        assert "found no gaps" not in note

    def test_mysql_to_aurora_postgresql_is_checked_for_feature_gaps(self):
        gaps = detect_relational_gaps(_collector("mysql", []), "aurora_postgresql")
        assert gaps is not None and gaps["feature_rules_checked"] > 0
        wave = {"engines": ["aurora_postgresql"], "moves_from": ["mysql"]}
        assert "no feature rules" not in heterogeneous_wave_note(wave, 2, gaps)

    def test_a_conversion_with_feature_rules_keeps_its_sentence(self):
        gaps = detect_relational_gaps(_collector("sqlserver", []), "aurora_postgresql")
        assert gaps["feature_rules_checked"] > 0
        wave = {"engines": ["aurora_postgresql"], "moves_from": ["sqlserver"]}
        assert "no feature rules" not in heterogeneous_wave_note(wave, 2, gaps)

    @pytest.mark.passes_on_main  # a negative or old-artifact case main already meets
    def test_a_gap_record_written_before_the_count_keeps_its_sentence(self):
        old = {
            "target_engine": "aurora_postgresql",
            "source_engine": "sqlserver",
            "gaps_by_impact": {},
        }
        wave = {"engines": ["aurora_postgresql"], "moves_from": ["sqlserver"]}
        assert "found no gaps" in heterogeneous_wave_note(wave, 2, old)


SRC = Path(__file__).resolve().parents[4] / "src"
GUARDED = ("check_replay", "check_in_scope")


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local names bound to the two checks (``from ... import check_replay as v``)."""
    names = {g: g for g in GUARDED}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in GUARDED:
                    names[alias.asname or alias.name] = alias.name
    return names


def _called(node: ast.AST, aliases: dict[str, str]) -> set[str]:
    """The functions ``node`` calls by name, an alias of a check read as the check."""
    out: set[str] = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            out.add(aliases.get(name, name))
    return out


def _functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, set[str], str]]:
    """Every function in ``src/`` (``move_ledger.py``, which defines the check, aside):
    ``(where, node, calls, its source text's string constants)``."""
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "move_ledger.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                strings = " ".join(
                    n.value
                    for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                )
                out.append(
                    (f"{path.relative_to(SRC)}::{node.name}", node, _called(node, aliases), strings)
                )
    return out


def _reaching(functions, target: str) -> set[str]:
    """Function names that call ``target``, directly or through another function."""
    calls: dict[str, set[str]] = {}
    for where, _node, called, _strings in functions:
        calls.setdefault(where.split("::")[1], set()).update(called)
    reach = {target}
    grew = True
    while grew:
        grew = False
        for name, called in calls.items():
            if name not in reach and called & reach:
                reach.add(name)
                grew = True
    return reach - {target}


# A function that checks a version's replay but whose caller checks the scope: the
# rollback's Moves are built where no store is at hand.
SCOPE_CHECKED_BY_CALLER = {"_rollback_moves": "_roll_back_post_schema_reroute"}

# Functions that write an assignment version file without replaying it, and why that
# cannot place a query outside the scope. A new writer fails the guard until it checks.
WRITES_NO_ENGINE = {
    "mark_assignment_customer_approved": "rewrites the same version's status only",
    "_persist_cache_safety_net": "only drops cache overlays the design does not serve",
}


def test_every_writer_of_an_assignment_version_checks_the_scope():
    """Every function that runs ``move_ledger.check_replay`` before writing a version
    (under any name it is imported as) also runs ``check_in_scope`` (#1327): a new
    writer that skips it fails here."""
    functions = _functions()
    by_name = {where.split("::")[1]: called for where, _n, called, _s in functions}
    writers = {where: called for where, _n, called, _s in functions if "check_replay" in called}
    # The writers this guard was written for; a new one joins the list once it checks.
    assert {w.split("::")[1] for w in writers} >= {
        "resolve",
        "_check_replay",
        "apply_assignment_overrides",
        "reroute_split_writes",
        "_rerouted_assignment",
        "_rollback_moves",
    }, sorted(writers)
    missing = []
    for where, called in writers.items():
        name = where.split("::")[1]
        if name in SCOPE_CHECKED_BY_CALLER:
            called = by_name[SCOPE_CHECKED_BY_CALLER[name]]
        if "check_in_scope" not in called:
            missing.append(where)
    assert not missing, f"writers that skip the engine scope check: {missing}"


def _literal_none_scopes(tree: ast.Module) -> list[int]:
    """Lines where ``check_in_scope`` (under any name) gets a literal ``None`` scope."""
    aliases = _aliases(tree)
    lines = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if aliases.get(name, name) != "check_in_scope":
            continue
        scope = call.args[1] if len(call.args) > 1 else None
        scope = next((k.value for k in call.keywords if k.arg == "scope"), scope)
        if isinstance(scope, ast.Constant) and scope.value is None:
            lines.append(call.lineno)
    return lines


def test_no_scope_check_is_handed_a_literal_none():
    """``check_in_scope(qas, None, ...)`` checks nothing; every writer passes the scope
    it read from the job's triage."""
    literal = [
        f"{path.relative_to(SRC)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in _literal_none_scopes(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not literal, f"check_in_scope given a literal None scope: {literal}"


def test_every_function_that_writes_an_assignment_version_replays_it():
    """A function that writes an ``assignment/v<N>/assignment.json`` (through
    ``assignment_artifact_path`` or the path itself) reaches ``check_replay``, and so
    the scope check, directly or through what it calls; the listed exceptions change
    no engine."""
    functions = _functions()
    reaching = _reaching(functions, "check_replay")
    writers = [
        where
        for where, _node, called, strings in functions
        if "write_json" in called
        and ("assignment_artifact_path" in called or "assignment.json" in strings)
    ]
    assert len(writers) >= 10, writers  # the storage API's version writers today
    unchecked = [
        w
        for w in writers
        if w.split("::")[1] not in reaching and w.split("::")[1] not in WRITES_NO_ENGINE
    ]
    assert not unchecked, f"assignment version writers that never run check_replay: {unchecked}"


def test_the_guards_see_an_alias_and_a_literal_none():
    """The source-reading guards on snippets: an aliased ``check_replay`` is a writer,
    and a literal ``None`` scope is found."""
    tree = ast.parse(
        "from src.agents.referee.move_ledger import check_replay as verify\n"
        "def write():\n    verify([], {}, {}, 'x')\n"
        "def scoped():\n    check_in_scope([], None, 'x')\n"
    )
    aliases = _aliases(tree)
    fns = {n.name: _called(n, aliases) for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "check_replay" in fns["write"]
    assert "check_in_scope" not in fns["write"]
    assert _literal_none_scopes(tree) == [5]
    keyword = ast.parse("from m import check_in_scope as c\nc([], scope=None, writer='x')\n")
    assert _literal_none_scopes(keyword) == [2]
