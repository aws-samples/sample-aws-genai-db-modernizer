# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ddl_parser dialect support.

Covers both MySQL (regression — the parser is used by mysql/mariadb/postgresql
collectors today) and T-SQL (new behavior added for the SQL Server collector).
The parser is dialect-tolerant by default; the ``dialect`` parameter only makes an
Oracle source read Oracle's multi-word type names (#2231).
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
# ---------------------------------------------------------------------------
# Oracle dialect
# ---------------------------------------------------------------------------


class TestOracleDialect:
    def test_generated_always_as_identity(self) -> None:
        ddl = """
        CREATE TABLE orders (
            order_id NUMBER(10) GENERATED ALWAYS AS IDENTITY NOT NULL,
            customer_name VARCHAR2(100 CHAR),
            total_amount NUMBER(12,2) DEFAULT 0
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        assert len(tables) == 1
        cols = tables[0]["columns"]
        assert cols[0]["column_name"] == "order_id"
        assert cols[0]["extra"] == "auto_increment"
        assert cols[0]["max_length"] == 10

    def test_generated_by_default_on_null(self) -> None:
        ddl = """
        CREATE TABLE items (
            item_id NUMBER GENERATED BY DEFAULT ON NULL AS IDENTITY,
            name VARCHAR2(200 BYTE) NOT NULL
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        cols = tables[0]["columns"]
        assert cols[0]["extra"] == "auto_increment"

    def test_varchar2_char_and_byte(self) -> None:
        ddl = """
        CREATE TABLE customers (
            id NUMBER(10) NOT NULL,
            name VARCHAR2(100 CHAR) NOT NULL,
            code VARCHAR2(50 BYTE),
            description CLOB
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        cols = tables[0]["columns"]
        assert cols[1]["max_length"] == 100  # CHAR qualifier
        assert cols[2]["max_length"] == 50  # BYTE qualifier
        assert cols[3]["max_length"] is None  # CLOB has no length

    def test_number_precision_scale(self) -> None:
        ddl = """
        CREATE TABLE pricing (
            id NUMBER(10) NOT NULL,
            amount NUMBER(12,2),
            qty NUMBER
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        cols = tables[0]["columns"]
        assert cols[0]["max_length"] == 10
        assert cols[1]["max_length"] == 12
        assert cols[2]["max_length"] is None  # NUMBER with no precision

    def test_schema_qualified_table(self) -> None:
        ddl = """
        CREATE TABLE HR.EMPLOYEES (
            employee_id NUMBER(10) NOT NULL,
            name VARCHAR2(200) NOT NULL
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        assert tables[0]["table_name"] == "EMPLOYEES"

    def test_foreign_key_with_on_delete(self) -> None:
        ddl = """
        CREATE TABLE order_items (
            item_id NUMBER(10) GENERATED ALWAYS AS IDENTITY,
            order_id NUMBER(10) NOT NULL,
            CONSTRAINT fk_order FOREIGN KEY (order_id) REFERENCES orders (order_id) ON DELETE CASCADE
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        fks = tables[0]["foreign_keys"]
        assert len(fks) == 1
        assert fks[0]["constraint_name"] == "fk_order"
        assert fks[0]["on_delete"] == "CASCADE"
        # Oracle doesn't support ON UPDATE but parser leaves it as None
        assert fks[0]["on_update"] is None

    def test_create_sequence_ignored(self) -> None:
        """CREATE SEQUENCE statements should not crash the parser."""
        ddl = """
        CREATE SEQUENCE order_seq START WITH 1 INCREMENT BY 1;
        CREATE TABLE orders (
            order_id NUMBER(10) NOT NULL,
            status VARCHAR2(20) DEFAULT 'PENDING'
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        assert len(tables) == 1
        assert tables[0]["table_name"] == "orders"

    def test_timestamp_with_time_zone(self) -> None:
        ddl = """
        CREATE TABLE events (
            event_id NUMBER(10) NOT NULL,
            created_at TIMESTAMP(6) WITH TIME ZONE DEFAULT SYSTIMESTAMP
        );
        """
        tables = parse_ddl(ddl, "testdb", dialect="oracle")
        cols = tables[0]["columns"]
        # TIMESTAMP(6) → base_type "timestamp", length 6
        assert cols[1]["data_type"] == "timestamp"
        assert cols[1]["max_length"] == 6


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


# ---------------------------------------------------------------------------
# Scripted DDL forms (#2231): each column keeps its whole type
# ---------------------------------------------------------------------------

# A table as SQL Server Management Studio's "Script Table as > CREATE To" writes it:
# bracketed type names, the PK on its own lines with WITH (...) ON [PRIMARY]. Synthetic.
_SSMS_TABLE = """SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Orders](
\t[OrderID] [int] IDENTITY(1,1) NOT NULL,
\t[PlacedAt] [datetime2](7) NULL,
\t[Notes] [nvarchar](max) NULL,
\t[Code] [varchar](20) NOT NULL,
\t[Total] [decimal](18, 2) NOT NULL,
\t[Hash] [binary](16) NULL,
\t[Created] [datetime] NOT NULL,
 CONSTRAINT [PK_Orders] PRIMARY KEY CLUSTERED\x20
(
\t[OrderID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY]
GO
"""

# An Oracle table as DBMS_METADATA.GET_DDL writes it: quoted names, a space before a
# TIMESTAMP's precision, the time-zone words after it. Synthetic.
_GET_DDL_TABLE = """
  CREATE TABLE "APP"."EVENTS"
   (\t"ID" NUMBER(10,0) NOT NULL ENABLE,
\t"AT_TZ" TIMESTAMP (6) WITH TIME ZONE,
\t"AT_LTZ" TIMESTAMP (3) WITH LOCAL TIME ZONE,
\t"AT" TIMESTAMP (9),
\t"PAYLOAD" LONG RAW,
\t"DOC" BFILE,
\t CONSTRAINT "EVENTS_PK" PRIMARY KEY ("ID")
  USING INDEX  ENABLE
   ) SEGMENT CREATION IMMEDIATE
  TABLESPACE "USERS"
"""


class TestScriptedTypes:
    def test_ssms_bracketed_types_keep_every_column(self) -> None:
        (table,) = parse_ddl(_SSMS_TABLE, "app", dialect="sqlserver")
        cols = {c["column_name"]: c for c in table["columns"]}
        assert list(cols) == [
            "OrderID", "PlacedAt", "Notes", "Code", "Total", "Hash", "Created"
        ]  # fmt: skip
        assert {n: c["column_type"] for n, c in cols.items()} == {
            "OrderID": "int",
            "PlacedAt": "datetime2(7)",
            "Notes": "nvarchar(max)",
            "Code": "varchar(20)",
            "Total": "decimal(18, 2)",
            "Hash": "binary(16)",
            "Created": "datetime",
        }
        assert cols["PlacedAt"]["data_type"] == "datetime2"
        assert cols["Notes"]["max_length"] is None
        assert cols["Code"]["max_length"] == 20
        assert cols["Hash"]["max_length"] == 16
        assert cols["OrderID"]["extra"] == "auto_increment"
        assert cols["Code"]["is_nullable"] == "NO"

    def test_a_bracketed_alias_type_is_not_read_as_its_schema(self) -> None:
        ddl = "CREATE TABLE [t] ([a] [dbo].[Phone] NULL, [b] [int] NULL);"
        (table,) = parse_ddl(ddl, "app", dialect="sqlserver")
        assert [(c["column_name"], c["column_type"]) for c in table["columns"]] == [("b", "int")]

    def test_ssms_primary_key_drops_the_sort_order(self) -> None:
        (table,) = parse_ddl(_SSMS_TABLE, "app", dialect="sqlserver")
        assert table["primary_key"] == ["OrderID"]
        pk = next(i for i in table["indexes"] if i["is_primary"])
        assert pk["columns"] == ["OrderID"]

    def test_oracle_get_ddl_types_keep_their_tails(self) -> None:
        (table,) = parse_ddl(_GET_DDL_TABLE, "app", dialect="oracle")
        types = {c["column_name"]: c["column_type"] for c in table["columns"]}
        assert types == {
            "ID": "NUMBER(10,0)",
            "AT_TZ": "TIMESTAMP(6) WITH TIME ZONE",
            "AT_LTZ": "TIMESTAMP(3) WITH LOCAL TIME ZONE",
            "AT": "TIMESTAMP(9)",
            "PAYLOAD": "LONG RAW",
            "DOC": "BFILE",
        }
        assert table["primary_key"] == ["ID"]

    @pytest.mark.parametrize(
        "written,column_type",
        [
            ("TIMESTAMP(6) WITH TIME ZONE", "TIMESTAMP(6) WITH TIME ZONE"),
            ("timestamp with time zone", "timestamp with time zone"),
            ("TIMESTAMP WITH LOCAL TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"),
            ("LONG RAW", "LONG RAW"),
            ("long  raw", "long raw"),
            ("LONG", "LONG"),
        ],
    )
    def test_oracle_multi_word_types(self, written: str, column_type: str) -> None:
        ddl = f"CREATE TABLE t (c1 {written} NOT NULL, c2 NUMBER(5));"
        (table,) = parse_ddl(ddl, "app", dialect="oracle")
        c1, c2 = table["columns"]
        assert (c1["column_name"], c1["column_type"], c1["is_nullable"]) == (
            "c1",
            column_type,
            "NO",
        )
        assert c2["column_type"] == "NUMBER(5)"

    @pytest.mark.passes_on_main  # the other dialects keep their parse
    def test_time_zone_words_are_oracle_only(self) -> None:
        """Another dialect keeps the type as before: the tail is Oracle's spelling here."""
        ddl = "CREATE TABLE t (c TIMESTAMP(6) WITH TIME ZONE);"
        (table,) = parse_ddl(ddl, "app", dialect="mysql")
        assert table["columns"][0]["column_type"] == "TIMESTAMP(6)"


# ---------------------------------------------------------------------------
# Review round on #2231: quoted names, spacing, comments, unreadable parts
# ---------------------------------------------------------------------------


class TestQuotedNamesAndSpacing:
    def test_a_bracketed_name_with_a_space_is_one_column_and_the_key_resolves(self) -> None:
        ddl = """CREATE TABLE [dbo].[Orders](
        \t[Order ID] [int] NOT NULL,
        \t[Placed At] [datetime2](7) NULL,
         CONSTRAINT [PK Orders] PRIMARY KEY CLUSTERED ([Order ID] ASC)
        ) ON [PRIMARY]"""
        (table,) = parse_ddl(ddl, "app", dialect="sqlserver")
        cols = [(c["column_name"], c["column_type"]) for c in table["columns"]]
        assert cols == [("Order ID", "int"), ("Placed At", "datetime2(7)")]
        assert table["primary_key"] == ["Order ID"]

    def test_an_escaped_bracket_in_a_name_is_kept(self) -> None:
        ddl = "CREATE TABLE [t] ([a]]b] [int] NOT NULL, [c] [int]);"
        (table,) = parse_ddl(ddl, "app", dialect="sqlserver")
        assert [c["column_name"] for c in table["columns"]] == ["a]b", "c"]

    @pytest.mark.parametrize(
        "ddl,dialect",
        [
            ("CREATE TABLE [dbo].[Order Lines] ([id] [int]);", "sqlserver"),
            ('CREATE TABLE "APP"."Order Lines" ("id" NUMBER(10));', "oracle"),
            ("CREATE TABLE `Order Lines` (`id` INT);", "mysql"),
        ],
    )
    def test_a_quoted_table_name_with_a_space_is_read(self, ddl: str, dialect: str) -> None:
        (table,) = parse_ddl(ddl, "app", dialect=dialect)
        assert table["table_name"] == "Order Lines"
        assert table["table_id"] == "app.Order Lines"

    def test_a_foreign_key_to_a_quoted_name_with_a_space(self) -> None:
        ddl = """CREATE TABLE [Lines] ([Order ID] [int],
          CONSTRAINT [FK Lines] FOREIGN KEY ([Order ID]) REFERENCES [dbo].[Order Heads] ([Order ID]));"""
        (table,) = parse_ddl(ddl, "app", dialect="sqlserver")
        (fk,) = table["foreign_keys"]
        assert (fk["constraint_name"], fk["columns"], fk["referenced_table"]) == (
            "FK Lines",
            ["Order ID"],
            "Order Heads",
        )
        assert fk["referenced_columns"] == ["Order ID"]

    @pytest.mark.parametrize(
        "written,column_type,max_length",
        [
            ("[datetime2] (7)", "datetime2(7)", 7),
            ("[nvarchar] (50)", "nvarchar(50)", 50),
            ("binary (16)", "binary(16)", 16),
            ("[varchar] (max)", "varchar(max)", None),
        ],
    )
    def test_a_space_before_the_parameters_keeps_them(
        self, written: str, column_type: str, max_length: int | None
    ) -> None:
        (table,) = parse_ddl(f"CREATE TABLE [t] ([c] {written} NULL);", "app", "sqlserver")
        (col,) = table["columns"]
        assert (col["column_type"], col["max_length"]) == (column_type, max_length)

    def test_oracle_time_zone_right_after_the_precision(self) -> None:
        ddl = "CREATE TABLE t (c TIMESTAMP(6)WITH TIME ZONE);"
        (table,) = parse_ddl(ddl, "app", dialect="oracle")
        assert table["columns"][0]["column_type"] == "TIMESTAMP(6) WITH TIME ZONE"

    def test_comments_in_the_column_list_are_not_columns(self) -> None:
        ddl = """CREATE TABLE [t] (
          [id] [int], -- a comment, with commas
          [x] [int], /* [y] [int], */ [z] [bigint]
        );"""
        (table,) = parse_ddl(ddl, "app", dialect="sqlserver")
        assert [c["column_name"] for c in table["columns"]] == ["id", "x", "z"]

    def test_a_comma_in_a_default_string_does_not_split_the_column(self) -> None:
        ddl = "CREATE TABLE t (s VARCHAR(10) DEFAULT 'a,b', n INT);"
        (table,) = parse_ddl(ddl, "app", dialect="mysql")
        assert [c["column_name"] for c in table["columns"]] == ["s", "n"]
        assert table["columns"][0]["column_default"] == "a,b"


class TestUnreadablePartsAreReported:
    def test_a_column_it_cannot_read_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        ddl = "CREATE TABLE [t] ([a] [dbo].[Phone] NULL, [b] [int] NULL);"
        with caplog.at_level("WARNING", logger="src.tools.database.ddl_parser"):
            (table,) = parse_ddl(ddl, "app", dialect="sqlserver")
        assert [c["column_name"] for c in table["columns"]] == ["b"]
        assert any(
            "app.t" in r.getMessage() and "column 1" in r.getMessage() for r in caplog.records
        )

    def test_a_primary_key_on_no_column_of_the_table_is_left_out_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ddl = "CREATE TABLE t (id INT, CONSTRAINT pk PRIMARY KEY (missing));"
        with caplog.at_level("WARNING", logger="src.tools.database.ddl_parser"):
            (table,) = parse_ddl(ddl, "app", dialect="mysql")
        assert table["primary_key"] == []
        assert not [i for i in table["indexes"] if i["is_primary"]]
        assert any("primary key" in r.getMessage() for r in caplog.records)

    def test_a_primary_key_matches_its_column_whatever_the_case(self) -> None:
        ddl = "CREATE TABLE [t] ([OrderID] [int], PRIMARY KEY ([orderid]));"
        (table,) = parse_ddl(ddl, "app", dialect="sqlserver")
        assert table["primary_key"] == ["OrderID"]

    @pytest.mark.passes_on_main  # temp tables were never read; the new warnings skip them
    def test_a_temporary_table_in_a_routine_body_is_not_a_table(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ddl = "CREATE PROCEDURE p AS BEGIN CREATE TABLE #tmp ([a] [int]); END\nGO\n"
        with caplog.at_level("WARNING", logger="src.tools.database.ddl_parser"):
            assert parse_ddl(ddl, "app", dialect="sqlserver") == []
        assert not caplog.records
