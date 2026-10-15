"""Tests for the collector digest normalization step (#556).

Query text taken from the sample fixtures is inlined verbatim rather than
unzipped at test time:

- MySQL digests from ``docs/examples/wordpress/wordpress.zip``
  (``wordpress-collection.json``), including the three queries whose digest
  text is cut off mid-statement because
  ``performance_schema_max_digest_length`` capped it around 1024 characters.
- PostgreSQL query text from ``docs/examples/discourse/discourse.zip``
  (``discourse-collection.json``), which already uses ``$1``-style
  placeholders and should pass through unchanged.

``test_normalize_fixtures.py`` sweeps every query in both samples through
``normalize_digest_text`` and checks the ``truncated`` flag against ground
truth; this file covers each rule and heuristic case by case.
"""

import pytest

from src.tools.sql.normalize import NormalizedQuery, normalize_digest_text

# ---------------------------------------------------------------------------
# Real MySQL digests (docs/examples/wordpress) -- paren-ellipsis form
# ---------------------------------------------------------------------------


class TestParenEllipsis:
    def test_values_list_becomes_null(self) -> None:
        query = (
            "INSERT INTO `wp_postmeta` ( `post_id` , `meta_key` , `meta_value` ) " "VALUES (...)"
        )
        result = normalize_digest_text(query, "mysql")
        assert result.text == (
            "INSERT INTO `wp_postmeta` ( `post_id` , `meta_key` , `meta_value` ) " "VALUES (NULL)"
        )
        assert result.substitutions == ("paren_ellipsis",)
        assert result.truncated is False

    def test_in_list_becomes_null(self) -> None:
        query = (
            "SELECT `p` . `ID` FROM `wp_posts` `p` WHERE `post_type` = ? "
            "AND `p` . `post_title` = ? AND `p` . `post_status` IN (...) "
            "ORDER BY `p` . `post_date_gmt` ASC LIMIT ?, ..."
        )
        result = normalize_digest_text(query, "mysql")
        assert "`post_status` IN (NULL)" in result.text
        assert "(...)" not in result.text
        assert result.substitutions.count("paren_ellipsis") == 1

    def test_on_duplicate_key_update_values_list(self) -> None:
        query = (
            "INSERT INTO `wp_options` ( `option_name` , `option_value` , `autoload` ) "
            "VALUES (...) ON DUPLICATE KEY UPDATE `option_name` = VALUES ( `option_name` ) , "
            "`option_value` = VALUES ( `option_value` ) , `autoload` = VALUES ( `autoload` )"
        )
        result = normalize_digest_text(query, "mysql")
        assert "VALUES (NULL) ON DUPLICATE KEY" in result.text
        assert result.truncated is False

    def test_spacing_variants_all_normalize_to_null(self) -> None:
        for variant in ("(...)", "( ... )", "(  ...  )", "( ...)", "(...  )"):
            query = f"SELECT 1 FROM t WHERE x IN {variant}"
            result = normalize_digest_text(query, "mysql")
            assert result.text == "SELECT 1 FROM t WHERE x IN (NULL)", variant
            assert result.substitutions == ("paren_ellipsis",)


# ---------------------------------------------------------------------------
# Real MySQL digests -- "?, ..." (an elided run of literals), anywhere in
# the text, not only at the end of the statement.
# ---------------------------------------------------------------------------


class TestListEllipsis:
    def test_limit_list_overflow_dropped(self) -> None:
        query = (
            "SELECT `a` . `action_id` FROM `wp_actionscheduler_actions` `a` "
            "WHERE ? = ? AND `a` . `hook` = ? AND `a` . `status` IN (...) "
            "ORDER BY `a` . `scheduled_date_gmt` ASC LIMIT ?, ..."
        )
        result = normalize_digest_text(query, "mysql")
        assert result.text.endswith("LIMIT NULL")
        assert ", ..." not in result.text
        assert "list_ellipsis" in result.substitutions
        # Not a truncated query: this is a known, complete digest form.
        assert result.truncated is False

    def test_bare_list_ellipsis_dropped(self) -> None:
        result = normalize_digest_text("SELECT 1 LIMIT ?, ...", "mysql")
        assert result.text == "SELECT 1 LIMIT NULL"
        assert result.substitutions == ("list_ellipsis",)
        assert result.truncated is False

    def test_mid_statement_list_ellipsis_real_example(self) -> None:
        # Real wordpress digest: the elided list sits mid-statement, in a
        # multi-row INSERT ... SELECT, not at the end of the digest text.
        query = (
            "INSERT INTO `wp_actionscheduler_actions` ( `hook` , `status` , "
            "`scheduled_date_gmt` , `scheduled_date_local` , `schedule` , `group_id` , "
            "`priority` , `args` ) SELECT ?, ... FROM DUAL WHERE ( SELECT ? FROM DUAL ) IS NULL"
        )
        result = normalize_digest_text(query, "mysql")
        assert "SELECT NULL FROM DUAL WHERE ( SELECT NULL FROM DUAL ) IS NULL" in result.text
        assert "?" not in result.text
        assert result.substitutions.count("list_ellipsis") == 1
        assert result.substitutions.count("bare_placeholder") == 1
        assert result.truncated is False

    def test_list_ellipsis_followed_by_more_text(self) -> None:
        # "LIMIT ?, ..." isn't always the last thing in the statement.
        result = normalize_digest_text(
            "SELECT `id` FROM `t` WHERE `id` = ? LIMIT ?, ... FOR UPDATE", "mysql"
        )
        assert result.text == "SELECT `id` FROM `t` WHERE `id` = NULL LIMIT NULL FOR UPDATE"
        assert result.truncated is False

    def test_list_ellipsis_inside_subquery(self) -> None:
        query = (
            "( SELECT `id` FROM `t` ORDER BY `id` LIMIT ?, ... ) "
            "UNION ( SELECT `id` FROM `u` WHERE `id` IN (...) )"
        )
        result = normalize_digest_text(query, "mysql")
        assert "?" not in result.text
        assert "..." not in result.text
        assert result.truncated is False


# ---------------------------------------------------------------------------
# Real MySQL digests -- bare "?" placeholders
# ---------------------------------------------------------------------------


class TestBarePlaceholder:
    def test_update_with_two_placeholders(self) -> None:
        query = "UPDATE `wp_woocommerce_api_keys` SET `last_access` = ? WHERE `key_id` = ?"
        result = normalize_digest_text(query, "mysql")
        assert result.text == (
            "UPDATE `wp_woocommerce_api_keys` SET `last_access` = NULL WHERE `key_id` = NULL"
        )
        assert result.substitutions == ("bare_placeholder", "bare_placeholder")
        assert result.truncated is False

    def test_select_with_limit_placeholder(self) -> None:
        query = "SELECT `option_value` FROM `wp_options` WHERE `option_name` = ? LIMIT ?"
        result = normalize_digest_text(query, "mysql")
        assert result.text == (
            "SELECT `option_value` FROM `wp_options` WHERE `option_name` = NULL LIMIT NULL"
        )
        assert result.substitutions == ("bare_placeholder", "bare_placeholder")

    def test_placeholder_inside_string_literal_untouched(self) -> None:
        query = "SELECT * FROM `t` WHERE `note` = 'what?' AND `id` = ?"
        result = normalize_digest_text(query, "mysql")
        assert result.text == "SELECT * FROM `t` WHERE `note` = 'what?' AND `id` = NULL"
        assert result.substitutions == ("bare_placeholder",)

    def test_placeholder_inside_quoted_identifier_untouched(self) -> None:
        query = "SELECT `col?name` FROM `t` WHERE `col?name` = ?"
        result = normalize_digest_text(query, "mysql")
        assert result.text == "SELECT `col?name` FROM `t` WHERE `col?name` = NULL"
        assert result.substitutions == ("bare_placeholder",)

    def test_dollar_placeholder_left_alone_even_under_mysql(self) -> None:
        # $1-style placeholders are a PostgreSQL convention; they never occur
        # in real MySQL digest text, but the "?" rule must not touch the "$".
        result = normalize_digest_text("SELECT * FROM t WHERE id = $1", "mysql")
        assert result.text == "SELECT * FROM t WHERE id = $1"
        assert result.substitutions == ()


# ---------------------------------------------------------------------------
# Real MySQL digests cut off mid-statement -- the truncated case
# ---------------------------------------------------------------------------


class TestTruncatedRealExamples:
    """These three digests are exact, unmodified text from
    ``wordpress-collection.json``: the only three queries whose digest text
    hit ``performance_schema_max_digest_length`` (around 1024 characters)
    and got cut off mid-statement. ``test_normalize_fixtures.py`` confirms
    these are the *only* three in the whole sample."""

    def test_dangling_dot_after_length_cap(self) -> None:
        # Cut off right after "`posts` ." -- a dangling column reference.
        # Parentheses and quotes are both still balanced, so only the
        # trailing-dot signal catches this one.
        query = (
            "SELECT SUM ( `meta__order_total` . `meta_value` ) AS `total_sales` , "
            "SUM ( `meta__order_shipping` . `meta_value` ) AS `total_shipping` , "
            "SUM ( `meta__order_tax` . `meta_value` ) AS `total_tax` , "
            "SUM ( `meta__order_shipping_tax` . `meta_value` ) AS `total_shipping_tax` , "
            "`posts` . `post_date` AS `post_date` FROM `wp_posts` AS `posts` "
            "INNER JOIN `wp_postmeta` AS `meta__order_total` ON ( `posts` . `ID` = "
            "`meta__order_total` . `post_id` AND `meta__order_total` . `meta_key` = ? ) "
            "INNER JOIN `wp_postmeta` AS `meta__order_shipping` ON ( `posts` . `ID` = "
            "`meta__order_shipping` . `post_id` AND `meta__order_shipping` . `meta_key` = ? ) "
            "INNER JOIN `wp_postmeta` AS `meta__order_tax` ON ( `posts` . `ID` = "
            "`meta__order_tax` . `post_id` AND `meta__order_tax` . `meta_key` = ? ) "
            "INNER JOIN `wp_postmeta` AS `meta__order_shipping_tax` ON ( `posts` . `ID` = "
            "`meta__order_shipping_tax` . `post_id` AND `meta__order_shipping_tax` . `meta_key` = ? ) "
            "WHERE `posts` . `post_type` IN (...) AND `posts` . `post_status` IN (...) "
            "AND `posts` . `post_date` >= ? AND `posts` ."
        )
        assert len(query) > 1000
        result = normalize_digest_text(query, "mysql")
        assert result.truncated is True

    def test_dangling_open_paren_after_length_cap(self) -> None:
        # Cut off right after an opening "(" from an "ON (...) AND (" join
        # condition -- an unbalanced parenthesis.
        query = (
            "SELECT `parent_meta__order_total` . `meta_value` AS `total_refund` , "
            "`parent_meta__order_shipping` . `meta_value` AS `total_shipping` , "
            "`parent_meta__order_tax` . `meta_value` AS `total_tax` , "
            "`parent_meta__order_shipping_tax` . `meta_value` AS `total_shipping_tax` , "
            "`posts` . `post_date` AS `post_date` FROM `wp_posts` AS `posts` "
            "INNER JOIN `wp_postmeta` AS `parent_meta__order_total` ON "
            "( `posts` . `post_parent` = `parent_meta__order_total` . `post_id` ) AND "
            "( `parent_meta__order_total` . `meta_key` = ? ) "
            "INNER JOIN `wp_postmeta` AS `parent_meta__order_shipping` ON "
            "( `posts` . `post_parent` = `parent_meta__order_shipping` . `post_id` ) AND "
            "( `parent_meta__order_shipping` . `meta_key` = ? ) "
            "INNER JOIN `wp_postmeta` AS `parent_meta__order_tax` ON "
            "( `posts` . `post_parent` = `parent_meta__order_tax` . `post_id` ) AND "
            "( `parent_meta__order_tax` . `meta_key` = ? ) "
            "INNER JOIN `wp_postmeta` AS `parent_meta__order_shipping_tax` ON "
            "( `posts` . `post_parent` = `parent_meta__order_shipping_tax` . `post_id` ) AND ("
        )
        assert len(query) > 1000
        result = normalize_digest_text(query, "mysql")
        assert result.truncated is True

    def test_dangling_keyword_after_length_cap(self) -> None:
        # Cut off right after a bare "AND" with no right-hand side.
        query = (
            "SELECT `posts` . `id` AS `refund_id` , `meta__refund_amount` . `meta_value` "
            "AS `total_refund` , `posts` . `post_date` AS `post_date` , "
            "`order_items` . `order_item_type` AS `item_type` , "
            "`meta__order_total` . `meta_value` AS `total_sales` , "
            "`meta__order_shipping` . `meta_value` AS `total_shipping` , "
            "`meta__order_tax` . `meta_value` AS `total_tax` , "
            "`meta__order_shipping_tax` . `meta_value` AS `total_shipping_tax` , "
            "SUM ( `order_item_meta__qty` . `meta_value` ) AS `order_item_count` "
            "FROM `wp_posts` AS `posts` INNER JOIN `wp_postmeta` AS `meta__refund_amount` ON "
            "( `posts` . `ID` = `meta__refund_amount` . `post_id` AND "
            "`meta__refund_amount` . `meta_key` = ? ) "
            "LEFT JOIN `wp_woocommerce_order_items` AS `order_items` ON "
            "( `posts` . `ID` = `order_items` . `order_id` ) "
            "INNER JOIN `wp_postmeta` AS `meta__order_total` ON "
            "( `posts` . `ID` = `meta__order_total` . `post_id` AND "
            "`meta__order_total` . `meta_key` = ? ) "
            "LEFT JOIN `wp_postmeta` AS `meta__order_shipping` ON "
            "( `posts` . `ID` = `meta__order_shipping` . `post_id` AND"
        )
        assert len(query) > 1000
        result = normalize_digest_text(query, "mysql")
        assert result.truncated is True


# ---------------------------------------------------------------------------
# Truncation detection must not misfire on the normal, complete forms
# ---------------------------------------------------------------------------


class TestTruncationFalsePositives:
    def test_complete_query_not_flagged(self) -> None:
        query = "UPDATE `wp_options` SET `option_value` = ? WHERE `option_name` = ?"
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_trailing_list_ellipsis_not_flagged_truncated(self) -> None:
        # The "..." marker is a deliberate digest convention for an
        # overflowed LIMIT/value list, not evidence of a cut-off statement.
        query = "SELECT `id` FROM `t` WHERE `a` = ? ORDER BY `id` LIMIT ?, ..."
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_paren_ellipsis_not_flagged_truncated(self) -> None:
        query = "SELECT `id` FROM `t` WHERE `status` IN (...)"
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_quoted_unbalanced_paren_not_flagged(self) -> None:
        # The unmatched "(" is inside a string literal, so it must not be
        # counted against the real paren balance.
        query = "SELECT * FROM `t` WHERE `note` = 'open (paren' AND `id` = ?"
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_subquery_parens_balance(self) -> None:
        query = "SELECT `id` FROM `t` WHERE `id` IN ( SELECT `id` FROM `u` WHERE `x` = ? )"
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_boolean_on_not_flagged(self) -> None:
        # Real discourse digest: a trailing "on" is a boolean literal here,
        # not the join keyword "ON".
        query = "SET standard_conforming_strings = on"
        assert normalize_digest_text(query, "postgresql").truncated is False

    def test_limit_all_not_flagged(self) -> None:
        query = "SELECT * FROM t LIMIT ALL"
        assert normalize_digest_text(query, "postgresql").truncated is False

    def test_show_all_not_flagged(self) -> None:
        query = "SHOW ALL"
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_for_update_not_flagged(self) -> None:
        query = "SELECT `id` FROM `t` WHERE `id` = ? FOR UPDATE"
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_returning_star_not_flagged(self) -> None:
        query = 'INSERT INTO "t" ("a") VALUES ($1) RETURNING *'
        assert normalize_digest_text(query, "postgresql").truncated is False

    def test_bare_update_with_no_for_is_still_flagged(self) -> None:
        # Unlike "... FOR UPDATE", a bare "UPDATE" with nothing before it is
        # a statement cut off right after its own keyword.
        assert normalize_digest_text("UPDATE", "mysql").truncated is True
        assert normalize_digest_text("SELECT 1; UPDATE", "mysql").truncated is True


class TestTruncationSyntheticSignals:
    def test_unterminated_string_literal(self) -> None:
        query = "SELECT * FROM t WHERE name = 'abc"
        assert normalize_digest_text(query, "mysql").truncated is True

    def test_unterminated_quoted_identifier(self) -> None:
        query = "SELECT * FROM `t WHERE x = 1"
        assert normalize_digest_text(query, "mysql").truncated is True


# ---------------------------------------------------------------------------
# Comments: quotes inside them are ignored, and so is a comment's own
# closing token when checking for a dangling trailing character.
# ---------------------------------------------------------------------------


class TestComments:
    def test_block_comment_quote_ignored(self) -> None:
        query = "SELECT * FROM t /* it's ? */ WHERE a = ?"
        result = normalize_digest_text(query, "mysql")
        assert result.substitutions == ("bare_placeholder",)
        assert result.text == "SELECT * FROM t /* it's ? */ WHERE a = NULL"
        assert result.truncated is False

    def test_line_comment_apostrophe_not_truncated(self) -> None:
        query = "SELECT 1 WHERE a = ? -- don't look back"
        result = normalize_digest_text(query, "mysql")
        assert result.truncated is False
        assert result.text.startswith("SELECT 1 WHERE a = NULL")

    def test_trailing_block_comment_close_not_dangling(self) -> None:
        # Previously, the trailing "/" of a comment's own "*/" could be
        # mistaken for a dangling binary operator.
        query = "SELECT 1 /* trailing comment */"
        assert normalize_digest_text(query, "mysql").truncated is False

    def test_unterminated_block_comment_is_truncated(self) -> None:
        query = "SELECT 1 FROM t /* this never closes"
        assert normalize_digest_text(query, "mysql").truncated is True

    def test_mysql_hash_comment_hides_placeholder(self) -> None:
        query = "SELECT 1 # a comment with a ? mark"
        result = normalize_digest_text(query, "mysql")
        assert result.substitutions == ()
        assert result.truncated is False

    def test_hash_comment_only_for_mysql_family(self) -> None:
        # Under MySQL, "#" starts a line comment that runs to the end of
        # the text (complete, nothing required to close it). Under
        # PostgreSQL, "#" has no special meaning, so the apostrophe right
        # after it opens a real, unterminated string literal.
        query = "SELECT 1 # unterminated 'string"
        assert normalize_digest_text(query, "mysql").truncated is False
        assert normalize_digest_text(query, "postgresql").truncated is True


# ---------------------------------------------------------------------------
# Backslash escaping is a MySQL-family convention, not a universal one.
# ---------------------------------------------------------------------------


class TestBackslashEscaping:
    def test_mysql_backslash_escapes_the_quote(self) -> None:
        query = "SELECT * FROM t WHERE a = 'it\\'s' AND b = ?"
        result = normalize_digest_text(query, "mysql")
        assert result.truncated is False
        assert result.substitutions == ("bare_placeholder",)

    def test_postgresql_backslash_is_not_an_escape(self) -> None:
        # With standard_conforming_strings on (PostgreSQL's default), a
        # backslash has no special meaning, so this literal ends at the
        # very next quote -- it must not swallow the rest of the text
        # looking for one that was never special in the first place.
        query = "SELECT * FROM t WHERE a = 'C:\\' AND b = $1"
        result = normalize_digest_text(query, "postgresql")
        assert result.truncated is False
        assert result.text == query


# ---------------------------------------------------------------------------
# PostgreSQL dollar-quoted strings ($$...$$ / $tag$...$tag$).
# ---------------------------------------------------------------------------


class TestDollarQuoting:
    def test_bare_dollar_quote_balances(self) -> None:
        query = "SELECT $$it's a $1 test$$ AS note WHERE a = $2"
        result = normalize_digest_text(query, "postgresql")
        assert result.truncated is False
        assert result.text == query

    def test_tagged_dollar_quote_balances(self) -> None:
        query = "SELECT $tag$it's fine$tag$ FROM t WHERE a = $1"
        result = normalize_digest_text(query, "postgresql")
        assert result.truncated is False

    def test_unterminated_dollar_quote_is_truncated(self) -> None:
        query = "SELECT $$it's never closed"
        assert normalize_digest_text(query, "postgresql").truncated is True

    def test_dollar_quote_gated_to_non_mysql_dialects(self) -> None:
        # "$$ ... $$" means nothing special in MySQL, so the "(" inside it
        # is a real, unmatched parenthesis there -- but under PostgreSQL
        # it's inside a dollar-quoted string and must not count.
        query = "SELECT 1 FROM t WHERE a = $$ unmatched ( paren $$ AND b = ?"
        assert normalize_digest_text(query, "mysql").truncated is True
        assert normalize_digest_text(query, "postgresql").truncated is False


# ---------------------------------------------------------------------------
# Real PostgreSQL query text (docs/examples/discourse) -- $N placeholders
# ---------------------------------------------------------------------------


class TestPostgresqlPassthrough:
    def test_dollar_placeholders_left_alone(self) -> None:
        query = 'SELECT "users".* FROM "users" WHERE "users"."username_lower" = $1 LIMIT $2'
        result = normalize_digest_text(query, "postgresql")
        assert result == NormalizedQuery(text=query, truncated=False, substitutions=())

    def test_insert_with_many_placeholders(self) -> None:
        query = (
            'INSERT INTO "scheduler_stats" ("name", "hostname", "pid", "duration_ms", '
            '"live_slots_start", "live_slots_finish", "started_at", "success", "error") '
            'VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING "id"'
        )
        result = normalize_digest_text(query, "postgresql")
        assert result.text == query
        assert result.substitutions == ()
        assert result.truncated is False

    def test_jsonb_question_mark_operator_left_alone(self) -> None:
        # PostgreSQL's jsonb "?" operator is real syntax, not a placeholder --
        # normalization must be a no-op for this dialect.
        query = "SELECT * FROM t WHERE data ? 'key'"
        result = normalize_digest_text(query, "postgresql")
        assert result.text == query
        assert result.substitutions == ()

    def test_other_dialects_pass_through_unchanged(self) -> None:
        for dialect in ("postgresql", "sqlserver", "oracle", "db2"):
            query = "SELECT * FROM t WHERE x = ? AND y IN (...)"
            result = normalize_digest_text(query, dialect)
            assert result.text == query
            assert result.substitutions == ()

    def test_mariadb_is_treated_as_mysql_family(self) -> None:
        result = normalize_digest_text("SELECT * FROM t WHERE x = ?", "mariadb")
        assert result.text == "SELECT * FROM t WHERE x = NULL"
        assert result.substitutions == ("bare_placeholder",)


class TestNestedBlockComments:
    """#825: the quote/comment scan nests block comments for PostgreSQL and SQL
    Server, and ends one at its first ``*/`` for the MySQL family and Oracle."""

    NESTED_OPEN = "SELECT 1 /* a /* b */"

    @pytest.mark.parametrize("dialect", ["postgresql", "sqlserver"])
    def test_an_inner_opener_keeps_the_comment_open(self, dialect):
        assert normalize_digest_text(self.NESTED_OPEN, dialect).truncated is True

    @pytest.mark.parametrize("dialect", ["mysql", "mariadb", "oracle", "db2"])
    def test_the_first_close_ends_it(self, dialect):
        assert normalize_digest_text(self.NESTED_OPEN, dialect).truncated is False

    @pytest.mark.parametrize("dialect", ["postgresql", "sqlserver"])
    def test_a_balanced_nested_comment_is_one_comment(self, dialect):
        text = "SELECT ( 1 /* a /* ) */ ) */ )"
        assert normalize_digest_text(text, dialect).truncated is False

    def test_the_mask_covers_the_whole_nested_comment(self):
        from src.tools.sql.normalize import quoted_mask

        text = "a /* b /* c */ d */ e"
        nested = quoted_mask(text, "postgresql")
        flat = quoted_mask(text, "mysql")
        assert nested[2:19] == [True] * 17
        assert nested[19:] == [False, False]
        assert flat[2:14] == [True] * 12
        assert flat[14:] == [False] * 7
