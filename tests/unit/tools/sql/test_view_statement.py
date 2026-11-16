# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A view's CREATE reaches a script only as exactly one view statement (#2082, ADR-034
Decision 5): the #2333 review's hostile definitions, on both engines."""

from __future__ import annotations

import pytest

from src.agents.collector.view_sources import view_body_status, view_create
from src.tools.sql.view_statement import (
    AMBIGUOUS,
    CLIENT_COMMAND,
    NOT_ONE_STATEMENT,
    statement_problem,
)

_COLS = [{"name": "id", "position": 1, "data_type": "bigint"}]

#: Definitions that end their statement early, close a literal early, carry a client
#: command or leave their statement open (the #2333 review's 12, and a few more).
PG_HOSTILE = [
    ("SELECT 1::bigint AS id; DROP TABLE public.audit_log; --", NOT_ONE_STATEMENT),
    ("SELECT 1::bigint AS id\n\\! touch /tmp/v2082-pwned\n", CLIENT_COMMAND),
    ("SELECT 'x''' AS id; DROP TABLE public.orders; --'", NOT_ONE_STATEMENT),
    ("SELECT 1::bigint AS id; COMMIT; DROP TABLE public.orders", NOT_ONE_STATEMENT),
    ("SELECT 1::bigint AS id -- the ; is swallowed", NOT_ONE_STATEMENT),
    ("SELECT 1::bigint AS id /* left open", NOT_ONE_STATEMENT),
    ("SELECT 1::bigint AS id\n\\i /etc/passwd", CLIENT_COMMAND),
    ("SELECT :USER::bigint AS id", CLIENT_COMMAND),
]
MY_HOSTILE = [
    ("select 1 AS `id`; drop table `shop`.`audit_log`", NOT_ONE_STATEMENT),
    ("select 1 AS `id`\nsystem touch /tmp/v2082-pwned", CLIENT_COMMAND),
    ("select 'x''' AS `id`; drop table `shop`.`orders`; -- '", NOT_ONE_STATEMENT),
    ("select 1 AS `id`\ndelimiter //", CLIENT_COMMAND),
    ("select 1 AS `id` \\! ls", CLIENT_COMMAND),
    ("select 1 AS `id`\nsource /tmp/x.sql", CLIENT_COMMAND),
    ("select 1 AS `id` # the ; is swallowed", NOT_ONE_STATEMENT),
    ("select 1 AS `id` -- the ; is swallowed", NOT_ONE_STATEMENT),
]


def _pg(definition: str, **kw):
    return {"view_name": "v", "schema_name": "public", "definition": definition,
            "check_option": "NONE", "columns": _COLS, **kw}  # fmt: skip


def _my(definition: str, **kw):
    return {"view_name": "v", "schema_name": "shop", "definition": definition,
            "check_option": "NONE", "security_type": "DEFINER", "columns": _COLS, **kw}  # fmt: skip


@pytest.mark.parametrize("definition,code", PG_HOSTILE)
def test_a_hostile_postgresql_definition_is_refused(definition, code):
    text = 'CREATE OR REPLACE VIEW "public"."v" ("id") AS\n' + definition + ";"
    assert statement_problem("postgresql", text, "public", "v") == code
    assert view_create("postgresql", _pg(definition)) is None
    assert view_body_status("postgresql", _pg(definition), include_bodies=True) == "not_supported"


@pytest.mark.parametrize("definition,code", MY_HOSTILE)
def test_a_hostile_mysql_definition_is_refused(definition, code):
    text = "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`v` (`id`) AS\n" + definition + ";"
    assert statement_problem("mysql", text, "shop", "v") == code
    assert view_create("mysql", _my(definition)) is None


@pytest.mark.parametrize(
    "engine,definition",
    [
        (
            "postgresql",
            " SELECT o.id,\n    o.note::text AS note\n   FROM public.orders o\n  WHERE o.note <> 'a; b -- c';",
        ),
        ("postgresql", " SELECT 'it''s'::text AS id\nUNION ALL\n SELECT $$x;y$$::text AS id;"),
        (
            "postgresql",
            " WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT c.n + 1 FROM c WHERE c.n < 3)\n SELECT c.n AS id FROM c;",
        ),
        ("mysql", "select count(0) AS `id` from `shop`.`orders` union select 2 AS `2`"),
    ],
)
def test_a_server_printed_definition_passes(engine, definition):
    row = (_pg if engine == "postgresql" else _my)(definition)
    assert view_create(engine, row) is not None


def test_a_statement_for_another_view_or_not_a_view_is_refused():
    assert statement_problem("postgresql", 'CREATE OR REPLACE VIEW "public"."w" ("id") AS SELECT 1;',
                             "public", "v") == NOT_ONE_STATEMENT  # fmt: skip
    assert statement_problem("postgresql", 'CREATE VIEW "public"."v" ("id") AS SELECT 1;',
                             "public", "v") == NOT_ONE_STATEMENT  # fmt: skip
    assert statement_problem("postgresql", "CREATE TABLE public.v (id int);", "public", "v") == (
        NOT_ONE_STATEMENT)  # fmt: skip
    assert statement_problem("mysql", "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`w` "
                             "(`id`) AS\nselect 1 AS `id`;", "shop", "v") == NOT_ONE_STATEMENT  # fmt: skip
    assert (
        statement_problem("sqlserver", "CREATE VIEW v AS SELECT 1", "dbo", "v") == NOT_ONE_STATEMENT
    )


@pytest.mark.parametrize(
    "engine,text,schema",
    [
        # The #2333 review's round-2 payloads: each ends its statement elsewhere when the
        # target reads backslashes the other way.
        (
            "postgresql",
            'CREATE OR REPLACE VIEW "public"."v" ("a", "id") AS\n'
            "SELECT 'x\\' AS a, '\\! touch /tmp/v2082-pwned #' AS id;",
            "public",
        ),
        (
            "mysql",
            "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`v` (`id`, `x`, `z`, `w`) AS\n"
            "select 'a\\' AS id, 1 AS x; DROP TABLE shop.audit_log; select ' AS z, 2 AS w;",
            "shop",
        ),
        # A MySQL literal with an escaped quote ends elsewhere under NO_BACKSLASH_ESCAPES too:
        # refused on the safe side (create such a view yourself).
        (
            "mysql",
            "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`v` (`id`) AS\n"
            "select _utf8mb4'it\\'s; x' AS `id`;",
            "shop",
        ),
    ],
)
def test_text_whose_end_depends_on_how_backslashes_are_read_is_refused(engine, text, schema):
    assert statement_problem(engine, text, schema, "v") == AMBIGUOUS


@pytest.mark.parametrize(
    "engine,text,schema",
    [
        (
            "postgresql",
            'CREATE OR REPLACE VIEW "public"."v" ("id") AS\n SELECT \'C:\\temp\'::text AS id;',
            "public",
        ),
        (
            "postgresql",
            'CREATE OR REPLACE VIEW "public"."v" ("id") AS\n SELECT E\'it\\\'s\'::text AS id;',
            "public",
        ),
        (
            "mysql",
            "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`v` (`id`) AS\n"
            "select 'C:\\\\temp' AS `id`;",
            "shop",
        ),
    ],
)
def test_a_backslash_both_readings_agree_on_passes(engine, text, schema):
    assert statement_problem(engine, text, schema, "v") is None


def test_a_client_command_word_inside_a_multi_line_literal_is_data():
    head = "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`v` (`id`) AS\n"
    assert (
        statement_problem(
            "mysql", head + "select 'line one\nsystem of record' AS `id`;", "shop", "v"
        )
        is None
    )
    assert (
        statement_problem("mysql", head + "select 'a\nsource: web' AS `id`;", "shop", "v") is None
    )
    assert (
        statement_problem("mysql", head + "select 1 AS `id`\nsystem ls;", "shop", "v")
        == CLIENT_COMMAND
    )


def test_mysql_text_must_end_at_its_only_semicolon():
    """Only the ';'-as-last-character rule catches this: sqlglot reads the query as one."""
    head = "CREATE OR REPLACE SQL SECURITY INVOKER VIEW `shop`.`v` (`id`) AS\n"
    assert statement_problem("mysql", head + "select 1 AS `id`;;", "shop", "v") == NOT_ONE_STATEMENT
    assert statement_problem("mysql", head + "select 1 AS `id`", "shop", "v") == NOT_ONE_STATEMENT
