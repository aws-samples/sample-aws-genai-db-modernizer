"""Regression tests for upsert clauses misread as table references.

Issue: the UPDATE keyword in ON DUPLICATE KEY UPDATE / ON CONFLICT DO UPDATE
was captured by the table regex, so the following column (or SET) was reported
as a table.
"""

from src.tools.aws.rds_tools import _extract_tables as rds_extract_tables
from src.tools.database.mysql_tools import _extract_tables as mysql_extract_tables
from src.tools.database.postgres_tools import _extract_tables as postgres_extract_tables


def test_mysql_duplicate_key_update_does_not_capture_column():
    sql = (
        "INSERT INTO `wp_options` (`option_name`) VALUES (...)"
        " ON DUPLICATE KEY UPDATE `option_name` = VALUES(`option_name`)"
    )
    assert mysql_extract_tables(sql) == ["wp_options"]


def test_mysql_update_statement_still_extracts_table():
    sql = "UPDATE `wp_options` SET `option_value` = 'x' WHERE `option_name` = 'siteurl'"
    assert mysql_extract_tables(sql) == ["wp_options"]


def test_postgres_do_update_does_not_capture_set():
    sql = (
        'INSERT INTO "posts" ("id") VALUES (1)'
        ' ON CONFLICT ("id") DO UPDATE SET "title" = EXCLUDED."title"'
    )
    assert postgres_extract_tables(sql) == ["posts"]


def test_postgres_update_statement_still_extracts_table():
    sql = 'UPDATE "posts" SET "title" = \'x\' WHERE "id" = 1'
    assert postgres_extract_tables(sql) == ["posts"]


def test_postgres_lowercase_do_update_does_not_capture_set():
    sql = (
        "insert into posts (id) values (1)" " on conflict (id) do update set title = excluded.title"
    )
    assert postgres_extract_tables(sql) == ["posts"]


def test_postgres_refresh_materialized_view_concurrently():
    sql = "REFRESH MATERIALIZED VIEW CONCURRENTLY leaderboard_cache"
    assert postgres_extract_tables(sql) == ["leaderboard_cache"]


def test_postgres_refresh_materialized_view_without_concurrently():
    sql = "REFRESH MATERIALIZED VIEW leaderboard_cache"
    assert postgres_extract_tables(sql) == ["leaderboard_cache"]


def test_postgres_refresh_materialized_view_quoted_name():
    sql = 'REFRESH MATERIALIZED VIEW CONCURRENTLY "leaderboard_cache"'
    assert postgres_extract_tables(sql) == ["leaderboard_cache"]


def test_postgres_refresh_materialized_view_lowercase():
    sql = "refresh materialized view concurrently leaderboard_cache"
    assert postgres_extract_tables(sql) == ["leaderboard_cache"]


def test_rds_mysql_duplicate_key_update_does_not_capture_column():
    sql = (
        "INSERT INTO `wp_options` (`option_name`) VALUES (...)"
        " ON DUPLICATE KEY UPDATE `option_name` = VALUES(`option_name`)"
    )
    assert rds_extract_tables(sql) == ["wp_options"]


def test_rds_postgres_do_update_does_not_capture_set():
    # rds_tools' table regex only handles backtick-quoted identifiers (no
    # double-quote support), so this uses a bare identifier -- the point of
    # this test is that SET is not captured, not the quoting style.
    sql = "INSERT INTO posts (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"
    assert rds_extract_tables(sql) == ["posts"]


def test_rds_update_statement_still_extracts_table():
    sql = "UPDATE `wp_options` SET `option_value` = 'x' WHERE `option_name` = 'siteurl'"
    assert rds_extract_tables(sql) == ["wp_options"]


def test_rds_refresh_materialized_view_concurrently():
    sql = "REFRESH MATERIALIZED VIEW CONCURRENTLY leaderboard_cache"
    assert rds_extract_tables(sql) == ["leaderboard_cache"]


def test_rds_refresh_materialized_view_without_concurrently():
    sql = "REFRESH MATERIALIZED VIEW leaderboard_cache"
    assert rds_extract_tables(sql) == ["leaderboard_cache"]
