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


#: The new calls the failed rule's fix hint names (the routine-migration catalog's).
FIX_CALLS = frozenset({"pg_current_wal_lsn"})


def _pg(text: str, name: str = "wal_pos", kind: str = "function"):
    return _screen().RepairedRoutine(
        kind=kind, schema_name="public", name=name, create_text=text, allowed_calls=FIX_CALLS
    )


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


PG_TRIGGER_WHEN = PG_TRIGGER.replace("FOR EACH ROW", "FOR EACH ROW WHEN (NEW.id > 0)")


@pytest.mark.parametrize(
    ("original", "candidate"),
    [
        (PG_TRIGGER, PG_TRIGGER_WHEN),
        (PG_TRIGGER_WHEN, PG_TRIGGER_WHEN.replace("NEW.id > 0", "false")),
        (PG_TRIGGER_WHEN, PG_TRIGGER_WHEN.replace("NEW.id > 0", "NEW.id >= 0")),
        (PG_TRIGGER_WHEN, PG_TRIGGER),
    ],
    ids=["when-added", "when-false", "when-changed", "when-dropped"],
)
def test_a_trigger_keeps_its_when_clause(original, candidate):
    # A repair never changes when a trigger fires.
    trigger = _pg(original, "audit_orders", "trigger")
    refusal = _screen().refusal_of(trigger, candidate, "postgresql")
    assert refusal is not None and refusal[0] == "signature_changed", refusal


# --------------------------------------------------------------------------- round 2

PG_INTO_SQL = (
    "CREATE OR REPLACE FUNCTION public.wal_pos(p integer)\n RETURNS text\n LANGUAGE sql\n"
    "AS $function$SELECT pg_current_xlog_location()::text || '?'$function$\n"
)


@pytest.mark.parametrize(
    "into",
    [
        "SELECT * INTO public.orders_copy FROM public.orders; ",
        "SELECT * INTO TEMP t2 FROM public.orders; ",
        "SELECT * INTO UNLOGGED t3 FROM public.orders; ",
        "SELECT * INTO TABLE t4 FROM public.orders; ",
    ],
    ids=["into-table", "into-temp", "into-unlogged", "into-table-keyword"],
)
def test_select_into_in_a_sql_body_creates_an_object(into):
    candidate = PG_SQL_FIXED.replace("AS $function$", "AS $function$" + into)
    refusal = _screen().refusal_of(_pg(PG_SQL), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_objects", refusal


def test_select_into_in_plpgsql_creates_an_object_only_as_a_table():
    # In PL/pgSQL, SELECT ... INTO <variable> assigns: no object, and a repair may add one.
    declare = PLPGSQL_FIXED.replace(
        "BEGIN\n", "DECLARE n integer;\nBEGIN\n  SELECT count(*) INTO n FROM public.orders;\n"
    )
    assert _screen().refusal_of(_pg(PLPGSQL, "report"), declare, "postgresql") is None
    # INTO TEMP / UNLOGGED / TABLE names a table, in the body and in an EXECUTEd string.
    from src.tools.routines.proof_screen import created_objects

    for into in ("TEMP t2", "UNLOGGED t3", "TABLE t4"):
        body = PLPGSQL.replace("BEGIN\n", f"BEGIN\n  SELECT * INTO {into} FROM public.orders;\n")
        added = created_objects(body, "postgresql") - created_objects(PLPGSQL, "postgresql")
        assert [k[1:] for k in added] == [("TABLE", "", into.split()[1])], (into, added)
        refusal = _screen().refusal_of(_pg(PLPGSQL, "report"), body, "postgresql")
        assert refusal is not None, into
    run = PLPGSQL.replace(
        "BEGIN\n", "BEGIN\n  EXECUTE 'SELECT * INTO public.orders_copy FROM public.orders';\n"
    )
    added = created_objects(run, "postgresql") - created_objects(PLPGSQL, "postgresql")
    assert ("", "TABLE", "public", "orders_copy") in added


@pytest.mark.parametrize(
    "statement",
    [
        "  NOTIFY chan;\n",
        "  COMMENT ON TABLE public.orders IS NULL;\n",
        "  LOCK TABLE public.orders;\n",
        "  CALL public.purge();\n",
        "  REFRESH MATERIALIZED VIEW public.totals;\n",
    ],
    ids=["notify", "comment-on", "lock", "call", "refresh"],
)
def test_a_new_postgresql_write_statement_is_refused_for_its_own_reason(statement):
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "BEGIN\n" + statement)
    refusal = _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.parametrize(
    "call",
    [
        "public.purge_everything()",
        "pg_catalog.pg_terminate_backend(p)",
        "pg_terminate_backend(p)",
        "pg_catalog.lo_unlink(p)",
        "dblink_exec(p::text)",
        "nextval(p::regclass)",
    ],
    ids=[
        "user-function",
        "terminate-qualified",
        "terminate",
        "lo-unlink",
        "dblink-exec",
        "nextval",
    ],
)
def test_a_call_the_original_does_not_make_is_refused(call):
    candidate = PG_SQL_FIXED.replace(
        "SELECT pg_current_wal_lsn()", f"SELECT {call}, pg_current_wal_lsn()"
    )
    refusal = _screen().refusal_of(_pg(PG_SQL), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_calls", refusal


def test_a_repair_calling_only_the_originals_functions_or_the_fix_passes():
    # count() and pg_current_xlog_location() are the original's; pg_current_wal_lsn() is
    # the one the failed rule's fix hint names. Nothing else is called.
    candidate = PLPGSQL_FIXED.replace("(SELECT count(*)", "(SELECT count(*) + 0")
    assert _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql") is None
    without_hint = _screen().RepairedRoutine(
        kind="function", schema_name="public", name="wal_pos", create_text=PG_SQL
    )
    refusal = _screen().refusal_of(without_hint, PG_SQL_FIXED, "postgresql")
    assert refusal is not None and refusal[0] == "adds_calls", refusal


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ("  RENAME TABLE users TO users_old;", "adds_statements"),
        ("  CALL shop.purge();", "adds_statements"),
        ("  SELECT purge_all() INTO @x;", "adds_calls"),
    ],
    ids=["rename-table", "call", "function"],
)
def test_a_new_mysql_write_is_refused_for_its_own_reason(statement, code):
    candidate = MYSQL_FIXED.replace("INTO r;", "INTO r;\n" + statement)
    refusal = _screen().refusal_of(_my(MYSQL), candidate, "mysql")
    assert refusal is not None and refusal[0] == code, refusal


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


# --------------------------------------------------------------------------- dynamic SQL
# #1941 review: a candidate may not add a statement built at run time, which no screen can
# read: a new PL/pgSQL EXECUTE (of a parameter, a format() call, a literal, with USING or
# INTO), or a new MySQL PREPARE or EXECUTE. Counted by kind, as created objects are.

PG_DYNAMIC = (
    "CREATE OR REPLACE FUNCTION public.run_it(p integer)\n RETURNS integer\n LANGUAGE plpgsql\n"
    "AS $function$\nBEGIN\n  EXECUTE format('?', p);\n  RETURN p;\nEND\n$function$\n"
)


@pytest.mark.parametrize(
    "added",
    [
        "  EXECUTE p::text;\n",
        "  EXECUTE p::text INTO STRICT p;\n",
        "  EXECUTE format('?', p) USING p;\n",
        "  EXECUTE 'SELECT 1';\n",
    ],
    ids=["parameter", "into-strict", "format-using", "literal"],
)
def test_a_postgresql_candidate_that_adds_dynamic_sql_is_refused(added):
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "BEGIN\n" + added)
    refusal = _screen().refusal_of(_pg(PLPGSQL, name="report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_dynamic_sql", refusal


def test_the_dynamic_sql_the_routine_already_runs_is_not_held_against_it():
    candidate = PG_DYNAMIC.replace("RETURN p;", "RETURN p + 0;")
    assert _screen().refusal_of(_pg(PG_DYNAMIC, name="run_it"), candidate, "postgresql") is None
    # A second one of the same kind is still new.
    twice = candidate.replace("RETURN p + 0;", "EXECUTE format('?', p);\n  RETURN p + 0;")
    refusal = _screen().refusal_of(_pg(PG_DYNAMIC, name="run_it"), twice, "postgresql")
    assert refusal is not None and refusal[0] == "adds_dynamic_sql", refusal


@pytest.mark.parametrize(
    "added",
    ["  PREPARE st FROM @s;\n", "  EXECUTE st;\n"],
    ids=["prepare", "execute"],
)
def test_a_mysql_candidate_that_adds_dynamic_sql_is_refused(added):
    candidate = MYSQL_FIXED.replace("INTO r;\n", "INTO r;\n" + added)
    refusal = _screen().refusal_of(_my(MYSQL), candidate, "mysql")
    assert refusal is not None and refusal[0] == "adds_dynamic_sql", refusal


# --------------------------------------------------------------------------- #2373
# Statement kinds are an allowlist: a candidate may run the kinds of statement the routine
# runs, plus the reads and control statements every routine uses. Any other kind is new.


@pytest.mark.parametrize(
    "statement",
    [
        "  CLUSTER public.orders;\n",
        "  REINDEX TABLE public.orders;\n",
        "  SECURITY LABEL ON TABLE public.orders IS NULL;\n",
        "  VACUUM public.orders;\n",
        "  ANALYZE public.orders;\n",
        "  CHECKPOINT;\n",
        "  LISTEN chan;\n",
        "  DISCARD PLANS;\n",
        "  SET LOCAL statement_timeout = 0;\n",
        "  COMMIT;\n",
    ],
    ids=[
        "cluster",
        "reindex",
        "security-label",
        "vacuum",
        "analyze",
        "checkpoint",
        "listen",
        "discard",
        "set-local",
        "commit",
    ],
)
def test_a_postgresql_statement_kind_the_routine_does_not_run_is_refused(statement):
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "BEGIN\n" + statement)
    refusal = _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


def test_a_statement_kind_in_a_sql_body_is_read_too():
    candidate = PG_SQL_FIXED.replace("AS $function$", "AS $function$VACUUM public.orders; ")
    refusal = _screen().refusal_of(_pg(PG_SQL), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.parametrize(
    "statement",
    [
        "  LOCK TABLES users WRITE;",
        "  UNLOCK TABLES;",
        "  HANDLER users OPEN;",
        "  FLUSH TABLES;",
        "  START TRANSACTION;",
        "  COMMIT;",
        "  SET autocommit = 0;",
        "  SET SESSION foreign_key_checks = 0;",
        "  SET @@foreign_key_checks = 0;",
        "  SET NAMES utf8mb4;",
    ],
    ids=[
        "lock-tables",
        "unlock-tables",
        "handler",
        "flush",
        "start-transaction",
        "commit",
        "system-variable",
        "session-variable",
        "session-parameter",
        "names",
    ],
)
def test_a_mysql_statement_kind_the_routine_does_not_run_is_refused(statement):
    candidate = MYSQL_FIXED.replace("INTO r;", "INTO r;\n" + statement)
    refusal = _screen().refusal_of(_my(MYSQL), candidate, "mysql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "added",
    [
        "  IF p IS NULL THEN\n    RETURN NULL;\n  END IF;\n",
        "  FOR i IN 1..p LOOP\n    NULL;\n  END LOOP;\n",
        "  PERFORM 1;\n",
    ],
    ids=["if-return", "for-loop", "perform"],
)
def test_a_postgresql_read_or_control_statement_may_be_added(added):
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "BEGIN\n" + added)
    assert _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql") is None
    assign = PLPGSQL_FIXED.replace(
        "BEGIN\n", "DECLARE n integer;\nBEGIN\n  n := p + 1;\n  GET DIAGNOSTICS n = ROW_COUNT;\n"
    )
    assert _screen().refusal_of(_pg(PLPGSQL, "report"), assign, "postgresql") is None


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "added",
    [
        "  SET rnk = rnk + 1;",
        "  SET r = CONCAT(p, rnk);",
        "  SET @seen = 1;",
        "  IF p IS NULL THEN\n    SET rnk = 0;\n  END IF;",
        "  WITH q AS (SELECT 1 AS a) SELECT a INTO rnk FROM q;",
    ],
    ids=["local-variable", "parameter", "user-variable", "if", "with-select"],
)
def test_a_mysql_variable_assignment_or_control_statement_may_be_added(added):
    candidate = MYSQL_FIXED.replace("INTO r;", "INTO r;\n" + added)
    assert _screen().refusal_of(_my(MYSQL), candidate, "mysql") is None


@pytest.mark.passes_on_main
def test_a_statement_kind_the_routine_already_runs_is_not_held_against_it():
    original = PLPGSQL.replace("BEGIN\n", "BEGIN\n  ANALYZE public.orders;\n")
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "BEGIN\n  ANALYZE public.orders;\n")
    assert _screen().refusal_of(_pg(original, "report"), candidate, "postgresql") is None
    mysql = MYSQL.replace("INTO r;", "INTO r;\n  SET autocommit = 1;")
    assert _screen().refusal_of(_my(mysql), mysql.replace("rank", "rnk"), "mysql") is None


def test_a_setting_the_routine_does_not_set_is_a_new_kind():
    original = PLPGSQL.replace("BEGIN\n", "BEGIN\n  SET LOCAL statement_timeout = 0;\n")
    candidate = PLPGSQL_FIXED.replace(
        "BEGIN\n", "BEGIN\n  SET LOCAL statement_timeout = 0;\n  SET LOCAL lock_timeout = 0;\n"
    )
    refusal = _screen().refusal_of(_pg(original, "report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal
    mysql = MYSQL.replace("INTO r;", "INTO r;\n  SET autocommit = 1;")
    added = mysql.replace("rank", "rnk").replace(
        "SET autocommit = 1;", "SET autocommit = 1;\n  SET foreign_key_checks = 0;"
    )
    refusal = _screen().refusal_of(_my(mysql), added, "mysql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


def test_a_nested_statement_is_read_by_its_own_kind():
    from src.tools.routines.proof_screen import run_statements

    cte = PLPGSQL.replace(
        "BEGIN\n", "BEGIN\n  WITH d AS (DELETE FROM public.orders RETURNING 1) SELECT 1;\n"
    )
    assert {"DeleteStmt", "SelectStmt"} <= run_statements(cte, "postgresql", "function")
    mysql = MYSQL.replace("INTO r;", "INTO r;\n  WITH q AS (SELECT 1) DELETE FROM users;")
    assert "DELETE" in run_statements(mysql, "mysql", "procedure")


def test_a_candidate_whose_body_the_parser_cannot_read_is_refused():
    candidate = PG_SQL_FIXED.replace("LANGUAGE sql", "LANGUAGE plpython3u")
    refusal = _screen().refusal_of(_pg(PG_SQL), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "not_one_statement", refusal
    # An original in such a language runs nothing the screen reads: a rewrite in SQL may
    # run reads only.
    python = (
        "CREATE OR REPLACE FUNCTION public.wal_pos(p integer)\n RETURNS text\n"
        " LANGUAGE plpython3u\nAS $function$return p$function$\n"
    )
    rewrite = python.replace("plpython3u", "sql").replace("return p", "SELECT p::text")
    assert _screen().refusal_of(_pg(python), rewrite, "postgresql") is None
    vacuum = rewrite.replace("SELECT p::text", "VACUUM; SELECT p::text")
    refusal = _screen().refusal_of(_pg(python), vacuum, "postgresql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


# Operators and casts: a function reached without its name. A candidate may use the
# operators and types the routine uses, PostgreSQL's built-in operator symbols written
# unqualified, and the types of pg_catalog.


@pytest.mark.parametrize(
    "expression",
    [
        "1 OPERATOR(public.===) 2",
        "p OPERATOR(public.+) 1",
        "p OPERATOR(pg_catalog.+) 1",
        "p === 1",
        "p @@@@ 1",
        "p::public.evil_type",
        "p::citext",
    ],
    ids=[
        "qualified-operator",
        "qualified-plus",
        "qualified-catalog-plus",
        "unknown-symbol",
        "unknown-long-symbol",
        "cast-to-user-type",
        "cast-to-extension-type",
    ],
)
def test_an_operator_or_cast_the_routine_does_not_use_is_refused(expression):
    candidate = PG_SQL_FIXED.replace(
        "SELECT pg_current_wal_lsn()", f"SELECT ({expression}), pg_current_wal_lsn()"
    )
    refusal = _screen().refusal_of(_pg(PG_SQL), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_calls", refusal


@pytest.mark.parametrize(
    "added",
    [
        "  IF p === 0 THEN\n    RETURN NULL;\n  END IF;\n",
        "  RETURN p::public.evil_type::text;\n",
    ],
    ids=["operator-in-a-condition", "cast-in-a-return"],
)
def test_an_operator_or_cast_in_a_plpgsql_expression_is_read(added):
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "BEGIN\n" + added)
    refusal = _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_calls", refusal


def test_a_plpgsql_variable_of_a_type_the_routine_does_not_use_is_refused():
    # Assigning to it runs the type's assignment cast.
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "DECLARE v public.evil_type;\nBEGIN\n")
    refusal = _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_calls", refusal


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "expression",
    [
        "p + 1",
        "p <> 1 AND p >= 0",
        "p BETWEEN 1 AND 2",
        "p::text ~~ p::text",
        "p::bigint",
        "p::numeric(10, 2)",
        "p::pg_catalog.int8",
        "p::double precision",
        "ARRAY[p]::integer[]",
    ],
    ids=[
        "plus",
        "comparisons",
        "between",
        "like-operator",
        "cast-bigint",
        "cast-numeric",
        "cast-qualified-catalog",
        "cast-sql-type-name",
        "cast-array",
    ],
)
def test_a_built_in_operator_or_catalog_cast_passes(expression):
    candidate = PG_SQL_FIXED.replace(
        "SELECT pg_current_wal_lsn()", f"SELECT ({expression}), pg_current_wal_lsn()"
    )
    assert _screen().refusal_of(_pg(PG_SQL), candidate, "postgresql") is None


@pytest.mark.passes_on_main
def test_the_operators_and_types_the_routine_uses_are_not_held_against_it():
    uses = "SELECT (1 OPERATOR(public.===) 2), p::public.evil_type, pg_current_xlog_location()"
    original = PG_SQL.replace("SELECT pg_current_xlog_location()", uses)
    candidate = original.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")
    assert _screen().refusal_of(_pg(original), candidate, "postgresql") is None


@pytest.mark.passes_on_main
def test_mysql_operators_and_casts_are_read_in_mysql():
    # MySQL has no user-defined operators or types: its own operators and cast types pass.
    candidate = MYSQL_FIXED.replace("CONCAT(p, rnk, '?')", "CONCAT(p <=> rnk, rnk DIV 2, '?')")
    assert _screen().refusal_of(_my(MYSQL), candidate, "mysql") is None


@pytest.mark.passes_on_main
def test_a_moved_placeholder_passes_the_screen_and_is_left_to_the_proof():
    # The screen checks the placeholders' count and quote form, never their places (#1941
    # rebuilds the exact text by position and the side-by-side proof tells them apart).
    moved = PG_SQL_FIXED.replace(
        "SELECT pg_current_wal_lsn()::text || '?'", "SELECT '?' || pg_current_wal_lsn()::text"
    )
    assert _screen().refusal_of(_pg(PG_SQL), moved, "postgresql") is None


# --------------------------------------------------------------------------- #2399 review


@pytest.mark.parametrize(
    ("original", "clause"),
    [
        (PG_SQL, " SET statement_timeout = 0\n"),
        (PG_SQL, " SET session_replication_role = replica\n"),
        (PLPGSQL, " SET row_security = off\n"),
        (PLPGSQL, " SET lock_timeout FROM CURRENT\n"),
    ],
    ids=["statement-timeout", "replication-role", "row-security", "from-current"],
)
def test_a_header_set_clause_is_a_statement_kind(original, clause):
    # A function's own SET clause changes a setting for the call and restores it after.
    fixed = PLPGSQL_FIXED if original is PLPGSQL else PG_SQL_FIXED
    name = "report" if original is PLPGSQL else "wal_pos"
    candidate = fixed.replace(" LANGUAGE ", clause + " LANGUAGE ", 1)
    refusal = _screen().refusal_of(_pg(original, name), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.passes_on_main
def test_a_header_set_clause_the_routine_has_is_kept():
    original = PG_SQL.replace(" LANGUAGE sql", " SET statement_timeout = 0\n LANGUAGE sql")
    candidate = original.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")
    assert _screen().refusal_of(_pg(original), candidate, "postgresql") is None


def test_set_local_and_set_are_different_kinds():
    original = PLPGSQL.replace("BEGIN\n", "BEGIN\n  SET LOCAL statement_timeout = 0;\n")
    candidate = PLPGSQL_FIXED.replace("BEGIN\n", "BEGIN\n  SET statement_timeout = 0;\n")
    refusal = _screen().refusal_of(_pg(original, "report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.parametrize(
    "setting", ["foreign_key_checks", "unique_checks", "sql_log_bin", "max_execution_time"]
)
def test_a_mysql_set_sees_only_the_declarations_in_scope(setting):
    # The inner block's variable is gone after its END: the SET there sets the server's.
    added = f"  BEGIN\n    DECLARE {setting} INT;\n  END;\n  SET {setting} = 0;"
    candidate = MYSQL_FIXED.replace("INTO r;", "INTO r;\n" + added)
    refusal = _screen().refusal_of(_my(MYSQL), candidate, "mysql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.passes_on_main
def test_a_mysql_set_of_a_variable_declared_in_its_own_block_passes():
    added = "  BEGIN\n    DECLARE n INT;\n    SET n = 0;\n  END;"
    candidate = MYSQL_FIXED.replace("INTO r;", "INTO r;\n" + added)
    assert _screen().refusal_of(_my(MYSQL), candidate, "mysql") is None


@pytest.mark.parametrize(
    "read",
    [
        "(SELECT o.evil FROM public.orders o LIMIT 1)",
        "(SELECT (o).evil FROM public.orders o LIMIT 1)",
    ],
    ids=["column-notation", "field-selection"],
)
def test_attribute_notation_the_routine_does_not_use_is_a_call(read):
    # o.evil runs evil(o) when orders has no column evil; the screen cannot tell which.
    candidate = PLPGSQL_FIXED.replace("(SELECT count(*)", f"{read} || (SELECT count(*)")
    refusal = _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_calls", refusal


@pytest.mark.passes_on_main
def test_attribute_notation_of_a_column_the_routine_names_passes():
    # x is a column the routine's own table defines.
    candidate = PLPGSQL_FIXED.replace(
        "(SELECT count(*)", "(SELECT s.x FROM scratch s LIMIT 1) || (SELECT count(*)"
    )
    assert _screen().refusal_of(_pg(PLPGSQL, "report"), candidate, "postgresql") is None


@pytest.mark.parametrize("target", ["OUTFILE", "DUMPFILE"])
def test_a_mysql_select_into_a_file_is_a_write(target):
    # The '?' moved from the CONCAT: the rebuilt text writes a file the literal names.
    candidate = MYSQL_FIXED.replace(
        "SELECT CONCAT(p, rnk, '?') INTO r;",
        f"SELECT CONCAT(p, rnk) INTO r;\n  SELECT rnk INTO {target} '?';",
    )
    refusal = _screen().refusal_of(_my(MYSQL), candidate, "mysql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.parametrize("cast", ["p::internal", "NULL::cstring", "p::anyelement"])
def test_a_cast_to_a_pseudo_type_is_refused(cast):
    candidate = PG_SQL_FIXED.replace(
        "SELECT pg_current_wal_lsn()", f"SELECT ({cast}), pg_current_wal_lsn()"
    )
    refusal = _screen().refusal_of(_pg(PG_SQL), candidate, "postgresql")
    assert refusal is not None and refusal[0] == "adds_calls", refusal


# --------------------------------------------------------------------------- #2399 round 2
# A MySQL SET of a name is a local assignment only where the body reader has that name in
# scope: its compound statements, never keyword counts, say where a block ends.

MYSQL_DROP = MYSQL.replace("INTO r;", "INTO r;\n  DROP TEMPORARY TABLE IF EXISTS tmp;")


@pytest.mark.parametrize("setting", ["foreign_key_checks", "optimizer_trace_limit"])
def test_an_if_exists_in_an_inner_block_does_not_keep_its_names_in_scope(setting):
    candidate = MYSQL_DROP.replace("rank", "rnk").replace(
        "  DROP TEMPORARY TABLE IF EXISTS tmp;",
        f"  BEGIN\n    DECLARE {setting} INT;\n    DROP TEMPORARY TABLE IF EXISTS tmp;\n"
        f"  END;\n  SET {setting} = 0;",
    )
    refusal = _screen().refusal_of(_my(MYSQL_DROP), candidate, "mysql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.parametrize("setting", ["unique_checks", "optimizer_trace_offset"])
def test_a_begin_alias_does_not_open_a_block(setting):
    added = (
        f"  BEGIN\n    DECLARE {setting} INT;\n    SELECT 1 AS begin INTO {setting};\n"
        f"  END;\n  SET {setting} = 0;"
    )
    candidate = MYSQL_FIXED.replace("INTO r;", "INTO r;\n" + added)
    refusal = _screen().refusal_of(_my(MYSQL), candidate, "mysql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


def test_a_declared_variable_named_like_a_server_variable_is_refused():
    added = "  BEGIN\n    DECLARE foreign_key_checks INT;\n    SET foreign_key_checks = 0;\n  END;"
    candidate = MYSQL_FIXED.replace("INTO r;", "INTO r;\n" + added)
    refusal = _screen().refusal_of(_my(MYSQL), candidate, "mysql")
    assert refusal is not None and refusal[0] == "adds_statements", refusal


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "compound",
    [
        "  IF (p IS NULL) THEN\n    SET rank = 0;\n  END IF;",
        "  CASE (p)\n    WHEN p THEN SET rank = 0;\n    ELSE SET rank = 2;\n  END CASE;",
    ],
    ids=["if-parenthesised", "case-parenthesised"],
)
def test_a_parenthesised_condition_keeps_the_blocks_variables_in_scope(compound):
    # The routine has the compound statement; the repair sets its own variable after it.
    original = MYSQL.replace("INTO r;", "INTO r;\n" + compound)
    candidate = original.replace("rank", "rnk").replace("END ", "END ", 1)
    end = "END IF;" if "IF" in compound else "END CASE;"
    candidate = candidate.replace(end, end + "\n  SET rnk = 1;")
    assert _screen().refusal_of(_my(original), candidate, "mysql") is None


# --------------------------------------------------------------------------- signature
# #2444: a repair keeps the routine's whole signature: its name, each parameter's name, mode
# and type, and the return type. A parameter renamed in the header and the body alike, or
# dropped, is refused, on MySQL and on PostgreSQL.

PG_PARAMS = (
    "CREATE OR REPLACE FUNCTION public.wal_pos(p integer, INOUT q text)\n LANGUAGE plpgsql\n"
    "AS $function$\nBEGIN\n  q := pg_current_xlog_location()::text || p || '?';\nEND\n$function$\n"
)
PG_PARAMS_FIXED = PG_PARAMS.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda t: t.replace("(p integer", "(n integer").replace("|| p ||", "|| n ||"),
         "signature_changed"),
        (_sub("INOUT q text", "OUT q text"), "signature_changed"),
        (_sub("p integer,", "p bigint,"), "signature_changed"),
        (_sub("p integer, ", ""), "signature_changed"),
        (_sub("public.wal_pos(", "public.wal_pos2("), "renamed"),
    ],
    ids=["parameter-renamed", "mode-changed", "type-changed", "parameter-dropped", "name"],
)  # fmt: skip
def test_a_postgresql_repair_keeps_the_whole_signature(edit, code):
    refusal = _screen().refusal_of(_pg(PG_PARAMS), edit(PG_PARAMS_FIXED), "postgresql")
    assert refusal is not None and refusal[0] == code, refusal
    assert _screen().refusal_of(_pg(PG_PARAMS), PG_PARAMS_FIXED, "postgresql") is None


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda t: t.replace("IN `p`", "IN `pw`").replace("(p, ", "(pw, "), "signature_changed"),
        (_sub("IN `p`", "INOUT `p`"), "signature_changed"),
        (_sub("IN `p` varchar(64)", "IN `p` varchar(32)"), "signature_changed"),
        (_sub("IN `p` varchar(64), ", ""), "signature_changed"),
        (_sub("`hashpw`(", "`hashpw2`("), "renamed"),
    ],
    ids=["parameter-renamed", "mode-changed", "type-changed", "parameter-dropped", "name"],
)
def test_a_mysql_repair_keeps_the_whole_signature(edit, code):
    refusal = _screen().refusal_of(_my(MYSQL), edit(MYSQL_FIXED), "mysql")
    assert refusal is not None and refusal[0] == code, refusal


@pytest.mark.passes_on_main  # the control: a name in another case is the same name
def test_a_parameter_name_differs_only_in_case_on_mysql():
    """MySQL reads parameter names case-insensitively: P and `p` are the same name."""
    fixed = MYSQL_FIXED.replace("IN `p`", "IN P")
    assert _screen().refusal_of(_my(MYSQL), fixed, "mysql") is None


# A parameter's DEFAULT is part of the signature too (#2444 review): callers that leave the
# argument out get it, and the side-by-side proof never sees that path. Literals compare as
# the redacted text has them ('?').

#: Defaults PostgreSQL accepts: once one input has a default, every later one has one.
PG_DEFAULTS = PG_PARAMS_FIXED.replace("(p integer,", "(p integer DEFAULT 0,").replace(
    "INOUT q text", "INOUT q text DEFAULT NULL"
)


def _original(text: str) -> str:
    return text.replace("pg_current_wal_lsn()", "pg_current_xlog_location()")


@pytest.mark.parametrize(
    ("base", "edit"),
    [
        (PG_DEFAULTS, _sub("DEFAULT 0", "DEFAULT 1")),
        (PG_PARAMS_FIXED, _sub("INOUT q text", "INOUT q text DEFAULT 7")),
        (PG_DEFAULTS, lambda t: t.replace(" DEFAULT 0", "").replace(" DEFAULT NULL", "")),
        (PG_DEFAULTS, _sub("DEFAULT 0", "DEFAULT (SELECT 1)")),
    ],
    ids=["changed", "added", "dropped", "subquery"],
)
def test_a_postgresql_repair_keeps_each_default(base, edit):
    refusal = _screen().refusal_of(_pg(_original(base)), edit(base), "postgresql")
    assert refusal is not None and refusal[0] == "signature_changed", refusal
    assert _screen().refusal_of(_pg(_original(base)), base, "postgresql") is None


@pytest.mark.parametrize(
    "edit",
    [
        _sub("varchar(64), OUT", "varchar(64) DEFAULT 0, OUT"),
        _sub("varchar(64), OUT", "varchar(64) DEFAULT (SELECT 1), OUT"),
    ],
    ids=["added", "subquery"],
)
@pytest.mark.passes_on_main  # main's signature read the DEFAULT as part of the type
def test_a_mysql_repair_adds_no_default(edit):
    """MySQL parameters take no DEFAULT: one written in a candidate is a signature change."""
    refusal = _screen().refusal_of(_my(MYSQL), edit(MYSQL_FIXED), "mysql")
    assert refusal is not None and refusal[0] == "signature_changed", refusal


@pytest.mark.passes_on_main  # the control: main compared no default at all
def test_a_default_string_compares_as_its_placeholder():
    """A default's string literal reads '?' in the redacted text: '?' and '?' are the same,
    whatever the exact values are (the rebuild and the proof see those)."""
    with_text = PG_DEFAULTS.replace("DEFAULT NULL", "DEFAULT '?'")
    assert _screen().refusal_of(_pg(_original(with_text)), with_text, "postgresql") is None
