# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The repair screen (#1940): what a model's repaired routine must keep.

It runs every rule of the proof's own screen (``proof_screen.create_refusal``) on the
candidate, then the repair rules on top, each against the routine's own redacted text.
The cases are the review probes on #2369: every one must be refused, and the controls
must pass. The module is imported inside each test, so on a branch without it each test
fails on its own.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

import pytest


def _screen():
    return importlib.import_module("src.tools.routines.repair_screen")


# --------------------------------------------------------------------------- PostgreSQL

PG_SQL = (
    "CREATE OR REPLACE FUNCTION public.wal_pos(p integer)\n RETURNS text\n LANGUAGE sql\n"
    "AS $function$SELECT pg_current_xlog_location()::text || '?'$function$\n"
)
PG_SQL_FIXED = PG_SQL.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")

PLPGSQL = (
    "CREATE OR REPLACE FUNCTION public.report(p integer)\n RETURNS text\n LANGUAGE plpgsql\n"
    "AS $function$\nBEGIN\n  CREATE TEMP TABLE scratch (x integer);\n"
    "  INSERT INTO scratch VALUES (p);\n"
    "  RETURN pg_current_xlog_location()::text || '?' || (SELECT count(*) FROM public.orders);\n"
    "END\n$function$\n"
)
PLPGSQL_FIXED = PLPGSQL.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")

PG_TRIGGER = (
    "CREATE TRIGGER audit_orders BEFORE INSERT ON public.orders FOR EACH ROW "
    "EXECUTE FUNCTION public.audit_fn()"
)


def _pg(text: str, name: str = "wal_pos", kind: str = "function"):
    return _screen().RepairedRoutine(kind=kind, schema_name="public", name=name, create_text=text)


def _sub(old: str, new: str) -> Callable[[str], str]:
    return lambda t: t.replace(old, new)


PG_REFUSED: list[tuple[str, str, Callable[[str], str], str]] = [
    # Swapped or new objects.
    (
        "temp-table-made-persistent",
        PLPGSQL,
        _sub("CREATE TEMP TABLE scratch", "CREATE TABLE public.persist"),
        "adds_objects",
    ),
    (
        "second-temp-table",
        PLPGSQL,
        _sub(
            "  CREATE TEMP TABLE scratch (x integer);\n",
            "  CREATE TEMP TABLE scratch (x integer);\n  CREATE TEMP TABLE other (y integer);\n",
        ),
        "adds_objects",
    ),
    (
        "object-created-through-execute",
        PLPGSQL,
        _sub(
            "  INSERT INTO scratch VALUES (p);\n",
            "  INSERT INTO scratch VALUES (p);\n  EXECUTE 'CREATE FUNCTION public.evil() "
            "RETURNS int LANGUAGE sql AS $$SELECT 1$$';\n",
        ),
        None,  # refused; the screen's own rule or the literal rule, whichever reads it first
    ),
    # search_path, in the header or the body.
    (
        "search-path-header",
        PG_SQL,
        _sub("LANGUAGE sql\n", "LANGUAGE sql\n SET search_path = other\n"),
        "screen_refused",
    ),
    (
        "search-path-body",
        PLPGSQL,
        _sub("BEGIN\n", "BEGIN\n  SET search_path = other;\n"),
        "screen_refused",
    ),
    # GRANT and REVOKE.
    (
        "grant",
        PLPGSQL,
        _sub("BEGIN\n", "BEGIN\n  GRANT ALL ON public.orders TO PUBLIC;\n"),
        "screen_refused",
    ),
    (
        "revoke",
        PLPGSQL,
        _sub("BEGIN\n", "BEGIN\n  REVOKE ALL ON public.orders FROM app;\n"),
        "screen_refused",
    ),
    # A role change in dynamic SQL.
    (
        "alter-role-in-execute",
        PLPGSQL,
        _sub("BEGIN\n", "BEGIN\n  EXECUTE 'ALTER ROLE app SUPERUSER';\n"),
        "screen_refused",
    ),
    (
        "set-role-in-execute",
        PLPGSQL,
        _sub("BEGIN\n", "BEGIN\n  EXECUTE 'SET ROLE postgres';\n"),
        "screen_refused",
    ),
    # A Unicode-escaped schema name.
    (
        "unicode-escaped-schema",
        PLPGSQL,
        _sub("FROM public.orders", 'FROM U&"oth\\0065r".orders'),
        "screen_refused",
    ),
    # An added DML statement.
    (
        "added-delete",
        PLPGSQL,
        _sub("  INSERT INTO scratch", "  DELETE FROM public.orders;\n  INSERT INTO scratch"),
        "adds_statements",
    ),
    (
        "added-update",
        PLPGSQL,
        _sub("  INSERT INTO scratch", "  UPDATE public.orders SET id = id;\n  INSERT INTO scratch"),
        "adds_statements",
    ),
    # Who it runs as.
    (
        "security-definer-added",
        PG_SQL,
        _sub("LANGUAGE sql\n", "LANGUAGE sql\n SECURITY DEFINER\n"),
        "changes_privileges",
    ),
    # Literal placeholders: count, order and quote form.
    ("placeholder-dropped", PG_SQL, _sub(" || '?'", ""), "literals_changed"),
    ("literal-added", PG_SQL, _sub("|| '?'", "|| '?' || 'abc'"), "literals_changed"),
    ("placeholder-quote-form", PG_SQL, _sub("'?'", "E'?'"), "literals_changed"),
    ("placeholder-dollar-quoted", PG_SQL, _sub("'?'", "$q$?$q$"), "literals_changed"),
    # Smuggling and renames (pinned: the screen refused them before too).
    ("second-statement", PG_SQL, lambda t: t + "; DROP TABLE public.orders;", "not_one_statement"),
    (
        "statement-behind-line-comment",
        PG_SQL,
        lambda t: t + "-- harmless\n; DROP TABLE public.orders;",
        "not_one_statement",
    ),
    (
        "statement-behind-block-comment",
        PG_SQL,
        lambda t: t + "/* harmless */; DROP TABLE public.orders;",
        "not_one_statement",
    ),
    (
        "dollar-quote-closed-early",
        PG_SQL,
        _sub(
            "$function$\n", "$function$; DROP TABLE public.orders; SELECT $function$x$function$\n"
        ),
        "not_one_statement",
    ),
    ("quoted-upper-case-rename", PG_SQL, _sub("public.wal_pos", 'public."WAL_POS"'), "renamed"),
    ("moved-schema", PG_SQL, _sub("public.wal_pos", "audit.wal_pos"), "renamed"),
]


@pytest.mark.parametrize(
    ("original", "edit", "code"),
    [(o, e, c) for _, o, e, c in PG_REFUSED],
    ids=[i for i, *_ in PG_REFUSED],
)
def test_a_postgresql_probe_is_refused(original, edit, code):
    fixed = PLPGSQL_FIXED if original is PLPGSQL else PG_SQL_FIXED
    name = "report" if original is PLPGSQL else "wal_pos"
    refusal = _screen().refusal_of(_pg(original, name), edit(fixed), "postgresql")
    assert refusal is not None
    if code is not None:
        assert refusal[0] == code, refusal


@pytest.mark.parametrize(
    ("original", "candidate"),
    [
        (PG_SQL, PG_SQL_FIXED),
        (PG_SQL, PG_SQL_FIXED.replace("public.wal_pos", "PUBLIC.WAL_POS")),
        (PG_SQL, PG_SQL_FIXED.replace("public.wal_pos", '"public"."wal_pos"')),
        (PLPGSQL, PLPGSQL_FIXED),
    ],
    ids=["fixed", "unquoted-upper-case", "quoted-same-name", "plpgsql-keeps-its-temp-table"],
)
def test_a_postgresql_control_passes(original, candidate):
    name = "report" if original is PLPGSQL else "wal_pos"
    assert _screen().refusal_of(_pg(original, name), candidate, "postgresql") is None


def test_whitespace_alone_is_no_change():
    spaced = PG_SQL.replace(" LANGUAGE", "   LANGUAGE").replace("\n", "\n\n")
    assert _screen().refusal_of(_pg(PG_SQL), spaced, "postgresql")[0] == "unchanged"


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (_sub("BEFORE INSERT", "AFTER INSERT"), "signature_changed"),
        (_sub("BEFORE INSERT", "BEFORE DELETE"), "signature_changed"),
        (_sub("BEFORE INSERT", "BEFORE INSERT OR UPDATE"), "signature_changed"),
        (_sub("FOR EACH ROW", "FOR EACH STATEMENT"), "signature_changed"),
        (_sub("public.audit_fn()", "public.other_fn()"), "signature_changed"),
        (_sub("ON public.orders", "ON public.payments"), "renamed"),
    ],
    ids=["timing", "event", "added-event", "level", "function", "table"],
)
def test_a_trigger_keeps_its_timing_events_table_and_function(edit, code):
    trigger = _pg(PG_TRIGGER, "audit_orders", "trigger")
    refusal = _screen().refusal_of(trigger, edit(PG_TRIGGER), "postgresql")
    assert refusal is not None and refusal[0] == code, refusal


def test_a_trigger_that_changes_only_its_condition_passes():
    trigger = _pg(PG_TRIGGER, "audit_orders", "trigger")
    candidate = PG_TRIGGER.replace("FOR EACH ROW", "FOR EACH ROW WHEN (NEW.id > 0)")
    assert _screen().refusal_of(trigger, candidate, "postgresql") is None


# --------------------------------------------------------------------------- MySQL

MYSQL = (
    "CREATE DEFINER=`app`@`%` PROCEDURE `hashpw`(IN `p` varchar(64), OUT `r` varchar(64))\n"
    "BEGIN\n  DECLARE rank INT DEFAULT 1;\n  SELECT CONCAT(p, rank, '?') INTO r;\nEND"
)
MYSQL_FIXED = MYSQL.replace("rank", "rnk")
MYSQL_TRIGGER = (
    "CREATE DEFINER=`app`@`%` TRIGGER `audit_users` BEFORE INSERT ON `users` FOR EACH ROW "
    "SET NEW.rank = 1"
)


def _my(text: str, name: str = "hashpw", kind: str = "procedure"):
    return _screen().RepairedRoutine(kind=kind, schema_name="shop", name=name, create_text=text)


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (_sub("DEFINER=`app`@`%`", "DEFINER=`root`@`%`"), "changes_privileges"),
        (_sub("DEFINER=`app`@`%` ", ""), "changes_privileges"),
        (_sub("varchar(64))\n", "varchar(64))\n    SQL SECURITY INVOKER\n"), "changes_privileges"),
        (_sub("INTO r;", "INTO r;\n  GRANT ALL ON shop.* TO 'x'@'%';"), "screen_refused"),
        (_sub("BEGIN\n", "BEGIN\n  /*!50000 DROP TABLE users */;\n"), "not_one_statement"),
        (_sub("INTO r;", "INTO r;\n  DELETE FROM users;"), "adds_statements"),
        (_sub("INTO r;", "INTO r;\n  CREATE TABLE shop.extra (a int);"), "adds_objects"),
        (_sub(", '?')", ")"), "literals_changed"),
        (_sub("'?'", "_binary'?'"), "literals_changed"),
    ],
    ids=[
        "definer-changed",
        "definer-dropped",
        "security-added",
        "grant",
        "versioned-comment",
        "added-delete",
        "new-table",
        "placeholder-dropped",
        "introducer-added",
    ],
)
def test_a_mysql_probe_is_refused(edit, code):
    refusal = _screen().refusal_of(_my(MYSQL), edit(MYSQL_FIXED), "mysql")
    assert refusal is not None and refusal[0] == code, refusal


def test_a_mysql_repair_passes():
    assert _screen().refusal_of(_my(MYSQL), MYSQL_FIXED, "mysql") is None


@pytest.mark.parametrize(
    "edit",
    [
        _sub("BEFORE INSERT", "AFTER INSERT"),
        _sub("BEFORE INSERT", "BEFORE DELETE"),
        _sub("FOR EACH ROW", "FOR EACH ROW FOLLOWS `other`"),
    ],
    ids=["timing", "event", "order"],
)
def test_a_mysql_trigger_keeps_its_timing_event_and_order(edit):
    trigger = _my(MYSQL_TRIGGER, "audit_users", "trigger")
    fixed = MYSQL_TRIGGER.replace("NEW.rank", "NEW.`rank`")
    refusal = _screen().refusal_of(trigger, edit(fixed), "mysql")
    assert refusal is not None and refusal[0] == "signature_changed", refusal
    assert _screen().refusal_of(trigger, fixed, "mysql") is None
