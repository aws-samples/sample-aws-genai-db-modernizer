# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The optional newer Aurora PostgreSQL target and its version-change checks (#1951).

Same-major stays the default target line; the customer may pick the newest listed line
instead (``target_choice="newest"``). Rules with a ``[version_change]`` then check what
the majors between the source and that line removed, renamed or changed for a routine's
body, and they never run on a same-major target. Every routine here is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.gaps import aurora_version_lines, parse_version_line
from src.shared.catalog.routine_checks import (
    RoutineCheckRule,
    load_routine_check_rules,
    routine_check_rules,
)
from src.tools.analysis.relational_gaps import TARGET_LINE_CHOICES, extension_landing_line
from src.tools.routines.postgresql_checks import (
    RoutineVerdict,
    check_postgresql_routine,
    routine_target_line,
)
from tests.unit.tools.routines.postgresql_routines import routine

TARGET = "aurora_postgresql"
NEWEST = aurora_version_lines()[TARGET].lines[-1]
MARKER = "MARKER_LITERAL_1951"


def _rules() -> list[RoutineCheckRule]:
    return [
        r
        for r in routine_check_rules()
        if r.version_change is not None and "postgresql" in r.source_engines
    ]


def _ids() -> set[str]:
    return {r.id for r in _rules()}


def _plpgsql(body: str, *, head: str = "", declare: str = "x text; n int;") -> str:
    return (
        f"CREATE FUNCTION public.f() RETURNS int LANGUAGE plpgsql {head} AS $fn$ "
        f"DECLARE {declare} BEGIN {body} RETURN 1; END $fn$"
    )


def _check(text: str, source: str | None = "13.4", line: str = NEWEST, **meta) -> RoutineVerdict:
    return check_postgresql_routine(
        routine(text, **meta), target_engine=TARGET, target_line=line, source_version=source
    )


def _found(verdict: RoutineVerdict) -> list[str]:
    return sorted(
        f"{c.rule_id}:{f}"
        for c in verdict.checks
        if c.outcome == "fail" and c.rule_id in _ids()
        for f in c.findings
    )


# Every PostgreSQL version-change rule, and a routine that trips it.
_TRIPS = {
    "postgresql-routine-v10-renamed-functions": "x := pg_current_xlog_location();",
    "postgresql-routine-v10-renamed-columns": (
        "SELECT r.replay_location INTO x FROM pg_stat_replication r;"
    ),
    "postgresql-routine-v11-removed-columns": (
        "SELECT relhaspkey INTO x FROM pg_catalog.pg_class LIMIT 1;"
    ),
    "postgresql-routine-v12-removed-columns": "SELECT adsrc INTO x FROM pg_attrdef LIMIT 1;",
    "postgresql-routine-v14-removed-functions": "n := numeric_fac(5);",
    "postgresql-routine-v15-removed-functions": "IF pg_is_in_backup() THEN n := 1; END IF;",
    "postgresql-routine-v15-public-schema": "CREATE TABLE app.audit_copy (a int); "
    "CREATE TABLE audit_log (a int);",
    "postgresql-routine-v17-removed-columns": (
        "SELECT checkpoints_timed INTO n FROM pg_stat_bgwriter;"
    ),
    "postgresql-routine-v17-maintenance-search-path": "SELECT count(*) INTO n FROM orders;",
    "postgresql-routine-v18-removed-columns": "SELECT wal_write INTO n FROM pg_stat_wal;",
    "postgresql-routine-v12-removed-types": "x := now()::abstime;",
    "postgresql-routine-v10-removed-settings": "SET sql_inheritance = on;",
    "postgresql-routine-v13-removed-settings": "x := current_setting('wal_keep_segments');",
    "postgresql-routine-v14-removed-settings": "SET LOCAL operator_precedence_warning = on;",
    "postgresql-routine-v15-removed-settings": "SHOW stats_temp_directory;",
    "postgresql-routine-v16-removed-settings": (
        "PERFORM set_config('force_parallel_mode', 'on', true);"
    ),
    "postgresql-routine-v17-removed-settings": "RESET old_snapshot_threshold;",
}


# --------------------------------------------------------------------------- the catalog


def test_the_rules_are_in_the_catalog_backed_by_release_notes() -> None:
    assert _ids() == set(_TRIPS)
    for rule in _rules():
        assert rule.version_change is not None
        line = parse_version_line("postgresql", rule.version_change.line)
        assert line is not None and line <= (int(NEWEST),)
        assert "release notes" in rule.docs and f" {rule.version_change.line} " in (rule.docs + " ")
        assert rule.reason.endswith(".") and rule.fix_hint.endswith(".")
        assert set(rule.kinds) <= {"function", "procedure"}


@pytest.mark.parametrize("rule_id", sorted(_TRIPS))
def test_each_rule_runs_only_from_a_source_older_than_its_line(rule_id: str) -> None:
    (rule,) = [r for r in _rules() if r.id == rule_id]
    assert rule.version_change is not None
    changed = int(rule.version_change.line)
    text = _plpgsql(_TRIPS[rule_id])
    older = _check(text, source=f"{changed - 1}.9" if changed > 10 else "9.6.24")
    assert rule_id in older.failed_rules
    for source in (f"{changed}.0", NEWEST + ".1"):
        assert rule_id not in {c.rule_id for c in _check(text, source=source).checks}


# --------------------------------------------------------------------------- when they run


@pytest.mark.parametrize("line", aurora_version_lines()[TARGET].lines)
def test_rules_never_run_on_a_same_major_target(line: str) -> None:
    text = _plpgsql(" ".join(_TRIPS.values()))
    verdict = _check(text, source=f"{line}.2", line=line)
    assert not {c.rule_id for c in verdict.checks} & _ids()
    assert verdict.failed_rules == ()


def test_the_target_line_decides_which_rules_run() -> None:
    """A 13 source: moving to 14 crosses 14 only; moving to the newest crosses 14 to 18."""
    text = _plpgsql(" ".join(_TRIPS.values()))

    def crossed(target: int) -> set[str]:
        return {r.id for r in _rules() if 13 < int(r.version_change.line) <= target}  # type: ignore[union-attr]

    assert crossed(14) == {
        "postgresql-routine-v14-removed-functions",
        "postgresql-routine-v14-removed-settings",
    }
    assert {c.rule_id for c in _check(text, line="14").checks} & _ids() == crossed(14)
    on_newest = {c.rule_id for c in _check(text).checks} & _ids()
    assert on_newest == crossed(int(NEWEST)) and "postgresql-routine-v18-removed-columns" in (
        on_newest
    )


def test_an_unknown_source_version_leaves_the_rules_unknown_never_pass() -> None:
    verdict = _check(_plpgsql("n := 1;"), source=None, line="16")
    expected = {r.id for r in _rules() if int(r.version_change.line) <= 16}  # type: ignore[union-attr]
    assert set(verdict.unknown_rules) == expected
    assert {c.findings for c in verdict.checks if c.rule_id in expected} == {
        ("source version unknown",)
    }
    assert verdict.status == "as_is"


def test_a_body_that_is_not_read_is_unknown_never_pass() -> None:
    unparseable = _check("CREATE FUNCTION public.f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN")
    c_function = _check(
        "CREATE FUNCTION public.f() RETURNS int LANGUAGE c AS 'mylib', 'f'", language="c"
    )
    for verdict in (unparseable, c_function):
        body = [c for c in verdict.checks if c.rule_id in _ids()]
        assert body and all(c.outcome == "unknown" for c in body)
        assert {c.findings for c in body} == {("body not read",)}


# --------------------------------------------------------------------------- what they find


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "x := pg_current_xlog_location(); PERFORM pg_catalog.pg_switch_xlog();",
            [
                "postgresql-routine-v10-renamed-functions:pg_current_xlog_location()",
                "postgresql-routine-v10-renamed-functions:pg_switch_xlog()",
            ],
        ),
        (
            "IF numeric_fac(n) > 1 THEN n := 0; END IF;",
            ["postgresql-routine-v14-removed-functions:numeric_fac()"],
        ),
        # pg_stop_backup() is the backup-control rule's, on every line.
        ("PERFORM pg_stop_backup();", []),
        # Look-alikes: a schema-qualified user function, the new names, a string, EXECUTE.
        ("PERFORM ops.pg_start_backup('x');", []),
        ("x := pg_current_wal_lsn(); PERFORM pg_backup_start('x'); n := factorial(3);", []),
        ("x := 'pg_start_backup()';", []),
        ("EXECUTE 'SELECT pg_start_backup(''x'')';", []),
        # #2016 review: PG 15 removed the backup status functions too.
        (
            "IF pg_is_in_backup() THEN x := pg_backup_start_time()::text; END IF;",
            [
                "postgresql-routine-v15-removed-functions:pg_backup_start_time()",
                "postgresql-routine-v15-removed-functions:pg_is_in_backup()",
            ],
        ),
    ],
)
def test_renamed_and_removed_functions(body: str, expected: list[str]) -> None:
    assert _found(_check(_plpgsql(body), source="9.6.24")) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "SELECT c.consrc INTO x FROM pg_constraint c; SELECT relhasoids INTO x FROM pg_class;",
            [
                "postgresql-routine-v12-removed-columns:pg_class.relhasoids",
                "postgresql-routine-v12-removed-columns:pg_constraint.consrc",
            ],
        ),
        (
            "SELECT buffers_backend INTO n FROM pg_catalog.pg_stat_bgwriter;",
            ["postgresql-routine-v17-removed-columns:pg_stat_bgwriter.buffers_backend"],
        ),
        (
            "SELECT sent_location INTO x FROM pg_stat_replication;",
            ["postgresql-routine-v10-renamed-columns:pg_stat_replication.sent_location"],
        ),
        # #2016 review: the rest of the PG 11, 12, 17 and 18 Migration lists.
        (
            "SELECT proisagg OR proiswindow INTO x FROM pg_proc;",
            [
                "postgresql-routine-v11-removed-columns:pg_proc.proisagg",
                "postgresql-routine-v11-removed-columns:pg_proc.proiswindow",
            ],
        ),
        (
            "SELECT clientdn INTO x FROM pg_stat_ssl;",
            ["postgresql-routine-v12-removed-columns:pg_stat_ssl.clientdn"],
        ),
        (
            "SELECT max_dead_tuples, num_dead_tuples INTO n, n FROM pg_stat_progress_vacuum;",
            [
                "postgresql-routine-v17-removed-columns:pg_stat_progress_vacuum.max_dead_tuples",
                "postgresql-routine-v17-removed-columns:pg_stat_progress_vacuum.num_dead_tuples",
            ],
        ),
        (
            "SELECT c.colliculocale INTO x FROM pg_collation c; "
            "SELECT daticulocale INTO x FROM pg_database;",
            [
                "postgresql-routine-v17-removed-columns:pg_collation.colliculocale",
                "postgresql-routine-v17-removed-columns:pg_database.daticulocale",
            ],
        ),
        (
            "SELECT domain_default INTO x FROM information_schema.element_types;",
            ["postgresql-routine-v17-removed-columns:element_types.domain_default"],
        ),
        (
            "SELECT wal_sync_time INTO n FROM pg_stat_wal; "
            "SELECT parent INTO x FROM pg_backend_memory_contexts;",
            [
                "postgresql-routine-v18-removed-columns:pg_backend_memory_contexts.parent",
                "postgresql-routine-v18-removed-columns:pg_stat_wal.wal_sync_time",
            ],
        ),
        # A column of that name on another table, or the catalog without it, passes.
        ("SELECT adsrc INTO x FROM app.defaults;", []),
        ("SELECT pg_get_expr(adbin, adrelid) INTO x FROM pg_attrdef;", []),
        ("SELECT num_timed INTO n FROM pg_stat_checkpointer;", []),
    ],
)
def test_removed_and_renamed_catalog_columns(body: str, expected: list[str]) -> None:
    assert _found(_check(_plpgsql(body, head="SET search_path = app"), source="9.6.24")) == (
        expected
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("CREATE TABLE audit_log (a int);", []),
        ("CREATE TABLE public.audit_log AS SELECT 1 AS a;", ["TABLE created in public"]),
        ("CREATE VIEW public.recent AS SELECT 1 AS a;", ["VIEW created in public"]),
        ("CREATE SEQUENCE public.ids;", ["SEQUENCE created in public"]),
        # #2016 review: functions, types and domains need CREATE on the schema too.
        (
            "CREATE FUNCTION public.g() RETURNS int LANGUAGE sql AS 'SELECT 1';",
            ["FUNCTION created in public"],
        ),
        ("CREATE TYPE public.mood AS ENUM ('ok');", ["TYPE created in public"]),
        ("CREATE TYPE public.pair AS (a int, b int);", ["TYPE created in public"]),
        ("CREATE DOMAIN public.posint AS int;", ["DOMAIN created in public"]),
        # Another schema, a temporary table, and dynamic SQL the check cannot see.
        ("CREATE TABLE app.audit_log (a int);", []),
        ("CREATE TEMP TABLE scratch (a int);", []),
        ("EXECUTE 'CREATE TABLE audit_log (a int)';", []),
    ],
)
def test_creating_in_the_public_schema(body: str, expected: list[str]) -> None:
    """With the routine's own ``SET search_path = app``, only an explicit public. name is
    in public; the unqualified cases are the next test's."""
    verdict = _check(_plpgsql(body, head="SET search_path = app"))
    rule = "postgresql-routine-v15-public-schema"
    assert [f.split(":", 1)[1] for f in _found(verdict) if f.startswith(rule)] == expected
    assert next(c for c in verdict.checks if c.rule_id == rule).impact == "behaves_differently"


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        (_plpgsql("SELECT count(*) INTO n FROM orders;"), True),
        (_plpgsql("SELECT count(*) INTO n FROM app.orders;"), False),
        (_plpgsql("SELECT count(*) INTO n FROM orders;", head="SET search_path = app"), False),
        (_plpgsql("WITH o AS (SELECT 1 AS a) SELECT count(*) INTO n FROM o;"), False),
        (
            "CREATE FUNCTION public.f() RETURNS bigint LANGUAGE sql AS "
            "'SELECT count(*) FROM orders'",
            True,
        ),
        (
            "CREATE FUNCTION public.f() RETURNS bigint LANGUAGE sql "
            "RETURN (SELECT count(*) FROM orders)",
            True,
        ),
    ],
)
def test_unqualified_tables_without_a_search_path_of_its_own(text: str, flagged: bool) -> None:
    language = "sql" if "LANGUAGE sql" in text else "plpgsql"
    verdict = _check(text, language=language)
    rule = "postgresql-routine-v17-maintenance-search-path"
    (result,) = [c for c in verdict.checks if c.rule_id == rule]
    assert (result.outcome == "fail") is flagged and result.impact == "informational"
    if flagged:
        assert verdict.status == "as_is"


def test_a_procedure_gets_the_body_checks_but_not_the_maintenance_one() -> None:
    text = (
        "CREATE PROCEDURE public.p() LANGUAGE plpgsql AS $fn$ BEGIN "
        "PERFORM pg_is_in_backup(); CREATE TABLE t (a int); END $fn$"
    )
    verdict = _check(text, kind="procedure", routine_id="public.p()")
    assert _found(verdict) == [
        "postgresql-routine-v15-public-schema:TABLE created in public",
        "postgresql-routine-v15-removed-functions:pg_is_in_backup()",
    ]
    assert "postgresql-routine-v17-maintenance-search-path" not in {
        c.rule_id for c in verdict.checks
    }


def test_the_verdict_never_holds_the_routine_text() -> None:
    text = _plpgsql(f"PERFORM pg_start_backup('{MARKER}'); CREATE TABLE {MARKER.lower()} (a int);")
    verdict = _check(text)
    assert verdict.failed_rules
    assert MARKER not in verdict.model_dump_json() and MARKER.lower() not in (
        verdict.model_dump_json()
    )


def test_an_unqualified_create_is_public_only_without_a_search_path_of_its_own() -> None:
    """#2016 review: SET search_path = app sends an unqualified CREATE to app."""
    rule = "postgresql-routine-v15-public-schema"
    body = "CREATE TABLE audit_log (a int); CREATE FUNCTION g() RETURNS int LANGUAGE sql AS 'SELECT 1';"
    pinned = _found(_check(_plpgsql(body, head="SET search_path = app")))
    assert not [f for f in pinned if f.startswith(rule)]
    unpinned = _found(_check(_plpgsql(body)))
    assert [f.split(":", 1)[1] for f in unpinned if f.startswith(rule)] == [
        "FUNCTION created in public",
        "TABLE created in public",
    ]


@pytest.mark.parametrize(
    "dynamic",
    [
        "EXECUTE 'SELECT pg_switch_xlog()';",
        "FOR x IN EXECUTE 'SELECT 1' LOOP END LOOP;",
        "RETURN QUERY EXECUTE 'SELECT 1';",
    ],
)
def test_dynamic_sql_leaves_the_body_checks_unknown_never_pass(dynamic: str) -> None:
    """#2016 review: the text an EXECUTE runs is not read, so a check that finds nothing in
    such a body is unknown; a finding elsewhere in the body still fails."""
    returns = "SETOF int" if "RETURN QUERY" in dynamic else "int"
    text = (
        f"CREATE FUNCTION public.f() RETURNS {returns} LANGUAGE plpgsql "
        f"SET search_path = app AS $fn$ DECLARE x int; BEGIN {dynamic} RETURN; END $fn$"
    )
    verdict = _check(text, source="9.6.24")
    body = [c for c in verdict.checks if c.rule_id in _ids()]
    assert body and {c.outcome for c in body} == {"unknown"}
    assert {c.findings for c in body} == {("dynamic SQL not read",)}
    found = _check(_plpgsql("PERFORM pg_switch_xlog(); EXECUTE 'SELECT 1';"), source="9.6.24")
    assert "postgresql-routine-v10-renamed-functions" in found.failed_rules


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_plpgsql("SET wal_keep_segments = 8;"), ["SET wal_keep_segments"]),
        (_plpgsql("RESET wal_keep_segments;"), ["SET wal_keep_segments"]),
        (_plpgsql("SHOW wal_keep_segments;"), ["SHOW wal_keep_segments"]),
        (
            _plpgsql("x := current_setting('wal_keep_segments');"),
            ["current_setting(wal_keep_segments)"],
        ),
        (
            _plpgsql("PERFORM pg_catalog.set_config('wal_keep_segments', '8', true);"),
            ["set_config(wal_keep_segments)"],
        ),
        # The function's own SET clause.
        (_plpgsql("n := 1;", head="SET wal_keep_segments = 8"), ["SET wal_keep_segments"]),
        # The 13 name, a variable's name, a string elsewhere.
        (_plpgsql("SET wal_keep_size = '1GB';"), []),
        (_plpgsql("x := current_setting(x);"), []),
        (_plpgsql("x := 'wal_keep_segments';"), []),
    ],
)
def test_removed_and_renamed_settings(text: str, expected: list[str]) -> None:
    """#2016 review, consistent with #2006's removed MySQL system variables."""
    rule = "postgresql-routine-v13-removed-settings"
    found = _found(_check(text, source="12.9", line="14"))
    assert [f.split(":", 1)[1] for f in found if f.startswith(rule)] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_plpgsql("x := now()::abstime;"), ["abstime"]),
        (_plpgsql("n := 1;", declare="t tinterval; n int; x text;"), ["tinterval"]),
        (
            "CREATE FUNCTION public.f(a reltime) RETURNS abstime LANGUAGE sql AS 'SELECT NULL'",
            ["abstime", "reltime"],
        ),
        (_plpgsql("x := now()::timestamptz;"), []),
    ],
)
def test_types_postgresql_12_removed(text: str, expected: list[str]) -> None:
    language = "sql" if "LANGUAGE sql" in text else "plpgsql"
    verdict = _check(text, source="11.22", line="14", language=language)
    (result,) = [c for c in verdict.checks if c.rule_id == "postgresql-routine-v12-removed-types"]
    assert list(result.findings) == expected and (result.outcome == "fail") is bool(expected)


@pytest.mark.parametrize("line", aurora_version_lines()[TARGET].lines)
def test_starting_or_stopping_a_base_backup_fails_on_every_line(line: str) -> None:
    """#2016 review: an Aurora capability rule, not a version change."""
    (rule,) = [r for r in routine_check_rules() if r.id == "postgresql-routine-backup-control"]
    assert rule.version_change is None
    text = _plpgsql("PERFORM pg_start_backup('x'); PERFORM pg_backup_stop();")
    verdict = _check(text, source=f"{line}.1", line=line)
    (result,) = [c for c in verdict.checks if c.rule_id == rule.id]
    assert result.outcome == "fail"
    assert result.findings == ("pg_backup_stop()", "pg_start_backup()")


def test_a_catalog_relation_is_not_an_unqualified_table() -> None:
    """#2016 review: pg_ relations still resolve under pg_catalog, pg_temp."""
    rule = "postgresql-routine-v17-maintenance-search-path"
    verdict = _check(_plpgsql("SELECT count(*) INTO n FROM pg_class;"))
    (result,) = [c for c in verdict.checks if c.rule_id == rule]
    assert result.outcome == "pass"


# --------------------------------------------------------------------------- the target option


@pytest.mark.parametrize(
    ("version", "installed", "same_major"),
    [("14.11", {}, "14"), ("16.4", {"postgis": "3.4.0"}, "16"), (None, {}, "14")],
)
def test_the_newest_line_is_an_option_and_same_major_stays_the_default(
    version: str | None, installed: dict[str, str], same_major: str
) -> None:
    assert routine_target_line(version, installed, TARGET) == same_major
    assert routine_target_line(version, installed, TARGET, target_choice="same_major") == (
        same_major
    )
    assert routine_target_line(version, installed, TARGET, target_choice="newest") == NEWEST
    assert (
        extension_landing_line(installed, "postgresql", version, TARGET, target_choice="newest")
        == NEWEST
    )
    assert TARGET_LINE_CHOICES == ("same_major", "newest")


def test_an_unknown_target_choice_is_refused() -> None:
    with pytest.raises(ValueError, match="target_choice"):
        routine_target_line("16.4", {}, TARGET, target_choice="latest")  # type: ignore[arg-type]
    # Before any other answer, even where there is no extension list (#2016 review).
    with pytest.raises(ValueError, match="target_choice"):
        extension_landing_line(
            {}, "mysql", "8.0.36", "aurora_mysql", target_choice="latest"  # type: ignore[arg-type]
        )


def test_no_extension_list_means_no_newest_line_either() -> None:
    assert routine_target_line("8.0.36", {}, "aurora_mysql", target_choice="newest") is None


def test_the_newest_line_brings_the_version_rules_the_default_line_does_not() -> None:
    text = _plpgsql("IF pg_is_in_backup() THEN RETURN 0; END IF;", head="SET search_path = app")
    default = routine_target_line("14.11", {}, TARGET)
    newest = routine_target_line("14.11", {}, TARGET, target_choice="newest")
    assert default == "14" and newest == NEWEST
    assert _check(text, source="14.11", line=default).failed_rules == ()
    assert _check(text, source="14.11", line=newest).failed_rules == (
        "postgresql-routine-v15-removed-functions",
    )


# --------------------------------------------------------------------------- the loader

_HEAD = (
    'id = "x-rule"\nfeature = "X"\nsource_engines = ["postgresql"]\n'
    'targets = ["aurora_postgresql"]\nkinds = {kinds}\nimpact = "needs_rewrite"\n'
    'reason = "Why."\nfix_hint = "How."\ndocs = "Release notes"\n'
)


@pytest.mark.parametrize(
    ("kinds", "match", "message"),
    [
        ('["trigger"]', 'called_functions = ["f"]', "cannot apply to a trigger"),
        ('["function"]', 'called_functions = ["Pg_Start_Backup"]', "lower case"),
        ('["function"]', 'catalog_columns = ["pg_class"]', "relation.column"),
        ('["function"]', 'catalog_columns = ["a.b", "a.b"]', "listed twice"),
        ('["procedure"]', "unqualified_without_search_path = true", "functions only"),
        ('["function"]', 'called_functions = ["f"]\ncreates_in_public_schema = true', "exactly"),
    ],
)
def test_a_bad_postgresql_body_rule_fails_to_load(
    tmp_path: Path, kinds: str, match: str, message: str
) -> None:
    text = _HEAD.format(kinds=kinds) + f'\n[match]\n{match}\n\n[version_change]\nline = "15"\n'
    (tmp_path / "x-rule.toml").write_text(text, encoding="utf-8")
    with pytest.raises(CatalogError, match=message):
        load_routine_check_rules(tmp_path)


def test_a_postgresql_version_line_newer_than_every_target_line_fails_to_load(
    tmp_path: Path,
) -> None:
    text = _HEAD.format(kinds='["function"]') + (
        '\n[match]\ncalled_functions = ["f"]\n\n[version_change]\nline = "99"\n'
    )
    (tmp_path / "x-rule.toml").write_text(text, encoding="utf-8")
    with pytest.raises(CatalogError, match="newer than every aurora_postgresql line"):
        load_routine_check_rules(tmp_path)
