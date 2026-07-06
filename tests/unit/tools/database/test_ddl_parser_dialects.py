"""Tests for ddl_parser dialect support.

Covers both MySQL (regression — the parser is used by mysql/mariadb/postgresql
collectors today) and T-SQL (new behavior added for the SQL Server collector).
The parser is dialect-tolerant by default; the ``dialect`` parameter is
informational and reserved for future dialect-specific behavior.
"""

import pytest

from src.tools.database.ddl_parser import _unquote, parse_ddl

# ---------------------------------------------------------------------------
# _unquote helper
# ---------------------------------------------------------------------------


class TestUnquote:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("`foo`", "foo"),
            ("[foo]", "foo"),
            ('"foo"', "foo"),
            ("foo", "foo"),
            ("  `foo`  ", "foo"),
            ("[my_col]", "my_col"),
        ],
    )
    def test_unquote_strips_all_quote_styles(self, raw: str, expected: str) -> None:
        assert _unquote(raw) == expected


# ---------------------------------------------------------------------------
# MySQL regression tests — must keep working since mysql/mariadb/postgresql
# all delegate to this parser.
# ---------------------------------------------------------------------------


class TestMySQLDialect:
    def test_basic_table_with_auto_increment(self) -> None:
        ddl = """
        CREATE TABLE `users` (
            `id` INT NOT NULL AUTO_INCREMENT,
            `email` VARCHAR(255) NOT NULL,
            PRIMARY KEY (`id`)
        );
        """
        tables = parse_ddl(ddl, "mydb")
        assert len(tables) == 1
        t = tables[0]
        assert t["table_name"] == "users"
        assert t["table_id"] == "mydb.users"
        assert t["primary_key"] == ["id"]

        cols = {c["column_name"]: c for c in t["columns"]}
        assert cols["id"]["extra"] == "auto_increment"
        assert cols["id"]["is_nullable"] == "NO"
        assert cols["email"]["data_type"] == "varchar"
        assert cols["email"]["max_length"] == 255

    def test_if_not_exists_handled(self) -> None:
        ddl = "CREATE TABLE IF NOT EXISTS `t` (`x` INT);"
        tables = parse_ddl(ddl)
        assert len(tables) == 1
        assert tables[0]["table_name"] == "t"

    def test_inline_index_and_unique_key(self) -> None:
        ddl = """
        CREATE TABLE `users` (
            `id` INT,
            `email` VARCHAR(255),
            `name` VARCHAR(100),
            PRIMARY KEY (`id`),
            UNIQUE KEY `idx_email` (`email`),
            INDEX `idx_name` (`name`)
        );
        """
        tables = parse_ddl(ddl)
        idxs = {i["index_name"]: i for i in tables[0]["indexes"]}
        assert idxs["PRIMARY"]["is_primary"] is True
        assert idxs["idx_email"]["is_unique"] is True
        assert idxs["idx_name"]["is_unique"] is False

    def test_foreign_key_with_on_delete(self) -> None:
        ddl = """
        CREATE TABLE `orders` (
            `id` INT,
            `user_id` INT,
            CONSTRAINT `fk_user` FOREIGN KEY (`user_id`)
                REFERENCES `users`(`id`) ON DELETE CASCADE
        );
        """
        tables = parse_ddl(ddl)
        fks = tables[0]["foreign_keys"]
        assert len(fks) == 1
        assert fks[0]["constraint_name"] == "fk_user"
        assert fks[0]["referenced_table"] == "users"
        assert fks[0]["on_delete"] == "CASCADE"

    def test_decimal_precision_extracted_as_first_param(self) -> None:
        # DECIMAL(10, 2) → max_length captures 10 (precision)
        ddl = "CREATE TABLE `t` (`amt` DECIMAL(10, 2));"
        tables = parse_ddl(ddl)
        assert tables[0]["columns"][0]["max_length"] == 10

    def test_multiple_tables(self) -> None:
        ddl = """
        CREATE TABLE `a` (`x` INT);
        CREATE TABLE `b` (`y` INT);
        """
        tables = parse_ddl(ddl)
        assert len(tables) == 2
        assert tables[0]["table_name"] == "a"
        assert tables[1]["table_name"] == "b"

    def test_default_value_extracted(self) -> None:
        ddl = """
        CREATE TABLE `t` (
            `created` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            `status` VARCHAR(20) DEFAULT 'active'
        );
        """
        tables = parse_ddl(ddl)
        cols = {c["column_name"]: c for c in tables[0]["columns"]}
        assert cols["created"]["column_default"] == "CURRENT_TIMESTAMP"
        assert cols["status"]["column_default"] == "active"


# ---------------------------------------------------------------------------
# T-SQL (SQL Server) — new behavior
# ---------------------------------------------------------------------------


class TestTSQLDialect:
    def test_bracketed_identifiers(self) -> None:
        ddl = """
        CREATE TABLE [dbo].[Customers] (
            [Id] INT NOT NULL,
            [Email] NVARCHAR(255)
        );
        """
        tables = parse_ddl(ddl, "mydb", dialect="sqlserver")
        assert len(tables) == 1
        t = tables[0]
        # Schema-qualified table — table_name is the second segment (Customers)
        assert t["table_name"] == "Customers"
        # Brackets stripped from columns
        assert t["columns"][0]["column_name"] == "Id"
        assert t["columns"][1]["column_name"] == "Email"

    def test_identity_with_seed_increment(self) -> None:
        ddl = """
        CREATE TABLE [Users] (
            [Id] INT IDENTITY(1, 1) NOT NULL
        );
        """
        tables = parse_ddl(ddl, dialect="sqlserver")
        col = tables[0]["columns"][0]
        assert col["extra"] == "auto_increment"

    def test_identity_without_args(self) -> None:
        # Bare IDENTITY (no seed/increment) — also valid T-SQL
        ddl = """
        CREATE TABLE [Users] (
            [Id] INT IDENTITY NOT NULL PRIMARY KEY
        );
        """
        tables = parse_ddl(ddl, dialect="sqlserver")
        col = tables[0]["columns"][0]
        assert col["extra"] == "auto_increment"
        assert col["is_primary"] is True

    @pytest.mark.parametrize(
        "ddl_type",
        ["NVARCHAR(MAX)", "VARCHAR(MAX)", "VARBINARY(MAX)", "nvarchar(max)"],
    )
    def test_max_length_keyword_becomes_none(self, ddl_type: str) -> None:
        ddl = f"CREATE TABLE [t] ([c] {ddl_type});"
        tables = parse_ddl(ddl, dialect="sqlserver")
        col = tables[0]["columns"][0]
        assert col["max_length"] is None

    @pytest.mark.parametrize(
        "ddl_type,expected_base",
        [
            ("UNIQUEIDENTIFIER", "uniqueidentifier"),
            ("BIT", "bit"),
            ("DATETIME2", "datetime2"),
            ("DATETIMEOFFSET", "datetimeoffset"),
            ("SMALLDATETIME", "smalldatetime"),
            ("MONEY", "money"),
            ("XML", "xml"),
        ],
    )
    def test_sqlserver_specific_types_preserved(self, ddl_type: str, expected_base: str) -> None:
        ddl = f"CREATE TABLE [t] ([c] {ddl_type});"
        tables = parse_ddl(ddl, dialect="sqlserver")
        col = tables[0]["columns"][0]
        assert col["data_type"] == expected_base

    def test_constraint_pk_clustered(self) -> None:
        ddl = """
        CREATE TABLE [Customers] (
            [Id] INT NOT NULL,
            [Tenant] INT NOT NULL,
            CONSTRAINT [PK_Customers] PRIMARY KEY CLUSTERED ([Tenant], [Id])
        );
        """
        tables = parse_ddl(ddl, dialect="sqlserver")
        t = tables[0]
        assert t["primary_key"] == ["Tenant", "Id"]
        pk_idx = next(i for i in t["indexes"] if i["is_primary"])
        assert pk_idx["columns"] == ["Tenant", "Id"]

    def test_constraint_unique_nonclustered(self) -> None:
        ddl = """
        CREATE TABLE [Customers] (
            [Id] INT,
            [Email] NVARCHAR(255),
            CONSTRAINT [UQ_Email] UNIQUE NONCLUSTERED ([Email])
        );
        """
        tables = parse_ddl(ddl, dialect="sqlserver")
        idxs = {i["index_name"]: i for i in tables[0]["indexes"]}
        assert "UQ_Email" in idxs
        assert idxs["UQ_Email"]["is_unique"] is True
        assert idxs["UQ_Email"]["is_primary"] is False
        assert idxs["UQ_Email"]["columns"] == ["Email"]

    def test_foreign_key_with_schema_qualified_reference(self) -> None:
        ddl = """
        CREATE TABLE [sales].[Orders] (
            [Id] INT,
            [CustomerId] INT,
            CONSTRAINT [FK_Orders_Customers] FOREIGN KEY ([CustomerId])
                REFERENCES [dbo].[Customers]([Id]) ON DELETE CASCADE
        );
        """
        tables = parse_ddl(ddl, "mydb", dialect="sqlserver")
        fk = tables[0]["foreign_keys"][0]
        assert fk["constraint_name"] == "FK_Orders_Customers"
        # Reference to [dbo].[Customers] should resolve to "Customers"
        assert fk["referenced_table"] == "Customers"
        assert fk["referenced_columns"] == ["Id"]
        assert fk["on_delete"] == "CASCADE"

    def test_realistic_tsql_table(self) -> None:
        """Comprehensive test combining many T-SQL features at once."""
        ddl = """
        CREATE TABLE [dbo].[Customers] (
            [Id] INT IDENTITY(1, 1) NOT NULL,
            [Email] NVARCHAR(255) NOT NULL,
            [Name] NVARCHAR(100),
            [Notes] NVARCHAR(MAX),
            [Photo] VARBINARY(MAX),
            [Created] DATETIME2 DEFAULT GETDATE(),
            [TenantId] UNIQUEIDENTIFIER NOT NULL,
            [Active] BIT DEFAULT 1,
            CONSTRAINT [PK_Customers] PRIMARY KEY CLUSTERED ([Id]),
            CONSTRAINT [UQ_Email] UNIQUE NONCLUSTERED ([Email])
        );
        """
        tables = parse_ddl(ddl, "mydb", dialect="sqlserver")
        t = tables[0]
        cols = {c["column_name"]: c for c in t["columns"]}

        assert len(cols) == 8
        assert cols["Id"]["extra"] == "auto_increment"
        assert cols["Notes"]["max_length"] is None
        assert cols["Photo"]["max_length"] is None
        assert cols["Email"]["max_length"] == 255
        assert cols["Active"]["data_type"] == "bit"
        assert cols["TenantId"]["data_type"] == "uniqueidentifier"
        assert cols["Created"]["data_type"] == "datetime2"
        assert cols["Created"]["column_default"] == "GETDATE()"
        assert cols["Active"]["column_default"] == "1"
        assert cols["Active"]["is_nullable"] == "YES"
        assert cols["TenantId"]["is_nullable"] == "NO"

        assert t["primary_key"] == ["Id"]
        idx_names = {i["index_name"] for i in t["indexes"]}
        assert idx_names == {"PRIMARY", "UQ_Email"}


# ---------------------------------------------------------------------------
# Dialect parameter behavior
# ---------------------------------------------------------------------------


class TestDialectParameter:
    def test_default_dialect_is_mysql(self) -> None:
        # No dialect passed → default mysql; MySQL DDL still works
        ddl = "CREATE TABLE `t` (`x` INT AUTO_INCREMENT PRIMARY KEY);"
        tables = parse_ddl(ddl)
        assert tables[0]["columns"][0]["extra"] == "auto_increment"

    def test_unknown_dialect_does_not_crash(self) -> None:
        # The dialect param is informational today — unknown values should
        # not break parsing.
        ddl = "CREATE TABLE `t` (`x` INT);"
        tables = parse_ddl(ddl, dialect="oracle")
        assert len(tables) == 1
        assert tables[0]["columns"][0]["column_name"] == "x"

    def test_postgresql_dialect_routed_to_parser(self) -> None:
        # PostgreSQL collector currently uses the MySQL-flavored parser;
        # the dialect param documents that intent without changing behavior.
        ddl = """
        CREATE TABLE "users" (
            "id" SERIAL PRIMARY KEY,
            "email" VARCHAR(255)
        );
        """
        tables = parse_ddl(ddl, dialect="postgresql")
        # Parser handles double-quoted identifiers via _unquote
        assert tables[0]["columns"][0]["column_name"] == "id"
        assert tables[0]["columns"][1]["column_name"] == "email"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_ddl_returns_empty_list(self) -> None:
        assert parse_ddl("") == []

    def test_no_create_table_returns_empty_list(self) -> None:
        assert parse_ddl("SELECT * FROM users;") == []

    def test_malformed_create_table_skipped(self) -> None:
        # Missing closing paren → parser returns None for the table
        ddl = "CREATE TABLE `t` (`x` INT"
        tables = parse_ddl(ddl)
        assert tables == []

    def test_schema_qualified_mysql(self) -> None:
        # MySQL also supports schema.table — the second segment wins
        ddl = "CREATE TABLE `mydb`.`users` (`id` INT);"
        tables = parse_ddl(ddl)
        assert tables[0]["table_name"] == "users"

    def test_mixed_quote_styles_in_one_ddl(self) -> None:
        # Defensive: even if input mixes styles, parser doesn't crash
        ddl = """
        CREATE TABLE `users` (
            [id] INT,
            "email" VARCHAR(255)
        );
        """
        tables = parse_ddl(ddl)
        assert tables[0]["table_name"] == "users"
        col_names = {c["column_name"] for c in tables[0]["columns"]}
        assert col_names == {"id", "email"}
