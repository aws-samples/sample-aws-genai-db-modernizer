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
    locking_clause,
    primary_key_ordered_limit,
    single_table_access_shape,
    skips_locked_rows,
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
# The maintainer decision of 2026-10-15 (#857): an offset, literal or
# placeholder, does not change that -- one row at any offset is still CRUD.
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
    # Maintainer decision of 2026-10-15 (#782, #857): one row from a known table
    # at any offset, literal or placeholder, is a key-value lookup. These were
    # ordered 1:M reads under the 2026-10-14 reading ("any skip is neutral").
    ("SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET 5", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET $1", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id DESC LIMIT 1 OFFSET ?", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET 5 ROWS", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id OFFSET 5 LIMIT 1", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id OFFSET $1 LIMIT 1", ["id"], None),
    ("SELECT * FROM `t1` ORDER BY `id` LIMIT 5, 1", ["id"], None),
    ("SELECT * FROM `t1` ORDER BY `id` LIMIT ?, 1", ["id"], None),
    ("SELECT * FROM [t1] ORDER BY [ID] OFFSET 5 ROWS FETCH NEXT 1 ROWS ONLY", ["id"], None),
    ("SELECT * FROM [t1] ORDER BY [ID] OFFSET 5 ROWS FETCH NEXT 1 ROW ONLY", ["id"], None),
    ("SELECT * FROM [t1] ORDER BY [ID] OFFSET @skip ROWS FETCH NEXT 1 ROWS ONLY", ["id"], None),
    ("SELECT * FROM t1 ORDER BY id OFFSET $1 ROWS FETCH FIRST ROW ONLY", ["id"], None),
    ('SELECT "t1".* FROM "t1" ORDER BY "t1"."id" ASC LIMIT 1 OFFSET $1', ["id"], None),
    ("SELECT * FROM t1 ORDER BY a, b LIMIT 1 OFFSET $1", ["a", "b"], None),
    # A placeholder limit bound to 1 is a limit of 1, so the offset does not matter either.
    ("SELECT * FROM t1 ORDER BY id LIMIT $1 OFFSET $2", ["id"], 1),
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
    # 2026-10-15 decision (#857): an offset does not make a multi-row, a
    # placeholder-limit, a WITH TIES or a PERCENT read a key lookup.
    "offset 5 fetch next 1 row with ties": (
        "SELECT * FROM [t1] ORDER BY [ID] OFFSET 5 ROWS FETCH NEXT 1 ROW WITH TIES",
        ["id"],
        None,
    ),
    "parameterised offset fetch 5": (
        "SELECT * FROM [t1] ORDER BY [ID] OFFSET @skip ROWS FETCH NEXT 5 ROWS ONLY",
        ["id"],
        None,
    ),
    "parameterised offset, parameterised fetch": (
        "SELECT * FROM [t1] ORDER BY [ID] OFFSET @skip ROWS FETCH NEXT @take ROWS ONLY",
        ["id"],
        None,
    ),
    "limit 5 offset placeholder": ("SELECT * FROM t1 ORDER BY id LIMIT 5 OFFSET $1", ["id"], None),
    "placeholder limit, literal offset": (
        "SELECT * FROM t1 ORDER BY id LIMIT $1 OFFSET 5",
        ["id"],
        None,
    ),
    "placeholder limit and offset, no evidence": (
        "SELECT * FROM t1 ORDER BY id LIMIT $1 OFFSET $2",
        ["id"],
        None,
    ),
    "a bound limit of 5 after a skip": (
        "SELECT * FROM t1 ORDER BY id LIMIT $1 OFFSET $2",
        ["id"],
        5,
    ),
    "mysql limit 5, 10": ("SELECT * FROM `t1` ORDER BY `id` LIMIT 5, 10", ["id"], None),
    "mysql literal offset, placeholder count": (
        "SELECT * FROM `t1` ORDER BY `id` LIMIT 5, ?",
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
    "mysql limit 0, 5": ("SELECT * FROM `t1` ORDER BY `id` LIMIT 0, 5", ["id"], None),
    "mysql limit ?, ?": ("SELECT * FROM `t1` ORDER BY `id` LIMIT ?, ?", ["id"], None),
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
    # 2026-10-15 decision (#857): the offset rule needs the table's own key order.
    "non-key order, one row at an offset": (
        "SELECT * FROM t1 ORDER BY created_at DESC LIMIT 1 OFFSET $1",
        ["id"],
        None,
    ),
    "hard-coded id, one row at an offset": (
        "SELECT * FROM `t1` ORDER BY `id` LIMIT ?, 1",
        ["key"],
        None,
    ),
    "filtered, one row at an offset": (
        "SELECT * FROM t1 WHERE flag IS NULL ORDER BY id LIMIT 1 OFFSET $1",
        ["id"],
        None,
    ),
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


# #799 (ADR-031 Decision 3 and 14): a locking clause is classified by the
# read's shape, not by the clause. ``FOR UPDATE`` / ``FOR NO KEY UPDATE`` /
# ``FOR SHARE`` / ``FOR KEY SHARE`` (with ``OF``, ``NOWAIT``, ``SKIP LOCKED``),
# MySQL ``LOCK IN SHARE MODE`` and the SQL Server lock hints ``UPDLOCK``,
# ``HOLDLOCK``, ``XLOCK`` take a row lock inside a transaction and, except for
# the skip-locked forms below, return the same rows as the read without it. A
# locking point read is still a key lookup, a locking ordered or range read
# keeps its non-locking classification, and the locking is recorded on its own
# (``locking_clause``) as a transactional need. ``NOWAIT`` only changes the
# error when a row is already locked, so it is shape-neutral too.
LOCKING_SQL = {
    # (locking sql, the same read without the lock, primary key, the clause recorded)
    "keyed for update": (
        "SELECT * FROM t1 WHERE id = $1 FOR UPDATE",
        "SELECT * FROM t1 WHERE id = $1",
        ["id"],
        "FOR UPDATE",
    ),
    "keyed for share": (
        "SELECT * FROM t1 WHERE id = $1 FOR SHARE",
        "SELECT * FROM t1 WHERE id = $1",
        ["id"],
        "FOR SHARE",
    ),
    "keyed for no key update": (
        "SELECT * FROM t1 WHERE id = $1 FOR NO KEY UPDATE",
        "SELECT * FROM t1 WHERE id = $1",
        ["id"],
        "FOR NO KEY UPDATE",
    ),
    "keyed for key share": (
        "SELECT * FROM t1 WHERE id = $1 FOR KEY SHARE",
        "SELECT * FROM t1 WHERE id = $1",
        ["id"],
        "FOR KEY SHARE",
    ),
    "keyed lock in share mode": (
        "SELECT * FROM `t1` WHERE `id` = ? LOCK IN SHARE MODE",
        "SELECT * FROM `t1` WHERE `id` = ?",
        ["id"],
        "LOCK IN SHARE MODE",
    ),
    "keyed updlock hint": (
        "SELECT * FROM [t1] WITH (UPDLOCK) WHERE [ID] = @p1",
        "SELECT * FROM [t1] WHERE [ID] = @p1",
        ["id"],
        "WITH (UPDLOCK)",
    ),
    "keyed holdlock hint": (
        "SELECT * FROM [t1] WITH (HOLDLOCK) WHERE [ID] = @p1",
        "SELECT * FROM [t1] WHERE [ID] = @p1",
        ["id"],
        "WITH (HOLDLOCK)",
    ),
    "keyed updlock, rowlock hints": (
        "SELECT * FROM [t1] WITH (UPDLOCK, ROWLOCK) WHERE [ID] = @p1",
        "SELECT * FROM [t1] WHERE [ID] = @p1",
        ["id"],
        "WITH (UPDLOCK, ROWLOCK)",
    ),
    "keyed for update with limit 1": (
        "SELECT * FROM t1 WHERE id = $1 LIMIT 1 FOR UPDATE",
        "SELECT * FROM t1 WHERE id = $1 LIMIT 1",
        ["id"],
        "FOR UPDATE",
    ),
    "first row for update": (
        "SELECT * FROM t1 ORDER BY id LIMIT 1 FOR UPDATE",
        "SELECT * FROM t1 ORDER BY id LIMIT 1",
        ["id"],
        "FOR UPDATE",
    ),
    "first row desc for update; trailing semicolon": (
        "SELECT * FROM t1 ORDER BY id DESC LIMIT 1 FOR UPDATE;",
        "SELECT * FROM t1 ORDER BY id DESC LIMIT 1;",
        ["id"],
        "FOR UPDATE",
    ),
    "first row, lock before limit (PostgreSQL accepts either order)": (
        "SELECT * FROM t1 ORDER BY id FOR UPDATE LIMIT 1",
        "SELECT * FROM t1 ORDER BY id LIMIT 1",
        ["id"],
        "FOR UPDATE",
    ),
    "first row for no key update nowait": (
        "SELECT * FROM t1 ORDER BY id LIMIT 1 FOR NO KEY UPDATE NOWAIT",
        "SELECT * FROM t1 ORDER BY id LIMIT 1",
        ["id"],
        "FOR NO KEY UPDATE NOWAIT",
    ),
    "first row for update of a schema-qualified table, nowait": (
        'SELECT * FROM "s"."orders" ORDER BY "id" LIMIT 1 FOR UPDATE OF "s"."orders" NOWAIT',
        'SELECT * FROM "s"."orders" ORDER BY "id" LIMIT 1',
        ["id"],
        "FOR UPDATE NOWAIT",
    ),
    "keyed updlock rowlock hints, no comma": (
        "SELECT * FROM [t1] WITH (UPDLOCK ROWLOCK) WHERE [ID] = @p1",
        "SELECT * FROM [t1] WHERE [ID] = @p1",
        ["id"],
        "WITH (UPDLOCK, ROWLOCK)",
    ),
    "relational: an index hint reads as a function call, as on main": (
        "SELECT TOP 1 * FROM [t1] WITH (UPDLOCK, INDEX(pk_t1)) ORDER BY [ID]",
        "SELECT TOP 1 * FROM [t1] WITH (INDEX(pk_t1)) ORDER BY [ID]",
        ["id"],
        "WITH (UPDLOCK, INDEX)",
    ),
    "first row read at an offset, for update (#857)": (
        "SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET $1 FOR UPDATE",
        "SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET $1",
        ["id"],
        "FOR UPDATE",
    ),
    "first row for share": (
        "SELECT * FROM t1 ORDER BY id LIMIT 1 FOR SHARE",
        "SELECT * FROM t1 ORDER BY id LIMIT 1",
        ["id"],
        "FOR SHARE",
    ),
    "first row lock in share mode": (
        "SELECT * FROM `t1` ORDER BY `id` LIMIT 1 LOCK IN SHARE MODE",
        "SELECT * FROM `t1` ORDER BY `id` LIMIT 1",
        ["id"],
        "LOCK IN SHARE MODE",
    ),
    "first row fetch first for update": (
        "SELECT * FROM t1 ORDER BY id FETCH FIRST 1 ROW ONLY FOR UPDATE",
        "SELECT * FROM t1 ORDER BY id FETCH FIRST 1 ROW ONLY",
        ["id"],
        "FOR UPDATE",
    ),
    "first row top 1 updlock": (
        "SELECT TOP 1 * FROM [t1] WITH (UPDLOCK) ORDER BY [ID]",
        "SELECT TOP 1 * FROM [t1] ORDER BY [ID]",
        ["id"],
        "WITH (UPDLOCK)",
    ),
    "first row top (1) updlock, holdlock": (
        "SELECT TOP (1) * FROM [t1] WITH (UPDLOCK, HOLDLOCK) ORDER BY [ID]",
        "SELECT TOP (1) * FROM [t1] ORDER BY [ID]",
        ["id"],
        "WITH (UPDLOCK, HOLDLOCK)",
    ),
    "ordered 1:M limit 5 for update": (
        "SELECT * FROM t1 ORDER BY id LIMIT 5 FOR UPDATE",
        "SELECT * FROM t1 ORDER BY id LIMIT 5",
        ["id"],
        "FOR UPDATE",
    ),
    "ordered 1:M parameterised limit for update": (
        "SELECT * FROM t1 ORDER BY id LIMIT $1 FOR UPDATE",
        "SELECT * FROM t1 ORDER BY id LIMIT $1",
        ["id"],
        "FOR UPDATE",
    ),
    "relational range read for update": (
        "SELECT * FROM t1 WHERE created_at < $1 FOR UPDATE",
        "SELECT * FROM t1 WHERE created_at < $1",
        ["id"],
        "FOR UPDATE",
    ),
    "relational non-key order for update": (
        "SELECT * FROM t1 ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
        "SELECT * FROM t1 ORDER BY created_at DESC LIMIT 1",
        ["id"],
        "FOR UPDATE",
    ),
    "relational no limit for update": (
        "SELECT * FROM t1 ORDER BY id FOR UPDATE",
        "SELECT * FROM t1 ORDER BY id",
        ["id"],
        "FOR UPDATE",
    ),
    "relational aggregate lock in share mode": (
        "SELECT COUNT(*) FROM t1 WHERE post_id = ? LOCK IN SHARE MODE",
        "SELECT COUNT(*) FROM t1 WHERE post_id = ?",
        ["id"],
        "LOCK IN SHARE MODE",
    ),
}

# The shape each locking read must keep: the same as the read without the lock.
LOCKING_EXPECTED_SHAPE = {
    **{name: CRUD for name in LOCKING_SQL if name.startswith(("keyed", "first row"))},
    **{name: ORDERED_ONE_TO_MANY for name in LOCKING_SQL if name.startswith("ordered 1:M")},
    **{name: RELATIONAL for name in LOCKING_SQL if name.startswith("relational")},
}

# Text that looks like a lock and is not one: a column named ``update`` or
# ``share`` (quoted, as it must be), ``FOR UPDATE`` in a string literal or a
# comment, a non-locking hint, ``FOR`` in another clause.
NOT_LOCKING_SQL = {
    "column named update": 'SELECT "update" FROM t1 WHERE id = $1',
    "column named update, mysql": "SELECT `update`, `share` FROM `t1` WHERE `id` = ?",
    "column named for update": 'SELECT * FROM t1 WHERE "for update" = $1',
    "for update in a string literal": "SELECT * FROM t1 WHERE note = 'FOR UPDATE' AND id = $1",
    "lock in share mode in a string literal": (
        "SELECT * FROM t1 WHERE note = 'x LOCK IN SHARE MODE' AND id = ?"
    ),
    "updlock in a string literal": "SELECT * FROM t1 WHERE note = 'WITH (UPDLOCK)' AND id = @p",
    "for update in a comment": "SELECT * FROM t1 WHERE id = $1 /* FOR UPDATE */",
    "for update in a line comment": "SELECT * FROM t1 WHERE id = $1 -- FOR UPDATE",
    "nolock hint": "SELECT * FROM [t1] WITH (NOLOCK) WHERE [ID] = @p1",
    "next value for": "SELECT NEXT VALUE FOR seq_orders",
    "for xml": "SELECT * FROM [t1] WHERE [ID] = @p1 FOR XML AUTO",
    "a plain read": "SELECT * FROM t1 WHERE id = $1",
}


class TestLockingReadsAreClassifiedByShape:
    @pytest.mark.parametrize("name", LOCKING_SQL.keys())
    def test_a_locking_read_keeps_the_shape_of_the_same_read_without_the_lock(self, name):
        sql, unlocked, primary_key, _clause = LOCKING_SQL[name]
        shape = single_table_access_shape(_q(sql), primary_key)
        assert shape == single_table_access_shape(_q(unlocked), primary_key)
        assert shape == LOCKING_EXPECTED_SHAPE[name]

    @pytest.mark.parametrize(
        "sql, unlocked, primary_key, clause", LOCKING_SQL.values(), ids=LOCKING_SQL.keys()
    )
    def test_the_locking_is_recorded_as_its_own_fact(self, sql, unlocked, primary_key, clause):
        assert locking_clause(sql) == clause
        assert locking_clause(unlocked) is None

    def test_a_locking_first_row_read_is_a_key_lookup(self):
        sql = "SELECT * FROM t1 ORDER BY id LIMIT 1 FOR UPDATE"
        assert is_primary_key_first_row_read(sql, ["id"])
        assert primary_key_ordered_limit(sql, ["id"]) == "1"
        assert is_single_table_crud(_q(sql), primary_key=["id"])

    def test_a_locking_ordered_read_keeps_its_limit(self):
        sql = "SELECT * FROM t1 ORDER BY id LIMIT 5 FOR SHARE"
        assert primary_key_ordered_limit(sql, ["id"]) == "5"

    @pytest.mark.parametrize("sql", NOT_LOCKING_SQL.values(), ids=NOT_LOCKING_SQL.keys())
    def test_text_that_only_looks_like_a_lock_is_not_one(self, sql):
        assert locking_clause(sql) is None

    def test_a_lock_word_in_a_literal_does_not_change_a_first_row_read(self):
        sql = "SELECT * FROM t1 ORDER BY id LIMIT 1 -- FOR UPDATE"
        assert single_table_access_shape(_q(sql), ["id"]) == CRUD
        assert locking_clause(sql) is None

    def test_an_empty_query_has_no_lock(self):
        assert locking_clause(None) is None
        assert locking_clause("") is None


# Review of #840: ``SKIP LOCKED`` (and T-SQL's ``READPAST``) DOES change which
# rows a read returns -- it skips every row another transaction holds, the
# work-queue claim pattern -- so the row is unknown. The 2026-10-14 rule for
# an unknown row applies: such a read is at most a neutral ordered 1:M read,
# never a key-value lookup. Relational evidence (a range, an aggregate, a
# non-key order) stays relational.
SKIP_LOCKED_SQL = {
    "first row for update skip locked": (
        "SELECT * FROM t1 ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
        ORDERED_ONE_TO_MANY,
        "FOR UPDATE SKIP LOCKED",
    ),
    "first row for update of, skip locked": (
        'SELECT "t1".* FROM "t1" ORDER BY "t1"."id" ASC LIMIT 1 FOR UPDATE OF "t1" SKIP LOCKED',
        ORDERED_ONE_TO_MANY,
        "FOR UPDATE SKIP LOCKED",
    ),
    "first row for share skip locked": (
        "SELECT * FROM t1 ORDER BY id FETCH FIRST 1 ROW ONLY FOR SHARE SKIP LOCKED",
        ORDERED_ONE_TO_MANY,
        "FOR SHARE SKIP LOCKED",
    ),
    "keyed for update skip locked": (
        "SELECT * FROM t1 WHERE id = $1 FOR UPDATE SKIP LOCKED",
        ORDERED_ONE_TO_MANY,
        "FOR UPDATE SKIP LOCKED",
    ),
    "keyed queue claim by status": (
        "SELECT * FROM t1 WHERE status = $1 LIMIT 1 FOR UPDATE SKIP LOCKED",
        ORDERED_ONE_TO_MANY,
        "FOR UPDATE SKIP LOCKED",
    ),
    # 2026-10-15 decision (#857): an offset makes a single-row read key-value,
    # but a skip-locked read at an offset still claims an unknown row.
    "one row at an offset, for update skip locked": (
        "SELECT * FROM t1 ORDER BY id LIMIT 1 OFFSET $1 FOR UPDATE SKIP LOCKED",
        ORDERED_ONE_TO_MANY,
        "FOR UPDATE SKIP LOCKED",
    ),
    "mysql one row at an offset, for update skip locked": (
        "SELECT * FROM `t1` ORDER BY `id` LIMIT 5, 1 FOR UPDATE SKIP LOCKED",
        ORDERED_ONE_TO_MANY,
        "FOR UPDATE SKIP LOCKED",
    ),
    "t-sql readpast, one row at an offset": (
        "SELECT * FROM [t1] WITH (UPDLOCK, READPAST) ORDER BY [ID]"
        " OFFSET @skip ROWS FETCH NEXT 1 ROW ONLY",
        ORDERED_ONE_TO_MANY,
        "WITH (UPDLOCK, READPAST)",
    ),
    "ordered 1:M skip locked queue read": (
        "SELECT * FROM t1 ORDER BY id LIMIT 10 FOR UPDATE SKIP LOCKED",
        ORDERED_ONE_TO_MANY,
        "FOR UPDATE SKIP LOCKED",
    ),
    "t-sql readpast first row": (
        "SELECT TOP 1 * FROM [t1] WITH (UPDLOCK, READPAST) ORDER BY [ID]",
        ORDERED_ONE_TO_MANY,
        "WITH (UPDLOCK, READPAST)",
    ),
    "t-sql readpast keyed": (
        "SELECT * FROM [t1] WITH (UPDLOCK, READPAST, ROWLOCK) WHERE [ID] = @p1",
        ORDERED_ONE_TO_MANY,
        "WITH (UPDLOCK, READPAST, ROWLOCK)",
    ),
    "relational range read skip locked": (
        "SELECT * FROM t1 WHERE created_at < $1 FOR UPDATE SKIP LOCKED",
        RELATIONAL,
        "FOR UPDATE SKIP LOCKED",
    ),
    "relational non-key order skip locked": (
        "SELECT * FROM t1 ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
        RELATIONAL,
        "FOR UPDATE SKIP LOCKED",
    ),
}


class TestSkipLockedIsNeverAKeyLookup:
    @pytest.mark.parametrize("name", SKIP_LOCKED_SQL.keys())
    def test_a_skip_locked_read_is_at_most_neutral(self, name):
        sql, shape, clause = SKIP_LOCKED_SQL[name]
        assert single_table_access_shape(_q(sql), ["id"]) == shape
        assert not is_single_table_crud(_q(sql), primary_key=["id"])
        assert not is_primary_key_first_row_read(sql, ["id"])
        assert locking_clause(sql) == clause

    def test_nowait_stays_shape_neutral(self):
        sql = "SELECT * FROM t1 ORDER BY id LIMIT 1 FOR UPDATE NOWAIT"
        assert single_table_access_shape(_q(sql), ["id"]) == CRUD
        assert is_primary_key_first_row_read(sql, ["id"])
        assert locking_clause(sql) == "FOR UPDATE NOWAIT"

    def test_readpast_alone_is_not_a_lock_but_still_skips_rows(self):
        sql = "SELECT TOP 1 * FROM [t1] WITH (READPAST) ORDER BY [ID]"
        assert locking_clause(sql) is None
        assert single_table_access_shape(_q(sql), ["id"]) == ORDERED_ONE_TO_MANY


class TestRecordedHintsAreAFixedVocabulary:
    def test_an_unknown_word_in_a_hint_list_is_not_recorded(self):
        sql = "SELECT * FROM [t1] WITH (UPDLOCK, CUSTOMERSECRET) WHERE [ID] = @p1"
        assert locking_clause(sql) == "WITH (UPDLOCK)"

    def test_an_index_hint_records_its_name_only(self):
        sql = "SELECT * FROM [t1] WITH (UPDLOCK, INDEX(ix_customer_name)) WHERE [ID] = @p1"
        assert locking_clause(sql) == "WITH (UPDLOCK, INDEX)"

    def test_a_non_locking_hint_list_records_nothing(self):
        assert locking_clause("SELECT * FROM [t1] WITH (NOLOCK, INDEX(pk)) WHERE [ID] = @p") is None


class TestTheAccessClassSupersedesTheTextCheck:
    """ADR-031 Decision 14 Amendment 5 (#728): the step-1 answer of a read follows its
    access-pattern class where the amendment says so, and nowhere else."""

    from src.agents.referee.access_pattern import (
        AD_HOC_FILTER,
        AGGREGATION,
        KEY_LOOKUP,
        KEY_RANGE,
        KEY_SCOPED,
        REFERENCE_READ,
        UNANCHORED_JOIN,
    )

    JOIN = {
        "query_id": "q",
        "query_type": "SELECT",
        "tables_accessed": ["t1", "t2"],
        "has_joins": True,
        "query_text": "SELECT * FROM t1 JOIN t2 ON t2.id = t1.t2_id WHERE t1.id = $1",
    }

    def test_a_key_anchored_join_is_neutral(self):
        assert single_table_access_shape(self.JOIN) == RELATIONAL
        assert single_table_access_shape(self.JOIN, access_class=self.KEY_SCOPED) == (
            ORDERED_ONE_TO_MANY
        )
        assert single_table_access_shape(self.JOIN, access_class=self.UNANCHORED_JOIN) == (
            RELATIONAL
        )

    @pytest.mark.parametrize(
        ("sql", "access_class"),
        [
            ("SELECT count(*) FROM t1 WHERE parent_id = $1", "KEY_SCOPED"),
            ("SELECT * FROM t1 WHERE id > $1", "KEY_RANGE"),
            ("SELECT * FROM t1 ORDER BY parent_id LIMIT 20", "KEY_RANGE"),
            ("SELECT DISTINCT parent_id FROM t1 WHERE parent_id = $1", "KEY_SCOPED"),
        ],
    )
    def test_a_relational_read_by_key_is_neutral(self, sql, access_class):
        query = _q(sql)
        assert single_table_access_shape(query) == RELATIONAL
        assert single_table_access_shape(query, access_class=getattr(self, access_class)) == (
            ORDERED_ONE_TO_MANY
        )

    def test_a_read_with_no_predicate_is_neutral_not_crud(self):
        query = _q("SELECT * FROM t1")
        assert single_table_access_shape(query) == CRUD
        for access_class in (self.KEY_RANGE, self.REFERENCE_READ):
            assert single_table_access_shape(query, access_class=access_class) == (
                ORDERED_ONE_TO_MANY
            )

    def test_a_key_lookup_that_only_deduplicates_is_crud(self):
        query = _q("SELECT DISTINCT name FROM t1 WHERE id = $1")
        assert single_table_access_shape(query) == RELATIONAL
        assert single_table_access_shape(query, access_class=self.KEY_LOOKUP) == CRUD

    @pytest.mark.parametrize(
        ("sql", "access_class"),
        [
            ("SELECT * FROM t1 WHERE id IN (SELECT id FROM t1)", "KEY_LOOKUP"),  # subquery
            ("WITH x AS (SELECT 1) SELECT * FROM t1 WHERE id = $1", "KEY_LOOKUP"),  # CTE
            ("SELECT * FROM t1 WHERE parent_id = $1 AND note LIKE '%a%'", "KEY_SCOPED"),
            (
                "SELECT row_number() OVER (ORDER BY id) FROM t1 WHERE parent_id = $1",
                "KEY_SCOPED",
            ),
            ("SELECT * FROM t1 WHERE total > $1", "AD_HOC_FILTER"),
            ("SELECT parent_id, count(*) FROM t1 GROUP BY parent_id", "AGGREGATION"),
        ],
    )
    def test_every_other_relational_reason_stays_relational(self, sql, access_class):
        query = _q(sql)
        assert single_table_access_shape(query, access_class=getattr(self, access_class)) == (
            RELATIONAL
        )

    def test_a_write_keeps_the_text_check(self):
        write = _q("UPDATE t1 SET total = 0 WHERE parent_id = $1 AND id < $2")
        assert single_table_access_shape(write, access_class=self.KEY_SCOPED) == RELATIONAL
        point = _q("DELETE FROM t1 WHERE id = $1")
        assert single_table_access_shape(point, access_class=self.KEY_LOOKUP) == CRUD


class TestSourceDialectInSqlHelpers:
    """Every helper that strips comments and literals reads them in the source's dialect (#1114)."""

    MYSQL_HASH = "SELECT id FROM t WHERE id = 1 # FOR UPDATE SKIP LOCKED\n"
    MYSQL_HASH_BACKTICK = "SELECT `a#b` FROM t WHERE id = 1 FOR UPDATE"
    PG_DOLLAR = "SELECT $$ FOR UPDATE $$ FROM t WHERE id = 1"

    def test_a_mysql_hash_comment_hides_a_locking_clause(self):
        assert locking_clause(self.MYSQL_HASH, "mysql") is None
        assert skips_locked_rows(self.MYSQL_HASH, "mysql") is False

    def test_the_hash_is_not_a_comment_outside_mysql(self):
        # Without the engine, or on another engine, `#` is not a comment: unchanged.
        assert locking_clause(self.MYSQL_HASH, None) == "FOR UPDATE SKIP LOCKED"
        assert locking_clause(self.MYSQL_HASH, "postgresql") == "FOR UPDATE SKIP LOCKED"

    def test_a_hash_inside_a_backtick_name_is_not_a_comment(self):
        assert locking_clause(self.MYSQL_HASH_BACKTICK, "mysql") == "FOR UPDATE"

    def test_a_postgres_dollar_string_hides_a_locking_clause(self):
        assert locking_clause(self.PG_DOLLAR, "postgresql") is None
        assert locking_clause(self.PG_DOLLAR, None) == "FOR UPDATE"

    def test_primary_key_order_reads_past_a_mysql_hash_comment(self):
        sql = "SELECT * FROM t ORDER BY id # WHERE x = 1\n LIMIT 1"
        assert primary_key_ordered_limit(sql, ["id"], "mysql") == "1"
        assert is_primary_key_first_row_read(sql, ["id"], None, "mysql") is True
        # Another engine reads `# WHERE` as code: a WHERE, so no ordered-limit read.
        assert primary_key_ordered_limit(sql, ["id"], "postgresql") is None

    def test_primary_key_order_keeps_a_hash_in_a_backtick_name(self):
        sql = "SELECT `a#b` FROM t ORDER BY id LIMIT 1"
        assert primary_key_ordered_limit(sql, ["id"], "mysql") == "1"

    def test_a_function_in_a_mysql_hash_comment_is_not_called(self):
        from src.shared.unsupported_pattern import non_scalar_function_names

        sql = "SELECT a FROM t # GROUP_CONCAT(x)\n"
        assert non_scalar_function_names(sql, "mysql") == set()
        assert non_scalar_function_names(sql, None) == {"group_concat"}

    def test_the_shape_of_a_query_follows_the_source_engine(self):
        from src.agents.referee.table_resolution import SourceScope

        query = {
            "query_type": "SELECT",
            "tables_accessed": ["t"],
            "query_text": "SELECT a FROM t WHERE id = ? # SELECT 1 FROM u\n",
        }
        mysql = SourceScope(engine="mysql")
        assert single_table_access_shape(query, source=mysql) == CRUD
        assert single_table_access_shape(query) == RELATIONAL

    def test_the_ownership_gate_reads_a_locking_clause_in_the_source_dialect(self):
        from src.agents.referee.cache_overlay import can_own, is_locking_read

        query = {"query_type": "SELECT", "query_text": self.MYSQL_HASH}
        assert is_locking_read(query, "mysql") is False
        assert can_own("opensearch", query, "mysql") is True
        # Another engine reads the `#` line as code: a skip-locked locking read.
        assert is_locking_read(query, "postgresql") is True
        assert can_own("opensearch", query, "postgresql") is False
