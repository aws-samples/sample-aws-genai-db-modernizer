"""Unit tests for the #381 heterogeneous Aurora engine choice.

A source with no Aurora dialect of its own (SQL Server, Oracle, DB2) has both
Aurora engines compete on the collected workload instead of being skipped
(``triage.py``). ``choose_heterogeneous_engine`` (and ``pick_aurora_engine``,
which delegates to it) decides the winner by total adjusted score, a feature
tie-break within a close margin, and Aurora PostgreSQL on an exact tie.
"""

from __future__ import annotations

from src.agents.referee.aurora_choice import (
    FEATURE_SIGNALS,
    MARGIN_RATIO,
    choose_heterogeneous_engine,
    detect_heterogeneous_features,
    pick_aurora_engine,
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
        assert "tie-break" in trace["reason"]

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

    def test_zero_zero_counts_with_no_prior_choice_falls_back_to_default(self):
        winner = pick_aurora_engine(
            {"aurora_mysql", "aurora_postgresql"},
            "sqlserver",
            query_counts={"aurora_mysql": 0, "aurora_postgresql": 0},
        )
        assert winner == "aurora_postgresql"

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
