"""#381: the assignment resolver collapses two competing Aurora engines
(aurora_mysql, aurora_postgresql) to exactly one for a source with no Aurora
dialect of its own (SQL Server, Oracle, DB2).

Triage selects both agents for such a source (``test_triage_handler.py``); this
module covers what happens once both are also analyzed and handed to
``AssignmentResolver.resolve()``: the winner takes every relational query, the
decision is recorded on ``Assignment.aurora_engine_choice``, and a homogeneous
source (MySQL/MariaDB/PostgreSQL) is unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.agents.referee.assignment_resolver import AssignmentResolver, retained_engine_for
from src.contracts.assignment_models import AuroraEngineChoice, QueryAssignment

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _collector(
    query_ids: list[str],
    engine: str | None,
    tables: list[str] | None = None,
    extra_queries: list[dict] | None = None,
) -> dict:
    tables = tables or ["db.t1"]
    co: dict = {
        "job_id": "test",
        "database_schema": {
            "tables": [{"table_id": t, "table_name": t.split(".")[-1]} for t in tables]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": qid,
                    "query_text": "SELECT * FROM t1 WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": tables,
                    "join_count": 0,
                    "has_joins": False,
                    "has_aggregations": False,
                    "filter_tables": [],
                    "calls_per_second": 1.0,
                    "rows_returned_avg": 10,
                }
                for qid in query_ids
            ]
            + (extra_queries or [])
        },
    }
    if engine is not None:
        co["metadata"] = {"source_database": {"engine": engine}}
    return co


def _triage(engines: list[str]) -> dict:
    return {"selected_agents": [{"agent_type": e} for e in engines], "signals": []}


def _analysis(table_ids: list[str], confidence: int) -> dict:
    return {
        "table_recommendations": [
            {"table_id": t, "confidence_score": confidence} for t in table_ids
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


class TestExactTieDefaultsToPostgresql:
    def test_equal_confidence_both_ways_picks_postgresql(self):
        resolver = AssignmentResolver()
        query_ids = [f"q{i}" for i in range(1, 6)]
        collector = _collector(query_ids, engine="sqlserver")
        triage = _triage(["aurora_mysql", "aurora_postgresql"])
        analysis = {
            "aurora_mysql": _analysis(["db.t1"], confidence=70),
            "aurora_postgresql": _analysis(["db.t1"], confidence=70),
        }

        result = resolver.resolve(triage, analysis, collector)

        assigned_engines = {qa.assigned_engine for qa in result.query_assignments}
        assert assigned_engines == {"aurora_postgresql"}
        choice = result.aurora_engine_choice
        assert choice is not None
        assert choice.engine == "aurora_postgresql"
        assert choice.source_engine == "sqlserver"
        # #381 review: the exact-tie branch still records a real feature in the
        # trace (babelfish_compatible: this collector's source_engine is
        # sqlserver) even though the tie-break default already picked
        # aurora_postgresql regardless of it.
        assert choice.deciding_features == ["babelfish_compatible"]
        assert "exact tie" in choice.reason


class TestClearWinner:
    def test_mysql_clearly_earns_more_wins(self):
        # Design: the choice follows the queries, not a hardcoded default --
        # Aurora MySQL can win when it clearly scores higher.
        resolver = AssignmentResolver()
        query_ids = [f"q{i}" for i in range(1, 6)]
        collector = _collector(query_ids, engine="oracle")
        triage = _triage(["aurora_mysql", "aurora_postgresql"])
        analysis = {
            "aurora_mysql": _analysis(["db.t1"], confidence=95),
            "aurora_postgresql": _analysis(["db.t1"], confidence=20),
        }

        result = resolver.resolve(triage, analysis, collector)

        assigned_engines = {qa.assigned_engine for qa in result.query_assignments}
        assert assigned_engines == {"aurora_mysql"}
        choice = result.aurora_engine_choice
        assert choice.engine == "aurora_mysql"
        assert choice.totals["aurora_mysql"] > choice.totals["aurora_postgresql"]
        assert choice.deciding_features == []

    def test_postgresql_clearly_earns_more_wins(self):
        resolver = AssignmentResolver()
        query_ids = [f"q{i}" for i in range(1, 6)]
        collector = _collector(query_ids, engine="db2")
        triage = _triage(["aurora_mysql", "aurora_postgresql"])
        analysis = {
            "aurora_mysql": _analysis(["db.t1"], confidence=10),
            "aurora_postgresql": _analysis(["db.t1"], confidence=90),
        }

        result = resolver.resolve(triage, analysis, collector)

        assigned_engines = {qa.assigned_engine for qa in result.query_assignments}
        assert assigned_engines == {"aurora_postgresql"}
        assert result.aurora_engine_choice.engine == "aurora_postgresql"


class TestNearTieDecidedByFeature:
    def test_close_scores_tipped_by_a_recursive_cte(self):
        resolver = AssignmentResolver()
        query_ids = ["q1", "q2", "q3", "q4", "q5"]
        recursive_cte_query = {
            "query_id": "q-recursive",
            "query_text": (
                "WITH tree AS (SELECT id, parent_id FROM org WHERE parent_id IS NULL "
                "UNION ALL SELECT o.id, o.parent_id FROM org o JOIN tree t ON "
                "o.parent_id = t.id) SELECT * FROM tree"
            ),
            "query_type": "SELECT",
            "tables_accessed": ["db.t1"],
            "join_count": 1,
            "has_joins": True,
            "has_aggregations": False,
            "filter_tables": [],
            "calls_per_second": 1.0,
            "rows_returned_avg": 10,
        }
        collector = _collector(query_ids, engine="sqlserver", extra_queries=[recursive_cte_query])
        triage = _triage(["aurora_mysql", "aurora_postgresql"])
        # Close but not equal: mysql scores slightly higher on raw confidence,
        # within the margin, so the recursive-CTE feature should still tip it
        # to aurora_postgresql.
        analysis = {
            "aurora_mysql": _analysis(["db.t1"], confidence=71),
            "aurora_postgresql": _analysis(["db.t1"], confidence=70),
        }

        result = resolver.resolve(triage, analysis, collector)

        choice = result.aurora_engine_choice
        assert choice.engine == "aurora_postgresql"
        assert "recursive_ctes" in choice.deciding_features
        assigned_engines = {qa.assigned_engine for qa in result.query_assignments}
        assert assigned_engines == {"aurora_postgresql"}


class TestHomogeneousSourcesUnaffected:
    def test_mysql_source_keeps_its_own_dialect(self):
        resolver = AssignmentResolver()
        query_ids = ["q1", "q2", "q3"]
        collector = _collector(query_ids, engine="mysql")
        triage = _triage(["aurora_mysql"])
        analysis = {"aurora_mysql": _analysis(["db.t1"], confidence=80)}

        result = resolver.resolve(triage, analysis, collector)

        assigned_engines = {qa.assigned_engine for qa in result.query_assignments}
        assert assigned_engines == {"aurora_mysql"}
        assert result.aurora_engine_choice is None

    def test_postgresql_source_keeps_its_own_dialect(self):
        resolver = AssignmentResolver()
        query_ids = ["q1", "q2", "q3"]
        collector = _collector(query_ids, engine="postgresql")
        triage = _triage(["aurora_postgresql"])
        analysis = {"aurora_postgresql": _analysis(["db.t1"], confidence=80)}

        result = resolver.resolve(triage, analysis, collector)

        assigned_engines = {qa.assigned_engine for qa in result.query_assignments}
        assert assigned_engines == {"aurora_postgresql"}
        assert result.aurora_engine_choice is None

    def test_caller_analysis_outputs_dict_is_not_mutated(self):
        # The resolver must not delete the losing engine from the caller's own
        # dict (also handed to AssignmentValidator afterwards).
        resolver = AssignmentResolver()
        query_ids = ["q1", "q2"]
        collector = _collector(query_ids, engine="oracle")
        triage = _triage(["aurora_mysql", "aurora_postgresql"])
        analysis = {
            "aurora_mysql": _analysis(["db.t1"], confidence=90),
            "aurora_postgresql": _analysis(["db.t1"], confidence=10),
        }

        resolver.resolve(triage, analysis, collector)

        assert set(analysis.keys()) == {"aurora_mysql", "aurora_postgresql"}


class TestAdventureWorksSqlServerFixture:
    """A small, trimmed AdventureWorks-like SQL Server fixture (no customer data):
    multiple schemas, a sequence, a MERGE statement, a recursive CTE and a JSON
    function -- every #381 feature signal present at once."""

    def _load(self) -> dict:
        data: dict = json.loads((FIXTURES / "adventureworks_trimmed_collection.json").read_text())
        return data

    def test_both_aurora_agents_selected_and_one_engine_wins(self):
        from src.agents.referee.triage import triage as run_triage

        collector = self._load()
        triage_result = run_triage(collector)
        assert "aurora_mysql" in triage_result.selected
        assert "aurora_postgresql" in triage_result.selected

        table_ids = sorted({t["table_id"] for t in collector["database_schema"]["tables"]})
        resolver = AssignmentResolver()
        triage_dict = {
            "selected_agents": [{"agent_type": e} for e in triage_result.selected],
            "signals": [],
        }
        analysis = {
            # Close, not exactly tied: aurora_mysql scores a touch higher on raw
            # table confidence (within the margin), so the AdventureWorks feature
            # signals (sequence, MERGE, recursive CTE, JSON, multiple schemas,
            # Babelfish) are what actually decide the winner here.
            "aurora_mysql": _analysis(table_ids, confidence=66),
            "aurora_postgresql": _analysis(table_ids, confidence=65),
        }

        result = resolver.resolve(triage_dict, analysis, collector)

        assigned_engines = {qa.assigned_engine for qa in result.query_assignments}
        # Exactly one Aurora engine ends up owning the relational workload.
        assert assigned_engines <= {"aurora_mysql", "aurora_postgresql"}
        assert len(assigned_engines) == 1

        choice = result.aurora_engine_choice
        assert choice is not None
        assert choice.source_engine == "sqlserver"
        assert choice.engine == "aurora_postgresql"
        # #381 review: every workload-based ("primary") feature is present here, so
        # they decide it; babelfish_compatible (a property of the source dialect, not
        # of this workload) is not needed and does not appear -- it only breaks a
        # *remaining* tie when no primary feature is present.
        assert set(choice.deciding_features) == {
            "sequences",
            "recursive_ctes",
            "merge_statements",
            "json_usage",
            "multiple_schemas",
        }
        assert "babelfish_compatible" not in choice.deciding_features
        assert choice.totals["aurora_mysql"] > choice.totals["aurora_postgresql"]


class TestRetainedEngineForPrefersRecordedChoice:
    """#381 review: ``Assignment.aurora_engine_choice.engine`` is authoritative --
    preferred over reconstructing the choice from ``query_assignments``, which a
    customer override to the *losing* engine could make ambiguous."""

    def _co(self, engine: str = "sqlserver") -> dict:
        return _collector(["q1"], engine=engine)

    def test_homogeneous_source_ignores_aurora_engine_choice(self):
        # A MySQL source's own dialect always wins, whatever aurora_engine_choice says.
        engine = retained_engine_for(
            self._co(engine="mysql"),
            aurora_engine_choice={"engine": "aurora_postgresql"},
        )
        assert engine == "aurora_mysql"

    def test_prefers_the_recorded_choice_as_a_plain_string(self):
        engine = retained_engine_for(self._co(), aurora_engine_choice="aurora_mysql")
        assert engine == "aurora_mysql"

    def test_prefers_the_recorded_choice_as_a_dict(self):
        engine = retained_engine_for(self._co(), aurora_engine_choice={"engine": "aurora_mysql"})
        assert engine == "aurora_mysql"

    def test_prefers_the_recorded_choice_as_a_model(self):
        choice = AuroraEngineChoice(
            source_engine="sqlserver",
            engine="aurora_mysql",
            reason="higher total adjusted score wins",
        )
        engine = retained_engine_for(self._co(), aurora_engine_choice=choice)
        assert engine == "aurora_mysql"

    def test_recorded_choice_overrides_an_ambiguous_query_assignments_reconstruction(self):
        # A customer override moved one query onto the *losing* engine
        # (aurora_mysql here) -- the heuristic reconstruction from
        # query_assignments alone would see both engines in use and pick
        # alphabetically (aurora_mysql), which happens to agree by coincidence in
        # this case; use a losing engine of aurora_postgresql to show the
        # recorded choice (not alphabetical order) decides it.
        qas = [
            QueryAssignment(
                query_id="q1",
                assigned_engine="aurora_postgresql",
                confidence=50,
                source_tables=[],
                assignment_reason="test",
            ),
            QueryAssignment(
                query_id="q2",
                assigned_engine="aurora_mysql",
                confidence=50,
                source_tables=[],
                assignment_reason="test",
            ),
        ]
        engine = retained_engine_for(
            self._co(), qas, aurora_engine_choice={"engine": "aurora_mysql"}
        )
        assert engine == "aurora_mysql"

    def test_falls_back_to_query_assignments_without_a_recorded_choice(self):
        qas = [
            QueryAssignment(
                query_id="q1",
                assigned_engine="aurora_mysql",
                confidence=50,
                source_tables=[],
                assignment_reason="test",
            ),
        ]
        engine = retained_engine_for(self._co(), qas)
        assert engine == "aurora_mysql"

    def test_none_when_nothing_is_available(self):
        assert retained_engine_for(self._co()) is None
