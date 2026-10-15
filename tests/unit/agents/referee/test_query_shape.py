"""``query_shape.is_single_table_crud``: the single-table CRUD fact (#690).

The fact ``aurora-anti-05`` ("no-relational-need") is documented on: "all
queries are single-table CRUD". A query fails it when it is an aggregate, a
window function, a CTE or subquery, a DISTINCT or GROUP BY, a pattern match,
a range or unkeyed write (a placeholder tautology such as ``$2=$3`` is not a
key), a table-wide ORDER BY or range read, or calls a function outside the
shared scalar allowlist. The SQL lists are shared with the detector tests in
``tests/unit/tools/analysis/test_aurora_anti_patterns.py``.
"""

import pytest

from src.agents.referee.query_shape import (
    CRUD,
    ORDERED_ONE_TO_MANY,
    RELATIONAL,
    is_primary_key_first_row_read,
    is_single_table_crud,
    primary_key_ordered_limit,
    single_table_access_shape,
)

SINGLE_TABLE_CRUD_SQL = [
    "SELECT * FROM t1 WHERE id = $1",
    "SELECT * FROM t1 WHERE id = $1 LIMIT $2",
    "SELECT id FROM t1 WHERE date = $1 AND user_agent = $2 LIMIT $3",
    "SELECT `a` , `b` FROM `t1` WHERE `id` IN (...) ORDER BY `seq` ASC",
    "SELECT * FROM t1 WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
    "SELECT * FROM t1 WHERE user_id = $1 AND created_at > $2",
    "SELECT * FROM t1 WHERE lower(email) = $1",
    "SELECT * FROM t1",
    "INSERT INTO t1 (id, val) VALUES ($1, $2)",
    'INSERT INTO "t1" ("id", "val") VALUES ($1, NOW())',
    "INSERT INTO `t1` ( `id` , `val` ) VALUES (...)",
    "INSERT INTO t1 (id, n) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET n = t1.n + 1",
    "UPDATE t1 SET count = count + $1 WHERE id = $2",
    "UPDATE `t1` SET `v` = ? WHERE `name` = ?",
    "DELETE FROM t1 WHERE id = $1",
    "DELETE FROM t1 WHERE id IN ($1, $2)",
    'SELECT "t1".* FROM "t1" WHERE "t1"."id" = $1 LIMIT $2',
    'UPDATE "t1" SET "v" = $1 WHERE "t1"."key" = $2',
    "SELECT * FROM t1 WHERE (a, b) IN (($1, $2))",
    "SELECT * FROM t1 WHERE user_id = $1 AND name LIKE 'abc%'",
    "SELECT * FROM t1 WHERE status = 'x' AND id = $1",
]

NOT_SINGLE_TABLE_CRUD_SQL = {
    "aggregate": "SELECT COUNT(*) FROM t1 WHERE post_id = $1",
    "group by": "SELECT kind, MAX(id) FROM t1 GROUP BY kind HAVING COUNT(*) > 1",
    "window function": "SELECT id, row_number() OVER (PARTITION BY a ORDER BY b) FROM t1",
    "cte": (
        "WITH ranked AS (SELECT id FROM t1 WHERE a = $1) "
        "DELETE FROM t1 WHERE id IN (SELECT id FROM ranked)"
    ),
    "subquery": "SELECT * FROM t1 WHERE id IN (SELECT ref_id FROM t1 WHERE a = $1)",
    "insert select": "INSERT INTO t1 (id, v) SELECT id, v FROM t1 WHERE a = $1",
    "range delete": "DELETE FROM t1 WHERE date < $1",
    "keyed range delete": "DELETE FROM t1 WHERE topic_id = $1 AND post_number > $2",
    "between update": "UPDATE t1 SET v = $1 WHERE created_at BETWEEN $2 AND $3",
    "unkeyed delete": "DELETE FROM t1",
    "unkeyed update": "UPDATE t1 SET flag = $1 WHERE flag IS NULL",
    "table-wide order by": "SELECT * FROM t1 ORDER BY id LIMIT $1",
    "range-only order by": "SELECT * FROM t1 WHERE id > $1 ORDER BY id LIMIT $2",
    "range-only read": "SELECT * FROM t1 WHERE created_at < $1",
    "engine-specific function": "SELECT * FROM t1 WHERE family(ip_address) = $1",
    "leading wildcard": "SELECT * FROM t1 WHERE name LIKE '%term%'",
    "placeholder tautology update": 'UPDATE "translation_overrides" SET "status" = $1 WHERE $2=$3',
    "literal tautology update": "UPDATE t1 SET status = $1 WHERE 1=1",
    "placeholder tautology delete": "DELETE FROM t1 WHERE $1 = $2",
    "mysql placeholder tautology": "UPDATE `t1` SET `v` = ? WHERE ? = ?",
    "not in with extra spaces": "UPDATE t1 SET v = $1 WHERE name NOT  IN ($2, $3)",
    "parameterised like": "SELECT * FROM t1 WHERE key = $1 AND value ILIKE $2",
    "parameterised like only": "SELECT * FROM t1 WHERE name LIKE $1",
    "literal prefix like delete": "DELETE FROM t1 WHERE name LIKE 'tmp_%'",
    "select distinct": "SELECT DISTINCT user_id FROM t1 WHERE topic_id = $1",
    "dedup group by": "SELECT user_id FROM t1 WHERE topic_id = $1 GROUP BY user_id",
}


def _q(sql: str) -> dict:
    return {
        "query_id": "q",
        "query_type": sql.split()[0].upper(),
        "tables_accessed": ["t1"],
        "has_joins": False,
        "query_text": sql,
    }


class TestIsSingleTableCrud:
    @pytest.mark.parametrize("sql", SINGLE_TABLE_CRUD_SQL)
    def test_point_and_simple_key_access_is_crud(self, sql):
        assert is_single_table_crud(_q(sql))

    @pytest.mark.parametrize(
        "sql", NOT_SINGLE_TABLE_CRUD_SQL.values(), ids=NOT_SINGLE_TABLE_CRUD_SQL.keys()
    )
    def test_relational_shapes_are_not_crud(self, sql):
        assert not is_single_table_crud(_q(sql))

    def test_a_join_is_not_single_table_crud(self):
        q = _q("SELECT * FROM t1 JOIN t2 ON t1.id = t2.ref WHERE t1.id = $1")
        q["tables_accessed"] = ["t1", "t2"]
        q["has_joins"] = True
        assert not is_single_table_crud(q)

    def test_a_string_literal_never_changes_the_verdict(self):
        # A '<' or a function call inside a literal is data, not a range or a call.
        assert is_single_table_crud(_q("UPDATE t1 SET note = 'family(x) < 3' WHERE id = $1"))


# The maintainer decision on #692: ``ORDER BY <primary key> LIMIT 1`` reads
# one row by the table's own key, so it is a key-value lookup and counts as
# CRUD -- for the schema's primary key only, never a hard-coded ``id``.
# ``ORDER BY <primary key> LIMIT n`` with n > 1 (or a bind parameter with no
# evidence it is 1) is an ordered 1:M read: neither CRUD nor relational.
PK_FIRST_ROW_SQL = [
    ("SELECT * FROM t1 ORDER BY id LIMIT 1", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id DESC LIMIT 1", ["id"], None),
    ('SELECT "t1".* FROM "t1" ORDER BY "t1"."id" ASC LIMIT $1', ["id"], 1),
    (
        'SELECT "unsubscribe_keys".* FROM "unsubscribe_keys" '
        'ORDER BY "unsubscribe_keys"."key" ASC LIMIT 1',
        ["key"],
        None,
    ),
    ("SELECT * FROM t1 ORDER BY a, b LIMIT ?", ["a", "b"], 1),
    ("SELECT TOP (1) * FROM [t1] ORDER BY [ID]", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id FETCH FIRST 1 ROW ONLY", ["id"], None),
    ("SELECT TOP 1 * FROM [t1] ORDER BY [ID]", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id FETCH NEXT 1 ROWS ONLY", ["id"], None),
    # #782: the count is optional in FETCH FIRST/NEXT ROW ONLY, and it means 1.
    ("SELECT * FROM t1 ORDER BY id FETCH FIRST ROW ONLY", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id FETCH NEXT ROWS ONLY", ["id"], None),
    # #782: T-SQL's only FETCH form needs an OFFSET clause; OFFSET 0 skips nothing.
    ("SELECT * FROM [t1] ORDER BY [ID] OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id OFFSET 0 ROWS FETCH FIRST ROW ONLY", ["id"], None),
    # #782: MySQL's LIMIT <offset>, <count>, and an explicit OFFSET 0.
    ("SELECT * FROM `t1` ORDER BY `id` LIMIT 0, 1", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET 0", ["id"], None),
    # #782 round 2: offsets compare as numbers, like limits.
    ("SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET 00", ["id"], None),
    ("SELECT * FROM [t1] ORDER BY [ID] OFFSET 00 ROWS FETCH NEXT 01 ROWS ONLY", ["id"], None),
    ("SELECT * FROM `t1` ORDER BY `id` LIMIT 00, 1", ["id"], None),
]

ORDERED_ONE_TO_MANY_SQL = {
    "limit 5": ("SELECT * FROM t1 ORDER BY id LIMIT 5", ["id"], None),
    "parameterised limit, no evidence": (
        'SELECT "t1".* FROM "t1" ORDER BY "t1"."id" ASC LIMIT $1',
        ["id"],
        None,
    ),
    "parameterised limit, bound to 5": ("SELECT * FROM t1 ORDER BY id LIMIT $1", ["id"], 5),
    "a literal limit ignores the bound value": (
        "SELECT * FROM t1 ORDER BY id LIMIT 5",
        ["id"],
        1,
    ),
    "unsubscribe_keys by its key, parameterised": (
        'SELECT "unsubscribe_keys".* FROM "unsubscribe_keys" '
        'ORDER BY "unsubscribe_keys"."key" ASC LIMIT $1',
        ["key"],
        None,
    ),
    "top n": ("SELECT TOP (50) * FROM [t1] ORDER BY [ID] DESC", ["id"], None),
    # Placeholder limits (no evidence the bound value is 1): already neutral before #782.
    "question-mark limit": ("SELECT * FROM t1 ORDER BY id LIMIT ?", ["id"], None),
    "numbered colon limit": ("SELECT * FROM t1 ORDER BY id LIMIT :1", ["id"], None),
    "named colon limit": ("SELECT * FROM t1 ORDER BY id LIMIT :n", ["id"], None),
    "at-sign limit": ("SELECT * FROM t1 ORDER BY id LIMIT @p", ["id"], None),
    "parameterised top": ("SELECT TOP (@p) * FROM [t1] ORDER BY [ID]", ["id"], None),
    "parameterised fetch": ("SELECT * FROM t1 ORDER BY id FETCH FIRST $1 ROWS ONLY", ["id"], None),
    # #782: TOP (1) PERCENT is a share of the table, not one row.
    "top (1) percent": ("SELECT TOP (1) PERCENT * FROM [t1] ORDER BY [ID]", ["id"], None),
    "top 1 percent": ("SELECT TOP 1 PERCENT * FROM [t1] ORDER BY [ID]", ["id"], None),
    "top (50) percent": ("SELECT TOP (50) PERCENT * FROM [t1] ORDER BY [ID]", ["id"], None),
    "top(1)percent, no spaces": ("SELECT TOP(1)PERCENT * FROM [t1] ORDER BY [ID]", ["id"], None),
    # #782 round 2: WITH TIES can return more than one row, so it is never a key lookup.
    "top 1 with ties": ("SELECT TOP 1 WITH TIES * FROM [t1] ORDER BY [ID]", ["id"], None),
    "top (1) with ties": ("SELECT TOP (1) WITH TIES * FROM [t1] ORDER BY [ID]", ["id"], None),
    "fetch first 1 rows with ties": (
        "SELECT * FROM t1 ORDER BY id FETCH FIRST 1 ROWS WITH TIES",
        ["id"],
        None,
    ),
    "offset 0 fetch next 1 row with ties": (
        "SELECT * FROM t1 ORDER BY id OFFSET 0 ROWS FETCH NEXT 1 ROW WITH TIES",
        ["id"],
        None,
    ),
    # #782: one row after a skip is a page of the key order, not a key lookup.
    "offset 5 fetch 1": (
        "SELECT * FROM [t1] ORDER BY [ID] OFFSET 5 ROWS FETCH NEXT 1 ROWS ONLY",
        ["id"],
        None,
    ),
    "parameterised offset fetch 1": (
        "SELECT * FROM [t1] ORDER BY [ID] OFFSET @skip ROWS FETCH NEXT 1 ROWS ONLY",
        ["id"],
        None,
    ),
    "offset 0 fetch 5": (
        "SELECT * FROM [t1] ORDER BY [ID] OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY",
        ["id"],
        None,
    ),
    "offset 0 parameterised fetch": (
        "SELECT * FROM [t1] ORDER BY [ID] OFFSET 0 ROWS FETCH NEXT @p ROWS ONLY",
        ["id"],
        None,
    ),
    "mysql limit 5, 1": ("SELECT * FROM `t1` ORDER BY `id` LIMIT 5, 1", ["id"], None),
    "mysql limit 0, 5": ("SELECT * FROM `t1` ORDER BY `id` LIMIT 0, 5", ["id"], None),
    "mysql limit ?, ?": ("SELECT * FROM `t1` ORDER BY `id` LIMIT ?, ?", ["id"], None),
    "limit 1 offset 5": ("SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET 5", ["id"], None),
    "limit 1 offset placeholder": ("SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET $1", ["id"], None),
    "a bound limit of 1 after a skip": (
        "SELECT * FROM t1 ORDER BY id LIMIT $1 OFFSET $2",
        ["id"],
        1,
    ),
    "offset before limit": ("SELECT * FROM t1 ORDER BY id OFFSET 5 LIMIT 1", ["id"], None),
}

RELATIONAL_ORDERED_SQL = {
    "hard-coded id is not the key": (
        'SELECT "unsubscribe_keys".* FROM "unsubscribe_keys" '
        'ORDER BY "unsubscribe_keys"."id" ASC LIMIT 1',
        ["key"],
        None,
    ),
    "no primary key in the schema": ("SELECT * FROM t1 ORDER BY id LIMIT 1", [], None),
    "non-key order (latest row)": (
        "SELECT * FROM t1 ORDER BY created_at DESC LIMIT 1",
        ["id"],
        None,
    ),
    "part of a composite key": ("SELECT * FROM t1 ORDER BY a LIMIT 1", ["a", "b"], None),
    "no limit": ("SELECT * FROM t1 ORDER BY id", ["id"], None),
    "offset, no limit": ("SELECT * FROM t1 ORDER BY id OFFSET 5 ROWS", ["id"], None),
    "filtered scan": ("SELECT * FROM t1 WHERE flag IS NULL ORDER BY id LIMIT 1", ["id"], None),
    "range then key order": ("SELECT * FROM t1 WHERE id > $1 ORDER BY id LIMIT 1", ["id"], None),
}


class TestPrimaryKeyFirstRowRead:
    @pytest.mark.parametrize("sql, primary_key, limit_value", PK_FIRST_ROW_SQL)
    def test_one_row_in_primary_key_order_is_crud(self, sql, primary_key, limit_value):
        assert is_primary_key_first_row_read(sql, primary_key, limit_value)
        assert single_table_access_shape(_q(sql), primary_key, limit_value) == CRUD
        assert is_single_table_crud(_q(sql), primary_key=primary_key, limit_value=limit_value)

    @pytest.mark.parametrize(
        "sql, primary_key, limit_value",
        ORDERED_ONE_TO_MANY_SQL.values(),
        ids=ORDERED_ONE_TO_MANY_SQL.keys(),
    )
    def test_n_rows_in_primary_key_order_is_an_ordered_one_to_many_read(
        self, sql, primary_key, limit_value
    ):
        assert not is_primary_key_first_row_read(sql, primary_key, limit_value)
        shape = single_table_access_shape(_q(sql), primary_key, limit_value)
        assert shape == ORDERED_ONE_TO_MANY == "ordered 1:M read"
        assert not is_single_table_crud(_q(sql), primary_key=primary_key, limit_value=limit_value)

    @pytest.mark.parametrize(
        "sql, primary_key, limit_value",
        RELATIONAL_ORDERED_SQL.values(),
        ids=RELATIONAL_ORDERED_SQL.keys(),
    )
    def test_other_ordered_reads_are_relational(self, sql, primary_key, limit_value):
        assert primary_key_ordered_limit(sql, primary_key) is None
        assert single_table_access_shape(_q(sql), primary_key, limit_value) == RELATIONAL

    def test_without_the_schema_key_no_ordered_unkeyed_read_qualifies(self):
        assert single_table_access_shape(_q("SELECT * FROM t1 ORDER BY id LIMIT 1")) == RELATIONAL

    def test_every_other_non_crud_example_is_relational(self):
        for sql in NOT_SINGLE_TABLE_CRUD_SQL.values():
            assert single_table_access_shape(_q(sql)) == RELATIONAL, sql
