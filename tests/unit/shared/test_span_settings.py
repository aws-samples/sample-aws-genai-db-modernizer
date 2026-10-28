"""The span grammar, the digest scan and redaction read the source's string settings (#1482).

MySQL and MariaDB read ``--`` as a comment only before a space or a control character,
whatever their settings. ``NO_BACKSLASH_ESCAPES`` (MySQL, MariaDB) gives a ``\\`` no
meaning in a string, and PostgreSQL's ``standard_conforming_strings = off`` makes it an
escape in a plain ``'...'`` string. The settings come from the collection
(``metadata.source_database.settings``, or a collection script's ``metadata.settings``);
without them every engine reads as it did.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from src.shared.sql_spans import (
    DEFAULT_SPAN_SETTINGS,
    SpanSettings,
    active_span_settings,
    code_text,
    comments_literals_and_identifiers_for,
    keyword_text,
    literals_and_comments_for,
    scan_grammar_for,
    span_settings,
    span_settings_of,
    spans,
    use_span_settings,
    with_span_settings,
)
from src.tools.sql.normalize import (
    literal_shape,
    normalize_digest_text,
    quoted_mask,
    without_comments_between_statements,
)
from src.tools.sql.query_facts import query_facts, remember_collected_facts
from src.tools.sql.redact import (
    redact_collector_output,
    redact_definition,
    redact_literals,
    redact_raw_collection,
    redact_values,
)
from tests.hypothesis_examples import scaled

NO_BACKSLASH_ESCAPES = SpanSettings(no_backslash_escapes=True)
BACKSLASH_STRINGS = SpanSettings(standard_conforming_strings=False)

# A MySQL text whose first string ends in a backslash: with NO_BACKSLASH_ESCAPES it ends
# there, and ``AND deleted_at IS NULL`` is SQL; by default the ``\'`` runs on into it.
MYSQL_TEXT = "SELECT id FROM t WHERE path = 'C:\\' AND deleted_at IS NULL AND name = 'Jane Doe'"
# A PostgreSQL text with an escaped quote in a plain string: with
# ``standard_conforming_strings = off`` the string is ``'it\'s'``; by default it ends at
# the backslash's quote.
POSTGRES_TEXT = (
    "SELECT id FROM t WHERE note = 'it\\'s' AND deleted_at IS NULL AND name = 'Jane Doe'"
)


def _strings(text: str, engine: str, settings: SpanSettings | None) -> list[str]:
    grammar = literals_and_comments_for(engine, settings)
    return [text[s:e] for s, e, o in spans(text, grammar) if o not in grammar.identifiers]


# -- the grammar -----------------------------------------------------------------------


class TestMySqlDashComment:
    @pytest.mark.parametrize("engine", ["mysql", "mariadb"])
    def test_a_dash_pair_before_a_non_space_is_two_minus_signs(self, engine):
        text = "SELECT 5--1 FROM t WHERE a = 1"
        assert code_text(text, engine) == text

    @pytest.mark.parametrize("engine", ["mysql", "mariadb"])
    @pytest.mark.parametrize("after", [" note", "\tnote", "\nnote", "\x01note", "\x7fnote", ""])
    def test_a_dash_pair_before_a_space_a_control_character_or_the_end_is_a_comment(
        self, engine, after
    ):
        text = f"SELECT 5 --{after}"
        assert "note" not in code_text(text, engine).split("\n")[0]
        assert code_text(text, engine).startswith("SELECT 5 ")

    @pytest.mark.parametrize("engine", ["postgresql", "sqlserver", "oracle", "db2", ""])
    def test_every_other_dialect_still_reads_it_as_a_comment(self, engine):
        assert code_text("SELECT 5--1 FROM t", engine).strip() == "SELECT 5"

    def test_no_setting_changes_it(self):
        for settings in (DEFAULT_SPAN_SETTINGS, NO_BACKSLASH_ESCAPES, BACKSLASH_STRINGS):
            assert code_text("SELECT 5--1", "mysql", settings=settings) == "SELECT 5--1"

    def test_a_string_after_the_dashes_is_a_string(self):
        # ``1--'x'`` is ``1 - -'x'``: the quote opens a string, not a comment.
        assert code_text("SELECT 1--'x' FROM t", "mysql") == "SELECT 1--? FROM t"


class TestMySqlNoBackslashEscapes:
    @pytest.mark.parametrize("engine", ["mysql", "mariadb"])
    def test_a_backslash_ends_nothing_with_the_mode_set(self, engine):
        assert _strings(MYSQL_TEXT, engine, NO_BACKSLASH_ESCAPES) == ["'C:\\'", "'Jane Doe'"]
        code = code_text(MYSQL_TEXT, engine, settings=NO_BACKSLASH_ESCAPES)
        assert "deleted_at IS NULL" in code

    @pytest.mark.parametrize("engine", ["mysql", "mariadb"])
    def test_by_default_a_backslash_escapes_as_before(self, engine):
        assert _strings(MYSQL_TEXT, engine, None)[0] == "'C:\\' AND deleted_at IS NULL AND name = '"
        assert "deleted_at" not in code_text(MYSQL_TEXT, engine)

    def test_a_double_quoted_string_is_read_the_same_way(self):
        text = 'SELECT "a\\" FROM t WHERE b = "c"'
        assert _strings(text, "mysql", NO_BACKSLASH_ESCAPES) == ['"a\\"', '"c"']

    def test_a_doubled_quote_still_escapes(self):
        assert _strings("SELECT 'it''s\\'", "mysql", NO_BACKSLASH_ESCAPES) == ["'it''s\\'"]

    @pytest.mark.parametrize("engine", ["postgresql", "sqlserver", "oracle", "db2", ""])
    def test_another_dialect_ignores_it(self, engine):
        text = "SELECT 'a\\' , b, 'c'"
        assert _strings(text, engine, NO_BACKSLASH_ESCAPES) == _strings(text, engine, None)


class TestPostgresStandardConformingStrings:
    @pytest.mark.parametrize("engine", ["postgresql", "postgres"])
    def test_off_reads_a_backslash_as_an_escape_in_a_plain_string(self, engine):
        assert _strings(POSTGRES_TEXT, engine, BACKSLASH_STRINGS) == ["'it\\'s'", "'Jane Doe'"]
        assert "deleted_at IS NULL" in code_text(POSTGRES_TEXT, engine, settings=BACKSLASH_STRINGS)

    def test_on_the_default_a_backslash_is_an_ordinary_character_as_before(self):
        assert _strings(POSTGRES_TEXT, "postgresql", None)[0] == "'it\\'"
        on = SpanSettings(standard_conforming_strings=True)
        assert code_text(POSTGRES_TEXT, "postgresql", settings=on) == code_text(
            POSTGRES_TEXT, "postgresql"
        )

    def test_off_leaves_a_quoted_name_alone(self):
        text = 'SELECT "a\\" FROM t WHERE b = 1'
        assert code_text(text, "postgresql", settings=BACKSLASH_STRINGS) == text

    def test_an_escape_string_escapes_either_way(self):
        text = "SELECT E'it\\'s' FROM t"
        for settings in (None, BACKSLASH_STRINGS):
            assert code_text(text, "postgresql", settings=settings) == "SELECT ? FROM t"

    def test_a_dollar_quote_is_unchanged(self):
        text = "SELECT $$a\\$$, b"
        assert code_text(text, "postgresql", settings=BACKSLASH_STRINGS) == "SELECT ?, b"

    @pytest.mark.parametrize("engine", ["mysql", "sqlserver", "oracle", "db2", ""])
    def test_another_dialect_ignores_it(self, engine):
        text = "SELECT 'a\\' , b, 'c'"
        assert _strings(text, engine, BACKSLASH_STRINGS) == _strings(text, engine, None)


def test_every_grammar_reads_the_settings():
    text = "SELECT 'C:\\', \"x\", 'y'"
    for grammar in (
        literals_and_comments_for("mysql", NO_BACKSLASH_ESCAPES),
        comments_literals_and_identifiers_for("mysql", NO_BACKSLASH_ESCAPES),
        scan_grammar_for("mysql", settings=NO_BACKSLASH_ESCAPES),
    ):
        assert [text[s:e] for s, e, _ in spans(text, grammar)][0] == "'C:\\'"
    assert keyword_text(MYSQL_TEXT, "mysql", settings=NO_BACKSLASH_ESCAPES).count("?") == 2


def test_the_defaults_read_every_dialect_as_before():
    for engine in ("mysql", "mariadb", "postgresql", "sqlserver", "oracle", "db2", ""):
        for text in (MYSQL_TEXT, POSTGRES_TEXT):
            assert code_text(text, engine, settings=DEFAULT_SPAN_SETTINGS) == code_text(
                text, engine
            )
            assert literals_and_comments_for(engine, DEFAULT_SPAN_SETTINGS) is (
                literals_and_comments_for(engine)
            )


# -- where the settings come from ------------------------------------------------------


class TestSettingsFromTheCollection:
    @pytest.mark.parametrize(
        ("engine", "reported", "expected"),
        [
            ("mysql", {"sql_mode": "STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES"}, True),
            ("mariadb", {"sql_mode": "no_backslash_escapes"}, True),
            ("mysql", {"sql_mode": "STRICT_TRANS_TABLES,ONLY_FULL_GROUP_BY"}, False),
            ("mysql", {"sql_mode": ""}, False),
            ("mysql", {}, False),
            ("mysql", None, False),
        ],
    )
    def test_mysql_reads_its_sql_mode(self, engine, reported, expected):
        assert span_settings(engine, reported).no_backslash_escapes is expected

    @pytest.mark.parametrize(
        ("reported", "expected"),
        [("off", False), ("OFF", False), (False, False), ("on", True), (None, True), ("?", True)],
    )
    def test_postgresql_reads_standard_conforming_strings(self, reported, expected):
        settings = span_settings("postgresql", {"standard_conforming_strings": reported})
        assert settings.standard_conforming_strings is expected

    def test_another_engines_setting_is_not_read(self):
        assert span_settings("postgresql", {"sql_mode": "NO_BACKSLASH_ESCAPES"}) == (
            DEFAULT_SPAN_SETTINGS
        )
        assert span_settings("mysql", {"standard_conforming_strings": "off"}) == (
            DEFAULT_SPAN_SETTINGS
        )
        assert span_settings("sqlserver", {"sql_mode": "NO_BACKSLASH_ESCAPES"}) == (
            DEFAULT_SPAN_SETTINGS
        )

    def test_a_collector_output_reports_them_on_its_source_database(self):
        output = _collector_output("mysql", {"sql_mode": "NO_BACKSLASH_ESCAPES"})
        assert span_settings_of(output) == NO_BACKSLASH_ESCAPES

    def test_an_older_collection_without_them_reads_the_defaults(self):
        assert span_settings_of(_collector_output("mysql", None)) == DEFAULT_SPAN_SETTINGS
        assert span_settings_of({}) == DEFAULT_SPAN_SETTINGS
        assert span_settings_of(None) == DEFAULT_SPAN_SETTINGS


class TestActiveSettings:
    def test_loading_a_collection_makes_its_settings_active(self):
        remember_collected_facts(_collector_output("mysql", {"sql_mode": "NO_BACKSLASH_ESCAPES"}))
        assert active_span_settings() == NO_BACKSLASH_ESCAPES
        assert "deleted_at IS NULL" in code_text(MYSQL_TEXT, "mysql")
        assert query_facts(MYSQL_TEXT, "mysql").is_null
        # The next collection loaded, without settings, reads the defaults again.
        remember_collected_facts(_collector_output("mysql", None))
        assert active_span_settings() == DEFAULT_SPAN_SETTINGS
        assert not query_facts(MYSQL_TEXT, "mysql").is_null

    def test_a_block_restores_the_settings_it_found(self):
        with with_span_settings(BACKSLASH_STRINGS):
            assert active_span_settings() == BACKSLASH_STRINGS
            with with_span_settings(NO_BACKSLASH_ESCAPES):
                assert active_span_settings() == NO_BACKSLASH_ESCAPES
            assert active_span_settings() == BACKSLASH_STRINGS
        assert active_span_settings() == DEFAULT_SPAN_SETTINGS

    def test_query_facts_keep_the_settings_they_were_made_with(self):
        facts = query_facts(POSTGRES_TEXT, "postgresql", BACKSLASH_STRINGS)
        use_span_settings(None)
        # Read later, under other settings: still the source's reading.
        assert facts.is_null
        assert not query_facts(POSTGRES_TEXT, "postgresql").is_null
        assert query_facts(POSTGRES_TEXT, "postgresql", BACKSLASH_STRINGS) is facts


# -- the digest scan -------------------------------------------------------------------


class TestDigestScan:
    def test_mysql_dashes_mask_nothing(self):
        assert not any(quoted_mask("SELECT 5--1 FROM t", "mysql"))
        assert all(quoted_mask("SELECT 5--1 FROM t", "postgresql")[len("SELECT 5") :])

    def test_no_backslash_escapes_ends_the_string_at_its_quote(self):
        text = MYSQL_TEXT
        on = quoted_mask(text, "mysql", settings=NO_BACKSLASH_ESCAPES)
        off = quoted_mask(text, "mysql")
        at = text.index("deleted_at")
        assert not on[at]
        assert off[at]

    def test_standard_conforming_strings_off_escapes_in_a_plain_string(self):
        text = POSTGRES_TEXT
        at = text.index("deleted_at")
        assert not quoted_mask(text, "postgresql", settings=BACKSLASH_STRINGS)[at]
        assert quoted_mask(text, "postgresql")[at]

    def test_truncation_reads_the_settings(self):
        # A string that ends in a backslash closes with the mode set: nothing is cut.
        text = "SELECT * FROM t WHERE a = 'C:\\'"
        assert not normalize_digest_text(text, "mysql", NO_BACKSLASH_ESCAPES).truncated
        assert normalize_digest_text(text, "mysql").truncated
        text = "SELECT * FROM t WHERE a = 'it\\'s'"
        assert not normalize_digest_text(text, "postgresql", BACKSLASH_STRINGS).truncated
        assert normalize_digest_text(text, "postgresql").truncated

    def test_literal_shape_reads_the_settings(self):
        a = "SELECT * FROM t WHERE a = 'C:\\' AND b = 'x'"
        b = "SELECT * FROM t WHERE a = 'D:\\' AND b = 'y'"
        assert literal_shape(a, "mysql", NO_BACKSLASH_ESCAPES) == (
            "SELECT * FROM T WHERE A = ? AND B = ?"
        )
        assert literal_shape(a, "mysql", NO_BACKSLASH_ESCAPES) == literal_shape(
            b, "mysql", NO_BACKSLASH_ESCAPES
        )
        assert literal_shape(a, "mysql") != literal_shape(a, "mysql", NO_BACKSLASH_ESCAPES)

    def test_comments_between_statements_read_the_settings(self):
        text = "SELECT 'C:\\'; /* x */ SELECT 2"
        assert without_comments_between_statements(text, "mysql", NO_BACKSLASH_ESCAPES) == (
            "SELECT 'C:\\';  SELECT 2"
        )
        assert without_comments_between_statements(text, "mysql") == text

    def test_the_defaults_scan_as_before(self):
        for engine in ("mysql", "postgresql", "sqlserver", "oracle", ""):
            for text in (MYSQL_TEXT, POSTGRES_TEXT):
                assert quoted_mask(text, engine, settings=DEFAULT_SPAN_SETTINGS) == (
                    quoted_mask(text, engine)
                )


# -- redaction -------------------------------------------------------------------------


class TestRedaction:
    def test_no_backslash_escapes_redacts_the_value_the_default_reading_left_in_the_code(self):
        # Fails closed: either reported setting masks what either reading hides.
        assert "Jane" not in redact_literals(MYSQL_TEXT, "mysql")
        redacted = redact_literals(MYSQL_TEXT, "mysql", NO_BACKSLASH_ESCAPES)
        assert "Jane" not in redacted
        assert redacted.startswith("SELECT id FROM t WHERE path = ?")

    def test_standard_conforming_strings_off_redacts_the_whole_string(self):
        assert "Jane" not in redact_literals(POSTGRES_TEXT, "postgresql")
        redacted = redact_literals(POSTGRES_TEXT, "postgresql", BACKSLASH_STRINGS)
        assert "Jane" not in redacted
        assert redacted.startswith("SELECT id FROM t WHERE note = $1")

    def test_a_mysql_dash_pair_hides_no_value_in_a_comment(self):
        text = "SELECT 1--'Jane\nDoe' FROM t"
        assert redact_literals(text, "mysql") == "SELECT ?--? FROM t"
        assert redact_literals(text, "mariadb") == "SELECT ?--? FROM t"

    def test_a_definition_masks_both_readings(self):
        text = "CREATE VIEW v AS SELECT * FROM t WHERE p = 'C:\\' AND n = 'Jane'"
        for settings in (None, NO_BACKSLASH_ESCAPES):
            redacted = redact_definition(text, "mysql", settings)
            assert "Jane" not in redacted
            assert redacted.startswith("CREATE VIEW v AS SELECT * FROM t WHERE p = '?'")

    def test_redaction_is_idempotent_with_the_settings(self):
        for text, engine, settings in (
            (MYSQL_TEXT, "mysql", NO_BACKSLASH_ESCAPES),
            (POSTGRES_TEXT, "postgresql", BACKSLASH_STRINGS),
            ("SELECT * FROM t WHERE p LIKE 'C:\\%' AND n = 'x'", "mysql", NO_BACKSLASH_ESCAPES),
        ):
            once = redact_literals(text, engine, settings)
            assert redact_literals(once, engine, settings) == once

    def test_the_cache_keeps_each_settings_reading_apart(self):
        from src.tools.sql.redact import _CACHE

        redact_literals(MYSQL_TEXT, "mysql")
        redact_literals(MYSQL_TEXT, "mysql", NO_BACKSLASH_ESCAPES)
        for settings in (DEFAULT_SPAN_SETTINGS, NO_BACKSLASH_ESCAPES):
            assert _CACHE.get((MYSQL_TEXT, "mysql", "query", settings)) is not None

    def test_a_collector_output_is_redacted_with_its_own_settings(self):
        output = _collector_output("mysql", {"sql_mode": "NO_BACKSLASH_ESCAPES"}, MYSQL_TEXT)
        text = redact_collector_output(output)["queries"]["query_patterns"][0]["query_text"]
        assert "Jane" not in text
        assert text == redact_literals(MYSQL_TEXT, "mysql", NO_BACKSLASH_ESCAPES)
        assert redact_values(output, "mysql")["queries"]["query_patterns"][0]["query_text"] == text

    def test_an_older_collector_output_is_redacted_as_before(self):
        output = _collector_output("mysql", None, MYSQL_TEXT)
        text = redact_collector_output(output)["queries"]["query_patterns"][0]["query_text"]
        assert text == redact_literals(MYSQL_TEXT, "mysql", DEFAULT_SPAN_SETTINGS)

    def test_a_raw_collection_is_redacted_with_the_settings_its_metadata_reports(self):
        raw = {
            "metadata": {
                "version": "PostgreSQL 16.4 on x86_64-pc-linux-gnu",
                "settings": {"standard_conforming_strings": "off"},
            },
            "queries": [{"query_text": POSTGRES_TEXT, "digest": "d1"}],
        }
        text = redact_raw_collection(raw)["queries"][0]["query_text"]
        assert "Jane" not in text
        assert text == redact_literals(POSTGRES_TEXT, "postgresql", BACKSLASH_STRINGS)
        older = {**raw, "metadata": {"version": "PostgreSQL 16.4 on x86_64-pc-linux-gnu"}}
        assert redact_raw_collection(older)["queries"][0]["query_text"] == redact_literals(
            POSTGRES_TEXT, "postgresql", DEFAULT_SPAN_SETTINGS
        )

    def test_the_active_settings_are_the_default(self):
        with with_span_settings(NO_BACKSLASH_ESCAPES):
            assert redact_literals(MYSQL_TEXT, "mysql") == redact_literals(
                MYSQL_TEXT, "mysql", NO_BACKSLASH_ESCAPES
            )


# -- a collection read end to end ------------------------------------------------------


class TestOfflineCollection:
    def _raw(self, settings: dict | None) -> dict:
        metadata: dict = {"version": "8.0.36", "database_name": "app"}
        if settings is not None:
            metadata["settings"] = settings
        return {
            "metadata": metadata,
            "tables": [{"table_name": "t"}],
            "queries": [{"query_text": MYSQL_TEXT, "digest": "d1", "exec_count": 5}],
        }

    def test_a_collection_is_parsed_with_the_settings_it_reports(self):
        from src.tools.database.offline_parser import parse_offline_collection

        parsed = parse_offline_collection(self._raw({"sql_mode": "NO_BACKSLASH_ESCAPES"}))
        query = parsed["queries"][0]
        assert query["query_text"] == redact_literals(MYSQL_TEXT, "mysql", NO_BACKSLASH_ESCAPES)
        assert "Jane" not in query["query_text"]
        assert query["tables_accessed"] == ["app.t"]
        # The parse leaves no settings active behind it.
        assert active_span_settings() == DEFAULT_SPAN_SETTINGS

    def test_an_older_collection_is_parsed_as_before(self):
        from src.tools.database.offline_parser import parse_offline_collection

        parsed = parse_offline_collection(self._raw(None))
        assert parsed["queries"][0]["query_text"] == redact_literals(
            MYSQL_TEXT, "mysql", DEFAULT_SPAN_SETTINGS
        )


def _collector_output(engine: str, settings: dict | None, query_text: str = "SELECT 1") -> dict:
    source: dict = {"engine": engine, "version": "8.0.36", "hostname": "db"}
    if settings is not None:
        source["settings"] = settings
    return {
        "metadata": {"source_database": source},
        "queries": {"query_patterns": [{"query_id": "q1", "query_text": query_text}]},
    }


# -- the other scanners read the same rules -------------------------------------------


class TestSqlComments:
    def test_mysql_dashes_are_read_the_mysql_way_by_every_scan(self):
        from src.shared.sql_comments import (
            strip_comments,
            strip_trailing_comments,
            trailing_comments,
        )

        assert strip_comments("SELECT 5--1", mysql_dashes=True) == "SELECT 5--1"
        assert strip_comments("SELECT 5--1") == "SELECT 5 "
        assert strip_trailing_comments("SELECT 5--1", mysql_dashes=True) == "SELECT 5--1"
        assert strip_trailing_comments("SELECT 5 -- x", mysql_dashes=True) == "SELECT 5"
        assert trailing_comments("SELECT 5--1\n", hash_comments=True, mysql_dashes=True) == []
        assert trailing_comments("SELECT 5 -- t\n", hash_comments=True, mysql_dashes=True) == [" t"]

    def test_a_backslash_escapes_only_where_the_settings_say(self):
        from src.shared.sql_comments import strip_comments, trailing_comments

        text = "SELECT 'a\\' -- x' -- y"
        assert strip_comments(text, backslash=True) == "SELECT 'a\\' -- x'  "
        assert strip_comments(text) == "SELECT 'a\\'  "
        tail = "SELECT 'a\\' /* x */ 'b' /* y */"
        assert trailing_comments(tail, backslash_quotes="") == [" y "]
        # With a backslash escape, ``'a\' /* x */ '`` is one string and ``b`` code.
        assert trailing_comments(tail, backslash_quotes="'") == []

    def test_the_callers_read_the_source_settings(self):
        from src.shared.sql_spans import backslash_quotes

        assert backslash_quotes("mysql") == "\"'"
        assert backslash_quotes("mysql", NO_BACKSLASH_ESCAPES) == ""
        assert backslash_quotes("postgresql") == ""
        assert backslash_quotes("postgresql", BACKSLASH_STRINGS) == "'"
        assert backslash_quotes("sqlserver", BACKSLASH_STRINGS) == ""
        assert backslash_quotes("") == ""

    def test_a_mysql_query_type_reads_its_dashes(self):
        from src.tools.sql.query_type import statement_query_type

        assert statement_query_type("-- note\nDELETE FROM t", "mysql") == "DELETE"
        # ``--x`` is no MySQL comment: the statement opens with an operator.
        assert statement_query_type("--x\nDELETE FROM t", "mysql") == "OTHER"
        assert statement_query_type("--x\nDELETE FROM t", "postgresql") == "DELETE"


class TestRegexScanners:
    def test_cte_references_read_mysql_dashes(self):
        from src.tools.database.sql_cte import drop_cte_references

        text = "WITH c AS (SELECT 1) SELECT * FROM t WHERE a = 5--1 OR b IN (SELECT 1 FROM c)"
        assert "FROM c" not in drop_cte_references(text, "mysql")
        assert drop_cte_references(text, "postgresql") == text

    def test_cte_references_read_the_string_settings(self):
        from src.tools.database.sql_cte import drop_cte_references

        text = (
            "WITH c AS (SELECT 1) SELECT * FROM t WHERE a = 'C:\\' OR b IN (SELECT 1 FROM c)"
            " OR d = 'x'"
        )
        assert drop_cte_references(text, "mysql") == text  # by default, ``FROM c`` is in a string
        with with_span_settings(NO_BACKSLASH_ESCAPES):
            assert "FROM c" not in drop_cte_references(text, "mysql")

    def test_a_retention_delete_reads_the_string_settings(self):
        from src.agents.referee.carried_writes import _older_than

        text = "DELETE FROM t WHERE note = 'it\\'s' AND created_at < now() AND x = 'y'"
        assert not _older_than(text, "created_at", {"t"}, "postgresql")
        with with_span_settings(BACKSLASH_STRINGS):
            assert _older_than(text, "created_at", {"t"}, "postgresql")

    def test_the_shadow_reads_comments_in_the_source_dialect(self):
        from src.tools.sql.shadow import _categories

        text = "SELECT a FROM t WHERE b = 5--1 OR c IN (SELECT 1 FROM u)"
        # MySQL: no comment, so ``FROM u`` is a keyword reference in the code too.
        assert "comment_text" not in _categories(text, ["db.u"], [], "mysql")
        assert _categories(text, ["db.u"], [], "postgresql") == ["comment_text"]


# -- sqlglot reads the source's strings ------------------------------------------------


class TestFacade:
    def test_a_no_backslash_escapes_string_is_rewritten_for_sqlglot(self):
        from src.tools.sql.facade import _strings_as_sqlglot_reads_them

        assert _strings_as_sqlglot_reads_them(MYSQL_TEXT, "mysql", NO_BACKSLASH_ESCAPES) == (
            MYSQL_TEXT.replace("'C:\\'", "'C:\\\\'")
        )
        assert _strings_as_sqlglot_reads_them(MYSQL_TEXT, "mysql") == MYSQL_TEXT

    def test_a_backslash_string_is_written_in_standard_form_for_sqlglot(self):
        from src.tools.sql.facade import _strings_as_sqlglot_reads_them

        rewritten = _strings_as_sqlglot_reads_them(POSTGRES_TEXT, "postgresql", BACKSLASH_STRINGS)
        assert rewritten == POSTGRES_TEXT.replace("'it\\'s'", "'it''s'")
        assert _strings_as_sqlglot_reads_them(POSTGRES_TEXT, "postgresql") == POSTGRES_TEXT

    @pytest.mark.parametrize("engine", ["sqlserver", "oracle", "db2", ""])
    def test_other_sources_are_never_rewritten(self, engine):
        from src.tools.sql.facade import _strings_as_sqlglot_reads_them

        for settings in (NO_BACKSLASH_ESCAPES, BACKSLASH_STRINGS):
            assert _strings_as_sqlglot_reads_them(MYSQL_TEXT, engine, settings) == MYSQL_TEXT

    @pytest.mark.parametrize(
        ("text", "engine", "settings"),
        [
            (
                "SELECT id FROM t WHERE p = 'C:\\' AND n LIKE '%x' AND id IN (SELECT id FROM u)",
                "mysql",
                NO_BACKSLASH_ESCAPES,
            ),
            (
                "SELECT id FROM t WHERE p = 'it\\'s' AND n LIKE '%x' AND id IN (SELECT id FROM u)",
                "postgresql",
                BACKSLASH_STRINGS,
            ),
        ],
    )
    def test_sqlglot_parses_the_statement_the_source_runs(self, text, engine, settings):
        from src.tools.sql.facade import read_sql

        default = read_sql(text, engine)
        assert default.facts.parsed_by == "regex"  # the default reading cannot parse it
        with with_span_settings(settings):
            reading = read_sql(text, engine)
        assert reading.facts.parsed_by == "sqlglot"
        assert reading.facts.wildcard_leading
        assert reading.facts.has_subquery
        assert {ref.name for ref in reading.tables.tables} == {"t", "u"}


# -- concurrent jobs -------------------------------------------------------------------


def _job(settings: dict | None, barrier, results: dict, key: str) -> None:
    remember_collected_facts(_collector_output("mysql", settings, MYSQL_TEXT))
    barrier.wait(timeout=10)  # both jobs have loaded their collections
    results[key] = (
        "deleted_at IS NULL" in code_text(MYSQL_TEXT, "mysql"),
        active_span_settings().no_backslash_escapes,
        query_facts(MYSQL_TEXT, "mysql").is_null,
    )


class TestConcurrentJobs:
    def test_two_threads_read_their_own_collections_settings(self):
        import threading

        barrier = threading.Barrier(2)
        results: dict = {}
        threads = [
            threading.Thread(
                target=_job,
                args=({"sql_mode": "NO_BACKSLASH_ESCAPES"}, barrier, results, "strict"),
            ),
            threading.Thread(target=_job, args=(None, barrier, results, "default")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert results == {"strict": (True, True, True), "default": (False, False, False)}
        assert active_span_settings() == DEFAULT_SPAN_SETTINGS  # the caller's are untouched

    def test_two_tasks_read_their_own_collections_settings(self):
        import asyncio

        async def job(settings: dict | None, ready: asyncio.Barrier) -> tuple[bool, bool]:
            remember_collected_facts(_collector_output("mysql", settings, MYSQL_TEXT))
            await ready.wait()
            return (
                "deleted_at IS NULL" in code_text(MYSQL_TEXT, "mysql"),
                query_facts(MYSQL_TEXT, "mysql").is_null,
            )

        async def both() -> list[tuple[bool, bool]]:
            ready = asyncio.Barrier(2)
            return list(
                await asyncio.gather(
                    job({"sql_mode": "NO_BACKSLASH_ESCAPES"}, ready), job(None, ready)
                )
            )

        assert asyncio.run(both()) == [(True, True), (False, False)]
        assert active_span_settings() == DEFAULT_SPAN_SETTINGS

    def test_a_pool_worker_keeps_no_settings_from_an_earlier_job(self):
        from concurrent.futures import ThreadPoolExecutor

        from src.shared.progress import in_context

        def load_then_read() -> SpanSettings:
            remember_collected_facts(
                _collector_output("mysql", {"sql_mode": "NO_BACKSLASH_ESCAPES"})
            )
            return active_span_settings()

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(in_context(load_then_read)).result() == NO_BACKSLASH_ESCAPES
            # The same worker thread, the next job: it starts from the caller's settings.
            assert pool.submit(in_context(active_span_settings)).result() == (DEFAULT_SPAN_SETTINGS)

    def test_a_step_that_reads_the_collection_for_a_model_reads_its_settings(self, tmp_path):
        from src.storage.collector_for_models import read_collector_for_models
        from src.storage.local_store import LocalArtifactStore

        store = LocalArtifactStore(str(tmp_path))
        store.write_json(
            "db/job/collector/output.json",
            _collector_output("postgresql", {"standard_conforming_strings": "off"}),
        )
        read_collector_for_models(store, "db", "job")
        assert active_span_settings() == BACKSLASH_STRINGS


# -- redaction fails closed on the settings (#1482 review) -----------------------------

_MARKER = "Zqmarker"
_FUZZ_TOKENS = [
    "'",
    "\\",
    "\\'",
    "''",
    '"',
    " ",
    f" {_MARKER} ",
    "x",
    " = ",
    "--",
    "-- ",
    "#",
    "/*",
    "*/",
    "\n",
    "E'",
    "$$",
    "$a$",
    "q'[",
    "]'",
    "N'",
    "[",
    "]",
    ",",
    "(",
    ")",
    " WHERE ",
]
_READINGS = {
    "mysql": (DEFAULT_SPAN_SETTINGS, NO_BACKSLASH_ESCAPES),
    "postgresql": (DEFAULT_SPAN_SETTINGS, BACKSLASH_STRINGS),
    # No setting moves a string's end there: one reading, the cut forms still fuzzed.
    "oracle": (DEFAULT_SPAN_SETTINGS,),
    "sqlserver": (DEFAULT_SPAN_SETTINGS,),
}


def _markers_in_strings(text: str, engine: str, settings: SpanSettings) -> set[int]:
    """Where a reading puts the secret inside a string: a closed one, or the rest of
    the text after a quote it leaves open (a text cut there)."""
    grammar = literals_and_comments_for(engine, settings)
    inside: set[int] = set()
    code = list(text)
    for start, end, opener in spans(text, grammar):
        code[start:end] = " " * (end - start)
        if opener not in ("--", "#", "/*") and opener not in grammar.identifiers:
            inside.update(range(start, end))
    # What is left open runs to the end: a string when a quote (or a PostgreSQL dollar
    # quote) opens first, a comment (whose rest redaction reads as SQL) when a ``/*`` does.
    blanked = "".join(code)
    open_quotes = "'\"" if engine == "mysql" else "'"
    quote = min((i for i, ch in enumerate(blanked) if ch in open_quotes), default=-1)
    dollar = re.search(r"(?<![\w$])\$(?:[^\W\d]\w*)?\$", "".join(code))
    if engine == "postgresql" and dollar is not None and (quote < 0 or dollar.start() < quote):
        quote = dollar.start()
    comment = blanked.find("/*")
    if quote >= 0 and (comment < 0 or quote < comment):
        inside.update(range(quote, len(text)))
    at = text.find(_MARKER)
    found = set()
    while at >= 0:
        if at in inside:
            found.add(at)
        at = text.find(_MARKER, at + 1)
    return found


@pytest.mark.parametrize("engine", list(_READINGS))
@pytest.mark.parametrize("reported", [0, 1])
@hypothesis_settings(max_examples=scaled(1500), deadline=None)
@given(tokens=st.lists(st.sampled_from(_FUZZ_TOKENS), max_size=40))
def test_no_reading_of_the_setting_leaks_a_string(engine, reported, tokens):
    """Whatever the statement ran under, the reported setting or the other one, no value
    it holds in a string survives redaction, and redacting again changes nothing."""
    text = "SELECT a FROM t WHERE b = " + "".join(tokens)
    readings = _READINGS[engine]
    reported = min(reported, len(readings) - 1)
    total = text.count(_MARKER)
    in_a_string = set().union(*(_markers_in_strings(text, engine, r) for r in readings))
    allowed = total - len(in_a_string)
    for redact in (redact_literals, redact_definition):
        once = redact(text, engine, readings[reported])
        assert once.count(_MARKER) <= allowed, (text, once)
        assert redact(once, engine, readings[reported]) == once, (text, once)


@pytest.mark.parametrize(
    ("engine", "text"),
    [
        ("postgresql", "SELECT a FROM t WHERE c0 = $$x Zqmarker"),
        ("postgresql", "SELECT a FROM t WHERE c0 = $tag$x Zqmarker"),
        ("postgresql", "SELECT a FROM t WHERE c0 = E'x Zqmarker"),
        ("postgresql", "SELECT a FROM t WHERE c0 = 'x Zqmarker"),
        ("mysql", 'SELECT a FROM t WHERE c0 = "x Zqmarker'),
        ("mysql", "SELECT a FROM t WHERE c0 = _utf8mb4'x Zqmarker"),
        ("sqlserver", "SELECT a FROM t WHERE c0 = N'x Zqmarker"),
        ("oracle", "SELECT a FROM t WHERE c0 = q'[x Zqmarker"),
        ("oracle", "SELECT a FROM t WHERE c0 = nq'{x Zqmarker"),
    ],
)
def test_a_text_cut_inside_any_string_form_keeps_none_of_its_tail(engine, text):
    for redact in (redact_literals, redact_definition):
        once = redact(text, engine)
        assert _MARKER not in once, once
        assert redact(once, engine) == once


def test_the_reviewed_leak_is_masked_whatever_the_reported_setting():
    text = "UPDATE users SET note = 'it\\'s' WHERE email = 'jane@example.com'"
    for engine in ("mysql", "postgresql"):  # the engines a setting moves a string's end in
        for reported in _READINGS[engine]:
            for redact in (redact_literals, redact_definition):
                assert "jane" not in redact(text, engine, reported)


def test_where_both_readings_agree_redaction_is_unchanged():
    text = "SELECT * FROM t WHERE a = 'x' AND b LIKE '%jane%' AND c = 'C:\\\\'"
    assert (
        redact_literals(text, "mysql") == "SELECT * FROM t WHERE a = ? AND b LIKE '%x%' AND c = ?"
    )


# -- review follow-ups: ANSI_QUOTES, the PostgreSQL rewrite, scoping ------------------

ANSI = SpanSettings(ansi_quotes=True)


class TestAnsiQuotes:
    def test_the_sql_mode_names_it(self):
        assert span_settings("mysql", {"sql_mode": "ANSI_QUOTES"}).ansi_quotes
        assert span_settings("mariadb", {"sql_mode": "ANSI,STRICT_ALL_TABLES"}).ansi_quotes
        assert not span_settings("mysql", {"sql_mode": "STRICT_ALL_TABLES"}).ansi_quotes
        assert not span_settings("postgresql", {"sql_mode": "ANSI_QUOTES"}).ansi_quotes

    def test_a_double_quoted_mysql_name_is_a_name_with_it(self):
        text = 'SELECT "created_at" FROM t WHERE "x" < now()'
        assert code_text(text, "mysql", settings=ANSI) == text
        assert code_text(text, "mysql") == "SELECT ? FROM t WHERE ? < now()"
        assert keyword_text('SELECT "where" FROM t', "mysql", settings=ANSI) == (
            'SELECT "quoted_name" FROM t'
        )

    def test_a_backslash_escapes_nothing_in_a_name(self):
        text = 'SELECT "a\\" FROM t WHERE b = 1'
        assert code_text(text, "mysql", settings=ANSI) == text

    def test_redaction_never_trusts_it(self):
        text = 'SELECT * FROM t WHERE name = "Jane Doe"'
        for settings in (None, ANSI):
            assert "Jane" not in redact_literals(text, "mysql", settings)
            assert "Jane" not in redact_definition(text, "mysql", settings)

    def test_sqlglot_reads_the_name_in_backticks(self):
        from src.tools.sql.facade import _strings_as_sqlglot_reads_them, read_sql

        text = 'SELECT "id" FROM "orders" WHERE "status" = 1'
        assert _strings_as_sqlglot_reads_them(text, "mysql", ANSI) == (
            "SELECT `id` FROM `orders` WHERE `status` = 1"
        )
        assert _strings_as_sqlglot_reads_them(text, "mysql") == text
        with with_span_settings(ANSI):
            reading = read_sql(text, "mysql")
        assert [ref.name for ref in reading.tables.tables] == ["orders"]

    def test_a_retention_delete_reads_a_quoted_column_with_it(self):
        from src.agents.referee.carried_writes import _older_than

        text = 'DELETE FROM t WHERE "created_at" < now()'
        assert not _older_than(text, "created_at", {"t"}, "mysql")  # a string, by default
        with with_span_settings(ANSI):
            assert _older_than(text, "created_at", {"t"}, "mysql")


def _pg_off_value(inner: str) -> str:
    """The value PostgreSQL reads in a ``standard_conforming_strings = off`` string body,
    one character at a time (the reference the one-pass rewrite is held to)."""
    out, i = [], 0
    while i < len(inner):
        if inner[i] == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            out.append(nxt if nxt in "'\\" else "\\" + nxt)
            i += 2
        elif inner.startswith("''", i):
            out.append("'")
            i += 2
        else:
            out.append(inner[i])
            i += 1
    return "".join(out)


@hypothesis_settings(max_examples=scaled(2000), deadline=None)
@given(st.lists(st.sampled_from(["''", "\\'", "\\\\", "\\n", "x", " ", "y"]), max_size=20))
def test_the_postgresql_rewrite_keeps_every_quote(parts):
    from src.tools.sql.facade import _strings_as_sqlglot_reads_them

    inner = "".join(parts)
    text = f"SELECT '{inner}' AS v"
    rewritten = _strings_as_sqlglot_reads_them(text, "postgresql", BACKSLASH_STRINGS)
    grammar = literals_and_comments_for("postgresql", DEFAULT_SPAN_SETTINGS)
    strings = [rewritten[s:e] for s, e, o in spans(rewritten, grammar) if o == "'"]
    # One string, read in standard form, holding the value the source reads.
    if "\\" in inner:
        assert len(strings) == 1, rewritten
        assert strings[0][1:-1].replace("''", "'") == _pg_off_value(inner)
    else:
        assert rewritten == text


def test_the_reviewed_postgresql_quote_case():
    from src.tools.sql.facade import _postgres_escaped_value

    assert _postgres_escaped_value("''\\'\\'x") == "'''x"


class TestScoping:
    def test_assess_restores_the_callers_settings(self, monkeypatch):
        from src.assessment import runner
        from tests.unit.assessment.test_progress_output import _request

        def phase(job):
            runner._ingest_and_analyse(job)  # loads the collection: its settings are active
            use_span_settings(NO_BACKSLASH_ESCAPES)

        monkeypatch.setattr(runner, "PHASES", (("analysis", phase),))
        with with_span_settings(BACKSLASH_STRINGS):
            runner.assess(_request("analysis"))
            assert active_span_settings() == BACKSLASH_STRINGS

    def test_a_missing_collection_reads_the_defaults(self, tmp_path):
        from src.storage.collector_for_models import read_collector_for_models
        from src.storage.local_store import LocalArtifactStore

        use_span_settings(NO_BACKSLASH_ESCAPES)
        store = LocalArtifactStore(str(tmp_path))
        assert read_collector_for_models(store, "db", "job", missing_ok=True) == {}
        assert active_span_settings() == DEFAULT_SPAN_SETTINGS

    def test_a_failed_settings_read_is_logged(self, caplog):
        import logging
        from unittest.mock import MagicMock

        from src.tools.database.mysql_tools import MySQLRemoteCollector
        from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

        for cls in (MySQLRemoteCollector, PostgreSQLRemoteCollector):
            ssm = MagicMock()
            ssm.run_sql_json.side_effect = RuntimeError("denied")
            db = cls(ssm=ssm, host="h", port=1, database="d", secret_arn="s", region="r")
            with caplog.at_level(logging.INFO):
                assert db.get_settings() == {}
        assert sum("not read" in r.getMessage() for r in caplog.records) == 2


def test_a_routine_body_is_read_with_its_own_sql_mode():
    from src.tools.analysis.relational_gaps import _text

    body = "BEGIN SET @p = 'C:\\'; SELECT LAST_INSERT_ID(); SET @q = 'x'; END"
    assert "LAST_INSERT_ID" not in _text("procedure", "p", body, "mysql", True).tokens.calls
    own = _text("procedure", "p", body, "mysql", True, sql_mode="NO_BACKSLASH_ESCAPES")
    assert "LAST_INSERT_ID" in own.tokens.calls
    # The routine's own mode wins over the collection's either way.
    with with_span_settings(NO_BACKSLASH_ESCAPES):
        mine = _text("procedure", "p", body, "mysql", True, sql_mode="STRICT_ALL_TABLES")
    assert "LAST_INSERT_ID" not in mine.tokens.calls


# -- the cost of reading both ways (re-review of #1640) ---------------------------------


def _best_cpu(fn, rounds: int = 5) -> float:
    from tests.unit.tools.sql._timing import _timed

    fn()  # warm-up
    return min(_timed(fn) for _ in range(rounds))


def test_redacting_a_collection_costs_about_what_one_reading_does():
    """Cold redaction of the discourse sample (646 KB of PostgreSQL text) against the
    base reading, one grammar and one pass, in the same process: a ratio, never seconds,
    so a slow runner slows both alike."""
    from src.tools.sql import redact
    from tests.unit.tools.sql.table_refs_snapshot import load_sample

    data = load_sample("discourse")
    texts = [str(q["query_text"]) for q in data["queries"] if q.get("query_text")]
    assert sum(map(len, texts)) > 500_000

    def one_reading() -> None:
        for text in texts:
            redactor = redact._Redactor("postgresql", "query", DEFAULT_SPAN_SETTINGS)
            redactor.other = None
            redactor.redact(text)

    def both_readings() -> None:
        for text in texts:
            redact._redacted_uncached(text, "postgresql", "query", DEFAULT_SPAN_SETTINGS)

    ratio = _best_cpu(both_readings) / max(_best_cpu(one_reading), 1e-6)
    assert ratio < 1.5, f"reading both ways costs {ratio:.2f}x one reading"
