"""Version and feature gaps for the relational move (#471): every rule, both ways.

Each catalog rule has a positive case below (a collection fragment that must raise it)
and a negative one (the same fragment that must not: another source engine, another
target, the text inside a string literal, or a version inside the target's lines).
``test_every_rule_has_a_case`` keeps the table complete as rules are added.
"""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from src.shared.catalog.gaps import GAP_IMPACTS, SOURCE_ENGINES, GapRule, gap_rules
from src.tools.analysis.relational_gaps import detect_relational_gaps, normalized_data_type

VERSIONS = {"mysql": "8.0.36", "mariadb": "10.6.12", "postgresql": "16.4"}


def _collector(
    engine: str,
    *,
    version: str | None = None,
    queries: list[str] | None = None,
    tables: list[dict[str, Any]] | None = None,
    procedures: list[dict[str, Any]] | None = None,
    triggers: list[dict[str, Any]] | None = None,
    views: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if version is None:
        version = VERSIONS.get(engine, "15.0.4322.2")
    return {
        "metadata": {"source_database": {"engine": engine, "version": version}},
        "database_schema": {
            "tables": tables or [_table("db.t")],
            "procedures": procedures or [],
            "triggers": triggers or [],
            "views": views or [],
        },
        "queries": {
            "query_patterns": [
                {"query_id": f"q{i}", "query_text": text, "tables_accessed": ["db.t"]}
                for i, text in enumerate(queries or [])
            ]
        },
    }


def _table(table_id: str, *, types: tuple[str, ...] = ("int",), **extra: Any) -> dict[str, Any]:
    return {
        "table_id": table_id,
        "table_name": table_id.split(".")[-1],
        "columns": [{"column_name": f"c{i}", "data_type": t} for i, t in enumerate(types)],
        **extra,
    }


def _column_table(table_id: str = "db.t", **column: Any) -> dict[str, Any]:
    """A table with one column: ``column`` overrides its name, type and flags."""
    return {
        "table_id": table_id,
        "table_name": table_id.split(".")[-1],
        "columns": [{"column_name": "c0", "data_type": "int", **column}],
    }


def _index(**fields: Any) -> dict[str, Any]:
    return {"index_name": "ix", "columns": ["c0"], "is_unique": False, **fields}


def _ids(result: dict[str, Any] | None) -> set[str]:
    return {g["rule_id"] for g in (result or {}).get("gaps", [])}


def _gap(result: dict[str, Any] | None, rule_id: str) -> dict[str, Any]:
    return next(g for g in (result or {})["gaps"] if g["rule_id"] == rule_id)


# rule id -> (source engine, target, positive collector kwargs, negative collector kwargs)
# A negative of None means "the same fragment on a source engine the rule does not
# cover" (the always rules, and the routine rules whose evidence is the object itself).
CASES: dict[str, tuple[str, str, dict[str, Any], dict[str, Any] | None]] = {
    # version rules
    "mysql-older-major-version": (
        "mysql",
        "aurora_mysql",
        {"version": "5.7.44-log"},
        {"version": "8.0.36"},
    ),
    "mysql-newer-major-version": (
        "mysql",
        "aurora_mysql",
        {"version": "9.0.1"},
        {"version": "8.4.3"},
    ),
    "postgresql-older-major-version": (
        "postgresql",
        "aurora_postgresql",
        {"version": "12.18"},
        {"version": "14.1"},
    ),
    "postgresql-newer-major-version": (
        "postgresql",
        "aurora_postgresql",
        {"version": "19.0"},
        {"version": "18.1"},
    ),
    "source-version-unknown": (
        "mysql",
        "aurora_mysql",
        {"version": "unknown"},
        {"version": "8.0.36"},
    ),
    # MySQL family
    "mysql-non-innodb-storage-engine": (
        "mysql",
        "aurora_mysql",
        {"tables": [_table("db.t", storage_engine="MyISAM")]},
        {"tables": [_table("db.t", storage_engine="InnoDB")]},
    ),
    "mysql-removed-functions": (
        "mysql",
        "aurora_mysql",
        {"queries": ["SELECT user FROM accounts WHERE pass = PASSWORD(?)"]},
        {"queries": ["SELECT user FROM accounts WHERE pass = 'PASSWORD(?)'"]},
    ),
    "mysql-query-cache-hint": (
        "mysql",
        "aurora_mysql",
        {"queries": ["SELECT SQL_CACHE id FROM wp_options WHERE autoload = 'yes'"]},
        {"queries": ["SELECT SQL_NO_CACHE id FROM wp_options WHERE autoload = 'SQL_CACHE'"]},
    ),
    "mysql-sql-calc-found-rows": (
        "mysql",
        "aurora_mysql",
        {
            "queries": [
                "SELECT SQL_CALC_FOUND_ROWS id FROM wp_posts LIMIT 10",
                "SELECT `FOUND_ROWS` ( )",
            ]
        },
        {"queries": ["SELECT COUNT(*) FROM wp_posts -- SQL_CALC_FOUND_ROWS"]},
    ),
    "mysql-utf8mb3-character-set": (
        "mysql",
        "aurora_mysql",
        {
            "tables": [
                _table("db.t", collation="utf8mb3_general_ci"),
                _table("db.u", collation="utf8_bin"),
            ]
        },
        {"tables": [_table("db.t", collation="utf8mb4_0900_ai_ci")]},
    ),
    "mariadb-sequences": (
        "mariadb",
        "aurora_mysql",
        {"queries": ["INSERT INTO orders (id) VALUES (NEXT VALUE FOR order_seq)"]},
        {"queries": ["INSERT INTO orders (id) VALUES (NULL) /* NEXT VALUE FOR order_seq */"]},
    ),
    "mariadb-to-aurora-mysql": ("mariadb", "aurora_mysql", {}, None),
    # PostgreSQL
    "postgresql-untrusted-language": (
        "postgresql",
        "aurora_postgresql",
        {
            "procedures": [
                {"procedure_id": "f", "procedure_type": "FUNCTION", "language": "plpython3u"}
            ]
        },
        {
            "procedures": [
                {"procedure_id": "f", "procedure_type": "FUNCTION", "language": "plpgsql"}
            ]
        },
    ),
    "postgresql-timescaledb": (
        "postgresql",
        "aurora_postgresql",
        {"queries": ["SELECT time_bucket('5 minutes', ts) AS b, avg(v) FROM metrics GROUP BY b"]},
        {"queries": ["SELECT date_bin('5 minutes', ts, now()) AS time_bucket FROM metrics"]},
    ),
    "postgresql-citus": (
        "postgresql",
        "aurora_postgresql",
        {"queries": ["SELECT create_distributed_table('events', 'tenant_id')"]},
        {"queries": ["SELECT 'create_distributed_table(x)' AS note"]},
    ),
    "postgresql-server-file-access": (
        "postgresql",
        "aurora_postgresql",
        {
            "queries": [
                "COPY imports FROM PROGRAM 'gunzip -c /tmp/x.gz'",
                "SELECT pg_read_file('/etc/x')",
            ]
        },
        {"queries": ["COPY imports FROM STDIN", "SELECT 'pg_read_file(1)'"]},
    ),
    "postgresql-pgvector-half-sparse": (
        "postgresql",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("halfvec(1536)",))]},
        {"tables": [_table("db.t", types=("vector(1536)",))]},
    ),
    # SQL Server
    "sqlserver-tsql-routines": (
        "sqlserver",
        "aurora_postgresql",
        {
            "procedures": [{"procedure_id": "usp_A", "procedure_type": "PROCEDURE"}],
            "triggers": [{"trigger_id": "trg_A", "table_id": "db.t"}],
        },
        None,
    ),
    "sqlserver-top-clause": (
        "sqlserver",
        "aurora_postgresql",
        {"queries": ["SELECT TOP (10) [Name] FROM [Product] ORDER BY [Name]"]},
        {"queries": ["SELECT [Top] FROM [Product] ORDER BY [Name]"]},
    ),
    "sqlserver-table-and-query-hints": (
        "sqlserver",
        "aurora_postgresql",
        {"queries": ["SELECT [Status] FROM [SalesOrder] WITH (NOLOCK) WHERE [SalesOrderID] = @id"]},
        {"queries": ["SELECT [Status] FROM [SalesOrder] WHERE [Note] = 'NOLOCK'"]},
    ),
    "sqlserver-date-functions": (
        "sqlserver",
        "aurora_mysql",
        {"queries": ["DELETE FROM [UserSession] WHERE [ExpiresAt] < SYSUTCDATETIME()"]},
        {"queries": ["DELETE FROM [UserSession] WHERE [ExpiresAt] < @now"]},
    ),
    "sqlserver-builtin-functions": (
        "sqlserver",
        "aurora_mysql",
        {"queries": ["SELECT ISNULL([Phone], '') FROM [Customer]"]},
        {"queries": ["SELECT COALESCE([Phone], 'ISNULL()') FROM [Customer]"]},
    ),
    "sqlserver-identity-functions": (
        "sqlserver",
        "aurora_postgresql",
        {"queries": ["INSERT INTO [Orders] ([Total]) VALUES (@t); SELECT SCOPE_IDENTITY()"]},
        {"queries": ["INSERT INTO [Orders] ([Total]) OUTPUT inserted.[Id] VALUES (@t)"]},
    ),
    "sqlserver-apply-and-openjson": (
        "sqlserver",
        "aurora_postgresql",
        {
            "queries": [
                "SELECT j.[id] FROM [Cart] c CROSS APPLY OPENJSON(c.[Json]) WITH ([id] int) AS j"
            ]
        },
        {"queries": ["SELECT c.[id] FROM [Cart] c CROSS JOIN [Apply] a"]},
    ),
    "sqlserver-merge-to-mysql": (
        "sqlserver",
        "aurora_mysql",
        {
            "queries": [
                "MERGE [Stock] AS tgt USING [Delta] AS src ON tgt.[Id] = src.[Id] WHEN MATCHED THEN UPDATE SET tgt.[Q] = src.[Q];"
            ]
        },
        {"queries": ["UPDATE [Stock] SET [Q] = 1 WHERE [Kind] = 'MERGE'"]},
    ),
    "sqlserver-merge-to-postgresql": (
        "sqlserver",
        "aurora_postgresql",
        {
            "queries": [
                "MERGE [Stock] AS tgt USING [Delta] AS src ON tgt.[Id] = src.[Id] WHEN MATCHED THEN DELETE;"
            ]
        },
        {"queries": ["UPDATE [Stock] SET [Merge] = 1"]},
    ),
    "sqlserver-types-no-equivalent": (
        "sqlserver",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("hierarchyid", "sql_variant"))]},
        {"tables": [_table("db.t", types=("int",))]},
    ),
    "sqlserver-types-behave-differently": (
        "sqlserver",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("bit", "datetimeoffset(7)"))]},
        {"tables": [_table("db.t", types=("int",))]},
    ),
    "sqlserver-types-mapped": (
        "sqlserver",
        "aurora_mysql",
        {"tables": [_table("db.t", types=("nvarchar(50)", "datetime2(7)"))]},
        {"tables": [_table("db.t", types=("int", "decimal(10,2)"))]},
    ),
    "sqlserver-case-insensitive-comparison": ("sqlserver", "aurora_postgresql", {}, None),
    "sqlserver-babelfish-option": ("sqlserver", "aurora_postgresql", {}, None),
    # Oracle
    "oracle-plsql-routines": (
        "oracle",
        "aurora_postgresql",
        {"procedures": [{"procedure_id": "PKG_A", "procedure_type": "FUNCTION"}]},
        None,
    ),
    "oracle-connect-by": (
        "oracle",
        "aurora_postgresql",
        {
            "queries": [
                "SELECT employee_id FROM employees START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id"
            ]
        },
        {"queries": ["SELECT employee_id FROM employees WHERE note = 'CONNECT BY'"]},
    ),
    "oracle-rownum-rowid": (
        "oracle",
        "aurora_mysql",
        {"queries": ["SELECT * FROM orders WHERE ROWNUM <= 10"]},
        {"queries": ['SELECT "ROWNUM" FROM orders FETCH FIRST 10 ROWS ONLY']},
    ),
    "oracle-outer-join-operator": (
        "oracle",
        "aurora_postgresql",
        {"queries": ["SELECT e.name, d.name FROM emp e, dept d WHERE e.dept_id = d.id(+)"]},
        {"queries": ["SELECT e.name FROM emp e WHERE e.total = e.a + (e.b)"]},
    ),
    "oracle-sequences": (
        "oracle",
        "aurora_postgresql",
        {"queries": ["INSERT INTO orders (id) VALUES (order_seq.NEXTVAL)"]},
        {"queries": ["INSERT INTO orders (id, nextval) VALUES (:1, :2)"]},
    ),
    "oracle-builtin-functions": (
        "oracle",
        "aurora_postgresql",
        {"queries": ["SELECT NVL(phone, 'none'), SYSDATE FROM customers"]},
        {"queries": ["SELECT COALESCE(phone, 'NVL(x)') FROM customers"]},
    ),
    "oracle-empty-string-is-null": ("oracle", "aurora_mysql", {}, None),
    "oracle-types-no-equivalent": (
        "oracle",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("LONG RAW", "XMLTYPE"))]},
        {"tables": [_table("db.t", types=("NUMBER(10)",))]},
    ),
    "oracle-date-type": (
        "oracle",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("DATE",))]},
        {"tables": [_table("db.t", types=("TIMESTAMP(6)",))]},
    ),
    "oracle-types-mapped": (
        "oracle",
        "aurora_mysql",
        {"tables": [_table("db.t", types=("NUMBER(10,2)", "VARCHAR2(100)"))]},
        {"tables": [_table("db.t", types=("DATE",))]},
    ),
    # DB2
    "db2-sql-pl-routines": (
        "db2",
        "aurora_postgresql",
        {"procedures": [{"procedure_id": "P1", "procedure_type": "PROCEDURE"}]},
        None,
    ),
    "db2-isolation-clause": (
        "db2",
        "aurora_postgresql",
        {"queries": ["SELECT * FROM orders WHERE status = 'OPEN' WITH UR"]},
        {"queries": ["WITH ur AS (SELECT 1 AS x) SELECT x FROM ur"]},
    ),
    "db2-special-registers": (
        "db2",
        "aurora_mysql",
        {"queries": ["SELECT CURRENT TIMESTAMP FROM SYSIBM.SYSDUMMY1"]},
        {"queries": ["SELECT CURRENT_TIMESTAMP FROM orders"]},
    ),
    "db2-types-behave-differently": (
        "db2",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("DECFLOAT(34)", "VARGRAPHIC(20)"))]},
        {"tables": [_table("db.t", types=("DECIMAL(31,2)",))]},
    ),
    # shared
    "fetch-first-to-mysql": (
        "oracle",
        "aurora_mysql",
        {"queries": ["SELECT id FROM orders ORDER BY created FETCH FIRST 10 ROWS ONLY"]},
        {"queries": ["SELECT id FROM orders ORDER BY created OFFSET 0 ROWS"]},
    ),
    # MySQL and MariaDB to Aurora PostgreSQL (#1330)
    "mysql-on-duplicate-key-update": (
        "mysql",
        "aurora_postgresql",
        {
            "queries": [
                "INSERT INTO `t` (`a`) VALUES (?) ON DUPLICATE KEY UPDATE `a` = VALUES (`a`)"
            ]
        },
        {"queries": ["INSERT INTO `t` (`a`, `key`) VALUES ('ON DUPLICATE KEY', ?)"]},
    ),
    "mysql-replace-into": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["REPLACE INTO `wp_lookup` (`id`, `sku`) VALUES (...)"]},
        {"queries": ["SELECT REPLACE(`sku`, ?, ?) INTO @x FROM `wp_lookup`"]},
    ),
    "mysql-insert-ignore": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["INSERT IGNORE INTO `t` (`a`) VALUES (?)"]},
        {"queries": ["SELECT `a` FROM `t` IGNORE INDEX (`k`) WHERE `a` = ?"]},
    ),
    "mysql-auto-increment-columns": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_column_table(is_auto_increment=True)]},
        {"tables": [_column_table(is_auto_increment=False)]},
    ),
    "mysql-last-insert-id": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT LAST_INSERT_ID ( )"]},
        {"queries": ["SELECT `last_insert_id` FROM `t`"]},
    ),
    "mysql-unsigned-integers": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("bigint(20) unsigned", "int(10) unsigned zerofill"))]},
        {"tables": [_table("db.t", types=("bigint(20)",))]},
    ),
    "mysql-enum-and-set-types": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("enum('draft','publish')", "set('a','b')"))]},
        {"tables": [_table("db.t", types=("varchar(20)",))]},
    ),
    "mysql-zero-dates": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_column_table(data_type="datetime", default_value="0000-00-00 00:00:00")]},
        {"tables": [_column_table(data_type="datetime", default_value="1970-01-01 00:00:01")]},
    ),
    "mysql-case-insensitive-collations": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_table("db.t", collation="utf8mb4_unicode_520_ci")]},
        {"tables": [_table("db.t", collation="utf8mb4_bin")]},
    ),
    "mysql-mixed-case-identifiers": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_column_table(column_name="comment_post_ID")]},
        {"tables": [_column_table(column_name="comment_post_id")]},
    ),
    "mysql-limit-offset-comma": (
        "mysql",
        "aurora_postgresql",
        {
            "queries": [
                "SELECT `id` FROM `t` ORDER BY `id` LIMIT ?, ...",
                "SELECT id FROM t LIMIT 10, 20",
            ]
        },
        {"queries": ["SELECT `id` FROM `t` ORDER BY `id` LIMIT 20 OFFSET 10"]},
    ),
    "mysql-builtin-functions": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT IFNULL(`a`, 0), GROUP_CONCAT(`b`) FROM `t`"]},
        {"queries": ["SELECT COALESCE(`a`, 0), `group_concat` FROM `t`"]},
    ),
    "mysql-date-functions": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT COUNT(*) FROM `p` GROUP BY YEAR ( `d` ) , `SQL_TSI_MONTH` ( `d` )"]},
        {"queries": ["SELECT EXTRACT(YEAR FROM `d`) FROM `p` WHERE `d` > NOW() - INTERVAL 1 DAY"]},
    ),
    "mysql-json-functions": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT JSON_EXTRACT(`doc`, '$.a'), `doc` ->> '$.b' FROM `t`"]},
        {"queries": ["SELECT `doc` FROM `t` WHERE `n` - 1 > 0 AND `doc` = '->'"]},
    ),
    "mysql-functions-behave-differently": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT LENGTH(`name`) FROM `t`"]},
        {"queries": ["SELECT CHAR_LENGTH(`name`), `length` FROM `t`"]},
    ),
    "mysql-fulltext-search": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT `id` FROM `t` WHERE MATCH (`body`) AGAINST (? IN BOOLEAN MODE)"]},
        {"queries": ["SELECT `id` FROM `t` WHERE `body` LIKE ?"]},
    ),
    "mysql-spatial-types": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("point",))]},
        {"tables": [_table("db.t", types=("varchar(20)",))]},
    ),
    "mysql-regexp-operators": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT `id` FROM `t` WHERE `name` REGEXP ?"]},
        {"queries": ["SELECT `id` FROM `t` WHERE `name` LIKE 'REGEXP'"]},
    ),
    "mysql-only-operators": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT `id` FROM `t` WHERE `a` <=> ?"]},
        {"queries": ["SELECT `id` FROM `t` WHERE `a` <= ? AND `b` >= ? OR `c` | ?"]},
    ),
    "mysql-select-modifiers-and-hints": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT DISTINCTROW `id` FROM `t` FORCE INDEX (`k`)"]},
        {"queries": ["SELECT DISTINCT `id` FROM `t` WHERE `dual` = ?"]},
    ),
    "mysql-found-rows-to-postgresql": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SELECT SQL_CALC_FOUND_ROWS `id` FROM `t` LIMIT ?", "SELECT FOUND_ROWS ( )"]},
        {"queries": ["SELECT COUNT(*) FROM `t` -- SQL_CALC_FOUND_ROWS"]},
    ),
    "mysql-user-variables": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SET @total = ?", "SELECT @rank := @rank + 1 FROM `t`"]},
        {"queries": ["SELECT @@autocommit, '@x' FROM `t`"]},
    ),
    "mysql-system-variables": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SET SESSION `SQL_BIG_SELECTS` = ?", "SELECT @@SESSION.sql_mode"]},
        {"queries": ["UPDATE t SET session = ?", "SELECT @total"]},
    ),
    "mysql-show-and-admin-statements": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["SHOW FULL FIELDS FROM `wp_options`"]},
        {"queries": ["SELECT `id` FROM `t` ORDER BY `id` DESC"]},
    ),
    "mysql-load-data-and-outfile": (
        "mysql",
        "aurora_postgresql",
        {"queries": ["LOAD DATA LOCAL INFILE ? INTO TABLE `t`"]},
        {"queries": ["SELECT `id` FROM `t` WHERE `note` = 'LOAD DATA'"]},
    ),
    "mysql-double-quoted-strings": (
        "mysql",
        "aurora_postgresql",
        {"queries": ['SELECT `id` FROM `t` WHERE `status` = "publish"']},
        {"queries": ["SELECT `id` FROM `t` WHERE `status` = 'say \"hi\"'"]},
    ),
    "mysql-stored-routines-to-postgresql": (
        "mysql",
        "aurora_postgresql",
        {
            "procedures": [{"procedure_id": "p", "procedure_type": "PROCEDURE"}],
            "triggers": [{"trigger_id": "trg", "table_id": "db.t"}],
        },
        None,
    ),
    "mysql-types-mapped-to-postgresql": (
        "mysql",
        "aurora_postgresql",
        {"tables": [_table("db.t", types=("datetime", "longtext"))]},
        {"tables": [_table("db.t", types=("int", "varchar(20)"))]},
    ),
    "mysql-implicit-type-conversion": ("mysql", "aurora_postgresql", {}, None),
    # PostgreSQL to Aurora MySQL (#1330)
    "postgresql-returning-clause": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["INSERT INTO t (a) VALUES ($1) RETURNING id"]},
        {"queries": ["INSERT INTO t (a, returning_id) VALUES ('RETURNING', $1)"]},
    ),
    "postgresql-on-conflict": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["INSERT INTO t (a) VALUES ($1) ON CONFLICT (a) DO NOTHING"]},
        {"queries": ["INSERT INTO t (on_conflict) VALUES ($1)"]},
    ),
    "postgresql-merge-to-mysql": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a"]},
        {"queries": ["SELECT merge FROM t"]},
    ),
    "postgresql-data-modifying-ctes": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["WITH w AS (INSERT INTO t (a) VALUES ($1) RETURNING a) SELECT * FROM w"]},
        {"queries": ["WITH w AS (SELECT a FROM t) INSERT INTO u SELECT * FROM w"]},
    ),
    "postgresql-distinct-on": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT DISTINCT ON (user_id) * FROM t ORDER BY user_id"]},
        {"queries": ["SELECT DISTINCT user_id FROM t"]},
    ),
    "postgresql-ilike": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT id FROM t WHERE name ILIKE $1"]},
        {"queries": ["SELECT id FROM t WHERE name LIKE $1"]},
    ),
    "postgresql-regex-operators": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT id FROM t WHERE name ~ $1"]},
        {"queries": ["SELECT id FROM t WHERE name = '~'"]},
    ),
    "postgresql-sequences-to-mysql": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT nextval('orders_id_seq')"]},
        {"queries": ["SELECT next_val FROM t"]},
    ),
    "postgresql-serial-columns-to-mysql": (
        "postgresql",
        "aurora_mysql",
        {"tables": [_column_table(is_auto_increment=True)]},
        {"tables": [_column_table(is_auto_increment=None)]},
    ),
    "postgresql-arrays": (
        "postgresql",
        "aurora_mysql",
        {"tables": [_table("db.t", types=("_int4", "text[]"))]},
        {"tables": [_table("db.t", types=("int4", "text"))]},
    ),
    "postgresql-json-operators-and-functions": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT data ->> $1 FROM t WHERE data @> $2"]},
        {"queries": ["SELECT id FROM t WHERE a - 1 > $1 AND b >= $2"]},
    ),
    "postgresql-full-text-search": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT id FROM t WHERE search @@ to_tsquery($1)"]},
        {"queries": ["SELECT id FROM t WHERE body LIKE $1"]},
    ),
    "postgresql-index-methods": (
        "postgresql",
        "aurora_mysql",
        {"tables": [_table("db.t", indexes=[_index(index_type="gin")])]},
        {"tables": [_table("db.t", indexes=[_index(index_type="btree")])]},
    ),
    "postgresql-partial-indexes": (
        "postgresql",
        "aurora_mysql",
        {"tables": [_table("db.t", indexes=[_index(predicate="(deleted_at IS NULL)")])]},
        {"tables": [_table("db.t", indexes=[_index(predicate=None)])]},
    ),
    "postgresql-cast-operator": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT $1::int"]},
        {"queries": ["SELECT a[1:2] FROM t"]},
    ),
    "postgresql-concatenation-operator": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT a || b FROM t"]},
        {"queries": ["SELECT a | b FROM t"]},
    ),
    "postgresql-only-clauses": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT id FROM t WHERE a IS DISTINCT FROM b ORDER BY a DESC NULLS LAST"]},
        {"queries": ["SELECT DISTINCT id FROM t ORDER BY a DESC"]},
    ),
    "postgresql-aggregate-filter-and-window-frames": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT COUNT(*) FILTER (WHERE a > 0) FROM t"]},
        {"queries": ["SELECT COUNT(*) FROM t WHERE a > 0"]},
    ),
    "postgresql-builtin-functions": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT string_agg(name, ',') FROM t"]},
        {"queries": ["SELECT COALESCE(name, 'string_agg(x)') FROM t"]},
    ),
    "postgresql-date-functions": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT date_trunc('day', a) FROM t WHERE a > NOW() - INTERVAL $1"]},
        {"queries": ["SELECT EXTRACT(YEAR FROM a) FROM t"]},
    ),
    "postgresql-functions-behave-differently": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT length(name) FROM t"]},
        {"queries": ["SELECT char_length(name) FROM t"]},
    ),
    "postgresql-system-catalog": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT relname FROM pg_class WHERE relkind = $1"]},
        {"queries": ["SELECT table_name FROM information_schema.tables"]},
    ),
    "postgresql-session-settings": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SET SESSION timezone TO 'UTC'", "SHOW search_path"]},
        {"queries": ["UPDATE t SET session = $1"]},
    ),
    "postgresql-materialized-views": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["REFRESH MATERIALIZED VIEW CONCURRENTLY leaderboard"]},
        {"queries": ["SELECT * FROM leaderboard_view"]},
    ),
    "postgresql-listen-notify": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["NOTIFY jobs, 'ready'", "SELECT pg_notify($1, $2)"]},
        {"queries": ["SELECT notify FROM t"]},
    ),
    "postgresql-copy-statements": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["COPY t (a, b) FROM STDIN"]},
        {"queries": ["SELECT copy FROM t"]},
    ),
    "postgresql-plpgsql-routines-to-mysql": (
        "postgresql",
        "aurora_mysql",
        {
            "procedures": [
                {"procedure_id": "f", "procedure_type": "FUNCTION", "language": "plpgsql"}
            ]
        },
        {"procedures": [{"procedure_id": "f", "procedure_type": "FUNCTION", "language": "c"}]},
    ),
    "postgresql-extensions-to-mysql": (
        "postgresql",
        "aurora_mysql",
        {"procedures": [{"procedure_id": "f", "procedure_type": "FUNCTION", "language": "C"}]},
        {
            "procedures": [
                {"procedure_id": "f", "procedure_type": "FUNCTION", "language": "plpgsql"}
            ]
        },
    ),
    "postgresql-types-no-mysql-equivalent": (
        "postgresql",
        "aurora_mysql",
        {"tables": [_table("db.t", types=("inet", "int4range"))]},
        {"tables": [_table("db.t", types=("integer",))]},
    ),
    "postgresql-types-mapped-to-mysql": (
        "postgresql",
        "aurora_mysql",
        {"tables": [_table("db.t", types=("boolean", "jsonb"))]},
        {"tables": [_table("db.t", types=("integer", "character varying(20)"))]},
    ),
    "postgresql-double-quoted-identifiers": (
        "postgresql",
        "aurora_mysql",
        {"queries": ['SELECT "users".* FROM "users"']},
        {"queries": ["SELECT id FROM users WHERE note = 'say \"hi\"'"]},
    ),
    "postgresql-schemas-to-mysql": (
        "postgresql",
        "aurora_mysql",
        {"tables": [_table("db.app.t", schema_name="app")]},
        {"tables": [_table("db.public.t", schema_name="public")]},
    ),
    "postgresql-lateral-joins": (
        "postgresql",
        "aurora_mysql",
        {"queries": ["SELECT * FROM t, LATERAL (SELECT 1) x"]},
        {"queries": ["SELECT lateral_id FROM t"]},
    ),
    "postgresql-case-sensitive-comparison": ("postgresql", "aurora_mysql", {}, None),
}


def _rule(rule_id: str) -> GapRule:
    return next(r for r in gap_rules() if r.id == rule_id)


def test_every_rule_has_a_case() -> None:
    assert set(CASES) == {r.id for r in gap_rules()}


@pytest.mark.parametrize("method", ["spgist", "brin"])
def test_postgresql_index_methods_names_the_methods_added_in_293(method: str) -> None:
    """Typed ``other`` before #293, they matched the rule by accident; now by name."""
    collector = _collector(
        "postgresql", tables=[_table("db.t", indexes=[_index(index_type=method)])]
    )

    assert "postgresql-index-methods" in _ids(detect_relational_gaps(collector, "aurora_mysql"))


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_positive(rule_id: str) -> None:
    engine, target, positive, _ = CASES[rule_id]
    rule = _rule(rule_id)
    assert engine in rule.source_engines and target in rule.targets
    result = detect_relational_gaps(_collector(engine, **positive), target)
    assert rule_id in _ids(result)
    gap = _gap(result, rule_id)
    assert (gap["impact"], gap["reason"], gap["docs"]) == (rule.impact, rule.reason, rule.docs)
    if not (rule.match.always or rule.match.version):
        assert gap["tables"] or gap["queries"] or gap["objects"], "a found gap names its evidence"


@pytest.mark.parametrize("rule_id", sorted(r for r, c in CASES.items() if c[3] is not None))
def test_negative(rule_id: str) -> None:
    engine, target, _, negative = CASES[rule_id]
    assert negative is not None
    result = detect_relational_gaps(_collector(engine, **negative), target)
    assert rule_id not in _ids(result)


@pytest.mark.parametrize("rule_id", sorted(CASES))
def test_another_source_engine_never_raises_the_rule(rule_id: str) -> None:
    _, target, positive, _ = CASES[rule_id]
    rule = _rule(rule_id)
    for other in sorted(SOURCE_ENGINES - set(rule.source_engines)):
        result = detect_relational_gaps(_collector(other, **positive), target)
        assert rule_id not in _ids(result), other


@pytest.mark.parametrize("rule_id", sorted(r for r in CASES if len(_rule(r).targets) == 1))
def test_the_other_target_never_raises_a_single_target_rule(rule_id: str) -> None:
    engine, target, positive, _ = CASES[rule_id]
    other = "aurora_mysql" if target == "aurora_postgresql" else "aurora_postgresql"
    assert rule_id not in _ids(detect_relational_gaps(_collector(engine, **positive), other))


def _matcher_entries() -> list[tuple[str, str, str]]:
    """(rule id, matcher, entry) for every text matcher entry in the catalog."""
    return [
        (r.id, kind, entry)
        for r in gap_rules()
        for kind in ("calls", "words", "syntax", "statement_endings", "extension_calls")
        for entry in getattr(r.match, kind)
    ]


@pytest.mark.parametrize(("rule_id", "kind", "entry"), _matcher_entries())
def test_every_text_matcher_entry_matches_and_a_literal_does_not(
    rule_id: str, kind: str, entry: str
) -> None:
    rule = _rule(rule_id)
    engine, target = rule.source_engines[0], rule.targets[0]
    code = entry.lower().replace("{value}", "10")
    code += "(x)" if kind in ("calls", "extension_calls") else ""
    # A word the rule reads only after an operand (PostgreSQL's infix ~) gets one.
    before = "b" if kind == "words" and rule.match.unless_preceded_by else "b ="
    hit = _collector(engine, queries=[f"SELECT a FROM t WHERE {before} {code}"])
    assert rule_id in _ids(detect_relational_gaps(hit, target))
    literal = code.replace("'", "''")
    miss = _collector(engine, queries=[f"SELECT a FROM t WHERE b = '{literal}'"])
    assert rule_id not in _ids(detect_relational_gaps(miss, target))


def _statement_starts() -> list[tuple[str, str]]:
    return [(r.id, entry) for r in gap_rules() for entry in r.match.statement_starts]


@pytest.mark.parametrize(("rule_id", "entry"), _statement_starts())
def test_every_statement_start_counts_only_where_a_statement_begins(
    rule_id: str, entry: str
) -> None:
    """#1330: ``SHOW`` begins a MySQL statement; a column named show, or the same words
    inside a statement, are not one."""
    rule = _rule(rule_id)
    engine, target = rule.source_engines[0], rule.targets[0]
    for text in (f"{entry.lower()} x", f"SELECT 1; {entry.lower()} x"):
        assert rule_id in _ids(detect_relational_gaps(_collector(engine, queries=[text]), target))
    for text in (f"SELECT a FROM t WHERE b = {entry.lower()}", f"SELECT '{entry.lower()}'"):
        miss = _collector(engine, queries=[text])
        assert rule_id not in _ids(detect_relational_gaps(miss, target)), text


def test_a_routine_or_view_definition_is_read_too() -> None:
    collector = _collector(
        "sqlserver",
        tables=[_table("db.s")],
        procedures=[
            {
                "procedure_id": "usp_Tidy",
                "procedure_type": "PROCEDURE",
                "definition": "CREATE PROCEDURE usp_Tidy AS DELETE FROM [S] WHERE [At] < GETDATE()",
            }
        ],
        views=[{"view_id": "v_recent", "definition": "SELECT TOP (5) [Id] FROM [S]"}],
        triggers=[{"trigger_id": "trg_S", "table_id": "db.s", "definition": "SELECT @@ROWCOUNT"}],
    )
    result = detect_relational_gaps(collector, "aurora_postgresql")
    assert _gap(result, "sqlserver-date-functions")["objects"] == [
        {"kind": "procedure", "id": "usp_Tidy"}
    ]
    assert _gap(result, "sqlserver-top-clause")["objects"] == [{"kind": "view", "id": "v_recent"}]
    identity = _gap(result, "sqlserver-identity-functions")
    assert identity["objects"] == [{"kind": "trigger", "id": "trg_S"}]
    assert identity["tables"] == ["db.s"]


def test_query_evidence_names_the_query_and_its_tables() -> None:
    collector = _collector(
        "oracle",
        queries=["SELECT 1 FROM a", "SELECT NVL(x, 0) FROM b"],
        tables=[_table("db.t"), _table("db.b"), _table("db.c")],
    )
    collector["queries"]["query_patterns"][1]["tables_accessed"] = ["db.b", "db.c"]
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "oracle-builtin-functions")
    assert (gap["queries"], gap["tables"], gap["objects"]) == (["q1"], ["db.b", "db.c"], [])


def test_a_query_whose_tables_the_collector_could_not_read_adds_no_table() -> None:
    """#1205: the collector lists ``unknown`` for an EXEC or an ALTER INDEX; it is no table."""
    collector = _collector(
        "sqlserver",
        queries=["SELECT GETDATE() FROM t", "EXEC usp_PlaceOrder @CustomerID = 1"],
        tables=[_table("db.t")],
    )
    patterns = collector["queries"]["query_patterns"]
    patterns[1]["tables_accessed"] = ["unknown"]
    patterns[0]["query_text"] = "SELECT GETDATE() FROM t JOIN u ON 1 = 1 WHERE EOMONTH(x) > 0"
    patterns[0]["tables_accessed"] = ["db.t", "unknown", "db.missing"]
    patterns.append(
        {"query_id": "q2", "query_text": "SELECT EOMONTH(x)", "tables_accessed": ["unknown"]}
    )
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "sqlserver-date-functions")
    assert gap["queries"] == ["q0", "q2"]
    assert gap["tables"] == ["db.t"]


def test_a_query_table_is_named_by_its_collected_id() -> None:
    collector = _collector("oracle", queries=["SELECT NVL(x, 0) FROM b"], tables=[_table("db.b")])
    collector["queries"]["query_patterns"][0]["tables_accessed"] = ["B"]
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "oracle-builtin-functions")
    assert gap["tables"] == ["db.b"]


def test_a_trigger_table_is_named_by_its_collected_id() -> None:
    """The collector writes a trigger's table by its bare name (trailgear's SalesOrder)."""
    collector = _collector(
        "sqlserver",
        tables=[_table("trailgear.dbo.SalesOrder")],
        triggers=[
            {"trigger_id": "trg", "table_id": "SalesOrder", "definition": "CREATE TRIGGER trg"},
            {"trigger_id": "trg2", "table_id": "Gone", "definition": "CREATE TRIGGER trg2"},
        ],
    )
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "sqlserver-tsql-routines")
    assert gap["tables"] == ["trailgear.dbo.SalesOrder"]
    assert {o["id"] for o in gap["objects"]} >= {"trg", "trg2"}


@pytest.mark.parametrize(
    ("name", "resolved"),
    [
        ("b", []),  # collected under two schemas: ambiguous, left out
        ("s1.b", ["db.s1.b"]),
        ("db.s2.b", ["db.s2.b"]),
        ('"S1"."B"', ["db.s1.b"]),  # quoted and case-folded
        ("s3.b", []),
    ],
)
def test_a_bare_name_in_two_schemas_is_dropped_but_a_qualified_one_resolves(
    name: str, resolved: list[str]
) -> None:
    collector = _collector(
        "oracle", queries=["SELECT NVL(x, 0) FROM b"], tables=[_table("db.s1.b"), _table("db.s2.b")]
    )
    collector["queries"]["query_patterns"][0]["tables_accessed"] = [name]
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "oracle-builtin-functions")
    assert (gap["queries"], gap["tables"]) == (["q0"], resolved)


def test_without_a_collected_schema_only_the_unknown_placeholder_is_dropped() -> None:
    collector = _collector("oracle", queries=["SELECT NVL(x, 0) FROM b"])
    collector["database_schema"]["tables"] = []
    collector["queries"]["query_patterns"][0]["tables_accessed"] = ["b", "unknown"]
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "oracle-builtin-functions")
    assert gap["tables"] == ["b"]


def test_column_evidence_names_the_column_and_its_table() -> None:
    collector = _collector("oracle", tables=[_table("db.t", types=("int", "DATE"))])
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "oracle-date-type")
    assert gap["tables"] == ["db.t"]
    assert gap["objects"] == [{"kind": "column", "id": "db.t.c1"}]


def test_the_source_dialect_decides_what_is_a_comment() -> None:
    # "#" starts a comment in MySQL only: in PostgreSQL the call after it is real SQL.
    mysql = _collector("mysql", queries=["SELECT 1 # PASSWORD(x)"])
    assert "mysql-removed-functions" not in _ids(detect_relational_gaps(mysql, "aurora_mysql"))
    pg = _collector("postgresql", queries=["SELECT 1 # pg_read_file('/x')"])
    assert "postgresql-server-file-access" in _ids(detect_relational_gaps(pg, "aurora_postgresql"))


def test_a_homogeneous_source_inside_the_target_lines_has_no_version_gap() -> None:
    for engine, target, version in (
        ("mysql", "aurora_mysql", "8.0.45"),
        ("postgresql", "aurora_postgresql", "PostgreSQL 16.10 on x86_64-pc-linux-gnu"),
        ("postgresql", "aurora_postgresql", "14.0"),
        ("postgresql", "aurora_postgresql", "17.9"),
        ("postgresql", "aurora_postgresql", "18.1"),
        ("mysql", "aurora_mysql", "8.4.8"),
    ):
        result = detect_relational_gaps(_collector(engine, version=version), target)
        assert result is not None and result["gaps"] == [], (engine, version)
        assert result["version_checked"] is True


def test_the_version_check_states_what_it_compared() -> None:
    result = detect_relational_gaps(_collector("postgresql", version="12.18"), "aurora_postgresql")
    assert result is not None
    assert (result["source_line"], result["target_lines"]) == ("12", ["14", "15", "16", "17", "18"])
    assert _ids(result) == {"postgresql-older-major-version"}


def test_a_heterogeneous_source_skips_the_version_check() -> None:
    for version in ("15.0.4322.2", "unknown", ""):
        result = detect_relational_gaps(_collector("sqlserver", version=version), "aurora_mysql")
        assert result is not None
        assert result["version_checked"] is False and result["source_line"] is None
        assert not {g["rule_id"] for g in result["gaps"]} & {
            r.id for r in gap_rules() if r.match.version
        }


def test_mariadb_is_not_compared_with_mysql_version_lines() -> None:
    result = detect_relational_gaps(_collector("mariadb", version="10.6.12"), "aurora_mysql")
    assert result is not None and result["version_checked"] is False
    assert _ids(result) == {"mariadb-to-aurora-mysql"}


@pytest.mark.parametrize(
    ("collector", "target"),
    [
        (None, "aurora_mysql"),
        ({}, "aurora_mysql"),
        (_collector("mysql"), None),
        (_collector("mysql"), "dynamodb"),
        (_collector("sybase"), "aurora_mysql"),
    ],
)
def test_no_result_without_a_collection_a_source_engine_or_an_aurora_target(
    collector: Any, target: Any
) -> None:
    assert detect_relational_gaps(collector, target) is None


def test_an_empty_collection_finds_nothing_but_the_always_rules() -> None:
    bare = {"metadata": {"source_database": {"engine": "oracle", "version": "19c"}}}
    result = detect_relational_gaps(bare, "aurora_postgresql")
    assert result is not None
    assert _ids(result) == {
        r.id for r in gap_rules() if r.match.always and "oracle" in r.source_engines
    }


def test_the_postgres_alias_reads_as_postgresql() -> None:
    result = detect_relational_gaps(_collector("postgres", version="12.1"), "aurora_postgresql")
    assert result is not None and result["source_engine"] == "postgresql"
    assert "postgresql-older-major-version" in _ids(result)


def test_gaps_are_ordered_and_counted_by_impact() -> None:
    collector = _collector(
        "sqlserver",
        queries=["SELECT TOP (1) [a] FROM [t] WITH (NOLOCK)"],
        tables=[_table("db.t", types=("nvarchar(10)",))],
        procedures=[{"procedure_id": "p", "procedure_type": "PROCEDURE"}],
    )
    result = detect_relational_gaps(collector, "aurora_postgresql")
    assert result is not None
    order = [(GAP_IMPACTS.index(g["impact"]), g["rule_id"]) for g in result["gaps"]]
    assert order == sorted(order)
    assert list(result["gaps_by_impact"]) == list(GAP_IMPACTS)
    assert sum(result["gaps_by_impact"].values()) == len(result["gaps"])
    assert result["rules_checked"] == sum(
        1
        for r in gap_rules()
        if "sqlserver" in r.source_engines and "aurora_postgresql" in r.targets
    )


def test_the_result_does_not_depend_on_collection_order() -> None:
    collector = _collector(
        "sqlserver",
        queries=[f"SELECT TOP ({i}) [a] FROM [t{i}] WHERE [d] < GETDATE()" for i in range(8)],
        tables=[_table(f"db.t{i}", types=("bit", "nvarchar(5)")) for i in range(5)],
    )
    for i, q in enumerate(collector["queries"]["query_patterns"]):
        q["tables_accessed"] = [f"db.t{i % 5}"]
    expected = detect_relational_gaps(collector, "aurora_mysql")
    shuffled = copy.deepcopy(collector)
    rng = random.Random(471)
    rng.shuffle(shuffled["queries"]["query_patterns"])
    rng.shuffle(shuffled["database_schema"]["tables"])
    assert detect_relational_gaps(shuffled, "aurora_mysql") == expected


def test_the_collection_is_not_modified() -> None:
    collector = _collector("oracle", queries=["SELECT NVL(a, 1) FROM t"])
    before = copy.deepcopy(collector)
    detect_relational_gaps(collector, "aurora_postgresql")
    assert collector == before


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("NVARCHAR(50)", "nvarchar"),
        ("TIMESTAMP(6) WITH TIME ZONE", "timestamp with time zone"),
        ("long  raw", "long raw"),
        ("decimal(10, 2)", "decimal"),
        (None, ""),
    ],
)
def test_normalized_data_type(raw: Any, normalized: str) -> None:
    assert normalized_data_type(raw) == normalized


# --- review round 1 (#1079): false positives the first catalog had -------------------


@pytest.mark.parametrize("target", ["aurora_mysql", "aurora_postgresql"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.[Id] FROM [A] a INNER MERGE JOIN [B] b ON a.[Id] = b.[Id]",
        "SELECT a.[Id] FROM [A] a JOIN [B] b ON a.[Id] = b.[Id] OPTION (MERGE JOIN)",
        "SELECT [Id] FROM [A] UNION SELECT [Id] FROM [B] OPTION (MERGE UNION)",
    ],
)
def test_merge_join_and_union_hints_are_not_merge_statements(sql: str, target: str) -> None:
    result = detect_relational_gaps(_collector("sqlserver", queries=[sql]), target)
    assert not _ids(result) & {"sqlserver-merge-to-mysql", "sqlserver-merge-to-postgresql"}


def test_a_merge_statement_after_a_cte_still_counts() -> None:
    sql = (
        "WITH src AS (SELECT [Id], [Q] FROM [Delta]) "
        "MERGE INTO [Stock] AS tgt USING src ON tgt.[Id] = src.[Id] WHEN MATCHED THEN DELETE;"
    )
    result = detect_relational_gaps(_collector("sqlserver", queries=[sql]), "aurora_mysql")
    assert "sqlserver-merge-to-mysql" in _ids(result)


def test_a_cursor_fetch_is_not_a_row_limit() -> None:
    body = (
        "CREATE PROCEDURE usp_Walk AS DECLARE c CURSOR FOR SELECT [Id] FROM [T]; "
        "OPEN c; FETCH NEXT FROM c INTO @id; FETCH FIRST FROM c INTO @id; CLOSE c;"
    )
    collector = _collector(
        "sqlserver",
        procedures=[
            {"procedure_id": "usp_Walk", "procedure_type": "PROCEDURE", "definition": body}
        ],
    )
    assert "fetch-first-to-mysql" not in _ids(detect_relational_gaps(collector, "aurora_mysql"))


@pytest.mark.parametrize(
    ("engine", "sql"),
    [
        ("sqlserver", "SELECT [Id] FROM [T] ORDER BY [Id] OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY"),
        (
            "sqlserver",
            "SELECT [Id] FROM [T] ORDER BY [Id] OFFSET @o ROWS FETCH NEXT (@n) ROWS ONLY",
        ),
        ("db2", "SELECT id FROM t ORDER BY id FETCH FIRST ROW ONLY"),
        ("oracle", "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS ONLY"),
    ],
)
def test_row_limit_fetch_shapes_count(engine: str, sql: str) -> None:
    result = detect_relational_gaps(_collector(engine, queries=[sql]), "aurora_mysql")
    assert "fetch-first-to-mysql" in _ids(result)


def _user_fn(name: str, schema: str, language: str = "plpgsql") -> dict[str, Any]:
    return {
        "procedure_id": f"{schema}.{name}",
        "procedure_name": name,
        "schema_name": schema,
        "procedure_type": "FUNCTION",
        "language": language,
    }


@pytest.mark.parametrize(
    ("rule_id", "sql"),
    [
        ("postgresql-timescaledb", "SELECT my_schema.time_bucket('5 minutes', ts) FROM m"),
        ("postgresql-citus", "SELECT app.create_distributed_table('events', 'tenant_id')"),
    ],
)
def test_a_call_qualified_with_a_user_schema_is_not_the_extension(rule_id: str, sql: str) -> None:
    result = detect_relational_gaps(_collector("postgresql", queries=[sql]), "aurora_postgresql")
    assert rule_id not in _ids(result)


@pytest.mark.parametrize(
    ("rule_id", "name", "sql"),
    [
        ("postgresql-timescaledb", "time_bucket", "SELECT time_bucket('5 minutes', ts) FROM m"),
        ("postgresql-timescaledb", "time_bucket", "SELECT \"time_bucket\"('5 minutes', ts) FROM m"),
        (
            "postgresql-citus",
            "create_distributed_table",
            "SELECT create_distributed_table('e', 't')",
        ),
    ],
)
def test_an_unqualified_call_to_a_collected_user_function_is_not_the_extension(
    rule_id: str, name: str, sql: str
) -> None:
    collector = _collector("postgresql", queries=[sql], procedures=[_user_fn(name, "app")])
    assert rule_id not in _ids(detect_relational_gaps(collector, "aurora_postgresql"))


@pytest.mark.parametrize(
    ("rule_id", "sql"),
    [
        ("postgresql-timescaledb", "SELECT public.time_bucket('5 minutes', ts) FROM m"),
        ("postgresql-timescaledb", "CREATE EXTENSION IF NOT EXISTS timescaledb"),
        ("postgresql-citus", "SELECT pg_catalog.create_distributed_table('e', 't')"),
        ("postgresql-citus", "CREATE EXTENSION citus"),
    ],
)
def test_the_extension_schema_or_create_extension_counts(rule_id: str, sql: str) -> None:
    result = detect_relational_gaps(_collector("postgresql", queries=[sql]), "aurora_postgresql")
    assert rule_id in _ids(result)


def test_the_extensions_installed_c_function_is_evidence() -> None:
    installed = {
        "procedure_id": "create_hypertable",
        "procedure_name": "create_hypertable",
        "procedure_type": "FUNCTION",
        "language": "C",
    }
    collector = _collector("postgresql", procedures=[installed])
    gap = _gap(detect_relational_gaps(collector, "aurora_postgresql"), "postgresql-timescaledb")
    assert gap["objects"] == [{"kind": "function", "id": "create_hypertable"}]
    # A user's plpgsql function of the same name is not the extension.
    user = _collector("postgresql", procedures=[_user_fn("create_hypertable", "app")])
    assert "postgresql-timescaledb" not in _ids(detect_relational_gaps(user, "aurora_postgresql"))


def test_a_mariadb_server_collected_as_mysql_reads_as_mariadb() -> None:
    result = detect_relational_gaps(
        _collector("mysql", version="10.6.12-MariaDB", queries=["SELECT SQL_CACHE id FROM t"]),
        "aurora_mysql",
    )
    assert result is not None and result["source_engine"] == "mariadb"
    assert "mysql-newer-major-version" not in _ids(result)
    assert {"mariadb-to-aurora-mysql", "mysql-query-cache-hint"} <= _ids(result)


# --- #1330: the matchers the MySQL <-> PostgreSQL rules use ----------------------------


@pytest.mark.parametrize(
    ("sql", "hit"),
    [
        ("SELECT id FROM t LIMIT ?, ...", True),  # a statement digest
        ("SELECT id FROM t LIMIT 10, 20", True),
        ("SELECT id FROM t LIMIT @off, @n", True),
        ("SELECT id FROM t LIMIT 20 OFFSET 10", False),
        ("SELECT id FROM t LIMIT 20", False),
        ("SELECT id FROM t LIMIT (SELECT 1), 2", False),
        ("SELECT id FROM t WHERE note = 'LIMIT 1, 2'", False),
    ],
)
def test_a_value_slot_takes_one_value(sql: str, hit: bool) -> None:
    result = detect_relational_gaps(_collector("mysql", queries=[sql]), "aurora_postgresql")
    assert ("mysql-limit-offset-comma" in _ids(result)) is hit


@pytest.mark.parametrize(
    ("sql", "user", "system"),
    [
        ("SET @total = 0", True, False),
        ("SELECT @_x", True, False),
        ("SELECT @@autocommit", False, True),
        ("SELECT @@SESSION.sql_mode", False, True),
        ("SELECT 'a@b.c', '@@x'", False, False),
        ("SELECT a FROM t -- @x @@y", False, False),
    ],
)
def test_word_prefixes_tell_user_from_system_variables(sql: str, user: bool, system: bool) -> None:
    ids = _ids(detect_relational_gaps(_collector("mysql", queries=[sql]), "aurora_postgresql"))
    assert ("mysql-user-variables" in ids, "mysql-system-variables" in ids) == (user, system)


@pytest.mark.parametrize(
    ("engine", "sql", "rule_id", "hit"),
    [
        (
            "postgresql",
            'SELECT "u"."id" FROM "users" "u"',
            "postgresql-double-quoted-identifiers",
            True,
        ),
        (
            "postgresql",
            "SELECT id FROM users WHERE a = 'x\"y'",
            "postgresql-double-quoted-identifiers",
            False,
        ),
        (
            "postgresql",
            'SELECT id FROM users -- "quoted"',
            "postgresql-double-quoted-identifiers",
            False,
        ),
        ("postgresql", 'SELECT $$ "x" $$', "postgresql-double-quoted-identifiers", False),
        ("mysql", 'SELECT `id` FROM `t` WHERE a = "x"', "mysql-double-quoted-strings", True),
        ("mysql", "SELECT `id` FROM `t` WHERE a = ?", "mysql-double-quoted-strings", False),
        ("mysql", "SELECT `id` FROM `t` WHERE a = 'x\"y'", "mysql-double-quoted-strings", False),
    ],
)
def test_quoted_with_reads_the_spans_the_source_dialect_quotes(
    engine: str, sql: str, rule_id: str, hit: bool
) -> None:
    target = "aurora_mysql" if engine == "postgresql" else "aurora_postgresql"
    result = detect_relational_gaps(_collector(engine, queries=[sql]), target)
    assert (rule_id in _ids(result)) is hit


def test_a_quoted_span_in_a_routine_counts_as_its_evidence() -> None:
    body = 'CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$; SELECT "f"()'
    collector = _collector(
        "postgresql",
        procedures=[{"procedure_id": "f", "procedure_type": "FUNCTION", "definition": body}],
    )
    gap = _gap(
        detect_relational_gaps(collector, "aurora_mysql"), "postgresql-double-quoted-identifiers"
    )
    assert gap["objects"] == [{"kind": "function", "id": "f"}]


@pytest.mark.parametrize(
    ("data_type", "rule_id", "hit"),
    [
        ("bigint(20) unsigned", "mysql-unsigned-integers", True),
        ("INT UNSIGNED", "mysql-unsigned-integers", True),
        ("int(4) zerofill", "mysql-unsigned-integers", True),
        ("bigint(20)", "mysql-unsigned-integers", False),
        ("_varchar", "postgresql-arrays", True),
        ("integer[]", "postgresql-arrays", True),
        ("ARRAY", "postgresql-arrays", True),
        ("integer", "postgresql-arrays", False),
        ("varchar", "postgresql-arrays", False),
    ],
)
def test_data_type_globs(data_type: str, rule_id: str, hit: bool) -> None:
    engine = "postgresql" if rule_id.startswith("postgresql") else "mysql"
    target = "aurora_mysql" if engine == "postgresql" else "aurora_postgresql"
    collector = _collector(engine, tables=[_table("db.t", types=(data_type,))])
    assert (rule_id in _ids(detect_relational_gaps(collector, target))) is hit


@pytest.mark.parametrize(
    ("default", "hit"),
    [
        ("0000-00-00 00:00:00", True),
        ("0000-00-00", True),
        (" 0000-00-00 ", True),
        ("2000-01-01 00:00:00", False),
        ("", False),
        (None, False),
        (0, False),
        (False, False),
    ],
)
def test_zero_date_defaults(default: Any, hit: bool) -> None:
    table = _column_table(data_type="datetime", default_value=default)
    result = detect_relational_gaps(_collector("mysql", tables=[table]), "aurora_postgresql")
    assert ("mysql-zero-dates" in _ids(result)) is hit


def test_column_rules_name_the_column_and_its_table() -> None:
    tables = [
        _column_table("db.a", column_name="ID", is_auto_increment=True),
        _column_table("db.b", column_name="id", is_auto_increment=False),
    ]
    result = detect_relational_gaps(_collector("mysql", tables=tables), "aurora_postgresql")
    for rule_id in ("mysql-auto-increment-columns", "mysql-mixed-case-identifiers"):
        gap = _gap(result, rule_id)
        assert (gap["tables"], gap["objects"]) == (["db.a"], [{"kind": "column", "id": "db.a.ID"}])


def test_a_table_name_with_capitals_is_evidence_with_no_column() -> None:
    table = _table("db.Orders")
    gap = _gap(
        detect_relational_gaps(_collector("mysql", tables=[table]), "aurora_postgresql"),
        "mysql-mixed-case-identifiers",
    )
    assert (gap["tables"], gap["objects"]) == (["db.Orders"], [])


def test_index_rules_name_the_table() -> None:
    tables = [
        _table("db.a", indexes=[_index(index_type="gin"), _index(index_type="btree")]),
        _table("db.b", indexes=[_index(index_type="btree", predicate="(x > 0)")]),
        _table("db.c", indexes=[_index(index_type="btree", predicate="  ")]),
        _table("db.d"),
    ]
    result = detect_relational_gaps(_collector("postgresql", tables=tables), "aurora_mysql")
    assert _gap(result, "postgresql-index-methods")["tables"] == ["db.a"]
    assert _gap(result, "postgresql-partial-indexes")["tables"] == ["db.b"]


def test_a_mysql_fulltext_index_is_evidence() -> None:
    table = _table("db.t", indexes=[_index(index_type="fulltext")])
    result = detect_relational_gaps(_collector("mysql", tables=[table]), "aurora_postgresql")
    assert _gap(result, "mysql-fulltext-search")["tables"] == ["db.t"]


def test_objects_outside_the_public_schema_are_evidence() -> None:
    collector = _collector(
        "postgresql",
        tables=[
            _table("db.app.t", schema_name="App"),
            _table("db.public.u", schema_name="public"),
            _table("db.v"),
        ],
        procedures=[
            {"procedure_id": "fn.f", "procedure_type": "FUNCTION", "schema_name": "fn"},
            {"procedure_id": "g", "procedure_type": "FUNCTION", "schema_name": "PUBLIC"},
            {"procedure_id": "h", "procedure_type": "FUNCTION"},
        ],
        views=[{"view_id": "rpt.v", "schema_name": "rpt", "definition": "SELECT 1"}],
        triggers=[{"trigger_id": "trg", "table_id": "db.app.t", "schema_name": "app"}],
    )
    gap = _gap(detect_relational_gaps(collector, "aurora_mysql"), "postgresql-schemas-to-mysql")
    assert gap["tables"] == ["db.app.t"]
    assert gap["objects"] == [
        {"kind": "function", "id": "fn.f"},
        {"kind": "trigger", "id": "trg"},
        {"kind": "view", "id": "rpt.v"},
    ]


def test_every_conversion_has_feature_rules() -> None:
    """#1330: every source engine has feature rules for both Aurora engines, so no wave 1
    says its feature gaps were not checked (#1327)."""
    for engine in sorted(SOURCE_ENGINES):
        for target in ("aurora_mysql", "aurora_postgresql"):
            result = detect_relational_gaps(_collector(engine), target)
            assert result is not None and result["feature_rules_checked"] > 0, (engine, target)


# --- #1330 review round 1 ---------------------------------------------------------------


def _pg_fn(name: str, language: str) -> dict[str, Any]:
    return {
        "procedure_id": name,
        "procedure_name": name,
        "procedure_type": "FUNCTION",
        "language": language,
    }


def test_an_extensions_sql_wrapper_is_not_an_application_routine() -> None:
    """citext installs SQL-language wrappers named like built-ins (regexp_replace,
    strpos) with no schema in the collection: they are not the application's routines."""
    collector = _collector(
        "postgresql", procedures=[_pg_fn("regexp_replace", "sql"), _pg_fn("strpos", "SQL")]
    )
    result = detect_relational_gaps(collector, "aurora_mysql")
    assert "postgresql-plpgsql-routines-to-mysql" not in _ids(result)


@pytest.mark.parametrize("language", ["plpgsql", "plpython3u", "plperl", "plv8", "PLPGSQL"])
def test_procedural_language_routines_count(language: str) -> None:
    collector = _collector("postgresql", procedures=[_pg_fn("f", language)])
    gap = _gap(
        detect_relational_gaps(collector, "aurora_mysql"), "postgresql-plpgsql-routines-to-mysql"
    )
    assert gap["objects"] == [{"kind": "function", "id": "f"}]


@pytest.mark.parametrize(
    ("sql", "hit"),
    [
        ("REPLACE INTO `t` (`a`) VALUES (?)", True),
        ("REPLACE `t` (`a`) VALUES (?)", True),  # INTO is optional in MySQL
        ("REPLACE t SET a = ?", True),
        ("SELECT REPLACE(`a`, ?, ?) FROM `t`", False),
        ("UPDATE `t` SET `a` = REPLACE(`a`, ?, ?)", False),
    ],
)
def test_replace_statements_with_or_without_into(sql: str, hit: bool) -> None:
    result = detect_relational_gaps(_collector("mysql", queries=[sql]), "aurora_postgresql")
    assert ("mysql-replace-into" in _ids(result)) is hit


@pytest.mark.parametrize(
    ("sql", "hit"),
    [
        ("SELECT IF(`a` > ?, ?, ?) FROM `t`", True),
        ("SELECT `if` FROM `t`", False),
        ("SELECT `a` FROM `t` WHERE `b` = 'IF(x)'", False),
    ],
)
def test_the_if_function(sql: str, hit: bool) -> None:
    result = detect_relational_gaps(_collector("mysql", queries=[sql]), "aurora_postgresql")
    assert ("mysql-builtin-functions" in _ids(result)) is hit


@pytest.mark.parametrize(
    ("sql", "hit"),
    [
        ("SET sql_mode = ?", True),
        ("SET autocommit = 0", True),
        ("SET `sql_mode` = ?", True),  # a digest quotes the variable name
        ("SET SESSION `SQL_BIG_SELECTS` = ?", True),
        ("SET @total = ?", False),
        ("SET NAMES utf8mb4", False),
        ("UPDATE `t` SET `sql_mode` = ?", False),
    ],
)
def test_mysql_server_variables_set_plainly(sql: str, hit: bool) -> None:
    result = detect_relational_gaps(_collector("mysql", queries=[sql]), "aurora_postgresql")
    assert ("mysql-system-variables" in _ids(result)) is hit


@pytest.mark.parametrize(
    ("sql", "hit"),
    [
        ("SET client_min_messages TO warning", True),
        ("SET standard_conforming_strings = on", True),
        ("UPDATE t SET client_min_messages = $1", False),
    ],
)
def test_postgresql_session_settings_set_plainly(sql: str, hit: bool) -> None:
    result = detect_relational_gaps(_collector("postgresql", queries=[sql]), "aurora_mysql")
    assert ("postgresql-session-settings" in _ids(result)) is hit


@pytest.mark.parametrize(
    ("sql", "hit"),
    [
        ("SELECT id FROM t WHERE name ~ $1", True),
        ("SELECT id FROM t WHERE name ~* $1", True),
        ("SELECT id FROM t WHERE name !~ $1", True),
        ("SELECT id FROM t WHERE (name) ~ $1", True),
        ("SELECT ~ a FROM t", False),  # bitwise NOT, which MySQL has too
        ("SELECT a FROM t WHERE b = ~ c", False),
        ("SELECT a, ~b FROM t", False),
        ("SELECT id FROM t WHERE name ~~ $1", False),  # LIKE
        ("SELECT id FROM t WHERE name !~~ $1", False),  # NOT LIKE
    ],
)
def test_the_regex_operator_only_between_operands(sql: str, hit: bool) -> None:
    result = detect_relational_gaps(_collector("postgresql", queries=[sql]), "aurora_mysql")
    assert ("postgresql-regex-operators" in _ids(result)) is hit
