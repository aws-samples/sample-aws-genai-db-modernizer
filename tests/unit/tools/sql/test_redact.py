# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Literal redaction of collected SQL text (#1625): ``src.tools.sql.redact``."""

from __future__ import annotations

import dataclasses
import functools
import json
import re
import zipfile
from pathlib import Path

import pytest

from src.tools.database.collection_text import extract_json_object
from src.tools.sql.facade import read_sql
from src.tools.sql.query_facts import QueryFacts, query_facts
from src.tools.sql.query_tags import statement_tags
from src.tools.sql.redact import (
    redact_comment,
    redact_definition,
    redact_literals,
    scrub_comment,
)
from tests.fixtures.pii_collections import ENGINES, leaked, pii_texts

REPO = Path(__file__).resolve().parents[4]


def _sample_texts(name: str) -> list[str]:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        data = json.loads(extract_json_object(z.read(f"{name}-collection.json").decode()))
    return [str(q.get("query_text") or "") for q in data["queries"]]


class TestValuesAreReplaced:
    @pytest.mark.parametrize(
        "engine,mark",
        [("sqlserver", "?"), ("db2", "?"), ("mysql", "?"), ("oracle", ":1"), ("postgresql", "$1")],
    )
    def test_each_engine_gets_its_own_placeholder(self, engine: str, mark: str) -> None:
        sql = "SELECT * FROM t WHERE email = 'jane@example.com'"
        assert redact_literals(sql, engine) == f"SELECT * FROM t WHERE email = {mark}"

    def test_placeholders_are_numbered_after_the_highest_bind(self) -> None:
        assert (
            redact_literals("SELECT * FROM t WHERE a = :2 AND b = 'x' AND c = 7", "oracle")
            == "SELECT * FROM t WHERE a = :2 AND b = :3 AND c = :4"
        )
        assert (
            redact_literals("SELECT * FROM t WHERE a = $1 AND b = 'x'", "postgresql")
            == "SELECT * FROM t WHERE a = $1 AND b = $2"
        )

    @pytest.mark.parametrize(
        "engine,sql,expected",
        [
            ("sqlserver", "WHERE n = 4111111111111111", "WHERE n = ?"),
            ("sqlserver", "WHERE n = -12.5e3", "WHERE n = -?"),
            ("sqlserver", "WHERE b = 0xDEADBEEF", "WHERE b = ?"),
            ("sqlserver", "WHERE d >= '1984-03-07'", "WHERE d >= ?"),
            ("oracle", "WHERE d = DATE '1984-03-07'", "WHERE d = :1"),
            ("oracle", "WHERE d = TIMESTAMP '1984-03-07 10:00:00'", "WHERE d = :1"),
            (
                "oracle",
                "WHERE t > SYSDATE - INTERVAL '1' DAY",
                "WHERE t > SYSDATE - INTERVAL :1 DAY",
            ),
            (
                "oracle",
                "WHERE n = TO_DATE('1984-03-07', 'YYYY-MM-DD')",
                "WHERE n = TO_DATE(:1, 'YYYY-MM-DD')",
            ),
            ("db2", "WHERE d = DATE('1984-03-07')", "WHERE d = DATE(?)"),
            ("sqlserver", "WHERE s IN ('a', 'b', 'c')", "WHERE s IN (?, ?, ?)"),
            ("sqlserver", "VALUES (@id, 'Jane', 3)", "VALUES (@id, ?, ?)"),
        ],
    )
    def test_strings_numbers_dates_and_binary_values(
        self, engine: str, sql: str, expected: str
    ) -> None:
        assert redact_literals(sql, engine) == expected

    @pytest.mark.parametrize(
        "engine,sql,expected",
        [
            # A prefixed string or another quote form keeps its form: it reads as the
            # literal it was (sqlglot reads no X'...').
            ("sqlserver", "WHERE n = N'Jane Doe'", "WHERE n = N'?'"),
            ("db2", "WHERE h = X'DEADBEEF'", "WHERE h = X'00'"),
            ("db2", "WHERE g = G'Jane'", "WHERE g = G'?'"),
            ("oracle", "WHERE n = q'[O'Brien]'", "WHERE n = q'[?]'"),
            ("postgresql", "WHERE n = E'Jane\\n'", "WHERE n = E'?'"),
            ("mysql", 'WHERE n = "Jane"', 'WHERE n = "?"'),
        ],
    )
    def test_other_string_forms_keep_their_form(self, engine: str, sql: str, expected: str) -> None:
        assert redact_literals(sql, engine) == expected

    def test_a_text_cut_inside_a_string_keeps_only_the_quote(self) -> None:
        assert redact_literals("SELECT * FROM t WHERE e = 'jane@exa", "oracle") == (
            "SELECT * FROM t WHERE e = '"
        )

    def test_a_line_comment_that_runs_to_the_end_is_the_statement(self) -> None:
        # SQL Server and Oracle collections put a statement on one line.
        sql = "-- lookup SELECT * FROM t WHERE n = 'Jane' AND m = 42 AND e = 'jane@example.com'"
        assert redact_literals(sql, "sqlserver") == (
            "-- lookup SELECT * FROM t WHERE n = ? AND m = ? AND e = '?'"
        )

    def test_dynamic_sql_is_redacted_as_sql(self) -> None:
        sql = "EXEC sp_executesql N'SELECT * FROM t WHERE a = ''Jane''', N'@p int', @p = 5"
        assert redact_literals(sql, "sqlserver") == (
            "EXEC sp_executesql N'SELECT * FROM t WHERE a = ?', N'?', @p = ?"
        )

    def test_a_dollar_quoted_constant_is_a_string(self) -> None:
        sql = "SELECT * FROM t WHERE n = $q$Ottoline$q$ AND m = $$Brackenbury$$"
        assert redact_literals(sql, "postgresql") == "SELECT * FROM t WHERE n = $1 AND m = $2"
        # Inside a routine body, a dollar-quoted statement is code, any other a value.
        body = "CREATE FUNCTION f() AS $$ SELECT $q$Jane$q$; EXECUTE $b$ DELETE FROM t $b$ $$"
        assert redact_definition(body, "postgresql") == (
            "CREATE FUNCTION f() AS $$ SELECT $q$?$q$; EXECUTE $b$ DELETE FROM t $b$ $$"
        )

    def test_a_postgresql_body_is_code(self) -> None:
        sql = "DO $$ BEGIN DELETE FROM t WHERE e = 'jane@example.com'; RAISE NOTICE 'Jane'; END $$"
        assert redact_literals(sql, "postgresql") == (
            "DO $$ BEGIN DELETE FROM t WHERE e = '?'; RAISE NOTICE '?'; END $$"
        )


class TestStructureIsKept:
    @pytest.mark.parametrize(
        "engine,sql",
        [
            ("sqlserver", "SELECT TOP (24) a FROM t ORDER BY 1, 2"),
            ("sqlserver", "SELECT TOP 10 PERCENT a FROM t"),
            ("sqlserver", "SELECT a FROM t ORDER BY a OFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY"),
            ("oracle", "SELECT a FROM t WHERE ROWNUM <= 10"),
            ("db2", "SELECT a FROM t FETCH FIRST 1 ROWS ONLY"),
            ("mysql", "SELECT a FROM t LIMIT 10, 20"),
            ("sqlserver", "SELECT CAST(a AS decimal(10, 2)), CONVERT(varchar(40), b) FROM t"),
            ("sqlserver", "SELECT COUNT(1) FROM t WHERE EXISTS (SELECT 1 FROM u)"),
            ("sqlserver", "SELECT SUM(a) OVER (ORDER BY b ROWS 2 PRECEDING) FROM t"),
            ("sqlserver", "SELECT a FROM t OPTION (MAXDOP 4)"),
            ("sqlserver", "SELECT a AS 'Total' FROM t"),
            ("sqlserver", "SELECT JSON_VALUE(doc, '$.customer.name') FROM t"),
            ("postgresql", "SELECT date_trunc('day', created_at) FROM t GROUP BY 1"),
            ("oracle", "SELECT TO_CHAR(d, 'YYYY-MM') FROM t"),
            ("mysql", "SELECT DATE_FORMAT(created_at, '%Y-%m-%d %H:00:00') FROM t"),
            ("sqlserver", "SELECT a FROM t WHERE b LIKE '%' + @term + '%' ESCAPE '\\'"),
            ("sqlserver", "SELECT a FROM [Order Details 2024] WHERE [O'Brien] = @p1"),
            ("oracle", "SELECT a FROM t WHERE b = :id AND c = :1"),
            ("mysql", "SELECT * FROM `wp_posts` WHERE `ID` = ? LIMIT ?"),
        ],
    )
    def test_no_value_nothing_changes(self, engine: str, sql: str) -> None:
        assert redact_literals(sql, engine) == sql

    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("WHERE e LIKE '%jane@example.com%'", "WHERE e LIKE '%?%'"),
            ("WHERE n LIKE '%Jane Doe%'", "WHERE n LIKE '%x x%'"),
            ("WHERE n LIKE 'Jane%'", "WHERE n LIKE 'x%'"),
            ("WHERE n LIKE 'Jane'", "WHERE n LIKE 'x'"),
            ("WHERE n LIKE '_ane%'", "WHERE n LIKE '_x%'"),
            ("WHERE n LIKE @p + 'Doe%'", "WHERE n LIKE @p + 'x%'"),
            ("WHERE REGEXP_LIKE(n, '^Jan?e')", "WHERE REGEXP_LIKE(n, '^xx?x')"),
        ],
    )
    def test_a_pattern_keeps_its_shape(self, sql: str, expected: str) -> None:
        assert redact_literals(sql, "oracle") == expected


class TestKeptNumbersAreSizes:
    @pytest.mark.parametrize(
        "engine,sql,expected",
        [
            ("oracle", "SELECT a FROM t FOR UPDATE WAIT 5", "SELECT a FROM t FOR UPDATE WAIT 5"),
            ("oracle", "SELECT * FROM t SAMPLE (10)", "SELECT * FROM t SAMPLE (10)"),
            (
                "sqlserver",
                "SELECT NTILE(4) OVER (ORDER BY a) FROM t",
                "SELECT NTILE(4) OVER (ORDER BY a) FROM t",
            ),
            ("sqlserver", "SELECT 4111111111111111 FROM t", "SELECT ? FROM t"),
            ("sqlserver", "SELECT COUNT(4111111111111111) FROM t", "SELECT COUNT(?) FROM t"),
            (
                "sqlserver",
                "SELECT DAY(19840307), YEAR(1984) FROM t",
                "SELECT DAY(?), YEAR(?) FROM t",
            ),
            (
                "sqlserver",
                "SELECT CAST(a AS DECIMAL(4111111111111111)) FROM t",
                "SELECT CAST(a AS DECIMAL(?)) FROM t",
            ),
            ("sqlserver", "SELECT TOP (4111111111111111) a FROM t", "SELECT TOP (?) a FROM t"),
            ("mysql", "SELECT a FROM t LIMIT 4111111111111111", "SELECT a FROM t LIMIT ?"),
            (
                "sqlserver",
                "SELECT a FROM t WHERE price = $1984.37",
                "SELECT a FROM t WHERE price = ?",
            ),
            # The money literal holds the number: one value, one placeholder.
            (
                "sqlserver",
                "SELECT a FROM t WHERE price = $ 1984.37",
                "SELECT a FROM t WHERE price = ?",
            ),
        ],
    )
    def test_only_small_integers_are_kept(self, engine: str, sql: str, expected: str) -> None:
        assert redact_literals(sql, engine) == expected

    def test_a_money_literal_reads_as_the_value_it_was(self) -> None:
        # The collected text misreads ``.37`` as a column; the redacted text reads only
        # the value, so the key equality is the same and no column is added.
        raw = "SELECT a FROM t WHERE price = $1984.37"
        before, after = read_sql(raw, "sqlserver"), read_sql(
            redact_literals(raw, "sqlserver"), "sqlserver"
        )
        assert before.access.scopes[0].equalities == after.access.scopes[0].equalities
        assert set(after.facts.columns) < set(before.facts.columns)


class TestComments:
    def test_e_mails_and_long_digit_runs_are_scrubbed(self) -> None:
        sql = "/* user: jane@example.com card 4111 1111 1111 1111 */ SELECT 1 FROM t"
        assert redact_literals(sql, "sqlserver") == "/* user: ? card ? */ SELECT 1 FROM t"

    def test_sqlcommenter_tags_keep_their_keys_and_code_names(self) -> None:
        sql = "/*controller='cart',action='add',user='jane%40example.com'*/ SELECT 1 FROM t"
        redacted = redact_literals(sql, "postgresql")
        assert redacted == "/*controller='cart',action='add',user='?'*/ SELECT 1 FROM t"
        assert statement_tags(redacted, "postgresql") == ["controller='cart',action='add',user='?'"]

    @pytest.mark.parametrize(
        "comment",
        [
            "app:billing route:'/cart' user='Ottoline'",
            "note user='Ottoline'",
            "controller='cart' user='Ottoline'",
            "controller='cart',,user='Ottoline'",
        ],
    )
    def test_only_a_comment_of_sqlcommenter_pairs_keeps_its_values(self, comment: str) -> None:
        # The specification: comma-separated key='value' pairs, keys URL-encoded.
        assert not leaked(redact_comment(comment, "postgresql"))
        assert redact_comment("controller='cart',user='Ottoline'", "postgresql") == (
            "controller='cart',user='Ottoline'"
        )

    def test_a_free_text_comment_has_its_quoted_values_redacted(self) -> None:
        sql = "/* lookup for 'Jane Doe' */\nSELECT 1 FROM t"
        assert redact_literals(sql, "postgresql") == "/* lookup for '?' */\nSELECT 1 FROM t"

    def test_an_apostrophe_in_a_comment_is_not_a_cut_string(self) -> None:
        assert redact_comment("don't cache this", "postgresql") == "don't cache this"

    def test_a_quoted_value_after_an_apostrophe_is_redacted(self) -> None:
        # The apostrophe pairs with the value's opening quote as a string would: every
        # run between two quotes goes, so the value cannot fall outside one.
        assert redact_comment("don't 'Ottoline Brackenbury'", "sqlserver") == "don'?'?'"
        sql = "/* don't 'Ottoline Brackenbury' */\nSELECT 1 FROM t"
        assert not leaked(redact_literals(sql, "sqlserver"))
        assert not leaked(redact_literals(sql, "oracle"))

    def test_a_tag_is_redacted_as_the_comment_it_came_from(self) -> None:
        tag = "endpoint: CartController.Get, by 'Jane Doe' jane@example.com"
        assert redact_comment(tag, "sqlserver") == "endpoint: CartController.Get, by '?' ?"
        assert scrub_comment("2026-10-27 order 123") == "2026-10-27 order 123"


class TestDefinitions:
    def test_strings_become_quoted_placeholders_and_numbers_stay(self) -> None:
        sql = "CREATE VIEW v AS SELECT * FROM t WHERE e = 'jane@example.com' AND f = 1"
        assert redact_definition(sql, "sqlserver") == (
            "CREATE VIEW v AS SELECT * FROM t WHERE e = '?' AND f = 1"
        )

    def test_a_postgresql_function_body_is_read_as_code(self) -> None:
        sql = "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RAISE NOTICE 'Jane'; RETURN 1; END $$"
        assert redact_definition(sql, "postgresql") == (
            "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RAISE NOTICE '?'; RETURN 1; END $$"
        )

    def test_the_quote_form_a_gap_rule_reads_is_kept(self) -> None:
        assert redact_definition('SELECT "Jane" FROM t', "mysql") == 'SELECT "?" FROM t'


class TestSeededValues:
    @pytest.mark.parametrize("engine", ENGINES)
    def test_no_seeded_value_survives(self, engine: str) -> None:
        for text in pii_texts(engine):
            assert not leaked(redact_literals(text, engine)), text


class TestNormalisedTextIsUntouched:
    def test_every_mysql_digest_text_is_unchanged(self) -> None:
        texts = _sample_texts("wordpress")
        assert texts and all(redact_literals(t, "mysql") == t for t in texts)

    def test_postgresql_text_is_unchanged_but_where_literals_remain(self) -> None:
        texts = _sample_texts("discourse")
        changed = [t for t in texts if redact_literals(t, "postgresql") != t]
        # pg_stat_statements normalises every statement but its utility ones; the sample
        # also holds two statements written with literals. Only those four change.
        assert len(changed) == 4, changed
        assert sorted(t.split()[0] for t in changed) == ["SET", "SET", "UPDATE", "WITH"]
        assert sum(redact_literals(t, "postgresql") == t for t in texts) == len(texts) - 4


# Statements of every source dialect whose literals carry what the routing parser reads:
# patterns, limits, positions, types, JSON paths, date parts, typed literals.
_PARSE_CORPUS = [
    ("sqlserver", "SELECT TOP (1) a FROM t WHERE e = 'jane@example.com' ORDER BY d DESC"),
    ("sqlserver", "SELECT a FROM t WHERE n LIKE '%Jane Doe%' AND s = N'PAID'"),
    ("sqlserver", "SELECT a FROM t WHERE n LIKE N'%jane%' OR n LIKE 'abc%' OR n LIKE 'x'"),
    ("sqlserver", "UPDATE t SET s = N'CLOSED', n = 4 WHERE id = @id AND v = 0xAB"),
    ("sqlserver", "SELECT COUNT(1) FROM t WHERE d >= DATEADD(day, -30, SYSUTCDATETIME())"),
    ("sqlserver", "SELECT YEAR(d), COUNT(*) FROM t WHERE status = 'open' GROUP BY YEAR(d)"),
    ("sqlserver", "INSERT INTO t (a, b, c) VALUES ('x', 1, '1984-03-07')"),
    ("sqlserver", "SELECT JSON_VALUE(doc, '$.a') FROM t WHERE JSON_VALUE(doc, '$.b') = 'v'"),
    ("sqlserver", "SELECT a FROM t WHERE CONTAINS(body, 'jane') AND id IN (1, 2, 3)"),
    ("sqlserver", "SELECT a FROM t WHERE b = 7 ORDER BY a OFFSET 50 ROWS FETCH NEXT 25 ROWS ONLY"),
    ("sqlserver", "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.v = 'x';"),
    ("oracle", "SELECT * FROM t WHERE e = 'jane@example.com' AND ROWNUM = 1"),
    ("oracle", "SELECT * FROM t WHERE d > DATE '2024-01-01' AND REGEXP_LIKE(n, '^Jane')"),
    ("oracle", "SELECT TRUNC(d, 'MM'), COUNT(*) FROM t WHERE k = 'A' GROUP BY TRUNC(d, 'MM')"),
    ("oracle", "SELECT * FROM t WHERE n = q'[O'Brien]' FETCH FIRST 5 ROWS ONLY"),
    ("oracle", "SELECT * FROM t WHERE ts > SYSTIMESTAMP - INTERVAL '7' DAY AND s = 'A'"),
    ("db2", "SELECT * FROM t WHERE c = '4111111111111111' FETCH FIRST 10 ROWS ONLY"),
    ("db2", "SELECT * FROM t WHERE h = X'DEADBEEF' AND d = DATE('1984-03-07')"),
    ("db2", "SELECT * FROM t WHERE n LIKE '%Doe' WITH UR"),
    ("postgresql", "SELECT date_trunc('day', c), COUNT(*) FROM t WHERE k = 'A' GROUP BY 1"),
    ("postgresql", "SET client_min_messages TO 'warning'"),
    # Structural numbers next to a value (review of #1631).
    ("oracle", "SELECT * FROM customer WHERE email = 'jane@example.com' FOR UPDATE WAIT 5"),
    ("oracle", "SELECT * FROM t WHERE id = 7 FOR UPDATE SKIP LOCKED"),
    ("oracle", "SELECT * FROM t SAMPLE (10) WHERE a = 'x'"),
    ("oracle", "SELECT * FROM t SAMPLE BLOCK (5) SEED (42) WHERE a = 'x'"),
    ("sqlserver", "SELECT a FROM t WHERE b = 'x' ORDER BY a OFFSET 10 ROWS"),
    ("sqlserver", "SELECT TOP (10) PERCENT a FROM t WHERE b = 'x'"),
    ("sqlserver", "SELECT NTILE(4) OVER (ORDER BY a) FROM t WHERE b = 'x'"),
    ("sqlserver", "SELECT ROUND(x, 2) FROM t WHERE b = 'x'"),
    ("oracle", "SELECT ROUND(x, 2) FROM t WHERE b = 'x'"),
]


def _parse_differences(text: str, engine: str) -> list[tuple[str, str]]:
    redacted = redact_literals(text, engine)
    raw, red = read_sql(text, engine), read_sql(redacted, engine)
    out = [
        (part, f.name)
        for part in ("tables", "facts", "access")
        for f in dataclasses.fields(getattr(raw, part))
        if getattr(getattr(raw, part), f.name) != getattr(getattr(red, part), f.name)
    ]
    raw_facts, red_facts = query_facts(text, engine), query_facts(redacted, engine)
    for name, value in vars(QueryFacts).items():
        if name.startswith("_") or not isinstance(value, functools.cached_property | property):
            continue
        if getattr(raw_facts, name) != getattr(red_facts, name):
            out.append(("query_facts", name))
    return out


class TestTheParserReadsTheSame:
    """The routing parser reads a redacted text exactly as it read the text collected:
    the same facade readings and the same query facts."""

    @pytest.mark.parametrize("engine,text", _PARSE_CORPUS)
    def test_corpus(self, engine: str, text: str) -> None:
        assert redact_literals(text, engine) != text
        assert not _parse_differences(text, engine)

    @pytest.mark.parametrize("engine", ENGINES)
    def test_seeded_statements(self, engine: str) -> None:
        for text in pii_texts(engine):
            assert not _parse_differences(text, engine), text

    @pytest.mark.parametrize(
        "sample,engine", [("trailgear", "sqlserver"), ("discourse", "postgresql")]
    )
    def test_every_sample_statement(self, sample: str, engine: str) -> None:
        changed = [t for t in _sample_texts(sample) if redact_literals(t, engine) != t]
        assert changed
        assert not [(t, d) for t in changed if (d := _parse_differences(t, engine))]


class TestIdempotent:
    @pytest.mark.parametrize("engine,text", _PARSE_CORPUS)
    def test_redacting_twice_changes_nothing(self, engine: str, text: str) -> None:
        once = redact_literals(text, engine)
        assert redact_literals(once, engine) == once
        assert redact_definition(redact_definition(text, engine), engine) == redact_definition(
            text, engine
        )

    @pytest.mark.parametrize("engine", ENGINES)
    def test_a_tag_read_from_redacted_text_is_already_redacted(self, engine: str) -> None:
        for text in pii_texts(engine):
            for tag in statement_tags(redact_literals(text, engine), engine):
                assert redact_comment(tag, engine) == tag


class TestLinearTime:
    """The comment scrub is linear: a quadratic one took 81 s on a 160k-character
    comment (review of #1631). Timed against the same code on a 10k-character comment,
    in the same process (``tests/unit/tools/sql/_timing.py``)."""

    @pytest.mark.parametrize(
        "unit",
        ["a.", "deadbeef" + "0123456789", "1 ", "x" * 63 + "@", "d = DATE '2024-01-01' AND "],
        ids=["dotted", "hex-dump", "spaced-digits", "at-signs", "typed-date-literals"],
    )
    def test_a_long_comment_takes_time_in_proportion_to_its_length(self, unit: str) -> None:
        from src.tools.sql.redact import _Redactor
        from tests.unit.tools.sql._timing import assert_linear

        def comment(chars: int) -> str:
            body = (unit * (chars // len(unit) + 1))[:chars]
            if unit.startswith("d = DATE"):
                return f"SELECT 1 FROM t WHERE {body}d = 1"  # typed literals, in the query
            # One ``@`` with no domain after it: every match attempt fails.
            return f"/* {body}@ */ SELECT 1 FROM t"

        def redact(text: str) -> None:
            _Redactor("sqlserver", "query").redact(text)  # the cached wrapper would hit
            scrub_comment(text)

        assert_linear(comment, redact, sizes=(10_000, 160_000))


def test_the_redaction_cache_is_bounded_by_size() -> None:
    from src.tools.sql.redact import _CACHE, _SizeBoundCache

    cache = _SizeBoundCache(max_chars=1_000)
    for n in range(100):
        cache.put((f"{n:03d}" + "x" * 50, "sqlserver", "query"), "y" * 50)
    assert cache.chars <= 1_000
    assert cache.get(("099" + "x" * 50, "sqlserver", "query")) == "y" * 50
    assert cache.get(("000" + "x" * 50, "sqlserver", "query")) is None
    cache.put(("z" * 500, "sqlserver", "query"), "z")  # too long to keep
    assert cache.get(("z" * 500, "sqlserver", "query")) is None
    assert _CACHE.max_chars <= 8_000_000


ALL_ENGINES = ("mysql", "postgresql", "sqlserver", "oracle", "db2")
# MySQL keeps a number glued to a word: an identifier may start with digits there.
GLUING_ENGINES = ("postgresql", "sqlserver", "oracle", "db2")
MARKS = {"mysql": "?", "postgresql": "$1", "sqlserver": "?", "oracle": ":1", "db2": "?"}


_PLACEHOLDER_RE = re.compile(r"(?<![\w])[$:]\d+")
_MAIN_REDACTIONS = [
    ("mysql", "SELECT 41111.5#", "SELECT ?#"),
    ("mysql", "SELECT 4111.$1", "SELECT ?.$1"),
    ("mysql", "SELECT 0x1F.1$1", "SELECT ?.1$1"),
    ("mysql", "SELECT 4111111111111111.5#", "SELECT ?#"),
    ("mysql", "SELECT 0xDEADBEEF.5$", "SELECT ?.5$"),
    ("mysql", "SELECT 1.5e3.2#", "SELECT ?.2#"),
    ("mysql", "SELECT 4111.5$x", "SELECT ?.5$x"),
    ("mysql", "SELECT 12$, 34#", "SELECT 12$, ?#"),
    ("mysql", "SELECT 4111111111111111$", "SELECT 4111111111111111$"),
    ("mysql", "SELECT 0x4111$", "SELECT 0x4111$"),
    ("mysql", "SELECT a FROM t WHERE c = 4111111111111111", "SELECT a FROM t WHERE c = ?"),
    ("mysql", "SELECT a FROM t WHERE c = 0x4111FF.2", "SELECT a FROM t WHERE c = ?.2"),
    ("mysql", "SELECT $1984.37", "SELECT $1984.37"),
    ("mysql", "SELECT $ 1984.37", "SELECT $ ?"),
    ("mysql", "SELECT $4111.5", "SELECT $4111.5"),
    ("mysql", "SELECT a FROM t ORDER BY 1, 2", "SELECT a FROM t ORDER BY 1, 2"),
    ("mysql", "SELECT a FROM t GROUP BY 2, 3", "SELECT a FROM t GROUP BY 2, 3"),
    ("mysql", "SELECT a FROM t ORDER BY 4111111111111111, 2", "SELECT a FROM t ORDER BY ?, 2"),
    ("mysql", "SELECT a FROM t ORDER BY a DESC, 3", "SELECT a FROM t ORDER BY a DESC, 3"),
    ("mysql", "SELECT 1.5e3", "SELECT ?"),
    ("mysql", "SELECT .5", "SELECT ?"),
    ("mysql", "SELECT 5.", "SELECT ?"),
    (
        "mysql",
        "SELECT a FROM t WHERE c BETWEEN 1234 AND 5678",
        "SELECT a FROM t WHERE c BETWEEN ? AND ?",
    ),
    ("mysql", "SELECT a FROM t LIMIT 10 OFFSET 20", "SELECT a FROM t LIMIT 10 OFFSET 20"),
    ("mysql", "SELECT t0x1F, a.1 FROM t", "SELECT t0x1F, a.1 FROM t"),
    ("mysql", "SELECT 4111.5#x", "SELECT ?#x"),
    ("mysql", "SELECT 1e5#", "SELECT ?#"),
    ("mysql", "SELECT 7$a", "SELECT 7$a"),
    ("mysql", "SELECT 4111111111111111#1", "SELECT ?#?"),
    ("postgresql", "SELECT 41111.5#", "SELECT $1.5#"),
    ("postgresql", "SELECT 4111.$1", "SELECT $2.$1"),
    ("postgresql", "SELECT 0x1F.1$1", "SELECT $1.1$1"),
    ("postgresql", "SELECT 4111111111111111.5#", "SELECT $1.5#"),
    ("postgresql", "SELECT 0xDEADBEEF.5$", "SELECT $1.5$"),
    ("postgresql", "SELECT 1.5e3.2#", "SELECT $1.2#"),
    ("postgresql", "SELECT 4111.5$x", "SELECT $1.5$x"),
    ("postgresql", "SELECT 12$, 34#", "SELECT 12$, 34#"),
    ("postgresql", "SELECT 4111111111111111$", "SELECT 4111111111111111$"),
    ("postgresql", "SELECT 0x4111$", "SELECT 0x4111$"),
    ("postgresql", "SELECT a FROM t WHERE c = 4111111111111111", "SELECT a FROM t WHERE c = $1"),
    ("postgresql", "SELECT a FROM t WHERE c = 0x4111FF.2", "SELECT a FROM t WHERE c = $1.2"),
    ("postgresql", "SELECT $1984.37", "SELECT $1984.37"),
    ("postgresql", "SELECT $ 1984.37", "SELECT $ $1"),
    ("postgresql", "SELECT $4111.5", "SELECT $4111.5"),
    ("postgresql", "SELECT a FROM t ORDER BY 1, 2", "SELECT a FROM t ORDER BY 1, 2"),
    ("postgresql", "SELECT a FROM t GROUP BY 2, 3", "SELECT a FROM t GROUP BY 2, 3"),
    (
        "postgresql",
        "SELECT a FROM t ORDER BY 4111111111111111, 2",
        "SELECT a FROM t ORDER BY $1, 2",
    ),
    ("postgresql", "SELECT a FROM t ORDER BY a DESC, 3", "SELECT a FROM t ORDER BY a DESC, 3"),
    ("postgresql", "SELECT 1.5e3", "SELECT $1"),
    ("postgresql", "SELECT .5", "SELECT $1"),
    ("postgresql", "SELECT 5.", "SELECT $1"),
    (
        "postgresql",
        "SELECT a FROM t WHERE c BETWEEN 1234 AND 5678",
        "SELECT a FROM t WHERE c BETWEEN $1 AND $2",
    ),
    ("postgresql", "SELECT a FROM t LIMIT 10 OFFSET 20", "SELECT a FROM t LIMIT 10 OFFSET 20"),
    ("postgresql", "SELECT t0x1F, a.1 FROM t", "SELECT t0x1F, a.1 FROM t"),
    ("postgresql", "SELECT 4111.5#x", "SELECT $1.5#x"),
    ("postgresql", "SELECT 1e5#", "SELECT 1e5#"),
    ("postgresql", "SELECT 7$a", "SELECT 7$a"),
    ("postgresql", "SELECT 4111111111111111#1", "SELECT 4111111111111111#1"),
    ("sqlserver", "SELECT 41111.5#", "SELECT ?.5#"),
    ("sqlserver", "SELECT 4111.$1", "SELECT ?.$1"),
    ("sqlserver", "SELECT 0x1F.1$1", "SELECT ?.1$1"),
    ("sqlserver", "SELECT 4111111111111111.5#", "SELECT ?.5#"),
    ("sqlserver", "SELECT 0xDEADBEEF.5$", "SELECT ?.5$"),
    ("sqlserver", "SELECT 1.5e3.2#", "SELECT ?.2#"),
    ("sqlserver", "SELECT 4111.5$x", "SELECT ?.5$x"),
    ("sqlserver", "SELECT 12$, 34#", "SELECT 12$, 34#"),
    ("sqlserver", "SELECT 4111111111111111$", "SELECT 4111111111111111$"),
    ("sqlserver", "SELECT 0x4111$", "SELECT 0x4111$"),
    ("sqlserver", "SELECT a FROM t WHERE c = 4111111111111111", "SELECT a FROM t WHERE c = ?"),
    ("sqlserver", "SELECT a FROM t WHERE c = 0x4111FF.2", "SELECT a FROM t WHERE c = ?.2"),
    ("sqlserver", "SELECT $1984.37", "SELECT ?"),
    ("sqlserver", "SELECT $ 1984.37", "SELECT ?"),
    ("sqlserver", "SELECT $4111.5", "SELECT ?"),
    ("sqlserver", "SELECT a FROM t ORDER BY 1, 2", "SELECT a FROM t ORDER BY 1, 2"),
    ("sqlserver", "SELECT a FROM t GROUP BY 2, 3", "SELECT a FROM t GROUP BY 2, 3"),
    ("sqlserver", "SELECT a FROM t ORDER BY 4111111111111111, 2", "SELECT a FROM t ORDER BY ?, 2"),
    ("sqlserver", "SELECT a FROM t ORDER BY a DESC, 3", "SELECT a FROM t ORDER BY a DESC, 3"),
    ("sqlserver", "SELECT 1.5e3", "SELECT ?"),
    ("sqlserver", "SELECT .5", "SELECT ?"),
    ("sqlserver", "SELECT 5.", "SELECT ?"),
    (
        "sqlserver",
        "SELECT a FROM t WHERE c BETWEEN 1234 AND 5678",
        "SELECT a FROM t WHERE c BETWEEN ? AND ?",
    ),
    ("sqlserver", "SELECT a FROM t LIMIT 10 OFFSET 20", "SELECT a FROM t LIMIT 10 OFFSET 20"),
    ("sqlserver", "SELECT t0x1F, a.1 FROM t", "SELECT t0x1F, a.1 FROM t"),
    ("sqlserver", "SELECT 4111.5#x", "SELECT ?.5#x"),
    ("sqlserver", "SELECT 1e5#", "SELECT 1e5#"),
    ("sqlserver", "SELECT 7$a", "SELECT 7$a"),
    ("sqlserver", "SELECT 4111111111111111#1", "SELECT 4111111111111111#1"),
    ("oracle", "SELECT 41111.5#", "SELECT :1.5#"),
    ("oracle", "SELECT 4111.$1", "SELECT :1.$1"),
    ("oracle", "SELECT 0x1F.1$1", "SELECT :1.1$1"),
    ("oracle", "SELECT 4111111111111111.5#", "SELECT :1.5#"),
    ("oracle", "SELECT 0xDEADBEEF.5$", "SELECT :1.5$"),
    ("oracle", "SELECT 1.5e3.2#", "SELECT :1.2#"),
    ("oracle", "SELECT 4111.5$x", "SELECT :1.5$x"),
    ("oracle", "SELECT 12$, 34#", "SELECT 12$, 34#"),
    ("oracle", "SELECT 4111111111111111$", "SELECT 4111111111111111$"),
    ("oracle", "SELECT 0x4111$", "SELECT 0x4111$"),
    ("oracle", "SELECT a FROM t WHERE c = 4111111111111111", "SELECT a FROM t WHERE c = :1"),
    ("oracle", "SELECT a FROM t WHERE c = 0x4111FF.2", "SELECT a FROM t WHERE c = :1.2"),
    ("oracle", "SELECT $1984.37", "SELECT $1984.37"),
    ("oracle", "SELECT $ 1984.37", "SELECT $ :1"),
    ("oracle", "SELECT $4111.5", "SELECT $4111.5"),
    ("oracle", "SELECT a FROM t ORDER BY 1, 2", "SELECT a FROM t ORDER BY 1, 2"),
    ("oracle", "SELECT a FROM t GROUP BY 2, 3", "SELECT a FROM t GROUP BY 2, 3"),
    ("oracle", "SELECT a FROM t ORDER BY 4111111111111111, 2", "SELECT a FROM t ORDER BY :1, 2"),
    ("oracle", "SELECT a FROM t ORDER BY a DESC, 3", "SELECT a FROM t ORDER BY a DESC, 3"),
    ("oracle", "SELECT 1.5e3", "SELECT :1"),
    ("oracle", "SELECT .5", "SELECT :1"),
    ("oracle", "SELECT 5.", "SELECT :1"),
    (
        "oracle",
        "SELECT a FROM t WHERE c BETWEEN 1234 AND 5678",
        "SELECT a FROM t WHERE c BETWEEN :1 AND :2",
    ),
    ("oracle", "SELECT a FROM t LIMIT 10 OFFSET 20", "SELECT a FROM t LIMIT 10 OFFSET 20"),
    ("oracle", "SELECT t0x1F, a.1 FROM t", "SELECT t0x1F, a.1 FROM t"),
    ("oracle", "SELECT 4111.5#x", "SELECT :1.5#x"),
    ("oracle", "SELECT 1e5#", "SELECT 1e5#"),
    ("oracle", "SELECT 7$a", "SELECT 7$a"),
    ("oracle", "SELECT 4111111111111111#1", "SELECT 4111111111111111#1"),
    ("db2", "SELECT 41111.5#", "SELECT ?.5#"),
    ("db2", "SELECT 4111.$1", "SELECT ?.$1"),
    ("db2", "SELECT 0x1F.1$1", "SELECT ?.1$1"),
    ("db2", "SELECT 4111111111111111.5#", "SELECT ?.5#"),
    ("db2", "SELECT 0xDEADBEEF.5$", "SELECT ?.5$"),
    ("db2", "SELECT 1.5e3.2#", "SELECT ?.2#"),
    ("db2", "SELECT 4111.5$x", "SELECT ?.5$x"),
    ("db2", "SELECT 12$, 34#", "SELECT 12$, 34#"),
    ("db2", "SELECT 4111111111111111$", "SELECT 4111111111111111$"),
    ("db2", "SELECT 0x4111$", "SELECT 0x4111$"),
    ("db2", "SELECT a FROM t WHERE c = 4111111111111111", "SELECT a FROM t WHERE c = ?"),
    ("db2", "SELECT a FROM t WHERE c = 0x4111FF.2", "SELECT a FROM t WHERE c = ?.2"),
    ("db2", "SELECT $1984.37", "SELECT $1984.37"),
    ("db2", "SELECT $ 1984.37", "SELECT $ ?"),
    ("db2", "SELECT $4111.5", "SELECT $4111.5"),
    ("db2", "SELECT a FROM t ORDER BY 1, 2", "SELECT a FROM t ORDER BY 1, 2"),
    ("db2", "SELECT a FROM t GROUP BY 2, 3", "SELECT a FROM t GROUP BY 2, 3"),
    ("db2", "SELECT a FROM t ORDER BY 4111111111111111, 2", "SELECT a FROM t ORDER BY ?, 2"),
    ("db2", "SELECT a FROM t ORDER BY a DESC, 3", "SELECT a FROM t ORDER BY a DESC, 3"),
    ("db2", "SELECT 1.5e3", "SELECT ?"),
    ("db2", "SELECT .5", "SELECT ?"),
    ("db2", "SELECT 5.", "SELECT ?"),
    (
        "db2",
        "SELECT a FROM t WHERE c BETWEEN 1234 AND 5678",
        "SELECT a FROM t WHERE c BETWEEN ? AND ?",
    ),
    ("db2", "SELECT a FROM t LIMIT 10 OFFSET 20", "SELECT a FROM t LIMIT 10 OFFSET 20"),
    ("db2", "SELECT t0x1F, a.1 FROM t", "SELECT t0x1F, a.1 FROM t"),
    ("db2", "SELECT 4111.5#x", "SELECT ?.5#x"),
    ("db2", "SELECT 1e5#", "SELECT 1e5#"),
    ("db2", "SELECT 7$a", "SELECT 7$a"),
    ("db2", "SELECT 4111111111111111#1", "SELECT 4111111111111111#1"),
]


def _twice(sql: str, engine: str) -> str:
    once = redact_literals(sql, engine)
    assert redact_literals(once, engine) == once
    return once


class TestNumberLiteralsAreConsumedWhole:
    """No digit of a number literal survives next to a placeholder, and a second pass changes nothing (#1715)."""

    @pytest.mark.parametrize("engine", ALL_ENGINES)
    @pytest.mark.parametrize(
        "literal",
        ["0x1F.1", "1.5e3.2", "1.5e+3.2", "0x1F.1e3", "0x1F1.5e3", ".5e3.2", "1e5.2.3"],
    )
    def test_hex_and_exponent_with_a_fraction_tail(self, engine: str, literal: str) -> None:
        assert _twice(f"SELECT {literal}", engine) == f"SELECT {MARKS[engine]}"

    @pytest.mark.parametrize("engine", ["mysql", "postgresql"])
    @pytest.mark.parametrize("literal", ["0b101", "0b101.1"])
    def test_binary_number_literals(self, engine: str, literal: str) -> None:
        assert _twice(f"SELECT {literal}", engine) == f"SELECT {MARKS[engine]}"

    def test_postgresql_reads_0b_in_either_case(self) -> None:
        assert _twice("SELECT 0B101", "postgresql") == "SELECT $1"

    @pytest.mark.passes_on_main  # a name, as on main
    def test_mysql_0b_is_lower_case_only(self) -> None:
        # ``0B101`` is a name on MySQL, like ``1_000``.
        assert _twice("SELECT 0B101", "mysql") == "SELECT 0B101"

    @pytest.mark.parametrize("engine", GLUING_ENGINES)
    def test_0b_is_a_number_and_an_alias_elsewhere(self, engine: str) -> None:
        # ``0b101`` reads as ``0 AS b101`` here; the alias holds the digits, so it goes too.
        assert _twice("SELECT 0b101", engine) == f"SELECT {MARKS[engine]}"

    @pytest.mark.passes_on_main  # what must not change
    @pytest.mark.parametrize("engine", ALL_ENGINES)
    @pytest.mark.parametrize(
        "text", ["SELECT b'101'", "SELECT B'101'", "SELECT X'1F'", "SELECT x'1f'"]
    )
    def test_quoted_bit_and_hex_strings_lose_their_value(self, engine: str, text: str) -> None:
        once = _twice(text, engine)
        assert "101" not in once and "1F" not in once.upper().replace("X'", "")

    @pytest.mark.passes_on_main  # what must not change
    @pytest.mark.parametrize("engine", ALL_ENGINES)
    @pytest.mark.parametrize(
        "text",
        [
            "SELECT t0x1F FROM t",
            "SELECT t0b101 FROM t",
            "SELECT t0o17, t1_000 FROM t",
            "SELECT a.t0x1F, col.1 FROM a",
            "SELECT x.0x1F, x.0b101, b.0b1, x.0o17 FROM x",
            "SELECT t.1e3, t.1_000, e3.2, schema.0x FROM t",
            "SELECT a.1abc, a.4111111111111111abc FROM a",
        ],
    )
    def test_identifiers_and_qualified_names_are_kept(self, engine: str, text: str) -> None:
        assert redact_literals(text, engine) == text

    @pytest.mark.passes_on_main  # what must not change
    @pytest.mark.parametrize("engine", ["mysql"])
    @pytest.mark.parametrize(
        "name", ["0b2", "0xZZ", "1_000", "4111_1111", "4111111111111111abc", "0o17"]
    )
    def test_mysql_keeps_a_word_that_starts_with_digits(self, engine: str, name: str) -> None:
        sql = f"SELECT {name} FROM t"
        assert _twice(sql, engine) == sql

    @pytest.mark.parametrize("literal", ["$1.5e3.2", "$1.5e3", "$1.5.2", "$ 19.99e2.5"])
    def test_sqlserver_money_literal_with_a_tail(self, literal: str) -> None:
        assert _twice(f"SELECT {literal}", "sqlserver") == "SELECT ?"


class TestNonDecimalAndSeparatedNumbers:
    """PostgreSQL 16 literals (``0o17``, ``_`` separators) and numbers glued to a word (#1715)."""

    @pytest.mark.parametrize(
        "literal",
        [
            "4111_1111_1111_1111",
            "0xFFFF_FFFF",
            "0b1010_1010",
            "0o17",
            "0O1_7",
            "1_000.5",
            "1_000e1_0",
            "1.000_000",
            "1_000.5.2",
        ],
    )
    @pytest.mark.parametrize("engine", GLUING_ENGINES)
    def test_postgresql_16_literals_leave_no_digit(self, engine: str, literal: str) -> None:
        once = _twice(f"WHERE c = {literal}", engine)
        assert once == f"WHERE c = {MARKS[engine]}"

    @pytest.mark.passes_on_main  # what must not change
    @pytest.mark.parametrize("literal", ["4111_1111_1111_1111", "1_000", "0xFFFF_FFFF", "0o17"])
    def test_mysql_keeps_them_as_names(self, literal: str) -> None:
        sql = f"WHERE c = {literal}"
        assert _twice(sql, "mysql") == sql

    @pytest.mark.parametrize("engine", GLUING_ENGINES)
    @pytest.mark.parametrize("word", ["abc", "x1", "days"])
    def test_a_number_glued_to_a_word_is_a_number_and_an_alias(
        self, engine: str, word: str
    ) -> None:
        once = _twice(f"WHERE c = 4111111111111111{word}", engine)
        assert "4111" not in once
        assert once.startswith(f"WHERE c = {MARKS[engine]}")
        assert once.endswith(word)

    @pytest.mark.parametrize("engine", GLUING_ENGINES)
    def test_no_digit_stays_beside_the_placeholder(self, engine: str) -> None:
        once = _twice("SELECT 0x1F.1abc", engine)
        assert once == f"SELECT {MARKS[engine]} abc"

    @pytest.mark.passes_on_main  # what must not change
    @pytest.mark.parametrize("engine", GLUING_ENGINES)
    def test_a_number_before_a_dollar_or_hash_is_kept_whole(self, engine: str) -> None:
        sql = "SELECT 12$, 34#"
        assert _twice(sql, engine) == sql

    @pytest.mark.passes_on_main  # what must not change
    @pytest.mark.parametrize("engine", GLUING_ENGINES)
    @pytest.mark.parametrize("text", ["SELECT 1ORDER BY ORDER BY 1", "SELECT 1EXEC X'a'"])
    def test_a_number_glued_to_a_keyword_reads_the_same_on_a_second_pass(
        self, engine: str, text: str
    ) -> None:
        _twice(text, engine)


class TestMoneyGluedToAWord:
    """SQL Server's money literal ends like a number token (#1715)."""

    @pytest.mark.parametrize(
        "literal,word",
        [
            ("$1.5", "x"),
            ("$1.5e3", "x"),
            ("$1.41111111", "abc"),
            ("$4111111111111111", "abc"),
            ("$4111111111111111", "ORDER BY a"),
            ("$ 4111111111111111", "abc"),
        ],
    )
    def test_the_whole_money_number_goes_and_the_word_stays(self, literal: str, word: str) -> None:
        once = _twice(f"SELECT {literal}{word}", "sqlserver")
        assert once == f"SELECT ? {word}"

    @pytest.mark.parametrize("literal", ["$4111_1111_1111_1111", "$1.5e3.2", "$1984.37"])
    def test_money_groups_and_tails(self, literal: str) -> None:
        assert _twice(f"SELECT {literal}", "sqlserver") == "SELECT ?"


class TestShorterReadingWhereADollarOrHashFollows:
    """Digits main redacted stay redacted where a ``$`` or ``#`` follows the number (#1715)."""

    @pytest.mark.passes_on_main  # what main already redacts or keeps
    @pytest.mark.parametrize("engine", ["oracle", "db2"])
    def test_decimal_before_a_hash(self, engine: str) -> None:
        assert "41111" not in _twice("SELECT 41111.5#", engine)

    @pytest.mark.passes_on_main  # what main already redacts or keeps
    @pytest.mark.parametrize("engine", ["sqlserver", "db2"])
    def test_decimal_before_a_dollar(self, engine: str) -> None:
        assert "4111" not in _twice("SELECT 4111.$1", engine)

    @pytest.mark.passes_on_main  # what main already redacts or keeps
    def test_hex_before_a_dollar_on_postgresql(self) -> None:
        once = _twice("SELECT 0x1F.1$1", "postgresql")
        assert "x1F" not in once and "0x" not in once

    @pytest.mark.passes_on_main  # what main already redacts or keeps
    @pytest.mark.parametrize(("engine", "text", "main"), _MAIN_REDACTIONS)
    def test_no_input_main_redacted_keeps_more_digits(
        self, engine: str, text: str, main: str
    ) -> None:
        def kept(out: str) -> int:
            return sum(ch.isdigit() for ch in _PLACEHOLDER_RE.sub("", out))

        assert kept(redact_literals(text, engine)) <= kept(main), (engine, text)


class TestPositionsAreKeptOrRedactedTogether:
    """``ORDER BY``/``GROUP BY`` positions are kept up to the first replaced item, on both passes (#1715)."""

    @pytest.mark.passes_on_main  # what main already redacts or keeps
    @pytest.mark.parametrize("engine", ALL_ENGINES)
    def test_positions_are_kept(self, engine: str) -> None:
        sql = "SELECT a FROM t GROUP BY 2, 3 ORDER BY 1, 2 DESC"
        assert _twice(sql, engine) == sql

    @pytest.mark.parametrize(
        ("engine", "marks"),
        [
            ("mysql", "?, ?"),
            ("postgresql", "$1, $2"),
            ("sqlserver", "?, ?"),
            ("oracle", ":1, :2"),
            ("db2", "?, ?"),
        ],
    )
    def test_a_replaced_item_ends_the_positions(self, engine: str, marks: str) -> None:
        # Pass one used to keep the 2 and pass two (``ORDER BY ?, 2``) redacted it.
        assert _twice("SELECT a FROM t ORDER BY 0x1F, 2", engine) == (
            f"SELECT a FROM t ORDER BY {marks}"
        )

    @pytest.mark.parametrize("engine", ALL_ENGINES)
    @pytest.mark.parametrize(
        "text",
        [
            "SELECT a FROM t ORDER BY 4111111111111111, 2",
            "SELECT a FROM t ORDER BY a, 1.5, 2",
            "SELECT a FROM t GROUP BY 0x101, 2",
        ],
    )
    def test_both_passes_agree(self, engine: str, text: str) -> None:
        assert "4111" not in _twice(text, engine)

    @pytest.mark.parametrize("engine", GLUING_ENGINES)
    def test_a_number_glued_to_a_word_is_no_position(self, engine: str) -> None:
        once = _twice("SELECT a FROM t ORDER BY 0x1F,4111TOP", engine)
        assert "4111" not in once
