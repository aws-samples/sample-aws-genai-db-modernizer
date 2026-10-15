"""Unit tests for the reality check agent core logic.

#490/#549: the reality check's own fit-scoring tables and
``_engine_fit_score``/``_can_engine_serve_query`` (which read them) are
deleted -- every fit-scoring decision now reads the resolver's own score
(``query_scoring.score_query_for_engine``) and the shared capability
registry (``capability_registry.can_engine_serve_capability``).
``TestEngineFitScore`` and ``TestCanEngineServeQuery`` tested those deleted
functions directly; the resolver's own equivalent scoring is already
covered by ``tests/unit/test_assignment_resolver.py``, so they are removed
rather than rewritten against a function that no longer exists.

#588: the Aurora absorption pass and its own thresholds are deleted too, so
``TestAuroraAbsorptionPass`` and ``TestTinyMandatoryAbsorption`` (which called
it directly) are removed. The rule that replaces it is covered by
``tests/unit/agents/referee/test_reality_check_shared_rule.py``; a tiny
signal-pinned engine is judged by the justification floors
(``TestOpenSearchJustificationFloor`` below and
``test_mandatory_engine_cost_share_floor.py``).
"""

import itertools
import random
import re

from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.reality_check import (
    _find_best_absorber_for_query,
    _place_settled_queries,
    _settle_committed_engines,
    _workload_ever_justified,
    run_reality_check,
)


def _make_assignment(
    query_engine_pairs: list[tuple[str, str]],
    signal_overrides: dict[str, str] | None = None,
) -> dict:
    """Build a minimal assignment dict.

    ``signal_overrides`` (``query_id -> signal name``) marks a query the way
    the real resolver does (#475 review): ``signal_override`` set, plus an
    ``assignment_reason`` starting with "signal override" --
    ``_is_signal_override`` checks both. Omitting it (the default) means a
    query is NOT mandatory, even if its engine happens to be a specialist
    like OpenSearch -- tests that need Pass 0's mandatory protection must
    opt in here instead of relying on an inflated fit score to look unique
    by accident.
    """
    signal_overrides = signal_overrides or {}
    query_assignments = []
    for qid, engine in query_engine_pairs:
        signal = signal_overrides.get(qid)
        qa = {
            "query_id": qid,
            "assigned_engine": engine,
            "assignment_reason": f"signal override: {signal}" if signal else "test",
        }
        if signal:
            qa["signal_override"] = signal
        query_assignments.append(qa)
    return {"version": 1, "query_assignments": query_assignments}


def _make_triage(signals: list[dict] | None = None) -> dict:
    return {
        "selected_agents": [
            {"agent_type": "dynamodb"},
            {"agent_type": "documentdb"},
            {"agent_type": "opensearch"},
        ],
        "signals": signals or [],
    }


def _make_collector(query_ids: list[str], extra: dict[str, dict] | None = None) -> dict:
    extra = extra or {}
    patterns = []
    for qid in query_ids:
        pattern = {
            "query_id": qid,
            "tables_accessed": ["db.users"],
            "query_type": "SELECT",
        }
        pattern.update(extra.get(qid, {}))
        patterns.append(pattern)
    return {"queries": {"query_patterns": patterns}}


# A query_text/calls_per_second pair that clears the OpenSearch justification
# floor (#326): real search depth (MATCH...AGAINST) at traffic above
# OPENSEARCH_MIN_CALLS_PER_SECOND, with no source engine metadata (so the
# native-source-engine override does not apply). Tests that assert OpenSearch
# keeps a mandatory query use this so they test mandatory protection itself,
# not the justification floor (covered separately below).
_REAL_SEARCH_DEPTH_QUERY = {
    "query_text": "SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)",
    "calls_per_second": 5.0,
}


class TestTiedQueryCountsAsNativeEvidenceForTheCreditedEngineOnly:
    """#503 (the maintainer's design call): a tied query counts as native
    evidence for the ONE engine the first
    assignment's placeholder already credited it to (``assigned_engine``),
    never for every tied engine and never for none. Here DocumentDB's ONLY
    query (q_tied) is such a placeholder; DynamoDB has two other,
    genuinely decided queries. DocumentDB therefore has 1 real (if
    redundant) native query and IS evaluated in Phase 1, and q_tied's tie
    -- which the fresh resolver trace also sees as an exact 70/70 tie
    between the two -- resolves definitively onto DynamoDB once DocumentDB
    is judged redundant and removed: a real, explainable decision that
    clears the tie, not a second placeholder pick.
    """

    def test_documentdb_is_evaluated_and_the_tie_clears_onto_dynamodb(self):
        assignment = _make_assignment(
            [("q1", "dynamodb"), ("q2", "dynamodb"), ("q_tied", "documentdb")]
        )
        for qa in assignment["query_assignments"]:
            if qa["query_id"] == "q_tied":
                qa["tied_engines"] = ["documentdb", "dynamodb"]
        triage = _make_triage()
        collector = _make_collector(["q1", "q2", "q_tied"])
        # The same table, same confidence for both engines -- q1, q2 and
        # q_tied all score an exact 70/70 tie in the fresh resolver trace
        # ``run_reality_check`` recomputes.
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        # DocumentDB IS evaluated now (it has one native query, q_tied) --
        # the round-1 shape excluded it entirely, which is what C1/C2 found
        # could crash or silently drop a tied workload elsewhere.
        assert "documentdb" in result["unique_value_assessment"]
        revised_by_id = {qa["query_id"]: qa for qa in result["revised_assignments"]}
        assert revised_by_id["q_tied"]["assigned_engine"] == "dynamodb"
        # A real decision now -- the tie is cleared, not re-placeholdered.
        assert revised_by_id["q_tied"]["tied_engines"] == []
        assert revised_by_id["q1"]["assigned_engine"] == "dynamodb"
        assert revised_by_id["q2"]["assigned_engine"] == "dynamodb"


class TestTiedQueriesNeverCrashOrSilentlyDropToAWeakerEngine:
    """#503: excluding a tied query from EVERY engine's native set could
    leave an engine whose only queries were tied uncommitted with no
    fallback, crashing ``run_reality_check``'s placement invariant once
    nothing else was eligible either; in the same situation with a weaker
    engine present, the tie could silently disappear instead -- the
    workload moving onto the weaker engine with no sign a tie was ever
    involved.

    Three queries, all recorded on DynamoDB with ``tied_engines =
    ["documentdb", "dynamodb"]`` (both score 70 for them); no other query
    exists at all.
    """

    def _assignment_and_fixtures(self):
        assignment = _make_assignment([("q1", "dynamodb"), ("q2", "dynamodb"), ("q3", "dynamodb")])
        for qa in assignment["query_assignments"]:
            qa["tied_engines"] = ["documentdb", "dynamodb"]
        collector = _make_collector(["q1", "q2", "q3"])
        return assignment, collector

    def test_c1_no_crash_when_only_the_two_tied_engines_are_candidates(self):
        assignment, collector = self._assignment_and_fixtures()
        triage = _make_triage()
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            },
        }

        # Must not raise.
        result = run_reality_check(assignment, triage, analysis, collector)

        revised_by_id = {qa["query_id"]: qa for qa in result["revised_assignments"]}
        for qid in ("q1", "q2", "q3"):
            # DynamoDB has all three native queries' credit; DocumentDB has
            # none, so DynamoDB is the real, explainable survivor -- not a
            # crash, and not a second placeholder pick.
            assert revised_by_id[qid]["assigned_engine"] == "dynamodb"
            assert revised_by_id[qid]["tied_engines"] == []

    def test_c2_a_much_weaker_third_engine_never_silently_absorbs_the_tie(self):
        assignment, collector = self._assignment_and_fixtures()
        triage = {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "documentdb"},
                {"agent_type": "aurora_postgresql"},
            ],
            "signals": [],
        }
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            },
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 50}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        revised_by_id = {qa["query_id"]: qa for qa in result["revised_assignments"]}
        for qid in ("q1", "q2", "q3"):
            # The round-1 shape moved these to aurora_postgresql with "no
            # unique value -- dynamodb scores n/a, aurora_postgresql scores
            # 50.0" because Phase 1 never got to evaluate either tied
            # engine at all. They must stay on DynamoDB -- its native,
            # settled 70 clearly beats aurora_postgresql's 50 -- not fall
            # to the weaker engine.
            assert revised_by_id[qid]["assigned_engine"] == "dynamodb"
            assert revised_by_id[qid]["tied_engines"] == []


class TestTiedQueryEligibilityIsPlaceholderIndependent:
    """#503: a join query tied 70/70 between DocumentDB and DynamoDB, both
    committed, settled counts favoring DynamoDB 10 to 1. The
    join-consolidation gate (``_query_eligible_engines``) used to read only
    the ONE engine recorded as "current" -- with the placeholder on
    DynamoDB the join block never fires (DynamoDB is already "current"),
    but with the placeholder on DocumentDB, DynamoDB looked like a
    *consolidation* target away from a non-Aurora, non-DynamoDB engine, so
    the join block made DynamoDB ineligible and DocumentDB "won" by default
    -- the placeholder's own, otherwise-inconsequential pick decided the
    result. Both starting placeholders must now give the SAME answer.
    """

    @staticmethod
    def _place(placeholder: str) -> dict:
        trace_by_query = {"qj": {"engines": {"documentdb": 70, "dynamodb": 70}}}
        tied_engines_by_qid = {"qj": ["documentdb", "dynamodb"]}
        query_map = {"qj": {"has_joins": True}}
        qa = [
            {
                "query_id": "qj",
                "assigned_engine": placeholder,
                "assignment_reason": "placeholder",
                "tied_engines": ["documentdb", "dynamodb"],
            }
        ]
        revised, _ = _place_settled_queries(
            query_assignments=qa,
            committed_engines={"documentdb", "dynamodb"},
            mandatory_query_ids=set(),
            current_engine_by_qid={"qj": placeholder},
            query_signals={},
            query_map=query_map,
            query_capabilities=None,
            trace_by_query=trace_by_query,
            source_compatible_engine=None,
            workload_justified={},
            tie_unresolved_qids={"qj"},
            settled_counts={"dynamodb": 10, "documentdb": 1},
            tied_engines_by_qid=tied_engines_by_qid,
        )
        return revised[0]

    def test_both_starting_placeholders_give_the_same_answer(self):
        from_dynamodb = self._place("dynamodb")
        from_documentdb = self._place("documentdb")

        assert from_dynamodb["assigned_engine"] == from_documentdb["assigned_engine"]
        assert from_dynamodb["tied_engines"] == from_documentdb["tied_engines"]
        # DynamoDB carries more of the settled workload (10 vs 1), so it is
        # the placeholder either way -- never DocumentDB "winning" only
        # because the join block happened to disqualify DynamoDB when IT,
        # not DocumentDB, was the one recorded as current.
        assert from_dynamodb["assigned_engine"] == "dynamodb"


class TestRunRealityCheck:
    """Test the full reality check flow."""

    def test_no_consolidation_when_engines_have_unique_value(self):
        """Two engines with different signal specializations should both survive."""
        assignment = _make_assignment(
            [("q1", "dynamodb"), ("q2", "dynamodb"), ("q3", "opensearch")],
            signal_overrides={"q3": "text_search"},
        )
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q3"],
                    "evidence": "LIKE query",
                }
            ]
        )
        collector = _make_collector(["q1", "q2", "q3"], extra={"q3": _REAL_SEARCH_DEPTH_QUERY})
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        assert len(result["consolidations"]) == 0

    def test_consolidation_when_engine_is_redundant(self):
        """An engine with no unique queries should be consolidated."""
        assignment = _make_assignment(
            [
                ("q1", "dynamodb"),
                ("q2", "dynamodb"),
                ("q3", "documentdb"),
                ("q4", "documentdb"),
            ]
        )
        triage = _make_triage()
        collector = _make_collector(["q1", "q2", "q3", "q4"])
        # dynamodb scores a couple of points higher on their shared table --
        # under the 15-point margin, so documentdb is still "redundant," but
        # the pick is a real score comparison, not an exact tie (no engine is
        # favoured for being cheaper; #502 review).
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 78}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        # documentdb should be consolidated (it has no unique value here)
        assert len(result["consolidations"]) > 0
        consolidated_from = {c["from_engine"] for c in result["consolidations"]}
        assert "documentdb" in consolidated_from

    def test_revised_assignments_returned(self):
        """Consolidated queries appear in revised_assignments with new engine."""
        assignment = _make_assignment([("q1", "dynamodb"), ("q2", "documentdb")])
        triage = _make_triage()
        collector = _make_collector(["q1", "q2"])
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
            "documentdb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 78}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engines_after = {qa["assigned_engine"] for qa in result["revised_assignments"]}
        # After consolidation, all queries should be on dynamodb -- it scores
        # a couple of points higher on their shared table (not an exact tie).
        if result["consolidations"]:
            assert "dynamodb" in engines_after

    def test_unique_value_assessment_populated(self):
        """Every engine should have a unique value assessment."""
        assignment = _make_assignment([("q1", "dynamodb"), ("q2", "opensearch")])
        triage = _make_triage()
        collector = _make_collector(["q1", "q2"])

        result = run_reality_check(assignment, triage, {}, collector)
        assert "unique_value_assessment" in result
        # At least one engine should be assessed
        assert len(result["unique_value_assessment"]) > 0

    def test_architectural_patterns_detected(self):
        """Multi-engine setup should detect patterns like CQRS."""
        assignment = _make_assignment(
            [
                ("q1", "dynamodb"),
                ("q2", "dynamodb"),
                ("q3", "opensearch"),
            ],
            signal_overrides={"q3": "text_search"},
        )
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q3"],
                    "evidence": "text search",
                }
            ]
        )
        collector = _make_collector(["q1", "q2", "q3"], extra={"q3": _REAL_SEARCH_DEPTH_QUERY})
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 85}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        pattern_names = [p["name"] for p in result["architectural_patterns"]]
        # Should detect CQRS or Materialized View with DynamoDB + OpenSearch
        assert len(pattern_names) > 0

    def test_recommendations_always_present(self):
        assignment = _make_assignment([("q1", "dynamodb")])
        triage = _make_triage()
        collector = _make_collector(["q1"])

        result = run_reality_check(assignment, triage, {}, collector)
        assert len(result["recommendations"]) > 0

    def test_text_search_not_consolidated_to_dynamodb(self):
        """Text search queries must stay in OpenSearch even with high table confidence."""
        assignment = _make_assignment(
            [("q1", "dynamodb"), ("q2", "dynamodb"), ("q3", "opensearch")],
            signal_overrides={"q3": "text_search"},
        )
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q3"],
                    "evidence": "LIKE '%search%'",
                }
            ]
        )
        collector = _make_collector(["q1", "q2", "q3"], extra={"q3": _REAL_SEARCH_DEPTH_QUERY})
        # DynamoDB has high confidence on the same table — but can't do text search
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        # q3 must stay in opensearch — DynamoDB lacks text_search capability
        q3_engine = next(
            qa["assigned_engine"] for qa in result["revised_assignments"] if qa["query_id"] == "q3"
        )
        assert q3_engine == "opensearch"

    def test_mandatory_signal_override_protected(self):
        """Queries with signal override should not be consolidated."""
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
                {
                    "query_id": "q2",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "signal override: text_search",
                },
            ],
        }
        triage = _make_triage(
            [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q2"],
                    "evidence": "LIKE query",
                }
            ]
        )
        collector = _make_collector(["q1", "q2"], extra={"q2": _REAL_SEARCH_DEPTH_QUERY})

        result = run_reality_check(assignment, triage, {}, collector)
        # q2 should still be in opensearch (mandatory)
        q2_engine = next(
            qa["assigned_engine"] for qa in result["revised_assignments"] if qa["query_id"] == "q2"
        )
        assert q2_engine == "opensearch"

    def test_mandatory_detected_from_structured_field(self):
        """Protection reads signal_override, so rewording the reason can't disable it (#151)."""
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
                {
                    "query_id": "q2",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "routed for full-text search",
                    "signal_override": "text_search",
                },
            ],
        }
        collector = _make_collector(["q1", "q2"], extra={"q2": _REAL_SEARCH_DEPTH_QUERY})

        result = run_reality_check(assignment, _make_triage(), {}, collector)

        assert result["unique_value_assessment"]["opensearch"]["is_mandatory"] is True
        q2 = next(qa for qa in result["revised_assignments"] if qa["query_id"] == "q2")
        assert q2["assigned_engine"] == "opensearch"

    def test_absorber_prefers_dynamodb_over_opensearchs_inflated_table_score(self):
        """#475 (discourse/wordpress evidence): the consolidation absorber
        must not pick OpenSearch for a plain ``COUNT(*)`` just because
        OpenSearch's table-level recommendation is inflated by a DIFFERENT,
        genuinely search-shaped query on the same table. Before this fix,
        this is exactly how a maintainer's wordpress run moved plain
        ``SQL_CALC_FOUND_ROWS``/``COUNT(*)`` queries from Aurora MySQL onto
        OpenSearch during consolidation (``_find_best_absorber_for_query``
        is the function Pass 2 calls to pick an absorber).

        #502 review round 5, finding N3 / #490/#549: this function now
        ranks by the resolver's own per-query score
        (``query_scoring.score_query_for_engine``), not the
        analysis-confidence average ``_engine_fit_score`` used to compute --
        exactly the averaging #500 already fixed on the resolver's own side,
        so OpenSearch's own matched pattern gives it its real, per-query
        score (32: no search signal of its own) rather than a table-wide
        average.
        """
        qa = {"query_id": "q2", "assigned_engine": "documentdb"}
        # "aggregations" is broad enough to make OpenSearch an eligible
        # absorber at all (``SPECIALIST_ABSORB_SIGNALS``), the same signal a
        # plain COUNT(*) carries in the real pipeline -- this is not itself
        # the bug, so both candidates compete on resolver score alone.
        query_signals = {"q2": ["aggregations"]}
        query_map = {
            "q2": {"query_id": "q2", "tables_accessed": ["db.posts"], "query_type": "SELECT"}
        }
        # q2's own resolver score: dynamodb scores it well on its plain
        # table confidence. OpenSearch (a ``PATTERN_ONLY_ENGINES`` member)
        # scores it on its own matched pattern (32), never db.posts's
        # table-wide average -- a different, unrelated, genuinely
        # search-shaped query could inflate that average without this score
        # moving at all.
        ctx = ScoringContext(
            analysis_outputs={
                "dynamodb": {
                    "table_recommendations": [{"table_id": "db.posts", "confidence_score": 90}]
                },
                "opensearch": {
                    "table_recommendations": [{"table_id": "db.posts", "confidence_score": 32}],
                    "workload_analysis": {
                        "patterns_detected": [
                            {
                                "pattern_type": "aggregation-pattern",
                                "query_ids": ["q2"],
                                "table_ids": ["db.posts"],
                            }
                        ]
                    },
                },
            }
        )

        absorber = _find_best_absorber_for_query(
            qa,
            committed_engines={"dynamodb", "opensearch"},
            source_engine="documentdb",
            query_signals=query_signals,
            query_map=query_map,
            engine_queries={"dynamodb": [], "opensearch": []},
            mandatory_committed_engines=set(),
            primary_engine="dynamodb",
            ctx=ctx,
        )

        assert absorber is not None
        assert absorber["target_engine"] == "dynamodb"


class TestAuroraAbsorptionIntegration:
    """Test Aurora absorption through the full run_reality_check() flow."""

    def test_documentdb_with_a_genuine_edge_is_kept_not_absorbed(self):
        """#502 review: the old Aurora-absorption pass folded a specialist into
        Aurora whenever Aurora scored >=50 and the specialist's own edge was
        under 30 points -- a looser, Aurora-specific margin than the 15-point
        rule every other engine was judged by. That leniency is gone: one
        unified margin applies everywhere, so DocumentDB's genuine 17-point
        edge over Aurora on its own table now keeps it, same as it would for
        any other engine.
        - Aurora queries use db.main (Aurora=80, DocDB=50 → delta=30 for Aurora, unique)
        - DocDB queries use db.docs (Aurora=65, DocDB=82 → delta=17 for DocDB, >= the
          15-point threshold, so DocumentDB is kept, not consolidated away)
        """
        aurora_query_ids = [f"aq{i}" for i in range(30)]
        doc_query_ids = [f"dq{i}" for i in range(1, 6)]
        assignment = _make_assignment(
            [(f"aq{i}", "aurora_postgresql") for i in range(30)]
            + [(f"dq{i}", "documentdb") for i in range(1, 6)]
        )
        triage = {
            "selected_agents": [
                {"agent_type": "aurora_postgresql"},
                {"agent_type": "documentdb"},
            ],
            "signals": [],
        }
        # Aurora queries on db.main, DocDB queries on db.docs — different tables
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.main"], "query_type": "SELECT"}
                    for qid in aurora_query_ids
                ]
                + [
                    {"query_id": qid, "tables_accessed": ["db.docs"], "query_type": "SELECT"}
                    for qid in doc_query_ids
                ]
            }
        }
        # db.main: Aurora=80, DocDB=50 → Aurora queries have delta=30 (unique in Pass 0)
        # db.docs: Aurora=65, DocDB=82 → DocDB queries have delta=17 (unique in Pass 0,
        #          but aurora_fit=65 >= 50 and delta=17 < 30, so absorbable in Pass 1)
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 80},
                    {"table_id": "db.docs", "confidence_score": 65},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 50},
                    {"table_id": "db.docs", "confidence_score": 82},
                ]
            },
        }

        result = run_reality_check(
            assignment=assignment,
            triage=triage,
            analysis_outputs=analysis_outputs,
            collector_output=collector,
        )

        # DocumentDB is kept -- its 17-point edge on db.docs is genuine
        # unique value under the one rule every engine is judged by now.
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert {engine_of[qid] for qid in doc_query_ids} == {"documentdb"}

    def test_dynamodb_protected_by_high_delta(self):
        """DynamoDB with high-value PK lookups survives absorption despite low count."""
        query_ids = [f"aq{i}" for i in range(30)] + ["ddb1", "ddb2", "ddb3"]
        assignment = _make_assignment(
            [(f"aq{i}", "aurora_postgresql") for i in range(30)]
            + [("ddb1", "dynamodb"), ("ddb2", "dynamodb"), ("ddb3", "dynamodb")]
        )
        triage = {
            "selected_agents": [
                {"agent_type": "aurora_postgresql"},
                {"agent_type": "dynamodb"},
            ],
            "signals": [],
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.sessions"], "query_type": "SELECT"}
                    for qid in query_ids
                ]
            }
        }
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [{"table_id": "db.sessions", "confidence_score": 40}]
            },
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.sessions", "confidence_score": 95}]
            },
        }

        result = run_reality_check(
            assignment=assignment,
            triage=triage,
            analysis_outputs=analysis_outputs,
            collector_output=collector,
        )

        # DynamoDB should NOT be absorbed (delta = 95-40 = 55, well above threshold of 30)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert {engine_of[qid] for qid in ("ddb1", "ddb2", "ddb3")} == {"dynamodb"}


class TestAbsorptionAppliedToAssignments:
    """Consolidations and revised_assignments must agree (#168)."""

    @staticmethod
    def _run_partial_scenario() -> dict:
        """Aurora owns 30 join queries; DocumentDB has 5 redundant queries
        (no better than Aurora on db.cache) and 3 with a genuine edge on
        db.sessions that keeps it committed.

        ``query_capabilities`` (not just the ``complex_joins`` signal) is
        what makes Aurora's 30 queries irreplaceable (#502 review): Phase 1
        now judges engines by the resolver's own scores, which carry no
        signal-match bonus of their own, so a signal with no corresponding
        hard capability requirement protects nothing by itself.
        """
        aurora_ids = [f"aq{i}" for i in range(30)]
        doc_ids = [f"dq{i}" for i in range(1, 9)]
        assignment = _make_assignment(
            [(qid, "aurora_postgresql") for qid in aurora_ids]
            + [(qid, "documentdb") for qid in doc_ids]
        )
        triage = {
            "signals": [
                {
                    "signal": "complex_joins",
                    "targets": ["aurora_postgresql"],
                    "query_ids": aurora_ids,
                }
            ],
            "query_capabilities": {qid: ["complex_joins"] for qid in aurora_ids},
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.cache"]} for qid in aurora_ids
                ]
                + [
                    {
                        "query_id": f"dq{i}",
                        "tables_accessed": ["db.cache" if i < 6 else "db.sessions"],
                    }
                    for i in range(1, 9)
                ]
            }
        }
        analysis = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.cache", "confidence_score": 60},
                    {"table_id": "db.sessions", "confidence_score": 20},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.cache", "confidence_score": 55},
                    {"table_id": "db.sessions", "confidence_score": 85},
                ]
            },
        }
        return run_reality_check(assignment, triage, analysis, collector)

    def test_partial_absorption_moves_absorbed_queries(self):
        result = self._run_partial_scenario()
        # DocumentDB is reduced, not removed: one partial record.
        assert [
            (c["from_engine"], c["to_engine"], c["action"]) for c in result["consolidations"]
        ] == [("documentdb", "aurora_postgresql", "partial")]

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert [engine_of[f"dq{i}"] for i in range(1, 6)] == ["aurora_postgresql"] * 5
        assert [engine_of[f"dq{i}"] for i in range(6, 9)] == ["documentdb"] * 3

    def test_partial_absorption_records_reason_on_moved_queries(self):
        result = self._run_partial_scenario()
        moved = next(qa for qa in result["revised_assignments"] if qa["query_id"] == "dq1")
        # #503: display name, not the raw engine code.
        assert moved["assignment_reason"].startswith("reality check: consolidated from DocumentDB")

    def test_full_absorption_counts_each_query_once(self):
        """A fully absorbed engine yields one consolidation record set, not two."""
        aurora_ids = [f"aq{i}" for i in range(30)]
        doc_ids = [f"dq{i}" for i in range(1, 6)]
        assignment = _make_assignment(
            [(qid, "aurora_postgresql") for qid in aurora_ids]
            + [(qid, "documentdb") for qid in doc_ids]
        )
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": ["db.main"]} for qid in aurora_ids
                ]
                + [{"query_id": qid, "tables_accessed": ["db.docs"]} for qid in doc_ids]
            }
        }
        analysis = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 80},
                    # Within the 15-point margin of DocumentDB's own 72 --
                    # genuinely redundant, not a near-miss "unique" edge
                    # (#502 review: one unified margin, no Aurora-specific
                    # leniency to let a wider gap still count as absorbable).
                    {"table_id": "db.docs", "confidence_score": 70},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 50},
                    {"table_id": "db.docs", "confidence_score": 72},
                ]
            },
        }

        result = run_reality_check(assignment, {"signals": []}, analysis, collector)

        from_docdb = [c for c in result["consolidations"] if c["from_engine"] == "documentdb"]
        assert sum(c["query_count"] for c in from_docdb) == 5
        assert {c["to_engine"] for c in from_docdb} == {"aurora_postgresql"}
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert {engine_of[qid] for qid in doc_ids} == {"aurora_postgresql"}


def test_aurora_mysql_serves_inverted_index():
    """InnoDB FULLTEXT indexes give Aurora MySQL an inverted index (#165)."""
    from src.agents.referee.capability_registry import can_engine_serve_capability

    assert can_engine_serve_capability("aurora_mysql", ["inverted_index"])


def test_tiny_opensearch_absorbed_end_to_end():
    """WordPress shape: 3 text-search queries leave OpenSearch for Aurora MySQL (#165, #168)."""
    join_ids = [f"aq{i}" for i in range(7)]
    os_ids = ["os1", "os2", "os3"]
    assignment = {
        "version": 1,
        "query_assignments": [
            {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
            for i in range(20)
        ]
        + [
            {"query_id": qid, "assigned_engine": "aurora_mysql", "assignment_reason": "t"}
            for qid in join_ids
        ]
        + [
            {
                "query_id": qid,
                "assigned_engine": "opensearch",
                "assignment_reason": "signal override: text_search → opensearch",
                "signal_override": "text_search",
            }
            for qid in os_ids
        ],
    }
    triage = {
        "signals": [
            {"signal": "complex_joins", "targets": ["aurora_mysql"], "query_ids": join_ids},
            {"signal": "text_search", "targets": ["opensearch"], "query_ids": os_ids},
        ],
        "query_capabilities": {qid: ["inverted_index"] for qid in os_ids},
    }
    collector = {
        "queries": {
            "query_patterns": [
                {"query_id": f"dq{i}", "tables_accessed": ["db.options"]} for i in range(20)
            ]
            + [{"query_id": qid, "tables_accessed": ["db.posts"]} for qid in join_ids]
            + [{"query_id": qid, "tables_accessed": ["db.users"]} for qid in os_ids]
        }
    }
    analysis = {
        "dynamodb": {
            "table_recommendations": [
                {"table_id": "db.options", "confidence_score": 90},
                {"table_id": "db.posts", "confidence_score": 40},
                {"table_id": "db.users", "confidence_score": 85},
            ]
        },
        "aurora_mysql": {
            "table_recommendations": [
                {"table_id": "db.options", "confidence_score": 60},
                {"table_id": "db.posts", "confidence_score": 80},
                {"table_id": "db.users", "confidence_score": 80},
            ]
        },
        "opensearch": {"table_recommendations": [{"table_id": "db.users", "confidence_score": 60}]},
    }

    result = run_reality_check(assignment, triage, analysis, collector)

    engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
    assert {engine_of[qid] for qid in os_ids} == {"aurora_mysql"}
    from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
    assert len(from_os) == 1
    assert from_os[0]["to_engine"] == "aurora_mysql"
    assert from_os[0]["action"] == "full"
    assert from_os[0]["query_count"] == 3
    # The fixture's os_ids carry no query_text at all, so the floor's shallow
    # branch applies (no search predicate), not the native-source-override
    # branch "text_search_basic" would come from.
    assert "no search predicate" in from_os[0]["reason"]


class TestOpenSearchJustificationFloor:
    """OpenSearch is only kept as a standing engine when it earns its place (#326)."""

    def _run(
        self,
        os_query_text: str,
        os_cps: float,
        source_engine: str | None = None,
        include_aurora: bool = False,
        query_capabilities: dict[str, list[str]] | None = None,
    ):
        from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

        aurora_engine = SOURCE_ENGINE_TO_AURORA.get(source_engine or "") if include_aurora else None
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(20)
            ]
            + [
                {
                    "query_id": "search",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "signal override: text_search → opensearch",
                    "signal_override": "text_search",
                }
            ]
            + (
                [
                    {
                        "query_id": "a_mand",
                        "assigned_engine": aurora_engine,
                        "assignment_reason": "signal override: sql_admin",
                        "signal_override": "sql_admin",
                    }
                ]
                if aurora_engine
                else []
            ),
        }
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "opensearch"}]
            + ([{"agent_type": aurora_engine}] if aurora_engine else []),
            "signals": [
                {"signal": "text_search", "targets": ["opensearch"], "query_ids": ["search"]}
            ],
        }
        if query_capabilities is not None:
            # Isolates the #326 justification floor under test from the hard
            # capability gate: a real ``triage.triage()`` run would also mark
            # "search"'s own ``text_search`` signal as requiring
            # "inverted_index" (``SIGNAL_TO_CAPABILITY``) regardless of
            # whether the query shows real search depth -- some of these
            # tests are about that gate directly ("no eligible destination"
            # keeps OpenSearch because nothing else can serve it); others
            # are purely about the floor itself and pass an explicit ``{}``
            # here to keep the capability gate out of the resolver trace
            # ``run_reality_check`` re-derives internally (#549 round-2
            # review: ``ScoringContext.from_pipeline`` trusts an explicit
            # ``{}`` as-is, never recomputing it).
            triage["query_capabilities"] = query_capabilities
        collector: dict = {
            "queries": {
                "query_patterns": [
                    {"query_id": f"dq{i}", "tables_accessed": ["db.posts"], "query_type": "SELECT"}
                    for i in range(20)
                ]
                + [
                    {
                        "query_id": "search",
                        "tables_accessed": ["db.users"],
                        "query_type": "SELECT",
                        "query_text": os_query_text,
                        "calls_per_second": os_cps,
                    }
                ]
                + (
                    [
                        {
                            "query_id": "a_mand",
                            "tables_accessed": ["db.admin"],
                            "query_text": "SHOW FULL FIELDS FROM db.admin",
                        }
                    ]
                    if aurora_engine
                    else []
                )
            }
        }
        if source_engine:
            collector["metadata"] = {"source_database": {"engine": source_engine}}
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.posts", "confidence_score": 80}]
            },
            "opensearch": {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 60}]
            },
        }
        if aurora_engine:
            # Aurora has its own text-search capability (``inverted_index`` in
            # the capability registry -- tsvector/FULLTEXT), unlike DynamoDB,
            # which the resolver's own capability gate excludes outright for
            # these queries (#502 review round 6, finding 2).
            analysis[aurora_engine] = {
                "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}]
            }
        return run_reality_check(assignment, triage, analysis, collector)

    def test_like_prefix_query_at_low_traffic_is_dropped(self):
        """The wordpress shape: LIKE admin search, 4 queries at well under 1 call/s.

        An explicit, empty ``query_capabilities`` isolates the floor from
        the hard capability gate ``text_search`` would also trigger (see
        ``_run``) -- this test is about the floor, not about whether
        DynamoDB structurally can run ``LIKE ?``.
        """
        result = self._run(
            "SELECT * FROM wp_users WHERE wp_usermeta.meta_value LIKE ?",
            0.07,
            query_capabilities={},
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "dynamodb"
        from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
        assert len(from_os) == 1
        assert "justification floor" in from_os[0]["reason"] or "326" in from_os[0]["reason"]

    def test_a_floor_move_states_the_score_on_the_engine_it_left(self):
        """ADR-031 Decision 7 (#698): the reason carries before and after scores."""
        result = self._run(
            "SELECT * FROM wp_users WHERE wp_usermeta.meta_value LIKE ?",
            0.07,
            query_capabilities={},
        )
        reason = next(
            qa["assignment_reason"]
            for qa in result["revised_assignments"]
            if qa["query_id"] == "search"
        )
        assert re.search(r"OpenSearch scores \d+(\.\d+)?", reason), reason
        assert re.search(r"DynamoDB scores \d+(\.\d+)?", reason), reason
        assert "n/a" not in reason

    def test_a_floor_move_names_the_target_engine_by_its_display_name(self):
        """#761: the individual-query reason prints "DynamoDB", never the engine id."""
        from src.tools.validation.customer_text_guard import scan_text

        result = self._run(
            "SELECT * FROM wp_users WHERE wp_usermeta.meta_value LIKE ?",
            0.07,
            query_capabilities={},
        )
        reason = next(
            qa["assignment_reason"]
            for qa in result["revised_assignments"]
            if qa["query_id"] == "search"
        )
        assert "DynamoDB can serve this pattern" in reason, reason
        assert "dynamodb" not in reason, reason
        assert [f for f in scan_text(reason) if f.kind == "engine_id"] == []

    def test_the_customer_text_guard_catches_a_raw_engine_id_in_that_reason(self):
        """#761: the guard flags the pre-fix sentence, so a regression cannot ship unseen."""
        from src.tools.validation.customer_text_guard import scan_text

        findings = scan_text("fit=80, dynamodb can serve this pattern; OpenSearch scores 0.0")
        assert [(f.kind, f.text) for f in findings] == [("engine_id", "dynamodb")]

    def test_plain_join_with_no_search_predicate_is_dropped(self):
        """The Action Scheduler shape: a join with no text predicate at all.

        Same isolation as above -- a real triage run would not even raise
        the ``text_search`` signal for this query at all, so an explicit,
        empty ``query_capabilities`` is the more realistic choice here.
        """
        result = self._run(
            "SELECT a.action_id FROM actions a LEFT JOIN groups g ON g.id = a.group_id",
            0.45,
            query_capabilities={},
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "dynamodb"

    def test_real_depth_at_sufficient_traffic_is_kept(self):
        """MATCH...AGAINST at traffic above the floor, with no native source engine, is justified."""
        result = self._run("SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)", 5.0)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "opensearch"
        assert not any(c["from_engine"] == "opensearch" for c in result["consolidations"])

    def test_native_postgres_tsvector_at_low_traffic_prefers_aurora(self):
        """discourse shape: to_tsvector construction, native to Postgres, at tiny traffic.

        Aurora PostgreSQL is committed here (``include_aurora=True``) and has
        its own text-search capability (``inverted_index`` in the capability
        registry, via tsvector) -- unlike DynamoDB, which the resolver's own
        capability gate excludes outright for this query (#502 review round
        5, finding N3: confirmed directly against the real resolver trace --
        ``{"gate": "capability"}``). The floor moves the query to the
        source-compatible Aurora engine rather than leaving it on
        OpenSearch, which still fails its own traffic-based justification.
        """
        result = self._run(
            "SELECT to_tsvector($1, $2)", 0.03, source_engine="postgresql", include_aurora=True
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "aurora_postgresql"
        from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
        assert len(from_os) == 1
        assert from_os[0]["to_engine"] == "aurora_postgresql"

    def test_real_depth_insufficient_traffic_for_native_source_is_dropped(self):
        """A tsvector/MATCH query from a native source needs much higher traffic to justify
        OpenSearch over the source engine's own text_search_basic.

        Same reasoning as above: Aurora MySQL is committed and has its own
        text-search capability, unlike DynamoDB, so the floor moves the
        query there instead of leaving it on OpenSearch.
        """
        result = self._run(
            "SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)",
            5.0,
            source_engine="mysql",
            include_aurora=True,
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "aurora_mysql"
        from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
        assert len(from_os) == 1
        assert from_os[0]["to_engine"] == "aurora_mysql"

    def test_native_postgres_tsvector_with_no_eligible_destination_is_kept(self):
        """#502 review round 6, finding 2: without Aurora committed, DynamoDB
        is the only other engine and the resolver's own capability gate
        excludes it outright for a tsvector query (confirmed directly
        against the trace -- ``{"gate": "capability"}``), so there is
        nowhere eligible to send it. The floor must report this explicitly
        -- a reason naming the lack of an eligible engine -- rather than
        silently leaving ``revised_assignments`` unchanged with no trace of
        why.
        """
        result = self._run("SELECT to_tsvector($1, $2)", 0.03, source_engine="postgresql")
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "opensearch"
        assert not any(c["from_engine"] == "opensearch" for c in result["consolidations"])
        kept = result["unique_value_assessment"]["opensearch"]
        assert kept.get("floor_unplaced_queries") == ["search"]
        assert "no eligible engine" in kept.get("floor_unplaced_reason", "")

    def test_real_depth_with_no_eligible_destination_is_kept(self):
        """Same as above for the MATCH ... AGAINST / mysql shape, without Aurora."""
        result = self._run(
            "SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)", 5.0, source_engine="mysql"
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] == "opensearch"
        assert not any(c["from_engine"] == "opensearch" for c in result["consolidations"])
        kept = result["unique_value_assessment"]["opensearch"]
        assert kept.get("floor_unplaced_queries") == ["search"]
        assert "no eligible engine" in kept.get("floor_unplaced_reason", "")

    def test_never_justified_removal_claims_no_saving(self):
        """#475 review, finding 3: when none of OpenSearch's FIRST-ASSIGNMENT
        queries ever clear the floor, the recommendation text must not
        present its removal as an infrastructure saving -- even though a
        real cost estimate exists (``_engine_infra_cost`` is not patched out
        of this fixture's bare ``analysis`` dict here, so the "Removes $"
        branch is the only other thing that could fire). An explicit, empty
        ``query_capabilities`` isolates the floor under test from the hard
        capability gate the same way ``test_like_prefix_query_at_low_
        traffic_is_dropped`` does.
        """
        result = self._run(
            "SELECT * FROM wp_users WHERE wp_usermeta.meta_value LIKE ?",
            0.07,
            query_capabilities={},
        )
        os_recs = [r for r in result["recommendations"] if "opensearch" in r.lower()]
        assert os_recs, result["recommendations"]
        assert "No saving is claimed" in os_recs[0]
        assert "never justified by the workload" in os_recs[0]
        assert "Removes $" not in os_recs[0]
        from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
        assert all(c.get("workload_justified") is False for c in from_os)

    def test_justified_removal_still_claims_the_real_saving(self):
        """The new wording only fires when nothing ever justified the engine
        -- a genuinely justified-then-redundant removal still names the real
        dollar figure (unaffected: #475 review, finding 3's negative case).
        """
        result = self._run("SELECT * FROM posts WHERE MATCH(title, body) AGAINST (?)", 5.0)
        assert not any(c["from_engine"] == "opensearch" for c in result["consolidations"])
        # dynamodb's own analysis has no cost_estimate in this fixture, so the
        # "no specific infrastructure-cost figure" branch names it instead --
        # neither claims a saving it can't ground, nor the new wording, since
        # dynamodb is never subject to the opensearch-only #326 floor.
        assert all(c.get("workload_justified") is None for c in result["consolidations"])


class TestOpenSearchFloorAppliesWithoutMandatoryStatus:
    """#475 review round 1 follow-up: the #326 floor must run for OpenSearch
    whenever it is committed at all, not only when a signal override made
    it "mandatory" -- otherwise a query that wins OpenSearch purely on fit
    score (its own matched pattern, no override involved) is never
    re-examined, reopening the gap this PR closes.
    """

    def test_opensearch_wins_on_score_alone_and_the_floor_still_moves_it(self):
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(20)
            ]
            + [
                {
                    "query_id": "search",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "highest confidence for opensearch",
                }
            ],
        }
        # No text_search (or any) signal for "search" at all: it is not a
        # signal override, so it never becomes "mandatory".
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "opensearch"}],
            "signals": [],
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": f"dq{i}", "tables_accessed": ["db.posts"], "query_type": "SELECT"}
                    for i in range(20)
                ]
                + [
                    {
                        "query_id": "search",
                        "tables_accessed": ["db.users"],
                        "query_type": "SELECT",
                        # No search predicate at all -- shallow, whatever the score says.
                        "query_text": (
                            "SELECT a.id FROM db.users a LEFT JOIN db.groups g ON g.id=a.group_id"
                        ),
                        "calls_per_second": 0.3,
                    }
                ]
            }
        }
        analysis = {
            "dynamodb": {
                "table_recommendations": [{"table_id": "db.posts", "confidence_score": 80}]
            },
            # OpenSearch's own matched pattern gives it a genuinely high,
            # query-level score (not a table-wide bleed) -- high enough that
            # Pass 0 would call it "unique" and never flag it for ordinary
            # consolidation. Only the floor (now checking every committed
            # OpenSearch, not just mandatory ones) catches it.
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

        result = run_reality_check(assignment, triage, analysis, collector)

        assert result["unique_value_assessment"]["opensearch"]["unique_ratio"] == 1.0
        assert not result["unique_value_assessment"]["opensearch"]["is_mandatory"]
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] != "opensearch"
        from_os = [c for c in result["consolidations"] if c["from_engine"] == "opensearch"]
        assert from_os
        assert all(c.get("workload_justified") is False for c in from_os)
        assert all(c["saved_cost_estimate"] == 0 for c in from_os)
        assert any("justification floor" in c["reason"] for c in from_os)


class TestInCallReconcileAvoidsOrphanedRecords:
    """#475 review round 1 follow-up: widening the floor to any committed
    OpenSearch means a query Pass 2 just placed on OpenSearch can be moved
    AGAIN by the same floor within one call -- a two-hop move no single
    consolidation record describes, unless ``run_reality_check`` reconciles
    before returning. Reverting that reconciliation call breaks this test
    (and ``test_reality_check_request.py``'s external-mode fixtures) by
    leaving a stale record ``moved_queries()`` cannot resolve back to any
    query.
    """

    def test_queries_absorbed_then_floor_rejected_land_on_one_clean_record(self):
        # dynamodb and documentdb own enough queries, matching OpenSearch's
        # own "aggregations" capability closely enough, that OpenSearch
        # looks like a plausible absorber for aurora_mysql's redundant
        # COUNT(*) queries during Pass 2 -- then the floor (shallow, no
        # search depth) rejects all of OpenSearch's resulting membership and
        # redirects it onward in the SAME call.
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": "d0", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {"query_id": "am0", "assigned_engine": "aurora_mysql", "assignment_reason": "t"},
                {
                    "query_id": "search",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "highest confidence for opensearch",
                },
            ],
        }
        triage = {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "aurora_mysql"},
                {"agent_type": "opensearch"},
            ],
            "signals": [
                {"signal": "aggregations", "targets": ["dynamodb"], "query_ids": ["am0"]},
            ],
        }
        collector = {
            "metadata": {"source_database": {"engine": "mysql"}},
            "queries": {
                "query_patterns": [
                    {"query_id": "d0", "tables_accessed": ["db.a"], "query_type": "SELECT"},
                    {
                        "query_id": "am0",
                        "tables_accessed": ["db.posts"],
                        "query_type": "SELECT",
                        "query_text": "SELECT COUNT(*) FROM db.posts",
                    },
                    {
                        "query_id": "search",
                        "tables_accessed": ["db.users"],
                        "query_type": "SELECT",
                        "query_text": (
                            "SELECT a.id FROM db.users a LEFT JOIN db.groups g ON g.id=a.group_id"
                        ),
                        "calls_per_second": 0.3,
                    },
                ]
            },
        }
        analysis = {
            "dynamodb": {"table_recommendations": [{"table_id": "db.a", "confidence_score": 80}]},
            "aurora_mysql": {
                "table_recommendations": [{"table_id": "db.posts", "confidence_score": 60}]
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

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["search"] != "opensearch"
        # Every record is self-consistent with the final assignment: no
        # record claims more queries moved to an engine than actually sit
        # there, and none describes a query that never actually took that
        # path (the orphaned-record bug this guards against).
        for c in result["consolidations"]:
            moved = [
                qa
                for qa in result["revised_assignments"]
                if qa["query_id"]
                in {
                    q["query_id"]
                    for q in assignment["query_assignments"]
                    if q["assigned_engine"] == c["from_engine"]
                }
                and qa["assigned_engine"] == c["to_engine"]
            ]
            assert len(moved) == c["query_count"], c


class TestWorkloadEverJustified:
    """Direct tests of ``_workload_ever_justified`` (#475 review, finding 3)."""

    def test_none_when_no_floor_applies(self):
        qas = [{"query_id": "q1"}]
        query_map = {"q1": {"query_text": "SELECT * FROM t WHERE id = $1", "calls_per_second": 50}}
        assert _workload_ever_justified("dynamodb", qas, query_map, None) is None

    def test_none_when_engine_owns_nothing(self):
        assert _workload_ever_justified("opensearch", [], {}, None) is None

    def test_false_when_no_first_assignment_query_clears_the_floor(self):
        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE name LIKE ?", "calls_per_second": 0.1}
        }
        assert _workload_ever_justified("opensearch", qas, query_map, None) is False

    def test_true_when_any_first_assignment_query_clears_the_floor(self):
        qas = [{"query_id": "shallow"}, {"query_id": "deep"}]
        query_map = {
            "shallow": {"query_text": "SELECT * FROM t WHERE name LIKE ?", "calls_per_second": 0.1},
            "deep": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 5.0,
            },
        }
        assert _workload_ever_justified("opensearch", qas, query_map, None) is True


class TestOpenSearchJustificationFunction:
    """Direct tests of opensearch_justification() (#326). It now returns a
    per-query {query_id: (justified, reason)} dict (review finding B1)."""

    def test_no_depth_fails(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE name LIKE ?", "calls_per_second": 10.0}
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_depth_but_low_traffic_fails(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 0.01,
            }
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "calls/s" in reason

    def test_depth_and_traffic_passes(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True


class TestOpenSearchPerQueryDepth:
    """Search depth is decided per query, not for the whole domain by one
    deep query (review finding 4, and finding B1: a shallow query must not
    sink a deep one, and the floor is judged only across the deep ones)."""

    def test_one_deep_query_does_not_unlock_a_shallow_one(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "deep"}, {"query_id": "shallow"}]
        query_map = {
            "deep": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 5.0,
            },
            "shallow": {
                "query_text": "SELECT * FROM t WHERE name LIKE ?",
                "calls_per_second": 5.0,
            },
        }
        verdicts = opensearch_justification(qas, query_map, None)
        assert verdicts["shallow"][0] is False
        assert verdicts["deep"][0] is True

    def test_shallow_key_lookup_does_not_sink_a_real_search_at_high_traffic(self):
        """Review finding B1: a key lookup sitting next to a real `@@ plainto_tsquery`
        search at 50 calls/s must not drop the search too -- only the lookup moves."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "search"}, {"query_id": "lookup"}]
        query_map = {
            "search": {
                "query_text": "SELECT * FROM posts WHERE body_tsv @@ plainto_tsquery($1)",
                "calls_per_second": 50.0,
            },
            "lookup": {
                "query_text": "SELECT * FROM posts WHERE id = $1",
                "calls_per_second": 0.5,
            },
        }
        verdicts = opensearch_justification(qas, query_map, None)
        assert verdicts["lookup"][0] is False
        assert verdicts["search"][0] is True

    def test_bare_group_by_is_not_a_facet(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT status, COUNT(*) FROM orders GROUP BY status",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_search_predicate_with_group_by_in_same_query_is_a_facet(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": (
                    "SELECT category, COUNT(*) FROM products WHERE name LIKE ? "
                    "AND to_tsvector(name) @@ to_tsquery(?) GROUP BY category"
                ),
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_create_extension_is_not_search(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "CREATE EXTENSION IF NOT EXISTS pg_trgm",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_tsvector_document_construction_is_not_search(self):
        """A plain to_tsvector() call with no @@ match is building a document, not searching."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {"q1": {"query_text": "SELECT to_tsvector($1, $2)", "calls_per_second": 20.0}}
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_large_corpus_justifies_below_the_traffic_floor(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 0.1,
                "rows_returned_avg": 5000,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_small_corpus_and_low_traffic_fails(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE MATCH(a,b) AGAINST (?)",
                "calls_per_second": 0.1,
                "rows_returned_avg": 10,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False


class TestOpenSearchFuzzyMatchRegex:
    """Review finding B1: _FUZZY_MATCH_RE was inverted -- it matched a LIKE
    pattern's own wildcard and missed the real pg_trgm "%" operator."""

    def test_like_prefix_wildcard_is_not_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE name LIKE 'abc%'", "calls_per_second": 50.0}
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_like_both_sided_wildcard_is_not_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE name LIKE '%abc%'",
                "calls_per_second": 50.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_modulo_on_a_parameter_is_not_fuzzy_matching_inside_a_literal(self):
        """id % $2 used to match the old regex purely because of the literal check
        order; confirm a literal containing a percent sign is not mistaken for it."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE tag = 'sale%'", "calls_per_second": 50.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_real_trigram_operator_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE title % $1", "calls_per_second": 5.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_real_trigram_operator_against_a_literal_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE title % 'search term'",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_similarity_function_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE similarity(title, $1) > 0.3",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_word_similarity_function_is_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {
                "query_text": "SELECT * FROM t WHERE word_similarity(title, $1) > 0.3",
                "calls_per_second": 5.0,
            }
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_modulo_against_a_parameter_followed_by_a_comparison_is_not_fuzzy_matching(self):
        """A second review of #375: `id % $2 = 0` is modulo arithmetic, not the
        pg_trgm similarity operator -- the real operator is a boolean predicate
        on its own, never immediately followed by a comparison operator."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE id % $2 = 0", "calls_per_second": 50.0}
        }
        justified, reason = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False
        assert "no search predicate" in reason

    def test_modulo_against_a_parameter_followed_by_arithmetic_is_not_fuzzy_matching(self):
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE id % $2 + 1 = 0", "calls_per_second": 50.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is False

    def test_pg_trgm_word_similarity_right_threshold_operator_is_fuzzy_matching(self):
        """The pg_trgm `%>` ("right word is similar enough") operator."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE title %> $1", "calls_per_second": 5.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True

    def test_pg_trgm_word_similarity_left_threshold_operator_is_fuzzy_matching(self):
        """The pg_trgm `<%` ("left word is similar enough") operator."""
        from src.agents.referee.reality_check import opensearch_justification

        qas = [{"query_id": "q1"}]
        query_map = {
            "q1": {"query_text": "SELECT * FROM t WHERE $1 <% title", "calls_per_second": 5.0}
        }
        justified, _ = opensearch_justification(qas, query_map, None)["q1"]
        assert justified is True


class TestUtilityStatementsSurviveRealityCheck:
    """The #327 utility pin must survive the reality check, not just the
    assignment resolver (review finding 1): without "sql_admin", Aurora looked
    redundant by fit score and every utility statement moved to DynamoDB in v2."""

    def test_utility_statements_stay_on_aurora_even_when_aurora_looks_redundant(self):
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(20)
            ]
            + [
                {"query_id": f"aq{i}", "assigned_engine": "aurora_mysql", "assignment_reason": "t"}
                for i in range(3)
            ]
            + [
                {
                    "query_id": "show1",
                    "assigned_engine": "aurora_mysql",
                    "assignment_reason": "utility/metadata statement",
                },
                {
                    "query_id": "set1",
                    "assigned_engine": "aurora_mysql",
                    "assignment_reason": "utility/metadata statement",
                },
            ],
        }
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "aurora_mysql"}],
            "signals": [],
            "query_capabilities": {"show1": ["sql_admin"], "set1": ["sql_admin"]},
        }
        collector = {
            "queries": {
                "query_patterns": [
                    {"query_id": f"dq{i}", "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for i in range(20)
                ]
                + [
                    {"query_id": f"aq{i}", "tables_accessed": ["db.users"], "query_type": "SELECT"}
                    for i in range(3)
                ]
                + [
                    {
                        "query_id": "show1",
                        "tables_accessed": ["db.options"],
                        "query_type": "OTHER",
                        "query_text": "SHOW FULL FIELDS FROM wp_options",
                    },
                    {
                        "query_id": "set1",
                        "tables_accessed": ["db.options"],
                        "query_type": "OTHER",
                        "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
                    },
                ]
            }
        }
        # aurora_mysql scores identically to dynamodb on db.users (redundant by
        # fit score, no hard capability involved) but much worse on db.options
        # -- the utility queries' table -- so without the sql_admin gate the
        # reality check would judge aurora_mysql fully redundant.
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 80},
                    {"table_id": "db.options", "confidence_score": 80},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.users", "confidence_score": 80},
                    {"table_id": "db.options", "confidence_score": 10},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["show1"] == "aurora_mysql"
        assert engine_of["set1"] == "aurora_mysql"


# ---------------------------------------------------------------------------
# #501: Pass 0's elimination order must never decide whether a small engine
# survives, and a query must never be moved to an engine that scores it worse
# than the engine it is already on.
# ---------------------------------------------------------------------------


def _make_small_engine_scenario(
    order: tuple[str, str, str] = ("dynamodb", "aurora_mysql", "documentdb"),
    equal_cost: bool = False,
    include_tied_aurora_queries: bool = True,
) -> tuple[dict, dict, dict, dict]:
    """The wordpress #502 shape: a cheap engine (dynamodb), an expensive
    engine with a couple of hard-capability-only queries (aurora_mysql,
    protected by ``sql_admin``), and a smaller engine (documentdb) with a
    thin, barely-"unique"-by-margin workload that the ``TARGET_ENGINE_COUNT``
    burden check removes once there are 3 committed engines.

    documentdb's 3 queries score 80 on documentdb itself, 60 on dynamodb and
    85 on aurora_mysql -- Phase 1 judges each engine against the full
    committed set from the start (#502review), so this is no longer about
    hiding an alternative; it is a genuine "a $200/mo engine with 3 queries
    isn't worth it once two cheaper engines already cover them" call.

    ``order`` is the order the three engines' query blocks appear in
    ``query_assignments`` -- Phase 1 settles the SAME committed engine set
    and Phase 2 the SAME placement regardless of this order (that is what
    the permutation tests below check). ``equal_cost`` ties aurora_mysql's
    and documentdb's ``ENGINE_BASE_COST``, removing cost as a tiebreaker too.

    ``include_tied_aurora_queries`` adds ``a1``-``a4`` (tables Aurora MySQL
    and DynamoDB score identically, 80), which is what forces Aurora MySQL's
    own consolidation to be partial rather than skipped outright. An
    order-independence check that also ties Aurora MySQL's and DocumentDB's
    cost does not want that extra, genuinely-tied group in play too --
    which of two engines with an identical score for the same queries ends
    up keeping them is a separate, pre-existing ambiguity (#503), not the
    one this scenario is isolating.
    """
    aurora_block = [("util1", "aurora_mysql"), ("util2", "aurora_mysql")]
    if include_tied_aurora_queries:
        aurora_block = [
            ("a1", "aurora_mysql"),
            ("a2", "aurora_mysql"),
            ("a3", "aurora_mysql"),
            ("a4", "aurora_mysql"),
        ] + aurora_block
    blocks = {
        "dynamodb": [("d1", "dynamodb"), ("d2", "dynamodb"), ("d3", "dynamodb")],
        "aurora_mysql": aurora_block,
        "documentdb": [("b1", "documentdb"), ("b2", "documentdb"), ("b3", "documentdb")],
    }
    pairs = [pair for engine in order for pair in blocks[engine]]
    assignment = _make_assignment(pairs)

    triage = {
        "selected_agents": [
            {"agent_type": "dynamodb"},
            {"agent_type": "aurora_mysql"},
            {"agent_type": "documentdb"},
        ],
        "signals": [],
        "query_capabilities": {"util1": ["sql_admin"], "util2": ["sql_admin"]},
    }

    extra: dict[str, dict[str, object]] = {}
    for qid in ("d1", "d2", "d3"):
        extra[qid] = {"tables_accessed": ["db.d"]}
    for qid in ("a1", "a2", "a3", "a4"):
        extra[qid] = {"tables_accessed": ["db.a"]}
    extra["util1"] = {"tables_accessed": ["db.util"], "query_text": "SHOW FULL FIELDS FROM db.util"}
    extra["util2"] = {
        "tables_accessed": ["db.util"],
        "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
    }
    for qid in ("b1", "b2", "b3"):
        extra[qid] = {"tables_accessed": ["db.b"]}
    collector = _make_collector([qid for qid, _ in pairs], extra=extra)

    analysis = {
        "dynamodb": {
            "table_recommendations": [
                {"table_id": "db.d", "confidence_score": 90},
                {"table_id": "db.a", "confidence_score": 80},
                {"table_id": "db.util", "confidence_score": 30},
                {"table_id": "db.b", "confidence_score": 60},
            ]
        },
        "aurora_mysql": {
            "table_recommendations": [
                {"table_id": "db.d", "confidence_score": 50},
                {"table_id": "db.a", "confidence_score": 80},
                {"table_id": "db.util", "confidence_score": 60},
                {"table_id": "db.b", "confidence_score": 85},
            ]
        },
        "documentdb": {
            "table_recommendations": [
                {"table_id": "db.b", "confidence_score": 80},
            ]
        },
    }

    return assignment, triage, collector, analysis


class TestRealityCheckRechecksSmallEngines:
    """#502: Phase 1 settles the committed engine set before anything is
    placed, judging every engine against the full committed set from the
    start -- not a sequential pass that can shrink the alternatives one
    engine sees before another is even checked. DocumentDB's thin,
    barely-"unique"-by-margin workload fails the TARGET_ENGINE_COUNT burden
    check once DynamoDB and Aurora MySQL already cover it, so it is
    removed -- not kept on an evaluation-order technicality."""

    def test_wordpress_shape_removes_documentdb(self):
        assignment, triage, collector, analysis = _make_small_engine_scenario()

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}

        # DocumentDB provides no unique value once judged against the full
        # committed set from the start: it is removed, not kept on a
        # 2-engine technicality.
        assert engine_of["b1"] == "aurora_mysql"
        assert engine_of["b2"] == "aurora_mysql"
        assert engine_of["b3"] == "aurora_mysql"
        after_engines = {qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert "documentdb" not in after_engines

        # Aurora MySQL's own redundant queries are an exact tie with
        # DynamoDB (both score 80). Neither engine is source-compatible
        # here, so ``_pick_winner`` falls to its second tie rule: the
        # query's current engine wins a tie ("stability"), not an
        # alphabetical rule -- aurora_mysql keeps a1-a4 because that is
        # where they already are, not because its name sorts first. Its
        # sql_admin-only queries stay put regardless.
        assert engine_of["a1"] == "aurora_mysql"
        assert engine_of["util1"] == "aurora_mysql"
        assert engine_of["util2"] == "aurora_mysql"


class TestRemovalDoesNotStrandAnInheritedRescue:
    """#485 review: removing engine A can rescue a query/group onto engine

    B when B is the ONLY other committed engine eligible for it (the same
    ``_group_eligible_engines`` gate Phase 1 and Phase 2 share). That
    rescue must itself be protected in later rounds: if B is then judged
    removable purely on ITS OWN, unrelated native queries, B must stay
    committed anyway, because the rescued query now depends on it and has
    no other home. Before this fix, ``_evaluate_engine_removal`` only
    checked an engine's own ``native_qas`` (frozen from the first
    assignment, never updated as other engines are removed), so B's
    rescue of A's query was invisible to B's own removability check --
    B was removed anyway, and the rescued query was left pointing at A, a
    removed engine, tripping ``run_reality_check``'s own stranded-query
    invariant.

    Mirrors the production shape #485 exposed: Aurora MySQL's only
    native query/group needs a pattern only DocumentDB (among the other
    committed engines) can serve; DocumentDB's own native queries are
    otherwise fully redundant with DynamoDB. Removing Aurora MySQL first
    (highest burden) rescues that query onto DocumentDB -- DocumentDB must
    not then be removed too, stranding it.
    """

    def test_rescuer_engine_is_protected_once_it_inherits_a_dependent_query(self):
        engine_queries = {
            "aurora_mysql": [
                {"query_id": "q1", "assigned_engine": "aurora_mysql", "assignment_reason": "test"},
            ],
            "documentdb": [
                {"query_id": "qd1", "assigned_engine": "documentdb", "assignment_reason": "test"},
                {"query_id": "qd2", "assigned_engine": "documentdb", "assignment_reason": "test"},
            ],
            "dynamodb": [
                {"query_id": "qk1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
            ],
        }
        current_engine_by_qid = {
            "q1": "aurora_mysql",
            "qd1": "documentdb",
            "qd2": "documentdb",
            "qk1": "dynamodb",
        }
        trace_by_query = {
            # Only Aurora MySQL and DocumentDB can serve q1 at all (DynamoDB
            # has no score -- simulating a capability DynamoDB structurally
            # lacks); DocumentDB is the better fit, so q1 looks redundant
            # (not "unique") for Aurora MySQL on its own.
            "q1": {"engines": {"aurora_mysql": 70, "documentdb": 90}},
            # DocumentDB's own native queries are fully redundant with
            # DynamoDB -- nothing protects DocumentDB on its own merits.
            "qd1": {"engines": {"documentdb": 80, "dynamodb": 95}},
            "qd2": {"engines": {"documentdb": 80, "dynamodb": 95}},
            # DynamoDB's own query has no alternative anywhere -- always
            # irreplaceable, keeping DynamoDB committed throughout so the
            # scenario has a stable floor.
            "qk1": {"engines": {"dynamodb": 95}},
        }

        committed, assessment = _settle_committed_engines(
            engine_queries=engine_queries,
            mandatory_query_ids=set(),
            mandatory_committed_engines=set(),
            trace_by_query=trace_by_query,
            current_engine_by_qid=current_engine_by_qid,
            query_signals={},
            query_map={},
            query_capabilities=None,
            max_rounds=5,
        )

        # Aurora MySQL earns no unique value against DocumentDB and is
        # removed. DocumentDB, despite having no unique value of its OWN
        # either, must stay: it is q1's only remaining eligible home now
        # that Aurora MySQL is gone.
        assert committed == {"documentdb", "dynamodb"}
        assert assessment["documentdb"]["irreplaceable_queries"] == ["q1"]

        # #485 review (Important 2): the stranded, inherited query must not
        # wipe DocumentDB's OWN, real native stats -- qd1/qd2 really are
        # redundant (DynamoDB scores them higher) and DocumentDB's own
        # unique_ratio is really 0, both computed from native_qas alone,
        # unaffected by q1 landing in irreplaceable_queries too.
        assert assessment["documentdb"]["redundant_queries"] == ["qd1", "qd2"]
        assert assessment["documentdb"]["unique_queries"] == []
        assert assessment["documentdb"]["unique_ratio"] == 0.0
        assert assessment["documentdb"]["total_queries"] == 2


class TestRemovalTiesAreResolvedByDataNotName:
    """#502 review round 5, finding N1: the reviewer's reproduction --
    "two engines at 150 per unique query".

    DynamoDB and DocumentDB (its ``ENGINE_BASE_COST`` monkeypatched to 0,
    tying it with DynamoDB's) each have exactly 2 queries that clear
    ``UNIQUE_DELTA_THRESHOLD`` against their only eligible alternative,
    Aurora MySQL (90 vs 70, delta 20). With 3 engines committed (over
    ``TARGET_ENGINE_COUNT``), both get the identical rank key: ``(0 + 300)
    / 2 = 150``. The round-4 draft broke this tie by engine name
    (``documentdb`` sorts before ``dynamodb``), removing DocumentDB on an
    arbitrary technicality. ``_removal_tiebreak_key`` resolves ties by
    data instead -- simulating each removal and comparing (fewer distinct
    destination engines, then a higher total placed resolver score) -- and
    here BOTH engines redistribute to the exact same single destination
    (Aurora MySQL) for the exact same total score (140), so the tie
    survives even that: neither is removed, both stay committed, and both
    are flagged ``tie_unresolved`` in the assessment rather than one being
    picked by name.
    """

    def test_two_engines_tied_at_150_per_unique_query_are_both_kept(self, monkeypatch):
        import src.agents.referee.reality_check as reality_check

        monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "documentdb", 0)

        engine_queries = {
            "dynamodb": [
                {"query_id": "d1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
                {"query_id": "d2", "assigned_engine": "dynamodb", "assignment_reason": "test"},
            ],
            "documentdb": [
                {"query_id": "m1", "assigned_engine": "documentdb", "assignment_reason": "test"},
                {"query_id": "m2", "assigned_engine": "documentdb", "assignment_reason": "test"},
            ],
            "aurora_mysql": [
                {
                    "query_id": "a_mand",
                    "assigned_engine": "aurora_mysql",
                    "assignment_reason": "signal override: test",
                    "signal_override": "test",
                }
            ],
        }
        current_engine_by_qid = {
            "d1": "dynamodb",
            "d2": "dynamodb",
            "m1": "documentdb",
            "m2": "documentdb",
            "a_mand": "aurora_mysql",
        }
        trace_by_query = {
            "d1": {"engines": {"dynamodb": 90, "aurora_mysql": 70}},
            "d2": {"engines": {"dynamodb": 90, "aurora_mysql": 70}},
            "m1": {"engines": {"documentdb": 90, "aurora_mysql": 70}},
            "m2": {"engines": {"documentdb": 90, "aurora_mysql": 70}},
        }

        committed, assessment = _settle_committed_engines(
            engine_queries=engine_queries,
            mandatory_query_ids={"a_mand"},
            mandatory_committed_engines={"aurora_mysql"},
            trace_by_query=trace_by_query,
            current_engine_by_qid=current_engine_by_qid,
            query_signals={},
            query_map={},
            query_capabilities=None,
            max_rounds=5,
        )

        assert committed == {"dynamodb", "documentdb", "aurora_mysql"}
        assert assessment["dynamodb"]["tie_unresolved"] is True
        assert assessment["documentdb"]["tie_unresolved"] is True

    def test_fewer_destinations_tie_broken_by_least_total_fit_lost(self, monkeypatch):
        """#502 review round 6: the reviewer's reproduction.

        DynamoDB has 2 queries at 90 vs 70 (fit lost 20 each, 40 total).
        DocumentDB (``ENGINE_BASE_COST`` monkeypatched to 0, tying it with
        DynamoDB's) has those same 2 queries' shape PLUS 4 more at 75 vs 70
        (fit lost 5 each) -- redundant, not unique (delta 5 < 15), so they
        do not change DocumentDB's rank key (still (0 + 300) / 2 = 150, tied
        with DynamoDB's), but they DO add to DocumentDB's total fit lost:
        40 (from its 2 unique queries) + 4 * 5 = 60. Both candidates land
        every query on the same single destination (Aurora MySQL), so they
        tie on destinations (1 each) -- the decisive measure is total fit
        lost, and DynamoDB's (40) is less than DocumentDB's (60), so
        DynamoDB -- not DocumentDB -- is the one removed. (The round-5
        tiebreaker, which compared total placed score instead of fit lost,
        would have agreed by coincidence here since every query lands at
        the same destination score; the distinguishing scenario is any
        case where the two candidates' OWN scores differ, which this fixed
        version accounts for and the round-5 version did not.)
        """
        import src.agents.referee.reality_check as reality_check

        monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "documentdb", 0)

        engine_queries = {
            "dynamodb": [
                {"query_id": "d1", "assigned_engine": "dynamodb", "assignment_reason": "test"},
                {"query_id": "d2", "assigned_engine": "dynamodb", "assignment_reason": "test"},
            ],
            "documentdb": [
                {"query_id": f"m{i}", "assigned_engine": "documentdb", "assignment_reason": "test"}
                for i in range(1, 7)
            ],
            "aurora_mysql": [
                {
                    "query_id": "a_mand",
                    "assigned_engine": "aurora_mysql",
                    "assignment_reason": "signal override: test",
                    "signal_override": "test",
                }
            ],
        }
        current_engine_by_qid = {
            "d1": "dynamodb",
            "d2": "dynamodb",
            **{f"m{i}": "documentdb" for i in range(1, 7)},
            "a_mand": "aurora_mysql",
        }
        trace_by_query = {
            "d1": {"engines": {"dynamodb": 90, "aurora_mysql": 70}},
            "d2": {"engines": {"dynamodb": 90, "aurora_mysql": 70}},
            "m1": {"engines": {"documentdb": 90, "aurora_mysql": 70}},
            "m2": {"engines": {"documentdb": 90, "aurora_mysql": 70}},
            "m3": {"engines": {"documentdb": 75, "aurora_mysql": 70}},
            "m4": {"engines": {"documentdb": 75, "aurora_mysql": 70}},
            "m5": {"engines": {"documentdb": 75, "aurora_mysql": 70}},
            "m6": {"engines": {"documentdb": 75, "aurora_mysql": 70}},
        }

        committed, assessment = _settle_committed_engines(
            engine_queries=engine_queries,
            mandatory_query_ids={"a_mand"},
            mandatory_committed_engines={"aurora_mysql"},
            trace_by_query=trace_by_query,
            current_engine_by_qid=current_engine_by_qid,
            query_signals={},
            query_map={},
            query_capabilities=None,
            max_rounds=5,
        )

        assert committed == {"documentdb", "aurora_mysql"}
        assert "dynamodb" not in committed
        assert not assessment["dynamodb"].get("tie_unresolved")
        assert not assessment["documentdb"].get("tie_unresolved")


class TestSettledEngineSetIsOrderIndependent:
    """#502: Phase 1 must settle on the SAME committed engine set and Phase 2
    on the SAME placement no matter what order the engines' queries happen
    to appear in the input assignment. Checked exhaustively (every
    permutation of the engine blocks), not just the two orderings that
    happened to expose the original #501 bug."""

    def test_all_permutations_of_the_wordpress_shape_agree(self):
        engines = ("dynamodb", "aurora_mysql", "documentdb")
        results = []
        for order in itertools.permutations(engines):
            assignment, triage, collector, analysis = _make_small_engine_scenario(order=order)
            result = run_reality_check(assignment, triage, analysis, collector)
            engine_of = {
                qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]
            }
            results.append(engine_of)

        assert all(
            r == results[0] for r in results[1:]
        ), f"placement differs by input order: {results}"
        # And it is the correct (not just the identical) answer every time.
        assert results[0]["b1"] == "aurora_mysql"
        assert "documentdb" not in set(results[0].values())

    def test_all_permutations_with_two_equal_cost_engines_agree(self):
        """Aurora MySQL and DocumentDB given the identical ENGINE_BASE_COST
        removes cost as a tiebreaker too, on top of permuting input order."""
        import src.agents.referee.reality_check as reality_check

        engines = ("dynamodb", "aurora_mysql", "documentdb")
        original_cost = reality_check.ENGINE_BASE_COST["documentdb"]
        reality_check.ENGINE_BASE_COST["documentdb"] = reality_check.ENGINE_BASE_COST[
            "aurora_mysql"
        ]
        try:
            results = []
            for order in itertools.permutations(engines):
                assignment, triage, collector, analysis = _make_small_engine_scenario(
                    order=order, include_tied_aurora_queries=False
                )
                result = run_reality_check(assignment, triage, analysis, collector)
                engine_of = {
                    qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]
                }
                results.append(engine_of)
        finally:
            reality_check.ENGINE_BASE_COST["documentdb"] = original_cost

        assert all(
            r == results[0] for r in results[1:]
        ), f"placement differs by input order: {results}"
        assert results[0]["b1"] == "aurora_mysql"
        assert "documentdb" not in set(results[0].values())

    def test_all_permutations_of_four_engines_agree(self):
        """A fourth engine (opensearch, mandatory via a text_search signal
        override) added to the wordpress shape -- still every one of the
        4! = 24 possible input orders must settle on the same engine set
        and placement."""
        aurora_block = [
            ("a1", "aurora_mysql"),
            ("a2", "aurora_mysql"),
            ("a3", "aurora_mysql"),
            ("a4", "aurora_mysql"),
            ("util1", "aurora_mysql"),
            ("util2", "aurora_mysql"),
        ]
        documentdb_block = [("b1", "documentdb"), ("b2", "documentdb"), ("b3", "documentdb")]
        dynamodb_block = [("d1", "dynamodb"), ("d2", "dynamodb"), ("d3", "dynamodb")]
        search_block = [("s1", "opensearch")]
        blocks = {
            "dynamodb": dynamodb_block,
            "aurora_mysql": aurora_block,
            "documentdb": documentdb_block,
            "opensearch": search_block,
        }
        triage = {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "aurora_mysql"},
                {"agent_type": "documentdb"},
                {"agent_type": "opensearch"},
            ],
            "signals": [{"signal": "text_search", "targets": ["opensearch"], "query_ids": ["s1"]}],
            "query_capabilities": {"util1": ["sql_admin"], "util2": ["sql_admin"]},
        }
        extra: dict[str, dict[str, object]] = {
            "d1": {"tables_accessed": ["db.d"]},
            "d2": {"tables_accessed": ["db.d"]},
            "d3": {"tables_accessed": ["db.d"]},
            "a1": {"tables_accessed": ["db.a"]},
            "a2": {"tables_accessed": ["db.a"]},
            "a3": {"tables_accessed": ["db.a"]},
            "a4": {"tables_accessed": ["db.a"]},
            "util1": {
                "tables_accessed": ["db.util"],
                "query_text": "SHOW FULL FIELDS FROM db.util",
            },
            "util2": {
                "tables_accessed": ["db.util"],
                "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
            },
            "b1": {"tables_accessed": ["db.b"]},
            "b2": {"tables_accessed": ["db.b"]},
            "b3": {"tables_accessed": ["db.b"]},
            "s1": dict(_REAL_SEARCH_DEPTH_QUERY, tables_accessed=["db.s"]),
        }
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.d", "confidence_score": 90},
                    {"table_id": "db.a", "confidence_score": 80},
                    {"table_id": "db.util", "confidence_score": 30},
                    {"table_id": "db.b", "confidence_score": 60},
                    {"table_id": "db.s", "confidence_score": 50},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.d", "confidence_score": 50},
                    {"table_id": "db.a", "confidence_score": 80},
                    {"table_id": "db.util", "confidence_score": 60},
                    {"table_id": "db.b", "confidence_score": 85},
                    {"table_id": "db.s", "confidence_score": 55},
                ]
            },
            "documentdb": {"table_recommendations": [{"table_id": "db.b", "confidence_score": 80}]},
            "opensearch": {"table_recommendations": [{"table_id": "db.s", "confidence_score": 90}]},
        }

        results = []
        for order in itertools.permutations(blocks):
            pairs = [pair for engine in order for pair in blocks[engine]]
            assignment = _make_assignment(pairs, signal_overrides={"s1": "text_search"})
            collector = _make_collector([qid for qid, _ in pairs], extra=extra)
            result = run_reality_check(assignment, triage, analysis, collector)
            engine_of = {
                qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]
            }
            results.append(engine_of)

        assert all(
            r == results[0] for r in results[1:]
        ), f"placement differs by input order: {results}"
        # opensearch's mandatory query is never consolidated away.
        assert results[0]["s1"] == "opensearch"
        assert "documentdb" not in set(results[0].values())

    def test_reversed_evaluation_order_within_a_round_agrees_too(self, monkeypatch):
        """#502 review item 7: input order is not the only axis that must not
        matter -- the order a round evaluates each committed engine's
        removability in (``_round_evaluation_order``) must not matter
        either, since every engine is judged against the same frozen
        snapshot regardless of when it is checked. Forcing the opposite
        (reverse-alphabetical) evaluation order must settle on the exact
        same committed engine set and placement as the default order."""
        import src.agents.referee.reality_check_settle as reality_check_settle

        assignment, triage, collector, analysis = _make_small_engine_scenario()
        baseline = run_reality_check(assignment, triage, analysis, collector)
        baseline_engine_of = {
            qa["query_id"]: qa["assigned_engine"] for qa in baseline["revised_assignments"]
        }

        calls: list[set[str]] = []

        def reversed_order(snapshot: set[str]) -> list[str]:
            calls.append(set(snapshot))
            return sorted(snapshot, reverse=True)

        monkeypatch.setattr(reality_check_settle, "_round_evaluation_order", reversed_order)
        reversed_result = run_reality_check(assignment, triage, analysis, collector)
        # The patch must reach Phase 1; patching a re-export would silently do nothing.
        assert calls, "_round_evaluation_order was never called through the patch"
        reversed_engine_of = {
            qa["query_id"]: qa["assigned_engine"] for qa in reversed_result["revised_assignments"]
        }

        assert reversed_engine_of == baseline_engine_of
        assert "documentdb" not in set(reversed_engine_of.values())

    def test_randomized_scenarios_with_destination_ties_settle_the_same_regardless_of_order(
        self,
    ):
        """#569: ``_removal_tiebreak_key``'s per-query destination used to be
        ``max(alts, key=score)`` over a list built from the ``survivors``
        *set* -- a tie between two or more equally-scoring destinations
        depended on that set's iteration order, which Python does not
        guarantee is the same across runs. The companion subprocess test
        (``tests/e2e/test_hashseed_determinism.py::
        test_removal_tiebreak_destination_ignores_hash_seed``) pins one
        hand-built scenario and proves the fix across ``PYTHONHASHSEED``
        values, which is the axis the issue's own 4,000-case comparison
        actually caught. This test covers the OTHER axis named in the issue
        -- input order, within a single process/hash seed -- across many
        randomly generated scenarios, each deliberately built with
        redundant engines and tied destination scores (so Phase 1 removal
        and ``_removal_tiebreak_key``'s own destination pick are both
        exercised), run many times each with ``engine_queries``' engine
        order and every engine's query-list order shuffled. The settled
        engine set must be identical across every shuffle of the same
        logical scenario.
        """
        engines_pool = [
            "aurora_mysql",
            "aurora_postgresql",
            "dynamodb",
            "documentdb",
            "opensearch",
            "elasticache",
        ]
        master_rng = random.Random(20261013)
        for scenario_idx in range(40):
            scenario_rng = random.Random(master_rng.randint(0, 2**32 - 1))
            n_engines = scenario_rng.randint(3, len(engines_pool))
            engines = scenario_rng.sample(engines_pool, n_engines)

            engine_queries: dict[str, list[dict]] = {}
            trace_by_query: dict[str, dict] = {}
            current_engine_by_qid: dict[str, str] = {}
            qid_counter = 0
            for engine in engines:
                qas = []
                for _ in range(scenario_rng.randint(1, 3)):
                    qid = f"q{qid_counter}"
                    qid_counter += 1
                    qas.append(
                        {"query_id": qid, "assigned_engine": engine, "assignment_reason": "test"}
                    )
                    current_engine_by_qid[qid] = engine
                    others = [e for e in engines if e != engine]
                    scenario_rng.shuffle(others)
                    tied_alts = others[: scenario_rng.randint(2, min(3, len(others)))]
                    native_score = scenario_rng.choice([60, 65, 70])
                    # Delta against the tied alternatives stays below
                    # UNIQUE_DELTA_THRESHOLD (15), so this query is always
                    # "redundant" (removable), never "unique" -- the removal
                    # decision itself is not what is under test here, only
                    # the destination picked once removal is simulated.
                    tie_score = native_score + scenario_rng.choice([5, 8, 10, 12])
                    scores = {engine: native_score}
                    scores.update({alt: tie_score for alt in tied_alts})
                    scores.update(
                        {alt: scenario_rng.choice([30, 40]) for alt in others if alt not in scores}
                    )
                    trace_by_query[qid] = {"engines": scores}
                engine_queries[engine] = qas

            baseline, _ = _settle_committed_engines(
                engine_queries=dict(engine_queries),
                mandatory_query_ids=set(),
                mandatory_committed_engines=set(),
                trace_by_query=dict(trace_by_query),
                current_engine_by_qid=dict(current_engine_by_qid),
                query_signals={},
                query_map={},
                query_capabilities=None,
                max_rounds=10,
            )

            for perm_idx in range(15):
                items = list(engine_queries.items())
                scenario_rng.shuffle(items)
                shuffled_engine_queries = {}
                for engine, qas in items:
                    qas_copy = list(qas)
                    scenario_rng.shuffle(qas_copy)
                    shuffled_engine_queries[engine] = qas_copy

                result, _ = _settle_committed_engines(
                    engine_queries=shuffled_engine_queries,
                    mandatory_query_ids=set(),
                    mandatory_committed_engines=set(),
                    trace_by_query=dict(trace_by_query),
                    current_engine_by_qid=dict(current_engine_by_qid),
                    query_signals={},
                    query_map={},
                    query_capabilities=None,
                    max_rounds=10,
                )
                assert result == baseline, (
                    f"scenario {scenario_idx} permutation {perm_idx}: settled engines "
                    f"changed from {sorted(baseline)} to {sorted(result)} just from "
                    "input order"
                )


class TestRealityCheckAcceptsALowerScoreWhenConsolidating:
    """#502 review (correcting an earlier #501 draft): once an engine is
    being removed, its queries move to their highest-scoring remaining
    engine that can serve them, even if that scores them a little lower --
    accepting a slightly worse fit in exchange for fewer engines is the
    entire point of the CTO review. There is no score floor on a
    consolidation move; capability gates and the ownership/write gate are
    the only things that can keep a query off a committed engine."""

    def test_query_moves_to_a_lower_scoring_engine_when_its_own_engine_is_removed(self):
        assignment = _make_assignment(
            [
                ("d1", "dynamodb"),
                ("d2", "dynamodb"),
                ("d3", "dynamodb"),
                ("qx", "aurora_mysql"),
            ]
        )
        triage = _make_triage()
        collector = _make_collector(
            ["d1", "d2", "d3", "qx"],
            extra={
                "d1": {"tables_accessed": ["db.d"]},
                "d2": {"tables_accessed": ["db.d"]},
                "d3": {"tables_accessed": ["db.d"]},
                "qx": {"tables_accessed": ["db.a"]},
            },
        )
        # qx: aurora_mysql scores 80, dynamodb only 70 -- a 10-point gap,
        # under the 15-point "unique" margin, so aurora_mysql (and qx with
        # it) is redundant. DynamoDB is a worse home for qx than Aurora
        # MySQL was, but it is the only committed engine once Aurora MySQL
        # is removed, so qx moves there anyway.
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.d", "confidence_score": 90},
                    {"table_id": "db.a", "confidence_score": 70},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.a", "confidence_score": 80},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}

        # qx moves to DynamoDB -- Aurora MySQL earned no unique value, so it
        # is removed, and its queries go to whichever committed engine can
        # serve them, even at a lower score.
        assert engine_of["qx"] == "dynamodb"
        assert any(
            c["from_engine"] == "aurora_mysql" and c["to_engine"] == "dynamodb"
            for c in result["consolidations"]
        )


class TestRealityCheckNeverPadsASmallEngineToJustifyIt:
    """#502 review: Phase 1 judges an engine on its own native queries,
    snapshotted before anything is placed -- it never sees, let alone
    counts, a query another engine's placement might later put on it. An
    engine that would only look justified by absorbing someone else's
    redundant queries is removed on its own (native) merits, every time."""

    def test_a_small_engine_is_removed_even_though_another_engines_queries_score_it_highest(self):
        # dynamodb: cheap, well used. aurora_mysql: expensive, with two
        # sql_admin-only queries nothing else can serve (irreplaceable, so
        # it is never a removal candidate) plus four ordinary queries.
        # documentdb: a single native query, only barely "unique" against
        # the full committed set (the smallest possible 15-point margin) --
        # on its own it cannot clear the TARGET_ENGINE_COUNT burden check
        # once 3 engines are committed. DocumentDB also happens to score
        # Aurora MySQL's four ordinary queries highest of anyone -- if that
        # counted toward DocumentDB's own justification, it would look
        # well used. It does not count: Phase 1 never looks at it.
        assignment = _make_assignment(
            [("d1", "dynamodb"), ("d2", "dynamodb"), ("d3", "dynamodb")]
            + [(f"a{i}", "aurora_mysql") for i in range(1, 5)]
            + [("util1", "aurora_mysql"), ("util2", "aurora_mysql")]
            + [("b1", "documentdb")]
        )
        triage = {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "aurora_mysql"},
                {"agent_type": "documentdb"},
            ],
            "signals": [],
            "query_capabilities": {"util1": ["sql_admin"], "util2": ["sql_admin"]},
        }
        collector = _make_collector(
            ["d1", "d2", "d3", *[f"a{i}" for i in range(1, 5)], "util1", "util2", "b1"],
            extra={
                "d1": {"tables_accessed": ["db.d"]},
                "d2": {"tables_accessed": ["db.d"]},
                "d3": {"tables_accessed": ["db.d"]},
                **{f"a{i}": {"tables_accessed": ["db.a"]} for i in range(1, 5)},
                "util1": {
                    "tables_accessed": ["db.util"],
                    "query_text": "SHOW FULL FIELDS FROM db.util",
                },
                "util2": {
                    "tables_accessed": ["db.util"],
                    "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
                },
                "b1": {"tables_accessed": ["db.b"]},
            },
        )
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.d", "confidence_score": 90},
                    {"table_id": "db.a", "confidence_score": 75},
                    {"table_id": "db.util", "confidence_score": 30},
                    {"table_id": "db.b", "confidence_score": 55},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.a", "confidence_score": 80},
                    {"table_id": "db.util", "confidence_score": 60},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.a", "confidence_score": 95},
                    {"table_id": "db.b", "confidence_score": 70},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}

        # documentdb is removed -- its one native query (b1) alone cannot
        # justify it, and Aurora MySQL's four queries scoring highest on
        # documentdb never factors into that decision at all. Aurora MySQL
        # itself is never a removal candidate (util1/util2 are
        # irreplaceable), so a1-a4 simply stay there -- there was never a
        # "received queries" moment for documentdb to be padded by.
        assert "documentdb" not in engine_of.values()
        for i in range(1, 5):
            assert engine_of[f"a{i}"] == "aurora_mysql"
        assert engine_of["util1"] == "aurora_mysql"
        assert engine_of["util2"] == "aurora_mysql"


class TestMandatoryEngineAbsorbsATiedEngine:
    """#502 review (C3, round 5: state the behaviour factually): the
    original 80/80 tie fixture, restored explicitly.

    DynamoDB holds 39 ordinary queries that score IDENTICALLY (80/80, delta
    0) on DocumentDB. DocumentDB holds a single query protected by a
    mandatory signal override (ADR #296) -- its own survival is decided
    SOLELY by that one query, before Phase 1 even looks at what DynamoDB
    holds. ``UNIQUE_DELTA_THRESHOLD`` is 15: a query counts as "unique" for
    its engine only when that engine leads every eligible alternative by at
    least 15 points. A tie (delta 0) is nowhere near that lead, so none of
    DynamoDB's 39 queries counts as unique for it; DynamoDB is therefore
    fully redundant and is removed -- with only 2 engines committed, this
    does not even reach the ``TARGET_ENGINE_COUNT``/burden check (#494).

    DynamoDB's 39 queries then land on DocumentDB in Phase 2, because
    DocumentDB is the only committed engine left eligible for them. That is
    NOT "padding" DocumentDB to justify it -- DocumentDB was already
    committed on its own mandatory query, independent of anything DynamoDB
    holds; absorbing DynamoDB's redundant queries afterward is a normal,
    downstream consequence of Phase 2 placement once DynamoDB has nowhere
    else to go, not a reason manufactured to keep DocumentDB around.
    Whether a mandatory engine absorbing a much larger engine this way is
    desirable at all is a separate, pending maintainer decision -- this PR
    does not change the behaviour, only states it explicitly instead of
    leaving it implicit.
    """

    def test_dynamodb_is_removed_in_favor_of_a_mandatory_documentdb_at_equal_scores(self):
        dynamodb_pairs = [(f"d{i}", "dynamodb") for i in range(1, 40)]
        assignment = _make_assignment(
            dynamodb_pairs + [("m1", "documentdb")],
            signal_overrides={"m1": "flexible_schema"},
        )
        triage = {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
            "signals": [],
        }
        collector = _make_collector(
            [f"d{i}" for i in range(1, 40)] + ["m1"],
            extra={
                **{f"d{i}": {"tables_accessed": ["db.d"]} for i in range(1, 40)},
                "m1": {"tables_accessed": ["db.m"]},
            },
        )
        analysis = {
            "dynamodb": {"table_recommendations": [{"table_id": "db.d", "confidence_score": 80}]},
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.d", "confidence_score": 80},
                    {"table_id": "db.m", "confidence_score": 80},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}

        # DynamoDB is removed -- every one of its 39 queries ties DocumentDB
        # exactly, so none clears the "strictly higher" bar that would make
        # it unique. DocumentDB absorbs them because it is the only engine
        # left, not because it needed them to survive.
        assert "dynamodb" not in engine_of.values()
        for i in range(1, 40):
            assert engine_of[f"d{i}"] == "documentdb"
        assert engine_of["m1"] == "documentdb"
        assert result["unique_value_assessment"]["documentdb"]["is_mandatory"] is True


class TestGroupKeepsItsEngineWhenNoSingleSurvivorFitsEveryMember:
    """#502 review round 5, finding G: Phase 1 is group-aware.

    The round-4 draft judged an engine's native queries one at a time (the
    per-query ``_query_eligible_engines`` gate), so Phase 1 could judge an
    engine "safe to remove" even when one of its co-dependency groups had
    no single surviving engine eligible for every member -- Phase 2 would
    then either strand the group (the original bug) or split it apart (the
    round-4 "fix", itself against the rule that a group moves as one unit).
    Phase 1 now uses the SAME group-aware eligibility
    (``_group_eligible_engines``) Phase 2 does: an engine holding a member
    of a group is irreplaceable for that group -- and so never removed --
    unless some OTHER committed engine is eligible for every member at
    once, members on other engines included.

    Here, g1 and g2 are one co-dependency group, both currently on
    DocumentDB. The resolver has no score for g1 on Aurora MySQL at all,
    and no score for g2 on DynamoDB at all (set up directly in the trace,
    the same technique used elsewhere in this file) -- DynamoDB alone could
    take g1, Aurora MySQL alone could take g2, but no single survivor
    covers both. (Only one Aurora engine is ever committed at once --
    ADR-031 Decision 6 -- so the two alternatives here are DynamoDB and
    Aurora MySQL, not two Aurora engines.) DocumentDB is therefore
    irreplaceable for this group and stays committed; the group stays on
    DocumentDB in Phase 2 as well, not split and not stranded.
    """

    GROUP = {
        "key": "deadbeefcafe",
        "scores": {"documentdb": 80, "dynamodb": 70, "aurora_mysql": 70},
    }
    TRACE_BY_QUERY = {
        # Aurora MySQL omitted here: no resolver score for g1 on it at all.
        "g1": {"engines": {"documentdb": 80, "dynamodb": 70}, "group": GROUP},
        # DynamoDB omitted here: no resolver score for g2 on it at all.
        "g2": {"engines": {"documentdb": 80, "aurora_mysql": 70}, "group": GROUP},
    }
    CURRENT_ENGINE_BY_QID = {
        "g1": "documentdb",
        "g2": "documentdb",
        "d_mand": "dynamodb",
        "a_mand": "aurora_mysql",
    }
    MANDATORY_QUERY_IDS = {"d_mand", "a_mand"}
    ENGINE_QUERIES = {
        "documentdb": [
            {"query_id": "g1", "assigned_engine": "documentdb", "assignment_reason": "test"},
            {"query_id": "g2", "assigned_engine": "documentdb", "assignment_reason": "test"},
        ],
        # One mandatory (signal-override) query each, purely so DynamoDB and
        # Aurora MySQL are legitimately committed from the start and never
        # removal candidates themselves -- isolating the test to DocumentDB's
        # fate over the g1/g2 group.
        "dynamodb": [
            {
                "query_id": "d_mand",
                "assigned_engine": "dynamodb",
                "assignment_reason": "signal override: test",
                "signal_override": "test",
            }
        ],
        "aurora_mysql": [
            {
                "query_id": "a_mand",
                "assigned_engine": "aurora_mysql",
                "assignment_reason": "signal override: test",
                "signal_override": "test",
            }
        ],
    }

    def test_documentdb_is_not_removable(self):
        committed_engines, assessment = _settle_committed_engines(
            engine_queries=self.ENGINE_QUERIES,
            mandatory_query_ids=self.MANDATORY_QUERY_IDS,
            mandatory_committed_engines={"dynamodb", "aurora_mysql"},
            trace_by_query=self.TRACE_BY_QUERY,
            current_engine_by_qid=self.CURRENT_ENGINE_BY_QID,
            query_signals={},
            query_map={},
            query_capabilities=None,
            max_rounds=5,
        )

        assert committed_engines == {"documentdb", "dynamodb", "aurora_mysql"}
        assert assessment["documentdb"]["irreplaceable_queries"] == ["g1", "g2"]

    def test_group_stays_on_documentdb_in_phase_two(self):
        query_assignments = [
            *self.ENGINE_QUERIES["documentdb"],
            *self.ENGINE_QUERIES["dynamodb"],
            *self.ENGINE_QUERIES["aurora_mysql"],
        ]
        committed_engines = {"documentdb", "dynamodb", "aurora_mysql"}

        revised, consolidations = _place_settled_queries(
            query_assignments=query_assignments,
            committed_engines=committed_engines,
            mandatory_query_ids=self.MANDATORY_QUERY_IDS,
            current_engine_by_qid=self.CURRENT_ENGINE_BY_QID,
            query_signals={},
            query_map={},
            query_capabilities=None,
            trace_by_query=self.TRACE_BY_QUERY,
            source_compatible_engine=None,
            workload_justified={},
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in revised}

        # The group is kept together on its current engine, not split and
        # not stranded.
        assert engine_of["g1"] == "documentdb"
        assert engine_of["g2"] == "documentdb"
        assert consolidations == []


class TestGroupTieHashSeedInPhaseTwoIsOrderIndependent:
    """#503: Phase 2's group tie-break seeded the placeholder hash with
    ``qid``, whichever member happened to come first in
    ``trace_by_query``'s own iteration order -- not the sorted member
    list the resolver (``assignment_resolver.py``'s Step 6b) and the
    justification floor's group tie-break both use. That let the SAME two
    tied engines re-pick a different winner in Phase 2 purely because of
    input order, which the stable-seed rule everywhere else is meant to
    rule out.

    Repro: a group {q1, q101} tied 80/80 between DocumentDB and DynamoDB,
    both already an unresolved tie from the first assignment (so no
    stability candidate exists either), both engines still committed. The
    fix is to seed with ``",".join(sorted(member_ids))`` -- this test
    builds ``trace_by_query`` with the two members in each order and
    asserts the group lands on the same engine either way.
    """

    GROUP = {"key": "g101", "scores": {"documentdb": 80, "dynamodb": 80}}

    @staticmethod
    def _members(order: list[str]) -> dict[str, dict]:
        return {
            qid: {
                "engines": {"documentdb": 80, "dynamodb": 80},
                "group": TestGroupTieHashSeedInPhaseTwoIsOrderIndependent.GROUP,
            }
            for qid in order
        }

    def _place(self, trace_order: list[str]) -> dict[str, str]:
        query_assignments = [
            {
                "query_id": qid,
                "assigned_engine": "documentdb",
                "assignment_reason": "placeholder",
                "tied_engines": ["documentdb", "dynamodb"],
            }
            for qid in ("q1", "q101")
        ]
        revised, _ = _place_settled_queries(
            query_assignments=query_assignments,
            committed_engines={"documentdb", "dynamodb"},
            mandatory_query_ids=set(),
            current_engine_by_qid={"q1": "documentdb", "q101": "documentdb"},
            query_signals={},
            query_map={},
            query_capabilities=None,
            trace_by_query=self._members(trace_order),
            source_compatible_engine=None,
            workload_justified={},
            tie_unresolved_qids={"q1", "q101"},
            settled_counts={},
            tied_engines_by_qid={
                "q1": ["documentdb", "dynamodb"],
                "q101": ["documentdb", "dynamodb"],
            },
        )
        return {qa["query_id"]: qa["assigned_engine"] for qa in revised}

    def test_the_group_lands_on_the_same_engine_regardless_of_trace_order(self):
        forward = self._place(["q1", "q101"])
        backward = self._place(["q101", "q1"])

        assert forward["q1"] == forward["q101"]
        assert backward["q1"] == backward["q101"]
        assert forward["q1"] == backward["q1"]


class TestGroupMembersOnTwoTiedEnginesIsUnresolvedNotStability:
    """#503 review item 3: a co-dependency group's members can sit on two
    different engines today. If both of those engines are ALSO tied for the
    group's top score, there is no single "the" current engine for
    stability to grab onto -- this must not silently fall back to picking
    whichever of the two sorts first by name under the guise of
    "stability" (the exact bug ``tie_break.break_tie`` fixes at the shared
    function level; this test exercises it through the real Phase 2 caller,
    ``_place_settled_queries``'s group branch).

    g1 is currently on documentdb, g2 is currently on dynamodb -- two
    different engines -- and the group's score ties exactly between
    documentdb and dynamodb (80 each). No source-compatible engine is in
    play, so neither rule 1 nor rule 2 of ``break_tie`` can fire; the group
    must move together onto whichever placeholder engine the maintainer's
    unresolved-tie rule picks, carrying the honest tie-unresolved reason --
    never a plain "no unique value" sentence that would misreport this as a
    settled decision.
    """

    GROUP = {"key": "tiedacrosstwo", "scores": {"documentdb": 80, "dynamodb": 80}}
    TRACE_BY_QUERY = {
        "g1": {"engines": {"documentdb": 80, "dynamodb": 60}, "group": GROUP},
        "g2": {"engines": {"documentdb": 60, "dynamodb": 80}, "group": GROUP},
    }
    CURRENT_ENGINE_BY_QID = {"g1": "documentdb", "g2": "dynamodb"}

    def test_the_group_moves_together_as_an_unresolved_tie(self):
        query_assignments = [
            {"query_id": "g1", "assigned_engine": "documentdb", "assignment_reason": "test"},
            {"query_id": "g2", "assigned_engine": "dynamodb", "assignment_reason": "test"},
        ]

        revised, consolidations = _place_settled_queries(
            query_assignments=query_assignments,
            committed_engines={"documentdb", "dynamodb"},
            mandatory_query_ids=set(),
            current_engine_by_qid=self.CURRENT_ENGINE_BY_QID,
            query_signals={},
            query_map={},
            query_capabilities=None,
            trace_by_query=self.TRACE_BY_QUERY,
            source_compatible_engine=None,
            workload_justified={},
        )
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in revised}
        reason_of = {qa["query_id"]: qa.get("assignment_reason", "") for qa in revised}
        original_engine = {"g1": "documentdb", "g2": "dynamodb"}

        # The group moves as one unit, onto one of the two tied engines --
        # never split, and never left straddling both.
        assert engine_of["g1"] == engine_of["g2"]
        assert engine_of["g1"] in {"documentdb", "dynamodb"}

        # Exactly one member had to actually change engine (they started on
        # different engines).
        moved = [qid for qid in ("g1", "g2") if engine_of[qid] != original_engine[qid]]
        assert len(moved) == 1

        # #503: this tie first appears here (it was never flagged on the
        # input), so BOTH members' reasons report
        # it honestly -- the one that stays put is not left silently
        # reading its old, pre-tie "test" reason either.
        for qid in ("g1", "g2"):
            assert "no routing rule decides between them" in reason_of[qid]
            assert "ADR-031" not in reason_of[qid]
            assert "non-preferential" not in reason_of[qid]
            assert "tie_unresolved" not in reason_of[qid]


class TestReResolvingAnUnresolvedTieInPhaseTwo:
    """#503 review item 2: a query already flagged ``tied_engines`` from the
    first assignment is re-resolved in Phase 2 among whichever of the
    originally tied engines are still committed/eligible -- its placeholder
    current engine earns no stability credit (it was never a real "today",
    just the earlier placeholder's own pick). If only one of the tied
    engines survives, that is a real, explainable decision: the tie clears.
    If two or more still tie, ``tied_engines`` is refreshed, not left stale.
    """

    TRACE_BY_QUERY = {"q1": {"engines": {"documentdb": 70, "dynamodb": 70}}}
    CURRENT_ENGINE_BY_QID = {"q1": "documentdb"}

    def _query_assignments(self) -> list[dict]:
        return [
            {
                "query_id": "q1",
                "assigned_engine": "documentdb",
                "assignment_reason": "placeholder",
                "tied_engines": ["documentdb", "dynamodb"],
            }
        ]

    def test_only_one_surviving_tied_engine_clears_the_tie(self):
        revised, _ = _place_settled_queries(
            query_assignments=self._query_assignments(),
            committed_engines={"documentdb"},  # dynamodb no longer committed
            mandatory_query_ids=set(),
            current_engine_by_qid=self.CURRENT_ENGINE_BY_QID,
            query_signals={},
            query_map={},
            query_capabilities=None,
            trace_by_query=self.TRACE_BY_QUERY,
            source_compatible_engine=None,
            workload_justified={},
            tie_unresolved_qids={"q1"},
            settled_counts={},
            tied_engines_by_qid={"q1": ["documentdb", "dynamodb"]},
        )
        q1 = next(qa for qa in revised if qa["query_id"] == "q1")
        assert q1["assigned_engine"] == "documentdb"
        # A real, explainable decision now -- not an open tie anymore.
        assert q1["tied_engines"] == []
        # #503 review round 3, suggestion 2: the reason says WHY the tie
        # cleared (DynamoDB no longer qualifies), not just that it did.
        assert q1["assignment_reason"] == (
            "reality check: DocumentDB is kept -- DynamoDB no longer qualifies "
            "for this query, so the earlier scores tie is resolved"
        )

    def test_two_surviving_tied_engines_stay_an_unresolved_tie(self):
        revised, _ = _place_settled_queries(
            query_assignments=self._query_assignments(),
            committed_engines={"documentdb", "dynamodb"},  # both still committed
            mandatory_query_ids=set(),
            current_engine_by_qid=self.CURRENT_ENGINE_BY_QID,
            query_signals={},
            query_map={},
            query_capabilities=None,
            trace_by_query=self.TRACE_BY_QUERY,
            source_compatible_engine=None,
            workload_justified={},
            tie_unresolved_qids={"q1"},
            settled_counts={},
            tied_engines_by_qid={"q1": ["documentdb", "dynamodb"]},
        )
        q1 = next(qa for qa in revised if qa["query_id"] == "q1")
        # Still an open tie between the same two engines -- refreshed, not
        # stale, and the placeholder's current engine (documentdb) earned no
        # stability credit just for being where the earlier placeholder put it.
        assert q1["tied_engines"] == ["documentdb", "dynamodb"]


class TestJustificationFloorMovesACoDependencyGroupTogether:
    """#502 review round 6, finding 3: the justification floor routes a
    grouped query through ``_group_eligible_engines`` too, so a
    co-dependency group moves as ONE unit there as well -- not split by
    picking each member's own best individual absorber.

    DocumentDB is mandatory (a signal override) and holds a co-dependency
    group (g1, g2) plus nothing else, at a cost share far below
    ``MANDATORY_ENGINE_MAX_QUERY_SHARE`` -- it fails the floor outright.
    g1's OWN per-query resolver score strongly favors DynamoDB (85 vs 52);
    g2's OWN per-query score strongly favors Aurora MySQL (90 vs 52) --
    picking each member's own best absorber independently would split the
    group across both engines. The group's OWN aggregate score instead
    favors Aurora MySQL for the whole group (71.0, vs DynamoDB's 68.5), so
    both members move there together.
    """

    def test_group_members_move_to_the_same_engine(self):
        n_dynamo = 40
        assignment = {
            "version": 1,
            "query_assignments": (
                [
                    {"query_id": f"d{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                    for i in range(n_dynamo)
                ]
                + [
                    {
                        "query_id": "util1",
                        "assigned_engine": "aurora_mysql",
                        "assignment_reason": "t",
                    },
                    {
                        "query_id": "util2",
                        "assigned_engine": "aurora_mysql",
                        "assignment_reason": "t",
                    },
                ]
                + [
                    {
                        "query_id": qid,
                        "assigned_engine": "documentdb",
                        "assignment_reason": "signal override: flexible_schema",
                        "signal_override": "flexible_schema",
                    }
                    for qid in ("g1", "g2")
                ]
            ),
        }
        triage = {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "aurora_mysql"},
                {"agent_type": "documentdb"},
            ],
            "signals": [],
            "query_capabilities": {"util1": ["sql_admin"], "util2": ["sql_admin"]},
        }
        collector = {
            "queries": {
                "query_patterns": (
                    [
                        {"query_id": f"d{i}", "tables_accessed": ["db.d"], "query_type": "SELECT"}
                        for i in range(n_dynamo)
                    ]
                    + [
                        {
                            "query_id": "util1",
                            "tables_accessed": ["db.util"],
                            "query_text": "SHOW FULL FIELDS FROM db.util",
                        },
                        {
                            "query_id": "util2",
                            "tables_accessed": ["db.util"],
                            "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
                        },
                    ]
                    + [
                        {
                            "query_id": "g1",
                            "tables_accessed": ["db.shared", "db.g1"],
                            "query_type": "SELECT",
                            "has_joins": True,
                            "has_aggregations": True,
                        },
                        {
                            "query_id": "g2",
                            "tables_accessed": ["db.shared", "db.g2"],
                            "query_type": "SELECT",
                            "has_joins": True,
                            "has_aggregations": True,
                        },
                    ]
                )
            }
        }
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.d", "confidence_score": 90},
                    {"table_id": "db.util", "confidence_score": 30},
                    {"table_id": "db.shared", "confidence_score": 85},
                    {"table_id": "db.g1", "confidence_score": 85},
                    {"table_id": "db.g2", "confidence_score": 20},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.d", "confidence_score": 50},
                    {"table_id": "db.util", "confidence_score": 60},
                    {"table_id": "db.shared", "confidence_score": 85},
                    {"table_id": "db.g1", "confidence_score": 20},
                    {"table_id": "db.g2", "confidence_score": 95},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.shared", "confidence_score": 70},
                    {"table_id": "db.g1", "confidence_score": 70},
                    {"table_id": "db.g2", "confidence_score": 70},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}

        # Both members move together, to the engine the GROUP's own
        # aggregate score favors -- not split onto each member's own best
        # individual absorber (which would have been g1 -> dynamodb,
        # g2 -> aurora_mysql).
        assert engine_of["g1"] == engine_of["g2"]
        assert engine_of["g1"] == "aurora_mysql"
        from_docdb = [c for c in result["consolidations"] if c["from_engine"] == "documentdb"]
        assert len(from_docdb) == 1
        assert from_docdb[0]["to_engine"] == "aurora_mysql"
        assert from_docdb[0]["query_count"] == 2

        # ADR-031 Decision 7 (#698): a floor-moved group reports the group
        # score on the engine it left as well as on the one it went to; a
        # missing (None) before score would print "n/a" here.
        for qa in result["revised_assignments"]:
            if qa["query_id"] in {"g1", "g2"}:
                reason = qa["assignment_reason"]
                assert re.search(r"DocumentDB scores \d+(\.\d+)?", reason), reason
                assert re.search(r"Aurora MySQL scores \d+(\.\d+)?", reason), reason
                assert "n/a" not in reason, reason


class TestPhaseOneTieSurfacesBeyondUniqueValueAssessment:
    """#502 review round 6, finding 4: a Phase 1 tie_unresolved pair is now
    stated in the human-readable ``recommendations`` -- the trace a customer
    actually reads -- not just the internal ``unique_value_assessment``
    dict (and that dict's own fields are now declared on the Pydantic
    contract, round 6's other fix, so they stop being silently dropped the
    way ``cost_justification`` was before #375's 1.5 fix).

    No ``Consolidation`` record is created for the tie: a consolidation
    record's ``query_count`` must equal the number of queries that
    actually moved (``tests/unit/agents/referee/test_reality_check_llm_seam.py::
    test_each_consolidation_lists_its_moved_queries``), and a tie, by
    definition, moves none -- forcing a record here would violate that
    invariant and mislead ``_build_recommendations``'s per-consolidation
    text (which assumes ``from_engine`` left the architecture).
    """

    def test_tie_appears_in_recommendations_not_only_in_the_assessment(self, monkeypatch):
        import src.agents.referee.reality_check as reality_check

        monkeypatch.setitem(reality_check.ENGINE_BASE_COST, "documentdb", 0)

        assignment = {
            "version": 1,
            "query_assignments": (
                [
                    {"query_id": f"dd{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                    for i in range(2)
                ]
                + [
                    {
                        "query_id": f"mm{i}",
                        "assigned_engine": "documentdb",
                        "assignment_reason": "t",
                    }
                    for i in range(2)
                ]
                + [
                    {
                        "query_id": "a_mand",
                        "assigned_engine": "aurora_mysql",
                        "assignment_reason": "signal override: sql_admin",
                        "signal_override": "sql_admin",
                    }
                ]
            ),
        }
        triage = {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "documentdb"},
                {"agent_type": "aurora_mysql"},
            ],
            "signals": [],
        }
        collector = {
            "queries": {
                "query_patterns": (
                    [
                        {
                            "query_id": f"dd{i}",
                            "tables_accessed": [f"db.dd{i}"],
                            "query_type": "SELECT",
                        }
                        for i in range(2)
                    ]
                    + [
                        {
                            "query_id": f"mm{i}",
                            "tables_accessed": [f"db.mm{i}"],
                            "query_type": "SELECT",
                        }
                        for i in range(2)
                    ]
                    + [
                        {
                            "query_id": "a_mand",
                            "tables_accessed": ["db.admin"],
                            "query_text": "SHOW FULL FIELDS FROM db.admin",
                        }
                    ]
                )
            }
        }
        analysis = {
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.dd0", "confidence_score": 90},
                    {"table_id": "db.dd1", "confidence_score": 90},
                ]
            },
            "documentdb": {
                "table_recommendations": [
                    {"table_id": "db.mm0", "confidence_score": 90},
                    {"table_id": "db.mm1", "confidence_score": 90},
                ]
            },
            "aurora_mysql": {
                "table_recommendations": [
                    {"table_id": "db.dd0", "confidence_score": 70},
                    {"table_id": "db.dd1", "confidence_score": 70},
                    {"table_id": "db.mm0", "confidence_score": 70},
                    {"table_id": "db.mm1", "confidence_score": 70},
                ]
            },
        }

        result = run_reality_check(assignment, triage, analysis, collector)
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}

        # Both engines are kept -- the tie is not resolved by removing either.
        assert engine_of["dd0"] == "dynamodb"
        assert engine_of["mm0"] == "documentdb"
        assert result["unique_value_assessment"]["dynamodb"]["tie_unresolved"] is True
        assert result["unique_value_assessment"]["documentdb"]["tie_unresolved"] is True

        # No consolidation record exists for this pair -- nothing moved.
        assert not any(
            {c["from_engine"], c["to_engine"]} == {"dynamodb", "documentdb"}
            for c in result["consolidations"]
        )

        # But the tie is stated in the human-readable recommendations.
        assert any("DocumentDB and DynamoDB are tied" in rec for rec in result["recommendations"])
