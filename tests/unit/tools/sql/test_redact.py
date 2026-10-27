"""Literal redaction of collected SQL text (#1625): ``src.tools.sql.redact``."""

from __future__ import annotations

import dataclasses
import functools
import json
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
