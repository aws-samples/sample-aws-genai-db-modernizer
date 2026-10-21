"""Bulk-load statements name their real table and count as writes (#777).

PostgreSQL's ``COPY t FROM STDIN`` and MySQL's ``LOAD DATA ... INTO TABLE t``
load rows into ``t``. The regex table scan read them as tables named ``STDIN``
and ``TABLE``, and both were typed ``OTHER``, so routing saw a read of a table
that does not exist. They are application bulk I/O, not utility statements
(#774), so the collectors now attribute them to ``t`` and type them as an
``INSERT`` (the same way a MySQL ``REPLACE`` is), on both dialects.
"""

from __future__ import annotations

import pytest

from src.tools.sql.bulk_load import bulk_load_table, copy_table, is_bulk_load
from src.tools.sql.facade import table_references
from src.tools.sql.regex_tables import regex_table_refs
from tests.unit.tools.sql._timing import assert_linear

BULK_LOADS = [
    # (sql, (qualifiers, table))
    ("COPY wp_posts FROM STDIN", ([], "wp_posts")),
    ("copy wp_posts from stdin", ([], "wp_posts")),
    ("COPY public.wp_posts (id, title) FROM STDIN WITH CSV", (["public"], "wp_posts")),
    (
        'COPY "public"."wp_posts" FROM \'/tmp/posts.csv\' WITH (FORMAT csv)',
        (["public"], "wp_posts"),
    ),
    ("COPY wp_posts FROM PROGRAM 'gunzip -c posts.gz'", ([], "wp_posts")),
    ("/* importer */ COPY wp_posts FROM STDIN", ([], "wp_posts")),
    ("LOAD DATA LOCAL INFILE 'x.csv' INTO TABLE wp_posts", ([], "wp_posts")),
    ("LOAD DATA INFILE ? INTO TABLE wp_postmeta FIELDS TERMINATED BY ?", ([], "wp_postmeta")),
    (
        "LOAD DATA LOW_PRIORITY LOCAL INFILE 'x' REPLACE INTO TABLE shop.wp_posts CHARACTER SET utf8",
        (["shop"], "wp_posts"),
    ),
    (
        "LOAD DATA CONCURRENT INFILE 'x' IGNORE INTO TABLE `wp_posts` PARTITION (p0)",
        ([], "wp_posts"),
    ),
    (
        "LOAD XML LOCAL INFILE 'posts.xml' INTO TABLE wp_posts ROWS IDENTIFIED BY ?",
        ([], "wp_posts"),
    ),
    # A file name that looks like SQL is still only a file name.
    ("LOAD DATA INFILE '/tmp/from users.csv' INTO TABLE wp_posts", ([], "wp_posts")),
    ("-- nightly\nload data local infile ? into table wp_posts", ([], "wp_posts")),
    # A quoted name is read whole: spaces, hyphens, dots and doubled quotes.
    ('COPY "my table" FROM STDIN', ([], "my table")),
    ('COPY public."my table" (a, b) FROM STDIN', (["public"], "my table")),
    ('COPY "a.b" FROM STDIN', ([], "a.b")),
    ('COPY "say ""hi""" FROM STDIN', ([], 'say "hi"')),
    ("LOAD DATA INFILE 'f' INTO TABLE `my-table`", ([], "my-table")),
    ("LOAD DATA INFILE 'f' INTO TABLE `shop db`.`my-table`", (["shop db"], "my-table")),
    # A hint comment between the keywords is skipped.
    ("LOAD DATA /*+ x */ INFILE 'f' INTO TABLE wp_posts", ([], "wp_posts")),
    (
        "LOAD /* a */ DATA LOCAL /* b */ INFILE ? /* c */ INTO /* d */ TABLE wp_posts",
        ([], "wp_posts"),
    ),
]

NOT_BULK_LOADS = [
    # An export reads the table: not a write.
    "COPY wp_posts TO STDOUT",
    "COPY (SELECT status, count(*) FROM wp_posts GROUP BY status) TO STDOUT WITH CSV",
    "SELECT * FROM wp_posts WHERE copy_id = 1",
    "SELECT 'COPY wp_posts FROM STDIN'",
    "INSERT INTO wp_posts (id) VALUES (1)",
    "-- COPY wp_posts FROM STDIN\nSELECT 1",
    "SELECT load_data FROM t",
    "",
]


@pytest.mark.parametrize("sql,target", BULK_LOADS)
def test_a_bulk_load_names_its_table(sql, target):
    assert is_bulk_load(sql) is True
    assert bulk_load_table(sql) == target


@pytest.mark.parametrize("sql", NOT_BULK_LOADS)
def test_other_statements_are_not_bulk_loads(sql):
    assert is_bulk_load(sql) is False
    assert bulk_load_table(sql) is None


@pytest.mark.parametrize("sql,target", BULK_LOADS)
def test_the_regex_fallback_reads_only_the_target_table(sql, target):
    # The regex used to read FROM STDIN as STDIN and INTO TABLE as TABLE.
    assert regex_table_refs(sql) == [target]


def test_only_the_first_statement_is_read():
    assert bulk_load_table("COPY t FROM '/x'; SELECT 1 FROM z") == ([], "t")


def test_a_copy_export_names_its_table_as_a_read():
    assert copy_table("COPY wp_posts TO STDOUT") == ([], "wp_posts")
    assert copy_table('COPY "my table" TO STDOUT') == ([], "my table")
    assert copy_table("COPY (SELECT 1 FROM wp_posts) TO STDOUT") is None


@pytest.mark.parametrize(
    "make",
    [
        lambda n: "LOAD DATA " + "/* x */ " * (n // 8) + "INFILE",
        lambda n: "LOAD DATA /*" + "*" * n,
        lambda n: "COPY " + '"' * n,
    ],
    ids=["many comments", "unterminated comment", "many quotes"],
)
def test_long_input_does_not_backtrack(make):
    """Cost grows linearly up to 200k characters, in CPU time relative to the
    same shape at an eighth of the length (#1230): a wall-clock bound flaked."""
    assert_linear(make, bulk_load_table, sizes=(25_000, 200_000))


def test_the_growth_check_catches_a_quadratic_rescan():
    """The check above still fails quadratic code on these inputs: the same scan
    plus a rescan of the rest of the text from every 25th character (#1230).

    The quadratic part must dominate the small run, or one slow sample on a
    loaded runner makes it read as linear (#1304). ``str.find`` of one
    character is a vectorized ``memchr``, about 15x cheaper per character than
    ``str.count``, so the old rescan from every other character left the linear
    part (the Python loop and the scan) a third of the small run: a ratio under
    x50 instead of x64. This rescan runs about a twelfth of the loop iterations
    and reads near x58, against the x22.6 limit."""

    def rescanning(sql: str):
        for i in range(0, len(sql), 25):
            sql.count("\0", i)
        return bulk_load_table(sql)

    with pytest.raises(AssertionError, match="not linear"):
        assert_linear(
            lambda n: "LOAD DATA " + "/* x */ " * (n // 8) + "INFILE",
            rescanning,
            sizes=(25_000, 200_000),
        )


def test_the_regex_fallback_still_scans_a_copy_export_query():
    sql = "COPY (SELECT p.id FROM wp_posts p JOIN wp_users u ON u.id = p.author) TO STDOUT"
    assert regex_table_refs(sql) == [([], "wp_posts"), ([], "wp_users")]


@pytest.mark.parametrize(
    "sql,engine,written",
    [
        ("COPY wp_posts FROM STDIN", "postgresql", ["wp_posts"]),
        ("COPY public.wp_posts (id, title) FROM STDIN WITH CSV", "postgresql", ["public.wp_posts"]),
        ("LOAD DATA LOCAL INFILE 'x.csv' INTO TABLE wp_posts", "mysql", ["wp_posts"]),
        ("LOAD DATA INFILE ? INTO TABLE shop.wp_postmeta", "mysql", ["shop.wp_postmeta"]),
        ("LOAD XML LOCAL INFILE 'x.xml' INTO TABLE wp_posts", "mysql", ["wp_posts"]),
    ],
)
def test_the_facade_resolves_the_real_table(sql, engine, written):
    refs = table_references(sql, engine)
    assert [t.written for t in refs.tables] == written
