"""Unit tests for the consolidation validator."""

import json

from src.agents.referee.consolidation_validator import (
    _build_validation_prompt,
    _parse_flagged,
    _parse_llm_response,
    sanity_sweep,
)
from src.agents.referee.query_scoring import ScoringContext


def _query(query_id: str, table: str | None = None) -> dict:
    return {"query_id": query_id, "tables_accessed": [table] if table else []}


def _query_map(*query_ids: str) -> dict[str, dict]:
    """One query per id, each on its own table (named after the id) so each
    gets an independent, controllable score per engine via that table's own
    ``table_recommendations`` entry in each engine's analysis.
    """
    return {qid: _query(qid, table=f"t_{qid}") for qid in query_ids}


def _scoring_context(scores: dict[str, dict[str, float]]) -> ScoringContext:
    """A :class:`ScoringContext` giving query ``qid`` a controllable score on
    ``engine`` -- ``scores[qid][engine]`` -- by putting a single-table
    ``confidence_score`` for that query's own table (``t_{qid}``, see
    :func:`_query_map`) into ``engine``'s analysis output.
    """
    analysis_outputs: dict[str, dict] = {}
    for qid, engine_scores in scores.items():
        for engine, score in engine_scores.items():
            analysis = analysis_outputs.setdefault(engine, {"table_recommendations": []})
            analysis["table_recommendations"].append(
                {"table_id": f"t_{qid}", "confidence_score": score}
            )
    return ScoringContext(analysis_outputs=analysis_outputs)


def _flag(qid: str, engine: str = "dynamodb", **extra) -> dict:
    return {
        "query_ids": [qid],
        "flagged_engine": engine,
        "reason_category": "full_scan",
        "explanation": "no partition key",
        **extra,
    }


class TestParseResponse:
    """_parse_llm_response() validates against the ModelFlag contract (#647)."""

    def test_parses_no_flags(self):
        assert _parse_llm_response('{"flags": []}') == []

    def test_parses_flagged_queries(self):
        response = json.dumps({"flags": [_flag("q-1"), _flag("q-2")]})
        result = _parse_llm_response(response)
        assert [f.query_ids for f in result] == [["q-1"], ["q-2"]]
        assert result[0].reason_category.value == "full_scan"

    def test_handles_markdown_code_fences(self):
        response = "```json\n" + json.dumps({"flags": [_flag("q-1")]}) + "\n```"
        assert [f.query_ids for f in _parse_llm_response(response)] == [["q-1"]]

    def test_handles_invalid_json(self):
        assert _parse_llm_response("This is not JSON at all") == []
        assert _parse_flagged("This is not JSON at all") is None

    def test_the_old_array_shape_is_not_the_contract(self):
        response = json.dumps([{"query_id": "q-1", "reason": "bad fit"}])
        assert _parse_flagged(response) is None

    def test_a_bad_entry_is_dropped_and_the_valid_flags_kept(self):
        """#712 review: the same per-entry handling as the headless response."""
        response = json.dumps({"flags": [_flag("q-1"), _flag("q-2", target_engine="documentdb")]})
        assert len(_parse_flagged(response) or []) == 2  # raw, not yet validated
        assert [f.query_ids for f in _parse_llm_response(response)] == [["q-1"]]

    def test_handles_whitespace(self):
        assert _parse_llm_response('  \n  {"flags": []}  \n  ') == []


class TestBuildPrompt:
    """Tests for _build_validation_prompt()."""

    def test_includes_target_engine_context(self):
        prompt = _build_validation_prompt(
            from_engine="documentdb",
            to_engine="dynamodb",
            queries=[
                {
                    "query_id": "q-1",
                    "sql": "SELECT * FROM posts WHERE title LIKE '%test%'",
                    "type": "SELECT",
                    "tables": ["posts"],
                    "signals": ["text_search"],
                    "cps": 2.5,
                }
            ],
        )

        assert "DynamoDB" in prompt
        assert "key-value" in prompt
        assert "q-1" in prompt
        assert "text_search" in prompt

    def test_includes_query_details(self):
        prompt = _build_validation_prompt(
            from_engine="opensearch",
            to_engine="dynamodb",
            queries=[
                {
                    "query_id": "q-42",
                    "sql": "SELECT id FROM users WHERE id = ?",
                    "type": "SELECT",
                    "tables": ["users"],
                    "signals": [],
                    "cps": 10.0,
                }
            ],
        )

        assert "q-42" in prompt
        assert "users" in prompt
        assert "10.0 cps" in prompt
        assert "JSON" in prompt
        # The format asks for the flag contract, and never for a destination.
        assert '"flagged_engine" ("dynamodb")' in prompt
        assert "never names another engine" in prompt


class TestSanitySweepOrphanRemoval:
    """#579: whether a tiny orphan engine is removed is decided by ADR-031
    Decision 7 -- can the OTHER committed engines serve its queries at all --
    never by which engine happens to have the most queries. A removed
    engine's queries go to each one's own best-scoring serviceable
    committed engine, with ties through the shared ``tie_break.break_tie``.
    """

    def test_removes_the_engine_and_sends_each_query_to_its_own_best_engine(self):
        # The review's repro: documentdb's own 95-vs-10 and 5-vs-95 scores
        # used to make the old per-query "prefers staying" rule protect the
        # whole engine. Under Decision 7, documentdb's OWN score is not
        # even consulted -- only whether aurora_mysql/dynamodb (the other
        # committed engines) can serve each query, and which of THEM scores
        # best for it.
        # dynamodb carries 6 queries (above ORPHAN_ENGINE_THRESHOLD) so it is
        # a plain committed destination here, never itself swept.
        assignments = [
            {"query_id": "d0", "assigned_engine": "documentdb"},
            {"query_id": "d1", "assigned_engine": "documentdb"},
            {"query_id": "d2", "assigned_engine": "documentdb"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_ids = ["d0", "d1", "d2", "a1"] + [f"y{i}" for i in range(6)]
        query_map = _query_map(*query_ids)
        scoring_context = _scoring_context(
            {
                "d0": {"aurora_mysql": 10, "dynamodb": 90},
                "d1": {"aurora_mysql": 95, "dynamodb": 5},
                "d2": {"aurora_mysql": 95, "dynamodb": 5},
            }
        )

        updated, consolidations, _ = sanity_sweep(
            assignments, [], scoring_context, query_map, source_engine="mysql"
        )

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id["d0"] == "dynamodb"
        assert by_id["d1"] == "aurora_mysql"
        assert by_id["d2"] == "aurora_mysql"
        assert {c["from_engine"] for c in consolidations} == {"documentdb"}
        assert {c["to_engine"] for c in consolidations} == {"aurora_mysql", "dynamodb"}
        assert sum(c["query_count"] for c in consolidations) == 3

    def test_a_query_with_no_serviceable_committed_engine_keeps_the_whole_engine(
        self, capsys
    ) -> None:
        # g1 needs multi_doc_acid; the only other committed engine
        # (dynamodb) does not have it, so g1 has nowhere else to go.
        # Decision 7's removal check fails for the WHOLE engine -- g2 is
        # never moved either, even though g2 alone would have somewhere to
        # go on dynamodb. dynamodb carries 6 queries (above the threshold)
        # so it is a plain committed destination here, never itself swept.
        assignments = [
            {"query_id": "g1", "assigned_engine": "documentdb"},
            {"query_id": "g2", "assigned_engine": "documentdb"},
        ] + [{"query_id": f"z{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("g1", "g2", *(f"z{i}" for i in range(6)))
        scoring_context = ScoringContext(
            analysis_outputs={"dynamodb": {"table_recommendations": []}},
            query_capabilities={"g1": ["multi_doc_acid"]},
        )

        updated, consolidations, _ = sanity_sweep(
            assignments, [], scoring_context, query_map, source_engine=""
        )

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id["g1"] == "documentdb"
        assert by_id["g2"] == "documentdb"
        assert consolidations == []
        captured = capsys.readouterr()
        assert "g1" in captured.out
        assert "DocumentDB" in captured.out  # display name, never the raw engine id alone
        assert "lacks multi_doc_acid" in captured.out  # names the SPECIFIC missing capability

    def test_tied_engines_restricts_the_candidate_pool_per_decision_12(self) -> None:
        # Round 2 review's exact repro: tied_engines = [documentdb,
        # dynamodb], source mysql, all three score 50. ADR-031 Decision 12
        # and ``_place_settled_queries``'s own ``_candidate_pool`` keep a
        # tied query on the surviving originally-tied engine FIRST --
        # dynamodb -- never opened up to aurora_mysql just because it is
        # source-compatible and also ties at 50. The stale tied_engines
        # record (naming documentdb, the engine this sweep is about to
        # remove) is cleared too, since dynamodb wins outright once the
        # pool is restricted to just it.
        assignments: list[dict] = [
            {
                "query_id": "t1",
                "assigned_engine": "documentdb",
                "tied_engines": ["documentdb", "dynamodb"],
            },
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
        ]
        assignments.extend({"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6))
        query_map = _query_map("t1", "m1", *(f"y{i}" for i in range(6)))
        scoring_context = _scoring_context(
            {"t1": {"documentdb": 50, "aurora_mysql": 50, "dynamodb": 50}}
        )

        updated, _, _ = sanity_sweep(
            assignments, [], scoring_context, query_map, source_engine="mysql"
        )

        t1 = next(qa for qa in updated if qa["query_id"] == "t1")
        assert t1["assigned_engine"] == "dynamodb"
        assert t1["tied_engines"] == []

    def test_an_unresolved_tie_records_the_surviving_tied_set_and_calls_record_tie(self) -> None:
        # No tied_engines going in: the tie between aurora_mysql and
        # dynamodb is a FRESH one here. With no source match, nothing
        # resolves it, so it is reported honestly on tied_engines (ADR-031
        # Decision 12) rather than silently picked with no trace left for
        # the customer -- and, when a live RoutingTrace is given,
        # record_tie is called the same way the resolver's own tie-break
        # does.
        from src.agents.referee.assignment_resolver import RoutingTrace

        assignments = [
            {"query_id": "t1", "assigned_engine": "documentdb"},
            {"query_id": "m1", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("t1", "m1", *(f"y{i}" for i in range(6)))
        scoring_context = _scoring_context({"t1": {"aurora_mysql": 50, "dynamodb": 50}})
        trace = RoutingTrace()

        updated, _, _ = sanity_sweep(
            assignments, [], scoring_context, query_map, source_engine="", routing_trace=trace
        )

        t1 = next(qa for qa in updated if qa["query_id"] == "t1")
        assert t1["assigned_engine"] in {"aurora_mysql", "dynamodb"}
        assert set(t1["tied_engines"]) == {"aurora_mysql", "dynamodb"}
        assert trace.tie_break_by_query["t1"] == "tie_unresolved"

    def test_a_signal_override_blocks_removal_the_same_way_a_gate_does(self, capsys) -> None:
        # Round 2 review's exact repro: two mandatory (signal_override)
        # queries on opensearch, with aurora_mysql scoring them well. The
        # rest of the reality check treats a signal-override query as
        # mandatory for its engine (``mandatory_query_ids`` in Phase 1/2),
        # so this sweep must not undo that just because opensearch also
        # happens to be tiny.
        assignments = [
            {"query_id": "s1", "assigned_engine": "opensearch", "signal_override": "text_search"},
            {"query_id": "s2", "assigned_engine": "opensearch", "signal_override": "text_search"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ]
        query_map = _query_map("s1", "s2", "a1")
        scoring_context = _scoring_context({"s1": {"aurora_mysql": 90}, "s2": {"aurora_mysql": 90}})

        updated, consolidations, _ = sanity_sweep(assignments, [], scoring_context, query_map)

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id["s1"] == "opensearch"
        assert by_id["s2"] == "opensearch"
        assert consolidations == []
        captured = capsys.readouterr()
        assert "mandatory" in captured.out
        assert "text_search" in captured.out

    def test_a_co_dependency_group_moves_as_one_unit(self) -> None:
        # j1 alone would prefer dynamodb (90 vs 10); j2 alone would prefer
        # aurora_mysql (90 vs 10) -- without group awareness they would
        # split. The group's combined (mean) score ties them at 50/50, and
        # dynamodb (6 settled queries vs aurora_mysql's 1) wins the tie, so
        # BOTH members move there together.
        assignments = [
            {"query_id": "j1", "assigned_engine": "documentdb"},
            {"query_id": "j2", "assigned_engine": "documentdb"},
            {"query_id": "a0", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("j1", "j2", "a0", *(f"y{i}" for i in range(6)))
        scoring_context = _scoring_context(
            {
                "j1": {"dynamodb": 90, "aurora_mysql": 10},
                "j2": {"dynamodb": 10, "aurora_mysql": 90},
            }
        )

        updated, _, _ = sanity_sweep(
            assignments, [], scoring_context, query_map, co_dependency_groups=[["j1", "j2"]]
        )

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id["j1"] == "dynamodb"
        assert by_id["j2"] == "dynamodb"  # moved together, never split from j1

    def test_a_blocked_group_member_keeps_the_whole_group_and_engine(self, capsys) -> None:
        # j1 needs a capability no committed engine has; j2 (the same
        # group) would otherwise have somewhere to go on its own -- but a
        # group can never split, so both stay, and so does the engine.
        assignments = [
            {"query_id": "j1", "assigned_engine": "documentdb"},
            {"query_id": "j2", "assigned_engine": "documentdb"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("j1", "j2", *(f"y{i}" for i in range(6)))
        scoring_context = ScoringContext(
            analysis_outputs={"dynamodb": {"table_recommendations": []}},
            query_capabilities={"j1": ["multi_doc_acid"]},
        )

        updated, consolidations, _ = sanity_sweep(
            assignments, [], scoring_context, query_map, co_dependency_groups=[["j1", "j2"]]
        )

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id["j1"] == "documentdb"
        assert by_id["j2"] == "documentdb"
        assert consolidations == []
        captured = capsys.readouterr()
        assert "co-dependency group" in captured.out
        assert "lacks multi_doc_acid" in captured.out

    def test_the_old_count_rule_would_have_protected_the_biggest_small_engine(self):
        # documentdb (3 queries) is the biggest sub-threshold engine here --
        # the old `max(engine_counts, key=...)` rule would have protected
        # it and redirected dynamodb (2 queries) away unconditionally. Under
        # Decision 7, BOTH are removed: neither's queries are capability
        # -blocked, and aurora_mysql can serve all of them.
        assignments = [
            {"query_id": "d0", "assigned_engine": "documentdb"},
            {"query_id": "d1", "assigned_engine": "documentdb"},
            {"query_id": "d2", "assigned_engine": "documentdb"},
            {"query_id": "y0", "assigned_engine": "dynamodb"},
            {"query_id": "y1", "assigned_engine": "dynamodb"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ]
        query_map = _query_map("d0", "d1", "d2", "y0", "y1", "a1")
        scoring_context = _scoring_context(
            {
                "d0": {"dynamodb": 30, "aurora_mysql": 70},
                "d1": {"dynamodb": 30, "aurora_mysql": 70},
                "d2": {"dynamodb": 30, "aurora_mysql": 70},
                "y0": {"documentdb": 20, "aurora_mysql": 80},
                "y1": {"documentdb": 20, "aurora_mysql": 80},
            }
        )

        updated, consolidations, _ = sanity_sweep(
            assignments, [], scoring_context, query_map, source_engine="mysql"
        )

        assert all(
            qa["assigned_engine"] == "aurora_mysql" for qa in updated if qa["query_id"] != "a1"
        )
        assert {c["from_engine"] for c in consolidations} == {"documentdb", "dynamodb"}
        assert {c["to_engine"] for c in consolidations} == {"aurora_mysql"}


class TestSanitySweepDecisions:
    """#589: ``sanity_sweep``'s own kept/removed verdict for every orphan
    engine it evaluates, structured so it can reach
    ``reality-check/output.json`` -- before this it only ever reached a
    ``[reality-check]`` log line, and a KEPT engine left no trace at all.
    """

    def test_a_removed_engine_gets_one_decision_naming_every_destination(self) -> None:
        assignments = [
            {"query_id": "d0", "assigned_engine": "documentdb"},
            {"query_id": "d1", "assigned_engine": "documentdb"},
            {"query_id": "d2", "assigned_engine": "documentdb"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_ids = ["d0", "d1", "d2", "a1"] + [f"y{i}" for i in range(6)]
        query_map = _query_map(*query_ids)
        scoring_context = _scoring_context(
            {
                "d0": {"aurora_mysql": 10, "dynamodb": 90},
                "d1": {"aurora_mysql": 95, "dynamodb": 5},
                "d2": {"aurora_mysql": 95, "dynamodb": 5},
            }
        )

        _, _, sweep_decisions = sanity_sweep(
            assignments, [], scoring_context, query_map, source_engine="mysql"
        )

        assert len(sweep_decisions) == 1
        decision = sweep_decisions[0]
        assert decision["engine"] == "documentdb"
        assert decision["decision"] == "removed"
        assert decision["blocking_query_ids"] == []
        assert decision["blocking_rule"] is None
        assert "aurora_mysql" in decision["reason"] or "Aurora" in decision["reason"]
        assert "dynamodb" in decision["reason"] or "DynamoDB" in decision["reason"]

    def test_a_kept_engine_with_no_serviceable_destination_names_the_blocking_query(self) -> None:
        assignments = [
            {"query_id": "g1", "assigned_engine": "documentdb"},
            {"query_id": "g2", "assigned_engine": "documentdb"},
        ] + [{"query_id": f"z{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("g1", "g2", *(f"z{i}" for i in range(6)))
        scoring_context = ScoringContext(
            analysis_outputs={"dynamodb": {"table_recommendations": []}},
            query_capabilities={"g1": ["multi_doc_acid"]},
        )

        _, consolidations, sweep_decisions = sanity_sweep(
            assignments, [], scoring_context, query_map, source_engine=""
        )

        assert consolidations == []
        assert len(sweep_decisions) == 1
        decision = sweep_decisions[0]
        assert decision["engine"] == "documentdb"
        assert decision["decision"] == "kept"
        assert decision["blocking_query_ids"] == ["g1"]
        assert decision["blocking_rule"] == "no_serviceable_engine"
        assert "lacks multi_doc_acid" in decision["reason"]
        assert "DynamoDB" in decision["reason"]  # display name, never the raw engine id

    def test_a_kept_engine_blocked_by_a_signal_override_names_that_rule(self) -> None:
        assignments = [
            {"query_id": "s1", "assigned_engine": "opensearch", "signal_override": "text_search"},
            {"query_id": "s2", "assigned_engine": "opensearch", "signal_override": "text_search"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ]
        query_map = _query_map("s1", "s2", "a1")
        scoring_context = _scoring_context({"s1": {"aurora_mysql": 90}, "s2": {"aurora_mysql": 90}})

        _, consolidations, sweep_decisions = sanity_sweep(
            assignments, [], scoring_context, query_map
        )

        assert consolidations == []
        assert len(sweep_decisions) == 1
        decision = sweep_decisions[0]
        assert decision["engine"] == "opensearch"
        assert decision["decision"] == "kept"
        # Both s1 and s2 are individually mandatory for the signal override.
        assert set(decision["blocking_query_ids"]) == {"s1", "s2"}
        assert decision["blocking_rule"] == "signal_override"
        assert "text_search" in decision["reason"]

    def test_a_kept_engine_blocked_by_ineligible_destination_names_that_rule(self) -> None:
        # i1 (an INSERT) scores well on opensearch, but opensearch cannot own
        # writes -- and with no Aurora engine committed anywhere, there is no
        # eligible destination at all, so documentdb (the 1-query orphan) is
        # kept, never redirected onto a destination the reality check's own
        # placement rule would refuse.
        assignments = [
            {"query_id": "i1", "assigned_engine": "documentdb"},
        ] + [{"query_id": f"o{i}", "assigned_engine": "opensearch"} for i in range(6)]
        query_map = _query_map(*(f"o{i}" for i in range(6)))
        query_map["i1"] = {
            "query_id": "i1",
            "query_text": "INSERT INTO orders (id) VALUES (1)",
            "tables_accessed": ["t_i1"],
        }
        scoring_context = _scoring_context({"i1": {"opensearch": 90}})

        _, consolidations, sweep_decisions = sanity_sweep(
            assignments, [], scoring_context, query_map
        )

        assert consolidations == []
        assert len(sweep_decisions) == 1
        decision = sweep_decisions[0]
        assert decision["engine"] == "documentdb"
        assert decision["decision"] == "kept"
        assert decision["blocking_query_ids"] == ["i1"]
        assert decision["blocking_rule"] == "ineligible_destination"

    def test_a_kept_engine_with_different_blocking_rules_reports_mixed(self) -> None:
        # s1 is blocked by a signal override; s2 (a different, ungrouped
        # query on the same orphan engine) is blocked for lacking a
        # capability no committed engine has -- two different rules on the
        # same engine, neither folded into the other.
        assignments = [
            {"query_id": "s1", "assigned_engine": "opensearch", "signal_override": "text_search"},
            {"query_id": "s2", "assigned_engine": "opensearch"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("s1", "s2", *(f"y{i}" for i in range(6)))
        scoring_context = ScoringContext(
            analysis_outputs={"dynamodb": {"table_recommendations": []}},
            query_capabilities={"s2": ["multi_doc_acid"]},
        )

        _, consolidations, sweep_decisions = sanity_sweep(
            assignments, [], scoring_context, query_map
        )

        assert consolidations == []
        assert len(sweep_decisions) == 1
        decision = sweep_decisions[0]
        assert decision["engine"] == "opensearch"
        assert decision["decision"] == "kept"
        assert set(decision["blocking_query_ids"]) == {"s1", "s2"}
        assert decision["blocking_rule"] == "mixed"

    def test_a_kept_engine_with_no_other_committed_engine_at_all(self) -> None:
        # documentdb is the ONLY engine in the whole distribution -- no
        # Aurora, no other engine over the orphan threshold -- so there is
        # structurally nothing else to redirect to.
        assignments = [
            {"query_id": "d0", "assigned_engine": "documentdb"},
            {"query_id": "d1", "assigned_engine": "documentdb"},
        ]
        query_map = _query_map("d0", "d1")
        scoring_context = ScoringContext()

        _, consolidations, sweep_decisions = sanity_sweep(
            assignments, [], scoring_context, query_map
        )

        assert consolidations == []
        assert len(sweep_decisions) == 1
        decision = sweep_decisions[0]
        assert decision["engine"] == "documentdb"
        assert decision["decision"] == "kept"
        assert set(decision["blocking_query_ids"]) == {"d0", "d1"}
        assert decision["blocking_rule"] == "no_serviceable_engine"


class TestSanitySweepEligibility:
    """#579 round 3: the shared scorer's gates (exclusion, capability)
    are not the only ones Phase 2 applies. The sweep must also respect
    the write/ownership gate, the specialist-absorb restriction
    (``may_absorb``/``can_own``), and the join-off-a-non-Aurora-engine-
    never-onto-DynamoDB block (``reality_check._query_eligible_engines``)
    -- or it can place a query somewhere Phase 2 itself would refuse to.
    """

    def test_an_insert_never_lands_on_opensearch(self) -> None:
        # opensearch scores better by pattern (90 vs 40), but it is not a
        # system of record -- the write gate (``can_own``) must block it
        # regardless of score.
        assignments = [
            {"query_id": "i1", "assigned_engine": "documentdb"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"o{i}", "assigned_engine": "opensearch"} for i in range(6)]
        query_map = _query_map("a1", *(f"o{i}" for i in range(6)))
        query_map["i1"] = {
            "query_id": "i1",
            "query_text": "INSERT INTO orders (id) VALUES (1)",
            "tables_accessed": ["t_i1"],
        }
        scoring_context = _scoring_context({"i1": {"opensearch": 90, "aurora_mysql": 40}})

        updated, _, _ = sanity_sweep(assignments, [], scoring_context, query_map)

        i1 = next(qa for qa in updated if qa["query_id"] == "i1")
        assert i1["assigned_engine"] == "aurora_mysql"

    def test_a_plain_select_never_lands_on_opensearch_without_a_search_signal(self) -> None:
        # A read, so the write gate alone would not stop it -- but opensearch
        # is a specialist (``may_absorb``'s SPECIALIST_ABSORB_SIGNALS):
        # with no text_search/aggregations signal and no inverted_index
        # capability, it may only keep queries it already owns, never
        # absorb a plain PK lookup just because it scores well by pattern.
        assignments = [
            {"query_id": "p1", "assigned_engine": "documentdb"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"o{i}", "assigned_engine": "opensearch"} for i in range(6)]
        query_map = _query_map("p1", "a1", *(f"o{i}" for i in range(6)))
        scoring_context = _scoring_context({"p1": {"opensearch": 90, "aurora_mysql": 40}})

        updated, _, _ = sanity_sweep(assignments, [], scoring_context, query_map)

        p1 = next(qa for qa in updated if qa["query_id"] == "p1")
        assert p1["assigned_engine"] == "aurora_mysql"

    def test_a_join_never_lands_on_dynamodb_from_a_non_aurora_engine(self) -> None:
        # Leaving documentdb (neither DynamoDB nor Aurora) with a join
        # query: the join-off-non-Aurora-onto-DynamoDB block applies even
        # though dynamodb scores better (90 vs 40).
        assignments = [
            {"query_id": "j1", "assigned_engine": "documentdb"},
            {"query_id": "a1", "assigned_engine": "aurora_mysql"},
        ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("a1", *(f"y{i}" for i in range(6)))
        query_map["j1"] = {"query_id": "j1", "has_joins": True, "tables_accessed": ["t_j1"]}
        scoring_context = _scoring_context({"j1": {"dynamodb": 90, "aurora_mysql": 40}})

        updated, _, _ = sanity_sweep(assignments, [], scoring_context, query_map)

        j1 = next(qa for qa in updated if qa["query_id"] == "j1")
        assert j1["assigned_engine"] == "aurora_mysql"
