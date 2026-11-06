# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Safe deterministic rewrites of a routine's header, each with a recorded diff (#1937).

Every routine here is synthetic. ``LITERAL`` is a string several of them hold: no record
may repeat it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pgparse
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.shared.catalog.routine_rewrites import routine_rewrite_rules
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine, mysql_header
from src.tools.routines.postgresql_checks import (
    PostgresRoutine,
    check_postgresql_routine,
    parse_routine,
    postgresql_header,
)
from src.tools.routines.rewrites import (
    RewrittenRoutine,
    rewrite_mysql_routine,
    rewrite_postgresql_routine,
)
from tests.unit.tools.sql._timing import assert_linear

CORPUS = Path(__file__).resolve().parents[3] / "fixtures" / "routines" / "mysql"
LITERAL = "zq-literal-5521"
DEFINER_RULE = "mysql-routine-definer-to-target-user"
OWNER_RULE = "postgresql-routine-owner-to-target-user"
PATH_RULE = "postgresql-routine-pin-search-path"


# --------------------------------------------------------------------------- helpers


def _mysql(text: str, **fields: Any) -> MySqlRoutine:
    base: dict[str, Any] = {"routine_id": "shop.p", "kind": "procedure", "create_sql": text}
    return MySqlRoutine(**(base | fields))


def _rewrite_mysql(
    routine: MySqlRoutine, target_user: str = "admin", **kw: Any
) -> RewrittenRoutine:
    verdict = check_mysql_routine(routine, target_engine="aurora_mysql", target_user=target_user)
    return rewrite_mysql_routine(routine, verdict, target_user=target_user, **kw)


def _pg(text: str, **fields: Any) -> PostgresRoutine:
    base: dict[str, Any] = {
        "routine_id": "public.f()",
        "kind": "function",
        "create_sql": text,
        "language": "plpgsql",
        "owner": "app_owner",
        "owner_is_superuser": False,
        "security_definer": True,
    }
    return PostgresRoutine(**(base | fields))


def _rewrite_pg(
    routine: PostgresRoutine,
    target_owner: str = "app_owner",
    search_path: tuple[str, ...] = ("public",),
    **kw: Any,
) -> RewrittenRoutine:
    verdict = check_postgresql_routine(routine, target_engine="aurora_postgresql", target_line="16")
    return rewrite_postgresql_routine(
        routine, verdict, target_owner=target_owner, search_path=search_path, **kw
    )


def _rules(result: RewrittenRoutine) -> list[str]:
    return [r.rule_id for r in result.record.rewrites]


def _pg_function(options: str, body: str = "BEGIN RETURN a * 2; END") -> str:
    """A function as pg_get_functiondef prints one, with ``options`` as its option lines."""
    return (
        "CREATE OR REPLACE FUNCTION public.f(a integer)\n"
        " RETURNS integer\n"
        f"{options}"
        f"AS $function$\n{body}\n$function$\n"
    )


DEFINER_FUNCTION = _pg_function(" LANGUAGE plpgsql\n SECURITY DEFINER\n")


# --------------------------------------------------------------------------- MySQL DEFINER


def test_a_named_definer_becomes_the_target_user() -> None:
    text = (CORPUS / "definer-named.sql").read_text(encoding="utf-8")
    routine = _mysql(text, routine_id="shop.nightly_rollup")
    result = _rewrite_mysql(routine)
    record = result.record
    assert record.status == "rewritten" and _rules(result) == [DEFINER_RULE]
    assert record.failed_rules == ()
    (rewrite,) = record.rewrites
    assert rewrite.rewrite == "definer" and rewrite.reason.endswith(".")
    assert rewrite.diff == (
        "--- source/create\n"
        "+++ target/create\n"
        "@@ -1 +1 @@\n"
        "-CREATE DEFINER=`legacy_ops`@`10.0.%` PROCEDURE `nightly_rollup`()\n"
        "+CREATE DEFINER=`admin`@`%` PROCEDURE `nightly_rollup`()\n"
    )
    assert result.target_create == text.replace("`legacy_ops`@`10.0.%`", "`admin`@`%`")
    # The rewritten text passes the definer check it failed.
    after = routine.model_copy(update={"create_sql": result.target_create})
    verdict = check_mysql_routine(after, target_engine="aurora_mysql", target_user="admin")
    assert verdict.status == "as_is"


@pytest.mark.parametrize(
    ("clause", "target_user", "expected"),
    [
        ("DEFINER='ops'@'%'", "admin", "DEFINER=`admin`@`%`"),
        ('DEFINER="ops"@"localhost"', "admin@%", "DEFINER=`admin`@`%`"),
        ("DEFINER=ops@localhost", "admin@10.0.%", "DEFINER=`admin`@`10.0.%`"),
        ("DEFINER = `o``ps` @ `h`", "admin", "DEFINER=`admin`@`%`"),
        ("DEFINER=`ops`@`%`", "`we``ird`@`%`", "DEFINER=`we``ird`@`%`"),
        ("DEFINER=`ops`", "admin", "DEFINER=`admin`@`%`"),
    ],
)
def test_quoted_definers_are_rewritten(clause: str, target_user: str, expected: str) -> None:
    text = f"CREATE {clause} PROCEDURE `shop`.`p`()\nBEGIN\n  SELECT 1;\nEND"
    result = _rewrite_mysql(_mysql(text), target_user=target_user)
    assert result.record.status == "rewritten"
    assert result.target_create == text.replace(clause, expected)


@pytest.mark.parametrize(
    "clause",
    [
        "DEFINER=`admin`@`%`",
        "DEFINER='admin'@'%'",
        "DEFINER=CURRENT_USER",
        "DEFINER=CURRENT_USER()",
    ],
)
def test_a_definer_that_is_the_target_user_or_current_user_is_left_as_is(clause: str) -> None:
    text = f"CREATE {clause} PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND"
    result = _rewrite_mysql(_mysql(text, definer="someone@%"))
    assert result.record.status == "as_is" and result.record.rewrites == ()
    assert result.target_create == text


def test_a_text_with_no_definer_clause_gets_one_only_when_its_metadata_names_another_user() -> None:
    text = "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND"
    plain = _rewrite_mysql(_mysql(text))
    assert plain.record.status == "as_is" and plain.target_create == text
    same = _rewrite_mysql(_mysql(text, definer="admin@%"))
    assert same.record.status == "as_is" and same.target_create == text
    named = _rewrite_mysql(_mysql(text, definer="legacy@localhost"))
    assert named.record.status == "rewritten"
    assert (
        named.target_create == "CREATE DEFINER=`admin`@`%` PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND"
    )
    assert named.record.rewrites[0].diff.splitlines()[2:] == [
        "@@ -1 +1 @@",
        "-CREATE PROCEDURE p()",
        "+CREATE DEFINER=`admin`@`%` PROCEDURE p()",
    ]
    # A metadata definer that is not an account name: the explicit clause settles it.
    odd = _rewrite_mysql(_mysql(text, definer="not an account"))
    assert odd.record.status == "rewritten" and odd.target_create.startswith("CREATE DEFINER=")


def test_an_invoker_routines_definer_is_rewritten_too() -> None:
    """The definer check passes an SQL SECURITY INVOKER routine, but creating it with
    another account's DEFINER still needs privileges the target user lacks."""
    text = (CORPUS / "definer-invoker.sql").read_text(encoding="utf-8")
    routine = _mysql(text)
    verdict = check_mysql_routine(routine, target_engine="aurora_mysql", target_user="admin")
    assert verdict.status == "as_is"
    result = rewrite_mysql_routine(routine, verdict, target_user="admin")
    assert result.record.status == "rewritten" and _rules(result) == [DEFINER_RULE]
    assert "SQL SECURITY INVOKER" in result.target_create


@pytest.mark.parametrize(("file", "kind"), [("trigger.sql", "trigger"), ("event.sql", "event")])
def test_triggers_and_events_are_rewritten(file: str, kind: str) -> None:
    text = (CORPUS / file).read_text(encoding="utf-8")
    result = _rewrite_mysql(_mysql(text, kind=kind, routine_id="shop.x"), target_user="ops")
    assert result.record.status == "rewritten", result.record
    assert "DEFINER=`ops`@`%`" in result.target_create


def test_a_dump_texts_delimiters_are_kept() -> None:
    text = (
        "DROP PROCEDURE IF EXISTS `p`;\n"
        "DELIMITER $$\n"
        "CREATE DEFINER=`root`@`localhost` PROCEDURE `p`()\n"
        "BEGIN\n"
        "  SELECT 'a; $$ b';\n"
        "END$$\n"
        "DELIMITER ;\n"
    )
    result = _rewrite_mysql(_mysql(text))
    assert result.record.status == "rewritten"
    assert result.target_create == text.replace("`root`@`localhost`", "`admin`@`%`")


def test_a_definer_inside_an_executable_comment_is_left_as_is() -> None:
    """mysqldump wraps the clause in executable comments; the reader unwraps them, so the
    clause is not written verbatim and the rewriter does not edit it."""
    text = (
        "/*!50003 CREATE*/ /*!50020 DEFINER=`root`@`localhost`*/ /*!50003 PROCEDURE `p`()\n"
        "BEGIN\n  SELECT 1;\nEND */"
    )
    result = _rewrite_mysql(_mysql(text))
    assert result.record.rewrites == () and result.target_create == text
    assert result.record.status == "needs_attention"
    assert result.record.failed_rules == ("mysql-routine-definer",)


def test_a_no_backslash_escapes_routine_is_rewritten_with_its_strings_unchanged() -> None:
    text = "CREATE DEFINER=`root`@`%` PROCEDURE p() COMMENT 'a\\' \nBEGIN\n  SELECT 'b\\';\nEND"
    result = _rewrite_mysql(_mysql(text, sql_mode="NO_BACKSLASH_ESCAPES"))
    assert result.record.status == "rewritten"
    assert result.target_create == text.replace("`root`@`%`", "`admin`@`%`")


def test_an_unparseable_routine_is_never_rewritten() -> None:
    text = "CREATE DEFINER=`root`@`%` PROCEDURE p()\nBEGIN\n  SELECT 1;\n"
    result = _rewrite_mysql(_mysql(text))
    assert result.record.status == "needs_attention" and result.record.rewrites == ()
    assert result.target_create == text
    pg = _rewrite_pg(_pg("CREATE FUNCTION f( RETURNS int"), target_owner="other")
    assert pg.record.status == "needs_attention"
    assert _rules(pg) == [OWNER_RULE]  # the owner is metadata: it still changes
    assert pg.target_create == "CREATE FUNCTION f( RETURNS int"


def test_other_failed_checks_keep_the_routine_needing_attention() -> None:
    text = "CREATE DEFINER=`root`@`%` PROCEDURE p()\nBEGIN\n  SET GLOBAL max_connections = 10;\nEND"
    result = _rewrite_mysql(_mysql(text))
    assert _rules(result) == [DEFINER_RULE]
    assert result.record.status == "needs_attention"
    assert result.record.failed_rules == ("mysql-routine-set-global",)


@pytest.mark.parametrize(
    ("text", "fields"),
    [
        ("CREATE DEFINER=`legacy`@`%` PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND", {}),
        (
            "CREATE DEFINER=`legacy`@`%` PROCEDURE p()\nSQL SECURITY DEFINER\nBEGIN\n  SELECT 1;\nEND",
            {},
        ),
        ("CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND", {"definer": "legacy@%"}),
        (
            "CREATE DEFINER=`legacy`@`%` TRIGGER t BEFORE INSERT ON orders FOR EACH ROW SET NEW.a = 1",
            {"kind": "trigger"},
        ),
        (
            "CREATE DEFINER=`legacy`@`%` EVENT e ON SCHEDULE EVERY 1 DAY DO DELETE FROM t",
            {"kind": "event"},
        ),
    ],
)
def test_a_definer_rewrite_notes_when_the_routine_runs_as_the_target_user(
    text: str, fields: dict[str, Any]
) -> None:
    """The status stays rewritten, and the record says the routine now runs with the
    target user's privileges, naming the account it ran as (#1967 review)."""
    result = _rewrite_mysql(_mysql(text, **fields))
    assert result.record.status == "rewritten"
    (note,) = result.record.notes
    assert note.code == "runs_as_target_user" and note.rule_id == DEFINER_RULE
    assert "legacy@%" in note.reason and "admin@%" in note.reason
    assert "privileges of the target user" in note.reason


def test_an_invoker_routine_gets_no_note() -> None:
    text = (
        "CREATE DEFINER=`legacy`@`%` PROCEDURE p()\nSQL SECURITY INVOKER\nBEGIN\n  SELECT 1;\nEND"
    )
    result = _rewrite_mysql(_mysql(text))
    assert result.record.status == "rewritten" and result.record.notes == ()
    by_metadata = "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND"
    invoker = _rewrite_mysql(_mysql(by_metadata, definer="legacy@%", security_type="INVOKER"))
    assert invoker.record.status == "rewritten" and invoker.record.notes == ()


def test_a_target_user_with_no_host_is_any_host() -> None:
    """A target ``admin`` is ``admin@%`` (the account the target has), so a DEFINER naming
    ``admin@localhost`` is rewritten rather than left to fail with error 1449."""
    text = "CREATE DEFINER=`admin`@`localhost` PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND"
    result = _rewrite_mysql(_mysql(text), target_user="admin")
    assert result.record.status == "rewritten"
    assert result.target_create == text.replace("`localhost`", "`%`")
    same = "CREATE DEFINER=`admin`@`%` PROCEDURE p()\nBEGIN\n  SELECT 1;\nEND"
    assert _rewrite_mysql(_mysql(same), target_user="admin").record.status == "as_is"


def test_a_single_quoted_account_is_kept_in_the_diff() -> None:
    """An account name is an identifier, not data: its quoted parts stay readable."""
    text = f"CREATE DEFINER = 'bob' @ 'localhost' PROCEDURE p() COMMENT '{LITERAL}'\nBEGIN END"
    (rewrite,) = _rewrite_mysql(_mysql(text)).record.rewrites
    assert "-CREATE DEFINER = 'bob' @ 'localhost' PROCEDURE p() COMMENT ?" in rewrite.diff
    assert LITERAL not in rewrite.diff


# --------------------------------------------------------------------------- PostgreSQL search_path


def test_a_security_definer_routine_pins_the_search_path() -> None:
    routine = _pg(DEFINER_FUNCTION)
    result = _rewrite_pg(routine, search_path=("$user", "public"))
    assert result.record.status == "rewritten" and _rules(result) == [PATH_RULE]
    assert result.target_create == DEFINER_FUNCTION.replace(
        " SECURITY DEFINER\n", ' SECURITY DEFINER\n SET search_path TO "$user", "public"\n'
    )
    (rewrite,) = result.record.rewrites
    assert rewrite.diff == (
        "--- source/create\n"
        "+++ target/create\n"
        "@@ -4,0 +5 @@\n"
        '+ SET search_path TO "$user", "public"\n'
    )
    assert parse_routine(result.target_create, "function") is None
    stmt = pgparse.parse(result.target_create)[0]["stmt"]["CreateFunctionStmt"]
    (path,) = [
        o["DefElem"]["arg"]["VariableSetStmt"]
        for o in stmt["options"]
        if o["DefElem"]["defname"] == "set"
    ]
    assert [a["A_Const"]["sval"]["sval"] for a in path["args"]] == ["$user", "public"]


@pytest.mark.parametrize(
    "path",
    [("My Schema",), ('we"ird', "public"), ("user",), ("ünïcode", "x")],
)
def test_quoted_schema_names_are_pinned_as_identifiers(path: tuple[str, ...]) -> None:
    result = _rewrite_pg(_pg(DEFINER_FUNCTION), search_path=path)
    assert result.record.status == "rewritten"
    stmt = pgparse.parse(result.target_create)[0]["stmt"]["CreateFunctionStmt"]
    (set_path,) = [
        o["DefElem"]["arg"]["VariableSetStmt"]
        for o in stmt["options"]
        if o["DefElem"]["defname"] == "set"
    ]
    assert tuple(a["A_Const"]["sval"]["sval"] for a in set_path["args"]) == path


@pytest.mark.parametrize(
    "clause",
    [
        " SET search_path TO 'public', 'pg_temp'\n",
        " SET search_path = public\n",
        " SET search_path FROM CURRENT\n",
        " SET SEARCH_PATH TO public\n",
    ],
)
def test_an_existing_set_search_path_is_left_as_is(clause: str) -> None:
    text = _pg_function(" LANGUAGE plpgsql\n SECURITY DEFINER\n" + clause)
    result = _rewrite_pg(_pg(text))
    assert result.record.status == "as_is" and result.record.rewrites == ()
    assert result.target_create == text


@pytest.mark.parametrize(
    "options",
    [" LANGUAGE plpgsql\n SECURITY INVOKER\n", " LANGUAGE plpgsql\n"],
)
def test_a_security_invoker_routine_is_not_pinned(options: str) -> None:
    """A SECURITY INVOKER routine (the default) resolves names in its caller's path."""
    text = _pg_function(options)
    result = _rewrite_pg(_pg(text, security_definer=False))
    assert result.record.status == "as_is" and result.target_create == text


@pytest.mark.parametrize(
    "body",
    [
        "BEGIN SET search_path TO other; RETURN a; END",
        "BEGIN PERFORM set_config('search_path', 'other', true); RETURN a; END",
        "BEGIN RETURN current_setting('search_path')::int; END",
        "BEGIN RETURN length(current_schema()); END",
        "BEGIN RETURN cardinality(current_schemas(true)); END",
        "BEGIN EXECUTE 'select 1'; RETURN a; END",
        "BEGIN RESET ALL; RETURN a; END",
        "BEGIN RESET search_path; RETURN a; END",
        "BEGIN DISCARD ALL; RETURN a; END",
        "BEGIN SET ROLE reporting; RETURN a; END",
        "BEGIN SET LOCAL ROLE reporting; RETURN a; END",
        "BEGIN SET SESSION AUTHORIZATION reporting; RETURN a; END",
    ],
)
def test_a_body_that_reads_or_changes_the_search_path_is_not_pinned(body: str) -> None:
    text = _pg_function(" LANGUAGE plpgsql\n SECURITY DEFINER\n", body)
    result = _rewrite_pg(_pg(text))
    assert result.record.status == "as_is" and result.target_create == text


def test_an_sql_standard_body_gets_the_clause_before_its_first_option() -> None:
    text = "CREATE FUNCTION public.f(a integer) RETURNS integer LANGUAGE sql SECURITY DEFINER RETURN a + 1"
    result = _rewrite_pg(_pg(text, language="sql"))
    assert result.target_create == (
        'CREATE FUNCTION public.f(a integer) RETURNS integer SET search_path TO "public" '
        "LANGUAGE sql SECURITY DEFINER RETURN a + 1"
    )
    multi = "CREATE FUNCTION public.f(a integer)\n RETURNS integer\n LANGUAGE sql\n SECURITY DEFINER\nRETURN a + 1"
    result = _rewrite_pg(_pg(multi, language="sql"))
    assert result.target_create == multi.replace(
        " LANGUAGE sql", ' SET search_path TO "public"\n LANGUAGE sql'
    )
    assert parse_routine(result.target_create, "function") is None


def test_a_one_line_header_gets_the_clause_before_as() -> None:
    text = "CREATE PROCEDURE public.p() LANGUAGE plpgsql SECURITY DEFINER AS $$BEGIN NULL; END$$"
    result = _rewrite_pg(_pg(text, kind="procedure", routine_id="public.p()"))
    assert result.target_create == text.replace(" AS $$", ' SET search_path TO "public" AS $$')
    assert parse_routine(result.target_create, "procedure") is None


def test_a_header_with_non_ascii_names_is_edited_at_the_right_character() -> None:
    """The parser counts UTF-8 bytes; the clause still goes in just before AS."""
    text = (
        _pg_function(" LANGUAGE plpgsql\n SECURITY DEFINER\n")
        .replace("public.f(a integer)", '"schémä".fünc(ä integer)')
        .replace("a * 2", "ä * 2")
    )
    result = _rewrite_pg(_pg(text))
    assert result.record.status == "rewritten"
    assert ' SET search_path TO "public"\nAS $function$' in result.target_create
    assert parse_routine(result.target_create, "function") is None


# --------------------------------------------------------------------------- PostgreSQL owner


def test_the_owner_becomes_the_target_user() -> None:
    text = _pg_function(" LANGUAGE plpgsql\n")
    result = _rewrite_pg(
        _pg(text, owner="legacy_owner", security_definer=False), target_owner="app"
    )
    assert result.record.status == "rewritten" and _rules(result) == [OWNER_RULE]
    assert result.record.owner == "app" and result.target_create == text
    assert result.record.rewrites[0].diff == (
        "--- source/owner\n+++ target/owner\n@@ -1 +1 @@\n-legacy_owner\n+app\n"
    )
    same = _rewrite_pg(_pg(text, owner="app", security_definer=False), target_owner="app")
    assert same.record.status == "as_is" and same.record.owner == "app"
    unknown = _rewrite_pg(_pg(text, owner=None, security_definer=False), target_owner="app")
    assert unknown.record.status == "as_is" and unknown.record.owner == "app"


def test_an_owner_rewrite_notes_a_security_definer_routine() -> None:
    definer = _rewrite_pg(_pg(DEFINER_FUNCTION, owner="legacy_owner"), target_owner="app")
    assert definer.record.status == "rewritten"
    (note,) = definer.record.notes
    assert (note.code, note.rule_id) == ("runs_as_target_user", OWNER_RULE)
    assert "legacy_owner" in note.reason and "target user app" in note.reason
    invoker = _rewrite_pg(
        _pg(_pg_function(" LANGUAGE plpgsql\n"), owner="legacy_owner", security_definer=False),
        target_owner="app",
    )
    assert invoker.record.status == "rewritten" and invoker.record.notes == ()


def test_a_trigger_is_never_rewritten() -> None:
    text = (
        "CREATE TRIGGER t BEFORE INSERT ON public.orders FOR EACH ROW EXECUTE FUNCTION public.f()"
    )
    routine = PostgresRoutine(routine_id="public.orders.t", kind="trigger", create_sql=text)
    result = _rewrite_pg(routine, target_owner="other")
    assert result.record.status == "as_is" and result.record.owner is None
    assert result.target_create == text


def test_a_superuser_security_definer_still_needs_attention() -> None:
    result = _rewrite_pg(
        _pg(DEFINER_FUNCTION, owner="postgres", owner_is_superuser=True), target_owner="app"
    )
    assert _rules(result) == [OWNER_RULE, PATH_RULE]
    assert result.record.status == "needs_attention"
    assert result.record.failed_rules == ("postgresql-routine-superuser-security-definer",)


def test_a_c_function_is_not_pinned() -> None:
    text = (
        "CREATE OR REPLACE FUNCTION public.f(a integer)\n RETURNS integer\n LANGUAGE c\n"
        " SECURITY DEFINER\nAS '$libdir/f', $function$f$function$\n"
    )
    result = _rewrite_pg(_pg(text, language="c"))
    assert result.target_create == text and result.record.status == "needs_attention"


# --------------------------------------------------------------------------- shared


def test_bad_arguments_are_value_errors() -> None:
    routine = _mysql("CREATE PROCEDURE p() BEGIN END")
    verdict = check_mysql_routine(routine, target_engine="aurora_mysql", target_user="admin")
    for user in ("", "CURRENT_USER", "a b"):
        with pytest.raises(ValueError):
            rewrite_mysql_routine(routine, verdict, target_user=user)
    # A NUL, a C1 control or a line separator never reaches the DEFINER clause.
    for user in ("admin\x00", "`ad\x00min`@`%`", "admin\x85", "admin\u2028@%", "admin@h\u2029"):
        with pytest.raises(ValueError, match="control or line-separator"):
            rewrite_mysql_routine(routine, verdict, target_user=user)
    other = _mysql("CREATE PROCEDURE q() BEGIN END", routine_id="shop.q")
    with pytest.raises(ValueError, match="not this routine"):
        rewrite_mysql_routine(other, verdict, target_user="admin")
    pg = _pg(DEFINER_FUNCTION)
    pg_verdict = check_postgresql_routine(pg, target_engine="aurora_postgresql", target_line="16")
    for owner, path in (("", ("public",)), ("app", ()), ("app", ("",))):
        with pytest.raises(ValueError):
            rewrite_postgresql_routine(pg, pg_verdict, target_owner=owner, search_path=path)
    for bad in ("a\nb", "a\x00", "a\x85b", "a\u2028b", "a\u2029"):
        with pytest.raises(ValueError, match="search_path schema name holds a control"):
            rewrite_postgresql_routine(pg, pg_verdict, target_owner="app", search_path=(bad,))
        with pytest.raises(ValueError, match="target_owner must be a role name"):
            rewrite_postgresql_routine(pg, pg_verdict, target_owner=bad, search_path=("p",))


def test_rules_come_from_the_catalog() -> None:
    """With no rewrite rule, nothing is rewritten: the rules are data."""
    text = (CORPUS / "definer-named.sql").read_text(encoding="utf-8")
    result = _rewrite_mysql(_mysql(text), rules=())
    assert result.record.status == "needs_attention" and result.target_create == text
    pg = _rewrite_pg(_pg(DEFINER_FUNCTION, owner="x"), rules=[])
    assert pg.record.rewrites == () and pg.record.owner is None
    only_path = [r for r in routine_rewrite_rules() if r.id == PATH_RULE]
    pg = _rewrite_pg(_pg(DEFINER_FUNCTION, owner="x"), rules=only_path)
    assert _rules(pg) == [PATH_RULE]


def test_the_record_never_holds_a_literal() -> None:
    mysql = (
        f"CREATE DEFINER=`root`@`%` PROCEDURE p(IN a INT) COMMENT '{LITERAL}' /* {LITERAL} */ "
        f"BEGIN SELECT '{LITERAL}', 4242; END"
    )
    pg_inline = (
        f"CREATE FUNCTION public.f(a text DEFAULT '{LITERAL}', b int DEFAULT 4242) RETURNS text "
        f"LANGUAGE sql /* {LITERAL} */ SECURITY DEFINER RETURN a || '{LITERAL}'"
    )
    pg_lines = _pg_function(
        f" LANGUAGE plpgsql\n SECURITY DEFINER -- {LITERAL}\n", f"BEGIN RETURN '{LITERAL}'; END"
    )
    results = [
        _rewrite_mysql(_mysql(mysql)),
        _rewrite_pg(_pg(pg_inline, language="sql"), target_owner="other"),
        _rewrite_pg(_pg(pg_lines), target_owner="other"),
    ]
    for result in results:
        assert result.record.status == "rewritten", result.record
        assert LITERAL in result.target_create
        for text in (result.record.model_dump_json(), repr(result), str(result)):
            assert LITERAL not in text and "4242" not in text
    assert "COMMENT ? ? " in results[0].record.rewrites[0].diff
    assert "DEFAULT ?" in results[1].record.rewrites[-1].diff


def test_the_same_input_gives_byte_identical_output() -> None:
    mysql = _mysql((CORPUS / "definer-named.sql").read_text(encoding="utf-8"))
    pg = _pg(DEFINER_FUNCTION, owner="legacy")
    first = [_rewrite_mysql(mysql), _rewrite_pg(pg, target_owner="app")]
    second = [_rewrite_mysql(mysql), _rewrite_pg(pg, target_owner="app")]
    assert [json.dumps(r.model_dump(), sort_keys=True) for r in first] == [
        json.dumps(r.model_dump(), sort_keys=True) for r in second
    ]


# --------------------------------------------------------------------------- properties

_NAME = st.text(alphabet="abcxyz_`'\" @%.é", min_size=1, max_size=8)
_BODY_STATEMENT = st.sampled_from(
    [
        "SELECT 1;",
        "SELECT 'DEFINER=`x`@`y`';",
        "INSERT INTO t VALUES ('a', 2);",
        "SET @v = 'CREATE DEFINER=root PROCEDURE';",
        "-- DEFINER=`root`@`%`\n  SELECT 2;",
        "UPDATE t SET c = c + 1 WHERE id = 3;",
    ]
)


def _backticked(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


@settings(max_examples=150, deadline=None)
@given(
    user=_NAME,
    host=st.one_of(st.none(), _NAME),
    target=st.sampled_from(["admin", "admin@%", "app@10.0.%"]),
    comment=st.one_of(st.none(), st.text(alphabet="ab ;'", max_size=6)),
    statements=st.lists(_BODY_STATEMENT, min_size=1, max_size=4),
    security=st.sampled_from(["", "SQL SECURITY INVOKER\n", "SQL SECURITY DEFINER\n"]),
)
def test_a_definer_rewrite_never_changes_the_body(
    user: str,
    host: str | None,
    target: str,
    comment: str | None,
    statements: list[str],
    security: str,
) -> None:
    """Whatever the DEFINER and body, a rewrite changes only the header: the text from
    the body's first token on is the same, and so are the body's tokens."""
    account = _backticked(user) + ("" if host is None else "@" + _backticked(host))
    note = "" if comment is None else "COMMENT '" + comment.replace("'", "''") + "'\n"
    text = (
        f"CREATE DEFINER={account} PROCEDURE `shop`.`p`(IN a INT)\n{note}{security}"
        "BEGIN\n  " + "\n  ".join(statements) + "\nEND"
    )
    routine = _mysql(text)
    before = mysql_header(routine)
    assert before is not None
    result = _rewrite_mysql(routine, target_user=target)
    after = mysql_header(routine.model_copy(update={"create_sql": result.target_create}))
    assert after is not None
    assert result.target_create[after.body_start :] == text[before.body_start :]
    body = [(t.kind, t.text) for t in before.tokens if t.start >= before.body_start]
    assert [(t.kind, t.text) for t in after.tokens if t.start >= after.body_start] == body
    assert result.target_create[: before.definer[0]] == text[: before.definer[0]]  # type: ignore[index]


@settings(max_examples=150, deadline=None)
@given(
    options=st.permutations([" LANGUAGE plpgsql\n", " SECURITY DEFINER\n", " STABLE\n"]),
    default=st.text(alphabet="ab'$ ", max_size=6),
    path=st.lists(st.text(alphabet='ab "$_é', min_size=1, max_size=5), min_size=1, max_size=3),
    body=st.sampled_from(
        [
            "BEGIN RETURN a * 2; END",
            "BEGIN RETURN 'AS $x$'::int; END",
            "DECLARE v text := 'SET search'; BEGIN RETURN a; END",
        ]
    ),
)
def test_a_search_path_pin_never_changes_the_body(
    options: list[str], default: str, path: list[str], body: str
) -> None:
    literal = "'" + default.replace("'", "''") + "'"
    text = _pg_function("".join(options), body).replace(
        "(a integer)", f"(a integer, b text DEFAULT {literal})"
    )
    routine = _pg(text)
    header = postgresql_header(text, "function")
    assert header is not None and header.insert_at is not None
    result = _rewrite_pg(routine, search_path=tuple(path))
    assert result.record.status == "rewritten"
    after = postgresql_header(result.target_create, "function")
    assert after is not None and after.sets_search_path and after.body == header.body
    assert result.target_create.startswith(text[: header.insert_at])
    assert result.target_create.endswith(text[header.insert_at :])


def test_a_rewrite_is_linear_in_the_texts_length() -> None:
    def text(k: int) -> str:
        return "CREATE DEFINER=`root`@`%` PROCEDURE p()\nBEGIN\n" + "  SELECT 1;\n" * k + "END"

    def pg_text(k: int) -> str:
        return _pg_function(
            " LANGUAGE plpgsql\n SECURITY DEFINER\n",
            "BEGIN\n" + "  a := a + 1;\n" * k + "  RETURN a;\nEND",
        )

    assert_linear(text, lambda t: _rewrite_mysql(_mysql(t)), sizes=(250, 2000))
    assert_linear(pg_text, lambda t: _rewrite_pg(_pg(t)), sizes=(500, 4000))
