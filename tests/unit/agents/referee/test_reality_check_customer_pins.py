"""Phase 1 keeps an engine a customer pin holds (#860).

The handler holds customer-pinned queries (``customer_override``) out of the
consolidation and re-attaches them as they are, so the pin always keeps its
engine in the plan. Before #860 Phase 1 did not know that: it judged the
engine on its unpinned queries alone, removed it, and moved them to an engine
that scores them lower, for no saving (the engine's cost is paid for the pin
anyway). Under ADR-031 Decision 7 a query leaves only when its engine is
removed or a kept engine scores it higher, so an engine a pin holds is kept,
and it is a destination like any other kept engine.

The scenario: DynamoDB owns ``d1``-``d3`` (90 there, 40 on DocumentDB); DocumentDB owns ``b1``
and ``b2``, which score 80 on DocumentDB and 70 on DynamoDB -- a 10-point
margin, under ``UNIQUE_DELTA_THRESHOLD``, so without a pin Phase 1 removes
DocumentDB.
"""

from __future__ import annotations

import pytest

from src.agents.referee.move_ledger import check_replay, state_of
from src.agents.referee.reality_check import run_reality_check
from src.agents.referee.reality_check_handler import run_reality_check_handler
from src.storage.assignment_versioning import assignment_artifact_path
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-1"
QUERIES = ("d1", "d2", "d3", "b1", "b2")


def _qa(qid: str, engine: str, **extra: object) -> dict:
    return {
        "query_id": qid,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": [f"db.{qid[0]}"],
        "assignment_reason": "test",
        **extra,
    }


def _assignment(b2_engine: str = "documentdb", pin_b1: bool = True, version: int = 1) -> dict:
    qas = [_qa(q, "dynamodb") for q in ("d1", "d2", "d3")]
    qas.append(_qa("b1", "documentdb", **({"customer_override": True} if pin_b1 else {})))
    qas.append(_qa("b2", b2_engine))
    return {
        "job_id": JOB,
        "version": version,
        "status": "auto_generated",
        "timestamp": "2026-10-01T00:00:00+00:00",
        "query_assignments": qas,
        "table_assignments": [],
        "co_dependency_groups": [],
        "validation_warnings": [],
        "source": "assignment_resolution",
    }


TRIAGE = {
    "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
    "signals": [],
    "query_capabilities": {},
}
COLLECTOR = {
    "database_schema": {"tables": [{"table_id": "db.d"}, {"table_id": "db.b"}]},
    "queries": {
        "query_patterns": [
            {"query_id": q, "tables_accessed": [f"db.{q[0]}"], "query_type": "SELECT"}
            for q in QUERIES
        ]
    },
}
ANALYSIS = {
    "dynamodb": {
        "table_recommendations": [
            {"table_id": "db.d", "confidence_score": 90},
            {"table_id": "db.b", "confidence_score": 70},
        ]
    },
    "documentdb": {
        "table_recommendations": [
            {"table_id": "db.d", "confidence_score": 40},
            {"table_id": "db.b", "confidence_score": 80},
        ]
    },
}


def _unpinned(assignment: dict) -> dict:
    """What the handler hands ``run_reality_check``: the pins held out."""
    return {
        **assignment,
        "query_assignments": [
            qa for qa in assignment["query_assignments"] if not qa.get("customer_override")
        ],
    }


def _engines(result: dict) -> dict[str, str]:
    return {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}


class TestRunRealityCheck:
    def test_without_a_pin_the_engine_is_removed(self) -> None:
        """The baseline the pin changes: DocumentDB earns no place on its own."""
        result = run_reality_check(_assignment(pin_b1=False), TRIAGE, ANALYSIS, COLLECTOR)
        assert _engines(result)["b2"] == "dynamodb"

    def test_an_engine_a_pin_holds_keeps_its_other_queries(self) -> None:
        result = run_reality_check(
            _unpinned(_assignment()),
            TRIAGE,
            ANALYSIS,
            COLLECTOR,
            customer_kept_engines={"documentdb"},
        )
        assert _engines(result)["b2"] == "documentdb"
        assert result["consolidations"] == []
        assert result["move_ledger"].moves == []

    def test_a_kept_engine_takes_back_a_query_it_scores_higher(self) -> None:
        """The customer-gate flow: an earlier check moved b2 off DocumentDB, then
        the customer pinned b1 there. DocumentDB stays in the plan and scores b2
        80 against DynamoDB's 70, so b2 goes back (one Move, replay exact)."""
        assignment = _unpinned(_assignment(b2_engine="dynamodb"))
        result = run_reality_check(
            assignment, TRIAGE, ANALYSIS, COLLECTOR, customer_kept_engines={"documentdb"}
        )
        assert _engines(result)["b2"] == "documentdb"
        (move,) = result["move_ledger"].moves
        assert (move.query_id, move.from_engine, move.to_engine) == ("b2", "dynamodb", "documentdb")
        check_replay(
            result["move_ledger"].moves,
            state_of(assignment["query_assignments"]),
            state_of(result["revised_assignments"]),
            "test",
        )

    def test_kept_engines_never_bring_back_an_unanalyzed_engine(self) -> None:
        result = run_reality_check(
            _unpinned(_assignment(pin_b1=False)),
            TRIAGE,
            ANALYSIS,
            COLLECTOR,
            customer_kept_engines={"opensearch"},
        )
        assert "opensearch" not in set(_engines(result).values())


def _seed(store: LocalArtifactStore, assignment: dict) -> None:
    prefix = f"{DB}/{JOB}"
    store.write_json(f"{prefix}/referee-triage/triage.json", TRIAGE)
    store.write_json(f"{prefix}/collector/output.json", COLLECTOR)
    for engine, analysis in ANALYSIS.items():
        store.write_json(f"{prefix}/analysis-{engine}/analysis.json", analysis)
    store.write_json(assignment_artifact_path(DB, JOB, 1), assignment)


def _rc_output(store: LocalArtifactStore) -> dict:
    return store.read_json(f"{DB}/{JOB}/reality-check/output.json")


class TestHandler:
    @pytest.mark.parametrize("llm_mode", ["none", "external"])
    def test_a_pinned_engine_keeps_its_other_queries(self, tmp_path, llm_mode) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store, _assignment())

        summary = run_reality_check_handler(JOB, DB, store, llm_mode=llm_mode)

        assert summary["output_version"] is None  # nothing moved, no revision
        out = _rc_output(store)
        assert out["after_distribution"] == {"dynamodb": 3, "documentdb": 2}
        assert out["consolidations"] == []
        assert out["eliminated_engines"] == []

    def test_a_pinned_engine_takes_back_a_query_it_scores_higher(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store, _assignment(b2_engine="dynamodb"))

        summary = run_reality_check_handler(JOB, DB, store, llm_mode="none")

        revised = store.read_json(assignment_artifact_path(DB, JOB, summary["output_version"]))
        by_id = {qa["query_id"]: qa for qa in revised["query_assignments"]}
        assert by_id["b2"]["assigned_engine"] == "documentdb"
        assert by_id["b1"]["assigned_engine"] == "documentdb"
        assert by_id["b1"]["customer_override"] is True
        # The version holds exactly the Moves that produced it: b2's one.
        assert [(m["query_id"], m["from_engine"], m["to_engine"]) for m in revised["moves"]] == [
            ("b2", "dynamodb", "documentdb")
        ]


_SHALLOW_TEXT = "SELECT a.id FROM db.users a LEFT JOIN db.groups g ON g.id=a.group_id"
# Real search depth (relevance ranking), at traffic under the #326 floor.
_DEEP_TEXT = "SELECT * FROM db.users WHERE MATCH(name, bio) AGAINST (?)"


def _floor_case(query_text: str) -> tuple[dict, dict, dict, dict]:
    """20 DynamoDB queries plus one query OpenSearch scores highest (its own
    matched pattern), at 0.3 calls/s: the shape of
    ``TestOpenSearchFloorAppliesWithoutMandatoryStatus``, plus a committed
    Aurora MySQL."""
    assignment: dict = {
        "version": 1,
        "query_assignments": [
            {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
            for i in range(20)
        ]
        + [
            {"query_id": "util1", "assigned_engine": "aurora_mysql", "assignment_reason": "t"},
            {"query_id": "search", "assigned_engine": "opensearch", "assignment_reason": "t"},
        ],
    }
    # util1 (an admin statement only Aurora MySQL serves) keeps Aurora MySQL committed.
    triage: dict = {
        "selected_agents": [
            {"agent_type": "dynamodb"},
            {"agent_type": "aurora_mysql"},
            {"agent_type": "opensearch"},
        ],
        "signals": [],
        "query_capabilities": {"util1": ["sql_admin"]},
    }
    collector: dict = {
        "queries": {
            "query_patterns": [
                {"query_id": f"dq{i}", "tables_accessed": ["db.posts"], "query_type": "SELECT"}
                for i in range(20)
            ]
            + [
                {
                    "query_id": "util1",
                    "tables_accessed": ["db.util"],
                    "query_type": "OTHER",
                    "query_text": "SHOW FULL FIELDS FROM db.util",
                },
                {
                    "query_id": "search",
                    "tables_accessed": ["db.users"],
                    "query_type": "SELECT",
                    "query_text": query_text,
                    "calls_per_second": 0.3,
                },
            ]
        }
    }
    analysis: dict[str, dict] = {
        "dynamodb": {"table_recommendations": [{"table_id": "db.posts", "confidence_score": 80}]},
        # Aurora MySQL serves a full-text MATCH ... AGAINST (DynamoDB cannot),
        # so a deep query that fails the floor has somewhere to go.
        "aurora_mysql": {
            "table_recommendations": [
                {"table_id": "db.posts", "confidence_score": 40},
                {"table_id": "db.users", "confidence_score": 60},
                {"table_id": "db.util", "confidence_score": 60},
            ]
        },
        "opensearch": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 95}],
            "workload_analysis": {
                "patterns_detected": [
                    {
                        "pattern_type": "wildcard-search",
                        "query_ids": ["search"],
                        "table_ids": ["db.users"],
                    }
                ]
            },
        },
    }
    return assignment, triage, collector, analysis


def _floor_run(query_text: str, kept: set[str]) -> dict[str, str]:
    assignment, triage, collector, analysis = _floor_case(query_text)
    result = run_reality_check(assignment, triage, analysis, collector, customer_kept_engines=kept)
    return _engines(result)


class TestFloorsOnAPinnedEngine:
    """A pin keeps its engine and pays its standing cost (#860, ADR-031
    Decision 7), so the floors that ask whether the workload pays for the
    engine -- the OpenSearch traffic/corpus floor and the cost-share floor
    (#167) -- do not run for it. The per-query search-depth check (#326) is
    not about cost: a query with no real search depth still leaves a pinned
    OpenSearch, and never joins it for the pin's sake."""

    def test_a_shallow_query_leaves_opensearch_without_a_pin(self) -> None:
        assert _floor_run(_SHALLOW_TEXT, set())["search"] != "opensearch"

    def test_a_shallow_query_still_leaves_a_pinned_opensearch(self) -> None:
        assert _floor_run(_SHALLOW_TEXT, {"opensearch"})["search"] != "opensearch"

    def test_a_deep_query_under_the_traffic_floor_leaves_without_a_pin(self) -> None:
        assert _floor_run(_DEEP_TEXT, set())["search"] == "aurora_mysql"

    def test_a_deep_query_stays_on_a_pinned_opensearch(self) -> None:
        assert _floor_run(_DEEP_TEXT, {"opensearch"})["search"] == "opensearch"

    def test_a_shallow_query_never_joins_a_pinned_opensearch(self) -> None:
        """The customer-gate flow: the query already left OpenSearch, a pin
        keeps OpenSearch, and OpenSearch scores the query higher; it still
        stays where it is, with no Move off and back."""
        assignment, triage, collector, analysis = _floor_case(_SHALLOW_TEXT)
        assignment["query_assignments"][-1]["assigned_engine"] = "aurora_mysql"
        # A text_search signal makes it a query OpenSearch may absorb at all
        # (``may_absorb``), as discourse's to_tsvector selects are.
        triage["signals"] = [{"signal": "text_search", "query_ids": ["search"]}]
        result = run_reality_check(
            assignment, triage, analysis, collector, customer_kept_engines={"opensearch"}
        )
        assert _engines(result)["search"] == "aurora_mysql"
        assert [m.query_id for m in result["move_ledger"].moves] == []


class TestCostShareFloorOnAPinnedEngine:
    """#167's cost-share floor drops a mandatory engine with a tiny share; a
    pin pays for the engine, so it keeps its signal-override query."""

    def _case(self) -> tuple[dict, dict, dict, dict]:
        assignment: dict = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(39)
            ]
            + [
                {
                    "query_id": "doc1",
                    "assigned_engine": "documentdb",
                    "assignment_reason": "signal override: nested_document → documentdb",
                    "signal_override": "nested_document",
                }
            ],
        }
        triage: dict = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
            "signals": [],
        }
        collector: dict = {
            "queries": {
                "query_patterns": [
                    {"query_id": q, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for q in [*(f"dq{i}" for i in range(39)), "doc1"]
                ]
            }
        }
        analysis: dict[str, dict] = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            },
        }
        return assignment, triage, collector, analysis

    def test_without_a_pin_the_engine_is_dropped(self) -> None:
        assignment, triage, collector, analysis = self._case()
        result = run_reality_check(assignment, triage, analysis, collector)
        assert _engines(result)["doc1"] == "dynamodb"

    def test_a_pinned_engine_keeps_it(self) -> None:
        assignment, triage, collector, analysis = self._case()
        result = run_reality_check(
            assignment, triage, analysis, collector, customer_kept_engines={"documentdb"}
        )
        assert _engines(result)["doc1"] == "documentdb"
