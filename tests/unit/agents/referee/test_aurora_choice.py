"""Unit tests for the #381 heterogeneous Aurora engine choice.

A source with no Aurora dialect of its own (SQL Server, Oracle, DB2) has both
Aurora engines compete on the collected workload instead of being skipped
(``triage.py``). ``choose_heterogeneous_engine`` (and ``pick_aurora_engine``,
which delegates to it) decides the winner by total adjusted score, a feature
tie-break within a close margin, and Aurora PostgreSQL on an exact tie.
"""

from __future__ import annotations

from unittest.mock import patch

from src.agents.referee.aurora_choice import (
    FEATURE_SIGNALS,
    MARGIN_RATIO,
    choose_heterogeneous_engine,
    detect_heterogeneous_features,
    pick_aurora_engine,
    pick_aurora_engine_with_tie,
)


def _co(queries: list[str] | None = None, tables: list[dict] | None = None) -> dict:
    return {
        "database_schema": {"tables": tables or []},
        "queries": {
            "query_patterns": [
                {"query_id": f"q{i}", "query_text": text} for i, text in enumerate(queries or [])
            ]
        },
    }


class TestChooseHeterogeneousEngine:
    def test_clear_mysql_win(self):
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 9000.0, "aurora_postgresql": 3000.0},
        )
        assert winner == "aurora_mysql"
        assert trace["margin"] == -6000.0
        assert trace["deciding_features"] == []
        assert "higher total adjusted score wins" in trace["reason"]

    def test_clear_postgres_win(self):
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 1000.0, "aurora_postgresql": 8000.0},
        )
        assert winner == "aurora_postgresql"
        assert trace["margin"] == 7000.0

    def test_exact_tie_goes_to_postgresql(self):
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 5000.0, "aurora_postgresql": 5000.0},
        )
        assert winner == "aurora_postgresql"
        assert trace["deciding_features"] == []
        assert "exact tie" in trace["reason"]
        assert "earned" not in trace["reason"]

    def test_exact_tie_with_a_feature_present_still_records_it(self):
        # #381 review: an exact tie's default already picks Aurora PostgreSQL, but a
        # real workload feature favouring it should still show up in the trace rather
        # than be silently dropped just because the default already agreed.
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 5000.0, "aurora_postgresql": 5000.0},
            features={"sequences": True},
        )
        assert winner == "aurora_postgresql"
        assert trace["deciding_features"] == ["sequences"]
        assert "exact tie" in trace["reason"]
        assert "earned" not in trace["reason"]
        assert "tipped by: sequences" in trace["reason"]
        # #656: a feature decides before the maintainer rule is ever reached.
        assert "maintainer rule" not in trace["reason"]

    def test_no_score_at_all_is_an_exact_tie(self):
        winner, trace = choose_heterogeneous_engine(["aurora_mysql", "aurora_postgresql"], {})
        assert winner == "aurora_postgresql"
        assert "exact tie" in trace["reason"]

    def test_near_tie_decided_by_a_reported_feature(self):
        # Within MARGIN_RATIO of the larger total, and a feature favouring
        # PostgreSQL is present -- PostgreSQL wins even though MySQL's raw
        # total is (slightly) higher.
        totals = {"aurora_mysql": 1000.0, "aurora_postgresql": 980.0}
        assert abs(totals["aurora_mysql"] - totals["aurora_postgresql"]) <= MARGIN_RATIO * 1000.0
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            totals,
            features={"recursive_ctes": True, "sequences": False},
        )
        assert winner == "aurora_postgresql"
        assert trace["deciding_features"] == ["recursive_ctes"]
        assert "tipped by" in trace["reason"]

    def test_near_tie_with_no_feature_falls_back_to_higher_total(self):
        totals = {"aurora_mysql": 1000.0, "aurora_postgresql": 980.0}
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"], totals, features={}
        )
        assert winner == "aurora_mysql"
        assert trace["deciding_features"] == []
        assert "no deciding feature" in trace["reason"]

    def test_feature_not_in_the_tracked_list_is_ignored(self):
        totals = {"aurora_mysql": 1000.0, "aurora_postgresql": 990.0}
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            totals,
            features={"some_untracked_signal": True},
        )
        assert winner == "aurora_mysql"
        assert trace["deciding_features"] == []

    def test_single_engine_pool_returns_it_with_empty_trace(self):
        winner, trace = choose_heterogeneous_engine(["aurora_mysql"], {"aurora_mysql": 42.0})
        assert winner == "aurora_mysql"
        assert trace["margin"] is None
        assert trace["deciding_features"] == []

    def test_every_feature_signal_favours_postgresql(self):
        # Design: every tracked feature favours Aurora PostgreSQL today.
        for feature in FEATURE_SIGNALS:
            winner, trace = choose_heterogeneous_engine(
                ["aurora_mysql", "aurora_postgresql"],
                {"aurora_mysql": 1000.0, "aurora_postgresql": 980.0},
                features={feature: True},
            )
            assert winner == "aurora_postgresql", feature
            assert trace["deciding_features"] == [feature]


MAINTAINER_TIE_REASON = (
    "tie between Aurora MySQL and Aurora PostgreSQL; "
    "PostgreSQL preferred on a tie by maintainer rule, ADR-031 Decision 6"
)


class TestExactTiePrefersPostgresqlByMaintainerRule:
    """ADR-031 Decision 6 amendment (maintainer decision, 2026-10-14, #656).

    Aurora PostgreSQL wins by the maintainer rule ONLY as the final tie-break:
    after the per-query scores (which already carry per-operation support,
    through the scorer's hard capability gate), the margin band and the
    workload/source features all leave an exact tie. The trace then says it was
    the maintainer rule, not a score or a feature. A feature on an exact tie,
    and any close call inside ``MARGIN_RATIO``, are decided as before.
    """

    BOTH = ["aurora_mysql", "aurora_postgresql"]

    def test_exact_tie_with_no_feature_goes_to_postgresql_by_the_maintainer_rule(self):
        winner, trace = choose_heterogeneous_engine(
            self.BOTH, {"aurora_mysql": 6859.0, "aurora_postgresql": 6859.0}, features={}
        )
        assert winner == "aurora_postgresql"
        assert trace["margin"] == 0.0
        assert trace["deciding_features"] == []
        assert trace["reason"].startswith(MAINTAINER_TIE_REASON)
        assert "6859.0 each" in trace["reason"]

    def test_a_feature_decides_an_exact_tie_before_the_maintainer_rule(self):
        # SQL Server always reports Babelfish, so an exact tie on a SQL Server
        # source is decided by that feature and never reaches the rule.
        winner, trace = choose_heterogeneous_engine(
            self.BOTH,
            {"aurora_mysql": 6859.0, "aurora_postgresql": 6859.0},
            features={"babelfish_compatible": True},
        )
        assert winner == "aurora_postgresql"
        assert trace["deciding_features"] == ["babelfish_compatible"]
        assert "tipped by: Babelfish compatibility" in trace["reason"]
        assert "maintainer rule" not in trace["reason"]

    def test_features_reported_false_do_not_count_as_deciding(self):
        winner, trace = choose_heterogeneous_engine(
            self.BOTH,
            {"aurora_mysql": 6859.0, "aurora_postgresql": 6859.0},
            features={f: False for f in FEATURE_SIGNALS},
        )
        assert winner == "aurora_postgresql"
        assert trace["deciding_features"] == []
        assert trace["reason"].startswith(MAINTAINER_TIE_REASON)

    def test_input_order_does_not_change_the_tie_winner(self):
        totals = {"aurora_postgresql": 10.0, "aurora_mysql": 10.0}
        for pool in (self.BOTH, list(reversed(self.BOTH))):
            winner, _ = choose_heterogeneous_engine(pool, totals, features={})
            assert winner == "aurora_postgresql"

    def test_a_close_call_inside_the_margin_is_not_the_maintainer_tie_rule(self):
        # Today's margin-band behaviour, unchanged by #656: trailgear's shape
        # (MySQL 6896 vs PostgreSQL 6859, within 5%) with no deciding feature
        # goes to the higher total, and the trace never cites the tie rule.
        winner, trace = choose_heterogeneous_engine(
            self.BOTH, {"aurora_mysql": 6896.0, "aurora_postgresql": 6859.0}, features={}
        )
        assert winner == "aurora_mysql"
        assert "maintainer rule" not in trace["reason"]

    def test_a_feature_tipped_close_call_does_not_cite_the_tie_rule_either(self):
        winner, trace = choose_heterogeneous_engine(
            self.BOTH,
            {"aurora_mysql": 6896.0, "aurora_postgresql": 6859.0},
            features={"babelfish_compatible": True},
        )
        assert winner == "aurora_postgresql"
        assert "tipped by: Babelfish compatibility" in trace["reason"]
        assert "maintainer rule" not in trace["reason"]


class TestPickAuroraEngineHeterogeneous:
    def test_homogeneous_source_unaffected_by_score_totals(self):
        # A MySQL source keeps its own dialect even if the (hypothetical)
        # totals would otherwise favour PostgreSQL.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "mysql",
            score_totals={"aurora_mysql": 10.0, "aurora_postgresql": 9000.0},
        )
        assert winner == "aurora_mysql"

    def test_heterogeneous_source_uses_score_totals(self):
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            score_totals={"aurora_mysql": 10.0, "aurora_postgresql": 9000.0},
        )
        assert winner == "aurora_postgresql"

    def test_no_score_totals_falls_back_to_counts(self):
        # Every other caller (post-#381 collapse): the losing engine never
        # served a query, so counts alone agree with the score-based choice.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 50, "aurora_postgresql": 0},
        )
        assert winner == "aurora_mysql"

    def test_no_candidates_returns_none(self):
        assert pick_aurora_engine({"dynamodb"}, "sqlserver") is None

    def test_single_candidate_returned_without_needing_totals(self):
        assert pick_aurora_engine({"aurora_postgresql"}, "sqlserver") == "aurora_postgresql"

    def test_zero_zero_counts_do_not_override_a_recorded_choice(self):
        # #381 review: no score_totals and no query has been assigned to either
        # engine yet (0/0) -- the tie-break default (Aurora PostgreSQL) must not
        # silently override a choice the resolver already recorded, here MySQL.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
            prior_choice="aurora_mysql",
        )
        assert winner == "aurora_mysql"

    def test_zero_zero_counts_go_through_the_tie_break_not_a_name_preference(self):
        # #550: no score, no recorded settled workload at all -- this must not
        # be decided by an engine-name preference. It goes through the shared
        # ADR-031 tie-break (``tie_break.break_tie``) like every other tie in
        # the routing code: no source-compatible engine survives this far
        # (that already returned above), no "current" engine either, so
        # rule 3's settled-workload-share placeholder decides -- and with both
        # engines' settled count tied at zero, that placeholder falls through
        # to its deterministic hash of ``tie_key`` and the engine's name.
        # ``tie_key="q1"`` happens to hash to aurora_mysql for this pair; see
        # ``test_two_different_tie_keys_land_on_different_engines`` for the
        # same tied counts resolving the OTHER way under a different key --
        # the point is the mechanism, not this one key's particular answer.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
            tie_key="q1",
        )
        assert winner == "aurora_mysql"

    def test_a_genuine_count_tie_is_not_decided_by_name(self):
        # #550: a real, nonzero tie (both engines already carry the same
        # amount of recorded workload) must resolve the same way a 0/0 tie
        # does, for the SAME tie_key -- through ``tie_break.break_tie``'s
        # settled-workload-share placeholder -- never by sorting the two
        # engine names. Equal counts of 7 and of 0 both land on the same
        # winner here (``tie_key="q1"``, same as the 0/0 case above) because
        # the tie-break depends on which engines are tied and the key, not on
        # the size of the tied count.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 7, "aurora_postgresql": 7},
            tie_key="q1",
        )
        assert winner == "aurora_mysql"

    def test_two_different_tie_keys_land_on_different_engines(self):
        # #550 review Critical 1: the whole point of a per-call ``tie_key`` is
        # that a tie between the SAME two engines, with the SAME counts, is
        # free to resolve differently for different queries -- never a fixed
        # preference for one engine's name across every workload. If this
        # ever starts asserting the same winner for both keys, the fix has
        # regressed back into a constant seed.
        counts = {"aurora_mysql": 0, "aurora_postgresql": 0}
        winner_a = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"}, "sqlserver", counts, tie_key="q1"
        )
        winner_b = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"}, "sqlserver", counts, tie_key="q2"
        )
        assert winner_a == "aurora_mysql"
        assert winner_b == "aurora_postgresql"
        assert winner_a != winner_b

    def test_no_tie_key_passed_still_resolves_deterministically_but_is_not_a_default(self):
        # #550 review: a caller that leaves ``tie_key`` at its default ``""``
        # still gets a deterministic answer (never a crash, never a random
        # pick) -- just whatever this exact empty seed happens to hash to for
        # this engine pair, which must never be read as "PostgreSQL is the
        # default winner of a tie": every real caller in
        # ``assignment_resolver.py``/``consolidation_validator.py``/
        # ``reality_check.py`` always passes a real one (see those modules'
        # own tests), precisely so none of them repeat this degenerate case.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
        )
        assert winner == "aurora_postgresql"

    def test_unequal_nonzero_counts_use_the_settled_workload_share_rule(self):
        # #550: when the counts are NOT tied, the engine already carrying more
        # of the recorded workload wins -- ADR-031 Decision 12's approved
        # settled-workload-share placeholder, applied the same way
        # ``pick_unresolved_tie_placeholder`` applies it anywhere else, not a
        # bespoke count comparison local to this module.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 3, "aurora_postgresql": 10},
        )
        assert winner == "aurora_postgresql"
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 10, "aurora_postgresql": 3},
        )
        assert winner == "aurora_mysql"

    def test_nonzero_counts_are_not_overridden_by_a_stale_prior_choice(self):
        # Once a real count exists, counts (reflecting what actually happened)
        # take priority over a stale prior_choice.
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 10},
            prior_choice="aurora_mysql",
        )
        assert winner == "aurora_postgresql"

    def test_the_fallback_actually_calls_the_shared_tie_break(self):
        # #550: the fix is the mechanism, not just the output -- a count-tied,
        # score-less, prior-choice-less call must go through
        # ``tie_break.break_tie`` itself (the one place ADR-031 Decision 2/12
        # ties live), never a parallel, module-local comparison that happens
        # to agree with it on this engine pair. #550 review (Suggestion 5):
        # also checks the ``query_id`` ``break_tie`` actually received is the
        # real ``tie_key`` passed in, and that it is non-empty -- a mock that
        # only checked ``tied``/``settled_counts`` would have missed the
        # constant-seed bug entirely, since that bug never touched either of
        # those two arguments.
        with patch(
            "src.agents.referee.aurora_choice.break_tie",
            return_value=("aurora_mysql", "tie_unresolved"),
        ) as mock_break_tie:
            winner = pick_aurora_engine(
                {"aurora_mysql", "aurora_postgresql"},
                "sqlserver",
                query_counts={"aurora_mysql": 7, "aurora_postgresql": 7},
                tie_key="q1,q2",
            )
        assert winner == "aurora_mysql"
        mock_break_tie.assert_called_once()
        args, kwargs = mock_break_tie.call_args
        tied = args[0] if args else kwargs["tied"]
        assert sorted(tied) == ["aurora_mysql", "aurora_postgresql"]
        settled_counts = kwargs.get("settled_counts")
        if settled_counts is None and len(args) >= 4:
            settled_counts = args[3]
        assert settled_counts == {"aurora_mysql": 7, "aurora_postgresql": 7}
        query_id = kwargs.get("query_id")
        if query_id is None and len(args) >= 5:
            query_id = args[4]
        assert query_id == "q1,q2"
        assert query_id


class TestPickAuroraEngineWithTie:
    """#550 review Important 3: a caller that needs to know whether the pick
    was an unresolved tie (to record ``QueryAssignment.tied_engines``/
    ``routing_trace.record_tie``) calls ``pick_aurora_engine_with_tie``
    directly, instead of ``pick_aurora_engine`` discarding it.
    """

    def test_source_compatible_match_reports_no_tie(self):
        winner, tie_kind = pick_aurora_engine_with_tie(
            {"aurora_mysql", "aurora_postgresql"}, "mysql"
        )
        assert (winner, tie_kind) == ("aurora_mysql", None)

    def test_single_candidate_reports_no_tie(self):
        winner, tie_kind = pick_aurora_engine_with_tie({"aurora_postgresql"}, "sqlserver")
        assert (winner, tie_kind) == ("aurora_postgresql", None)

    def test_scored_heterogeneous_decision_reports_no_tie(self):
        winner, tie_kind = pick_aurora_engine_with_tie(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            score_totals={"aurora_mysql": 9000.0, "aurora_postgresql": 10.0},
        )
        assert (winner, tie_kind) == ("aurora_mysql", None)

    def test_live_prior_choice_reports_no_tie(self):
        winner, tie_kind = pick_aurora_engine_with_tie(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
            prior_choice="aurora_mysql",
        )
        assert (winner, tie_kind) == ("aurora_mysql", None)

    def test_no_candidates_reports_no_tie(self):
        assert pick_aurora_engine_with_tie({"dynamodb"}, "sqlserver") == (None, None)

    def test_the_no_score_fallback_reports_tie_unresolved(self):
        winner, tie_kind = pick_aurora_engine_with_tie(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
            tie_key="q1",
        )
        assert (winner, tie_kind) == ("aurora_mysql", "tie_unresolved")

    def test_pick_aurora_engine_is_just_the_winner_half_of_this(self):
        winner, tie_kind = pick_aurora_engine_with_tie(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
            tie_key="q1",
        )
        assert tie_kind == "tie_unresolved"
        assert (
            pick_aurora_engine(
                {"aurora_mysql", "aurora_postgresql"},
                "sqlserver",
                query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
                tie_key="q1",
            )
            == winner
        )


class TestDetectHeterogeneousFeatures:
    def test_sequences_sql_server_syntax(self):
        co = _co(["SELECT NEXT VALUE FOR dbo.OrderNumbers"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["sequences"] is True

    def test_sequences_oracle_syntax(self):
        co = _co(["SELECT order_seq.NEXTVAL FROM dual"])
        features = detect_heterogeneous_features(co, "oracle")
        assert features["sequences"] is True

    def test_no_sequences(self):
        co = _co(["SELECT * FROM orders"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["sequences"] is False

    def test_recursive_cte_detected(self):
        co = _co(
            [
                "WITH emp_tree AS ("
                "SELECT id, manager_id FROM employees WHERE manager_id IS NULL "
                "UNION ALL "
                "SELECT e.id, e.manager_id FROM employees e "
                "JOIN emp_tree t ON e.manager_id = t.id"
                ") SELECT * FROM emp_tree"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["recursive_ctes"] is True

    def test_non_recursive_cte_not_flagged(self):
        co = _co(["WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["recursive_ctes"] is False

    def test_merge_statement_detected(self):
        co = _co(
            [
                "MERGE INTO targettbl AS t USING sourcetbl AS s ON t.id = s.id "
                "WHEN MATCHED THEN UPDATE SET t.val = s.val"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is True

    def test_json_usage_from_query_function(self):
        co = _co(["SELECT JSON_VALUE(payload, '$.id') FROM events"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["json_usage"] is True

    def test_json_usage_from_schema_column(self):
        co = _co(
            [],
            tables=[
                {
                    "table_name": "events",
                    "columns": [{"column_name": "payload", "data_type": "json"}],
                }
            ],
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["json_usage"] is True

    def test_multiple_schemas_detected(self):
        co = _co(
            [],
            tables=[
                {"table_name": "orders", "schema_name": "Sales"},
                {"table_name": "products", "schema_name": "Production"},
            ],
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["multiple_schemas"] is True

    def test_single_schema_not_flagged(self):
        co = _co(
            [],
            tables=[
                {"table_name": "orders", "schema_name": "dbo"},
                {"table_name": "products", "schema_name": "dbo"},
            ],
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["multiple_schemas"] is False

    def test_recursive_cte_not_first_in_the_with_list_is_detected(self):
        # #381 review: a recursive CTE named anywhere but first in the WITH list
        # (introduced by a comma, not a second "WITH") must still be detected.
        co = _co(
            [
                "WITH recent AS (SELECT * FROM orders), "
                "emp_tree AS ("
                "SELECT id, manager_id FROM employees WHERE manager_id IS NULL "
                "UNION ALL "
                "SELECT e.id, e.manager_id FROM employees e "
                "JOIN emp_tree t ON e.manager_id = t.id"
                ") SELECT * FROM recent, emp_tree"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["recursive_ctes"] is True

    def test_sequence_keyword_only_in_a_comment_is_not_detected(self):
        co = _co(["SELECT * FROM orders -- see NEXT VALUE FOR legacy_seq\n"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["sequences"] is False

    def test_merge_keyword_only_in_a_string_literal_is_not_detected(self):
        co = _co(["SELECT 'see the MERGE INTO t USING s docs' AS note FROM orders"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is False

    def test_merge_as_a_bare_column_name_is_not_detected(self):
        # A column literally named "merge" followed, elsewhere, by an unrelated
        # "using" clause must not be mistaken for a MERGE statement.
        co = _co(["SELECT merge FROM audit_log USING (NOLOCK)"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is False

    def test_json_value_as_a_bare_column_name_is_not_detected(self):
        # A column literally named "json_value" (not a call to the function) must
        # not be mistaken for JSON usage.
        co = _co(["SELECT json_value FROM settings"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["json_usage"] is False

    def test_block_comment_keyword_is_not_detected(self):
        co = _co(["SELECT * /* uses MERGE INTO t USING s */ FROM orders"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is False


class TestBabelfishIsLastResort:
    """#381 review: babelfish_compatible is a property of the source *dialect*
    (always True for a SQL Server source, regardless of workload), so it must not
    decide a close call on equal footing with a workload-based feature -- it only
    breaks a *remaining* tie once no workload feature is present."""

    def test_a_mysql_favouring_workload_feature_present_wins_over_babelfish(self):
        # Design: every tracked workload feature favours aurora_postgresql too
        # today, so this exercises the precedence (primary over babelfish), not a
        # case where they disagree -- see choose_heterogeneous_engine's docstring
        # for why the pool has no mysql-favouring signal yet.
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 1000.0, "aurora_postgresql": 980.0},
            features={"babelfish_compatible": True, "recursive_ctes": True},
        )
        assert winner == "aurora_postgresql"
        # The workload-based feature is named; babelfish is not needed to decide.
        assert trace["deciding_features"] == ["recursive_ctes"]

    def test_babelfish_alone_breaks_a_remaining_tie(self):
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 1000.0, "aurora_postgresql": 980.0},
            features={"babelfish_compatible": True},
        )
        assert winner == "aurora_postgresql"
        assert trace["deciding_features"] == ["babelfish_compatible"]

    def test_babelfish_true_with_no_workload_feature_and_no_close_score_does_not_win(self):
        # Babelfish is always True for a SQL Server source -- without a close score
        # or a workload feature, it must not override a clear MySQL win.
        winner, trace = choose_heterogeneous_engine(
            ["aurora_mysql", "aurora_postgresql"],
            {"aurora_mysql": 9000.0, "aurora_postgresql": 1000.0},
            features={"babelfish_compatible": True},
        )
        assert winner == "aurora_mysql"
        assert trace["deciding_features"] == []

    def test_babelfish_only_for_sqlserver(self):
        assert detect_heterogeneous_features(_co(), "sqlserver")["babelfish_compatible"] is True
        assert detect_heterogeneous_features(_co(), "oracle")["babelfish_compatible"] is False

    def test_no_signals_at_all(self):
        co = _co(["SELECT * FROM orders WHERE id = ?"])
        features = detect_heterogeneous_features(co, "oracle")
        assert not any(features.values())


class TestScannerHandlesRealSqlServerSyntax:
    """#381 review (round 2): the regex-based comment/string stripper missed real
    SQL Server MERGE/CTE syntax -- bracketed/quoted identifiers, a MERGE into a temp
    table, a TOP clause, and an apostrophe inside a comment swallowing the following
    code. ``_scan_sql`` (single left-to-right scanner) fixes all of these."""

    def test_merge_with_bracketed_target_and_alias(self):
        co = _co(
            [
                "MERGE [dbo].[Target] AS t USING [dbo].[Source] AS s ON t.id = s.id "
                "WHEN MATCHED THEN UPDATE SET t.val = s.val"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is True

    def test_merge_into_a_temp_table(self):
        co = _co(
            [
                "MERGE #target AS t USING source AS s ON t.id = s.id "
                "WHEN MATCHED THEN UPDATE SET t.val = s.val"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is True

    def test_merge_into_a_bracketed_identifier(self):
        co = _co(
            ["MERGE INTO [x] USING y ON x.id = y.id " "WHEN MATCHED THEN UPDATE SET x.val = y.val"]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is True

    def test_merge_with_a_top_clause(self):
        co = _co(
            [
                "MERGE TOP (10) target USING source ON target.id = source.id "
                "WHEN MATCHED THEN UPDATE SET target.val = source.val"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is True

    def test_leading_line_comment_with_an_apostrophe_does_not_swallow_the_merge(self):
        co = _co(
            [
                "-- don't touch\n"
                "MERGE INTO target AS t USING source AS s ON t.id = s.id "
                "WHEN MATCHED THEN UPDATE SET t.val = s.val"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is True

    def test_block_comment_with_an_apostrophe_does_not_swallow_the_sequence(self):
        co = _co(["/* customer's id */ SELECT NEXT VALUE FOR dbo.Seq, 'x'"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["sequences"] is True

    def test_recursive_cte_with_a_bracketed_name(self):
        co = _co(
            [
                "WITH [Tree] AS ("
                "SELECT id, manager_id FROM employees WHERE manager_id IS NULL "
                "UNION ALL "
                "SELECT e.id, e.manager_id FROM employees e "
                "JOIN [Tree] t ON e.manager_id = t.id"
                ") SELECT * FROM [Tree]"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["recursive_ctes"] is True

    def test_recursive_cte_with_a_double_quoted_name(self):
        co = _co(
            [
                'WITH "Tree" AS ('
                "SELECT id, manager_id FROM employees WHERE manager_id IS NULL "
                "UNION ALL "
                "SELECT e.id, e.manager_id FROM employees e "
                'JOIN "Tree" t ON e.manager_id = t.id'
                ') SELECT * FROM "Tree"'
            ]
        )
        features = detect_heterogeneous_features(co, "oracle")
        assert features["recursive_ctes"] is True

    def test_nested_block_comment_does_not_leak_a_merge(self):
        # The real MERGE statement sits between the inner comment's close and the
        # outer comment's close -- still inside the outer comment. A non-nesting
        # stripper stops at the first "*/" and leaves it exposed as live text.
        co = _co(
            [
                "/* outer comment /* inner */ MERGE INTO target USING source "
                "ON target.id = source.id */ SELECT 1"
            ]
        )
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is False

    def test_merge_as_a_bare_column_name_still_not_detected(self):
        co = _co(["SELECT merge FROM audit_log USING (NOLOCK)"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is False

    def test_json_value_as_a_bare_column_name_still_not_detected(self):
        co = _co(["SELECT json_value FROM settings"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["json_usage"] is False

    def test_string_literal_containing_keywords_still_not_detected(self):
        co = _co(["SELECT 'MERGE INTO t USING s, NEXT VALUE FOR x' AS note FROM orders"])
        features = detect_heterogeneous_features(co, "sqlserver")
        assert features["merge_statements"] is False
        assert features["sequences"] is False
