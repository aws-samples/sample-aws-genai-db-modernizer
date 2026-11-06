# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The MySQL 5.7 to 8.0 version-change checks for routines (#1952).

Rules with a ``[version_change]`` run only when the move crosses that version: a MySQL
5.7 routine moving to an Aurora MySQL line of 8.0 or later. They never run on a
same-major target, and an unknown source version leaves them ``unknown``, never a pass.
The corpus entries are ``tests/fixtures/routines/mysql/v57-*.sql``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.routine_checks import (
    RoutineCheckRule,
    VersionChange,
    load_routine_check_rules,
    routine_check_rules,
    version_change_scope,
)
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
from src.tools.routines.verdict import RoutineVerdict

CORPUS = Path(__file__).resolve().parents[3] / "fixtures" / "routines" / "mysql"
MANIFEST: list[dict[str, Any]] = tomllib.loads((CORPUS / "manifest.toml").read_text())["routine"]
V57 = [e for e in MANIFEST if str(e.get("source_version", "")).startswith("5.7")]
SOURCE_57 = "5.7.44-log"
TARGET_USER = "admin"


def _routine(entry: dict[str, Any]) -> MySqlRoutine:
    return MySqlRoutine(
        routine_id="shop." + Path(entry["file"]).stem.replace("-", "_"),
        kind=entry["kind"],
        create_sql=(CORPUS / entry["file"]).read_text(encoding="utf-8"),
        sql_mode="STRICT_TRANS_TABLES",
    )


def _proc(body: str) -> MySqlRoutine:
    text = f"CREATE DEFINER=`admin`@`%` PROCEDURE p() BEGIN {body}; END"
    return MySqlRoutine(routine_id="shop.p", kind="procedure", create_sql=text)


def _check(routine: MySqlRoutine, **kwargs: Any) -> RoutineVerdict:
    kwargs.setdefault("source_version", SOURCE_57)
    return check_mysql_routine(
        routine, target_engine="aurora_mysql", target_user=TARGET_USER, **kwargs
    )


def _all_version_rules() -> list[RoutineCheckRule]:
    return [
        r
        for r in routine_check_rules()
        if r.version_change is not None and "mysql" in r.source_engines
    ]


def _version_rules() -> list[RoutineCheckRule]:
    """The 5.7 to 8.0 rules (#1952)."""
    return [r for r in _all_version_rules() if r.version_change == VersionChange(line="8.0")]


def _version_ids() -> set[str]:
    return {r.id for r in _version_rules()}


def _v84_ids() -> set[str]:
    """The 8.0 to 8.4 rules (#2006 review)."""
    return {r.id for r in _all_version_rules() if r.version_change == VersionChange(line="8.4")}


def _found(verdict: RoutineVerdict, rule_id: str) -> list[str]:
    (result,) = [c for c in verdict.checks if c.rule_id == rule_id]
    return list(result.findings) if result.outcome == "fail" else []


def _failed(verdict: RoutineVerdict) -> list[str]:
    return sorted(
        f"{c.rule_id}:{finding}"
        for c in verdict.checks
        if c.outcome == "fail"
        for finding in c.findings
    )


# --------------------------------------------------------------------------- the catalog


def test_the_57_to_80_rules_are_in_the_catalog_with_a_reason_and_fix_hint() -> None:
    assert _version_ids() == {
        "mysql-routine-v80-found-rows",
        "mysql-routine-v80-group-by-direction",
        "mysql-routine-v80-group-by-sort",
        "mysql-routine-v80-query-cache-hint",
        "mysql-routine-v80-removed-function",
        "mysql-routine-v80-removed-spatial-function",
        "mysql-routine-v80-removed-system-variable",
        "mysql-routine-v80-reserved-word",
    }
    for rule in _version_rules():
        assert rule.version_change == VersionChange(line="8.0")
        assert rule.source_engines == ("mysql",) and rule.targets == ("aurora_mysql",)
        assert rule.reason.endswith(".") and rule.fix_hint.endswith(".") and rule.docs
    assert _v84_ids() == {
        "mysql-routine-v84-removed-function",
        "mysql-routine-v84-removed-statement",
        "mysql-routine-v84-removed-system-variable",
        "mysql-routine-v84-reserved-word",
    }
    assert {r.id for r in _all_version_rules()} == _version_ids() | _v84_ids()
    impacts = {r.id: r.impact for r in _version_rules()}
    assert impacts["mysql-routine-v80-found-rows"] == "informational"
    assert impacts["mysql-routine-v80-group-by-sort"] == "behaves_differently"


def test_every_version_rule_has_a_57_corpus_routine_that_trips_it() -> None:
    tripped = {f.split(":", 1)[0] for e in V57 for f in e["findings"]}
    assert _version_ids() <= tripped


# --------------------------------------------------------------------------- when they run


@pytest.mark.parametrize("entry", V57, ids=lambda e: e["file"])
@pytest.mark.parametrize("target_line", [None, "8.0", "8.4"])
def test_rules_never_run_on_a_same_major_target(
    entry: dict[str, Any], target_line: str | None
) -> None:
    """An 8.0 source has every 8.0 change already, whichever line it moves to (the 8.4
    rules are the 8.4 tests')."""
    verdict = _check(_routine(entry), source_version="8.0.36", target_line=target_line)
    assert not {c.rule_id for c in verdict.checks} & _version_ids()
    assert not [f for f in _failed(verdict) if "-v80-" in f]


@pytest.mark.parametrize("entry", V57, ids=lambda e: e["file"])
@pytest.mark.parametrize("target_line", [None, "8.0", "8.4"])
def test_a_57_source_gets_the_rules_on_every_line(
    entry: dict[str, Any], target_line: str | None
) -> None:
    verdict = _check(_routine(entry), target_line=target_line)
    assert _version_ids() <= {c.rule_id for c in verdict.checks}
    assert _failed(verdict) == entry["findings"]


@pytest.mark.parametrize("source_version", ["5.6.51", "5.7.9", "5.7.44-log"])
def test_any_source_older_than_80_crosses_it(source_version: str) -> None:
    verdict = _check(_proc("SELECT SQL_CACHE id FROM t"), source_version=source_version)
    assert _found(verdict, "mysql-routine-v80-query-cache-hint") == ["SQL_CACHE"]


@pytest.mark.parametrize("source_version", [None, "", "unknown"])
def test_an_unknown_source_version_leaves_the_rules_unknown_never_pass(
    source_version: str | None,
) -> None:
    (entry,) = [e for e in V57 if e["file"] == "v57-reserved-words.sql"]
    verdict = _check(_routine(entry), source_version=source_version)
    every = _version_ids() | _v84_ids()
    assert set(verdict.unknown_rules) == every
    for result in verdict.checks:
        if result.rule_id in every:
            assert result.findings == ("source version unknown",)
    # Unknown never blocks: the routine fails no other rule.
    assert verdict.status == "as_is" and not verdict.failed_rules


def _with_line(rule_id: str, line: str) -> RoutineCheckRule:
    (rule,) = [r for r in routine_check_rules() if r.id == rule_id]
    return rule.model_copy(update={"version_change": VersionChange(line=line)})


@pytest.mark.parametrize(
    ("line", "source", "targets", "scope"),
    [
        ("8.0", "5.7.44", ["8.0"], "runs"),
        ("8.0", "5.7.44", ["8.0", "8.4"], "runs"),
        ("8.0", "8.0.36", ["8.0"], "not_a_version_change"),
        ("8.0", "8.0.36", ["8.4"], "not_a_version_change"),
        ("8.0", "8.4.3", ["8.4"], "not_a_version_change"),
        ("8.0", None, ["8.0"], "source_unknown"),
        # A change made in a later line: the target line decides.
        ("8.4", "8.0.36", ["8.4"], "runs"),
        ("8.4", "8.0.36", ["8.0"], "not_a_version_change"),
        ("8.4", "8.0.36", ["8.0", "8.4"], "target_unknown"),
        ("8.4", None, ["8.0"], "not_a_version_change"),
        ("8.4", None, ["8.0", "8.4"], "source_unknown"),
    ],
)
def test_the_version_change_scope(
    line: str, source: str | None, targets: list[str], scope: str
) -> None:
    rule = _with_line("mysql-routine-v80-query-cache-hint", line)
    assert version_change_scope(rule, "mysql", source, targets) == scope


def test_a_rule_with_no_version_change_has_no_scope() -> None:
    (rule,) = [r for r in routine_check_rules() if r.id == "mysql-routine-set-global"]
    assert version_change_scope(rule, "mysql", "8.0.36", ["8.0"]) is None


def test_a_line_only_some_targets_have_is_unknown_until_one_is_chosen() -> None:
    rules = [_with_line("mysql-routine-v80-query-cache-hint", "8.4")]
    routine = _proc("SELECT SQL_CACHE id FROM t")
    unchosen = _check(routine, source_version="8.0.36", rules=rules)
    assert unchosen.unknown_rules == ("mysql-routine-v80-query-cache-hint",)
    assert unchosen.checks[0].findings == ("target line not chosen",)
    chosen = _check(routine, source_version="8.0.36", target_line="8.4", rules=rules)
    assert chosen.failed_rules == ("mysql-routine-v80-query-cache-hint",)
    assert not _check(routine, source_version="8.0.36", target_line="8.0", rules=rules).checks


# --------------------------------------------------------------------------- what they find

_WORD = "mysql-routine-v80-reserved-word"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("DECLARE rank INT DEFAULT 0", ["RANK"]),
        ("SELECT id AS row_number FROM t", ["ROW_NUMBER"]),
        ("SELECT o.id FROM orders AS lateral", ["LATERAL"]),
        ("UPDATE t SET system = 1, groups = 2", ["GROUPS", "SYSTEM"]),
        ("CALL log_it(cume_dist)", ["CUME_DIST"]),
        ("SELECT rows FROM t", ["ROWS"]),
        ("SELECT function FROM t", ["FUNCTION"]),
        ("SELECT row FROM t", ["ROW"]),
        ("PREPARE s FROM 'SELECT rank FROM t'; EXECUTE s", ["RANK"]),
        # Not identifiers 8.0 rejects: quoted, a qualified name's later part, variables,
        # keyword uses 5.7 had, string literals, words that only start like one.
        ("SELECT `rank`, t.rank, t.`window` FROM t", []),
        ("SET @rank = 1, @@session.sql_mode = 'ANSI'", []),
        ("SELECT a FROM t WHERE ROW(a, b) = (1, 2)", []),
        ("DROP FUNCTION IF EXISTS f", []),
        # #2006 review: GRANT/REVOKE ... ON FUNCTION, and ARRAY and MEMBER (not reserved
        # in current 8.0 or 8.4).
        ("GRANT EXECUTE ON FUNCTION shop.f TO 'u'@'%'", []),
        ("REVOKE EXECUTE ON FUNCTION shop.f FROM 'u'@'%'", []),
        ("SELECT member, array FROM t", []),
        ("SELECT 'rank' AS label, ranking, ranked FROM t", []),
        ("SELECT SQL_CALC_FOUND_ROWS a FROM t ORDER BY a", []),
    ],
)
def test_words_80_reserves_used_as_identifiers(body: str, expected: list[str]) -> None:
    assert _found(_check(_proc(body)), _WORD) == expected


def test_a_parameter_or_label_named_with_a_reserved_word_is_found() -> None:
    text = "CREATE PROCEDURE p(IN rank INT) BEGIN window: LOOP LEAVE window; END LOOP window; END"
    routine = MySqlRoutine(routine_id="shop.p", kind="procedure", create_sql=text)
    assert _found(_check(routine), _WORD) == ["RANK", "WINDOW"]


def test_for_each_row_and_the_routine_s_own_name_are_not_identifiers() -> None:
    trigger = MySqlRoutine(
        routine_id="shop.t",
        kind="trigger",
        create_sql="CREATE TRIGGER `rank` BEFORE INSERT ON t FOR EACH ROW SET NEW.a = 1",
    )
    function = MySqlRoutine(
        routine_id="shop.f",
        kind="function",
        create_sql="CREATE FUNCTION `function`() RETURNS INT RETURN 1",
    )
    for routine in (trigger, function):
        assert _found(_check(routine), _WORD) == []


_SORT = "mysql-routine-v80-group-by-sort"
_DIRECTION = "mysql-routine-v80-group-by-direction"


@pytest.mark.parametrize(
    ("body", "sort", "direction"),
    [
        ("SELECT a, COUNT(*) FROM t GROUP BY a", ["GROUP BY without ORDER BY"], []),
        (
            "DECLARE c CURSOR FOR SELECT a FROM t GROUP BY a; OPEN c",
            ["GROUP BY without ORDER BY"],
            [],
        ),
        ("SELECT a INTO @a FROM t GROUP BY a LIMIT 1", ["GROUP BY without ORDER BY"], []),
        ("SELECT a FROM t GROUP BY a DESC", ["GROUP BY without ORDER BY"], ["GROUP BY DESC"]),
        ("SELECT a FROM t GROUP BY a, b ASC ORDER BY a", [], ["GROUP BY ASC"]),
        ("SELECT * FROM (SELECT a FROM t GROUP BY a DESC) x ORDER BY a", [], ["GROUP BY DESC"]),
        # Sorted, or where row order never mattered.
        ("SELECT a, COUNT(*) FROM t GROUP BY a ORDER BY a", [], []),
        ("SELECT a FROM t GROUP BY a WITH ROLLUP", ["GROUP BY without ORDER BY"], []),
        ("SELECT * FROM (SELECT a FROM t GROUP BY a) x ORDER BY a", [], []),
        ("SELECT COUNT(*) INTO @n FROM (SELECT a FROM t GROUP BY a) x", [], []),
        ("INSERT INTO s (a, n) SELECT a, COUNT(*) FROM t GROUP BY a", [], []),
        ("SELECT GROUP_CONCAT(b ORDER BY b DESC) FROM t GROUP BY a ORDER BY a", [], []),
        ("SELECT a FROM t ORDER BY a DESC", [], []),
        # #2006 review: GROUP BY with LIMIT picks other rows on 8.0, at any level.
        (
            "INSERT INTO s (a) SELECT a FROM t GROUP BY a LIMIT 5",
            ["GROUP BY with LIMIT, without ORDER BY"],
            [],
        ),
        (
            "SELECT * FROM (SELECT a FROM t GROUP BY a LIMIT 5) x ORDER BY a",
            ["GROUP BY with LIMIT, without ORDER BY"],
            [],
        ),
        ("INSERT INTO s (a) SELECT a FROM t GROUP BY a ORDER BY a LIMIT 5", [], []),
        ("SELECT 'GROUP BY a DESC' AS note", [], []),
    ],
)
def test_group_by_sorting(body: str, sort: list[str], direction: list[str]) -> None:
    verdict = _check(_proc(body))
    assert (_found(verdict, _SORT), _found(verdict, _DIRECTION)) == (sort, direction)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("SELECT PASSWORD('x')", ["mysql-routine-v80-removed-function:PASSWORD()"]),
        ("SET @v = ENCODE('x', 'k')", ["mysql-routine-v80-removed-function:ENCODE()"]),
        (
            "SELECT MBREqual(a, b) FROM t ORDER BY 1",
            ["mysql-routine-v80-removed-spatial-function:MBREQUAL()"],
        ),
        # #2006 review: a table name or a qualified user function is not a call.
        ("INSERT INTO area (id) VALUES (1)", []),
        ("INSERT INTO x (id) VALUES (1)", []),
        ("UPDATE area SET id = 1", []),
        ("SELECT shop.area(x), shop.password(y) FROM t ORDER BY 1", []),
        ("SELECT a FROM t JOIN area (id) ORDER BY 1", []),
        (
            "SELECT @@innodb_large_prefix INTO @x",
            ["mysql-routine-v80-removed-system-variable:@@innodb_large_prefix"],
        ),
        (
            "SET SESSION show_compatibility_56 = ON",
            ["mysql-routine-v80-removed-system-variable:SET show_compatibility_56"],
        ),
        ("FLUSH QUERY CACHE", ["mysql-routine-v80-query-cache-hint:FLUSH QUERY CACHE"]),
        ("RESET QUERY CACHE", ["mysql-routine-v80-query-cache-hint:RESET QUERY CACHE"]),
        (
            "SELECT GLength(g), ST_Length(g) FROM t ORDER BY 1",
            ["mysql-routine-v80-removed-spatial-function:GLENGTH()"],
        ),
        ("SELECT ST_X(p), MBRContains(a, b) FROM t ORDER BY 1", []),
        ("SELECT password, x FROM t ORDER BY 1", []),
        (
            "SELECT SQL_CALC_FOUND_ROWS a FROM t ORDER BY a LIMIT 5; SELECT FOUND_ROWS()",
            [
                "mysql-routine-v80-found-rows:FOUND_ROWS()",
                "mysql-routine-v80-found-rows:SQL_CALC_FOUND_ROWS",
            ],
        ),
        (
            "SET SESSION tx_isolation = 'READ-COMMITTED'",
            ["mysql-routine-v80-removed-system-variable:SET tx_isolation"],
        ),
        (
            "SET @@tx_read_only = 0",
            ["mysql-routine-v80-removed-system-variable:SET tx_read_only"],
        ),
        (
            "SELECT @@GLOBAL.query_cache_size INTO @q",
            ["mysql-routine-v80-removed-system-variable:@@query_cache_size"],
        ),
        (
            "IF @@old_passwords = 0 THEN SELECT 1; END IF",
            ["mysql-routine-v80-removed-system-variable:@@old_passwords"],
        ),
        ("SET SESSION transaction_isolation = 'READ-COMMITTED', @tx_isolation = 1", []),
        ("SELECT @@transaction_read_only INTO @r", []),
    ],
)
def test_removed_functions_variables_and_deprecated_modifiers(
    body: str, expected: list[str]
) -> None:
    assert [f for f in _failed(_check(_proc(body))) if "-v80-" in f] == expected


def test_a_found_rows_finding_is_informational_and_keeps_the_routine_as_is() -> None:
    verdict = _check(_proc("SELECT SQL_CALC_FOUND_ROWS a FROM t ORDER BY a LIMIT 5"))
    assert verdict.failed_rules == ("mysql-routine-v80-found-rows",)
    assert verdict.status == "as_is"


def test_the_clean_57_routine_passes_every_version_rule() -> None:
    (entry,) = [e for e in V57 if e["file"] == "v57-clean.sql"]
    verdict = _check(_routine(entry))
    outcomes = {c.rule_id: c.outcome for c in verdict.checks if c.rule_id in _version_ids()}
    assert outcomes == {rule_id: "pass" for rule_id in _version_ids()}


# --------------------------------------------------------------------------- the loader

_HEAD = (
    'id = "x-rule"\nfeature = "X"\nsource_engines = ["mysql"]\ntargets = ["aurora_mysql"]\n'
    'kinds = ["procedure"]\nimpact = "needs_rewrite"\nreason = "Why."\n'
    'fix_hint = "How."\ndocs = "Manual"\n'
)


def _rule_file(tmp_path: Path, match: str, version: str | None = None) -> None:
    text = f"{_HEAD}\n[match]\n{match}\n"
    if version is not None:
        text += f"\n[version_change]\n{version}\n"
    (tmp_path / "x-rule.toml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("match", "version", "message"),
    [
        ('statement_starts = ["X"]', 'line = "eight"', "line"),
        ('statement_starts = ["X"]', 'line = "9.0"', "newer than every aurora_mysql line"),
        ('statement_starts = ["X"]', 'line = "8"', "not a mysql version line"),
        ('statement_starts = ["X"]', 'line = "8.0"\nsince = 1', "since"),
        ("parses = true", 'line = "8.0"', "applies to every version"),
        ('reserved_word_contexts = ["EACH ROW"]\nparses = true', None, "goes with reserved_words"),
        ('reserved_words = ["RANK"]\nreserved_word_contexts = ["EACH ROW"]', None, "holds one"),
        ('reserved_words = ["ROW"]\nreserved_word_contexts = ["each row"]', None, "upper-case"),
        ('reserved_words = ["rank"]', None, "upper case"),
        ('reserved_words = ["RANK", "RANK"]', None, "listed twice"),
        ('system_variables = ["TX_ISOLATION"]', None, "lower case"),
        ('group_by = "always"', None, "group_by"),
        ('group_by = "with_direction"\nreserved_words = ["RANK"]', None, "exactly one of"),
    ],
)
def test_a_bad_version_change_rule_fails_to_load(
    tmp_path: Path, match: str, version: str | None, message: str
) -> None:
    _rule_file(tmp_path, match, version)
    with pytest.raises(CatalogError, match=message):
        load_routine_check_rules(tmp_path)


def test_a_version_change_rule_for_a_target_with_no_lines_fails_to_load(tmp_path: Path) -> None:
    _rule_file(tmp_path, 'statement_starts = ["X"]', 'line = "8.0"')
    with pytest.raises(CatalogError, match="needs version lines"):
        load_routine_check_rules(tmp_path, versions={})


def test_a_version_change_rule_loads_with_its_check(tmp_path: Path) -> None:
    _rule_file(tmp_path, 'group_by = "without_order_by"', 'line = "8.0"')
    (rule,) = load_routine_check_rules(tmp_path)
    assert rule.match.check == "group_by" and rule.version_change == VersionChange(line="8.0")


# --------------------------------------------------------------------------- 8.0 to 8.4 (#2006 review)

_V84_BODIES = {
    "SELECT qualify, tablesample FROM t ORDER BY 1": [
        "mysql-routine-v84-reserved-word:QUALIFY",
        "mysql-routine-v84-reserved-word:TABLESAMPLE",
    ],
    "FLUSH HOSTS": ["mysql-routine-v84-removed-statement:FLUSH HOSTS"],
    "SHOW SLAVE STATUS": ["mysql-routine-v84-removed-statement:SHOW SLAVE STATUS"],
    "SHOW MASTER STATUS": ["mysql-routine-v84-removed-statement:SHOW MASTER STATUS"],
    "RESET MASTER": ["mysql-routine-v84-removed-statement:RESET MASTER"],
    "SET @@expire_logs_days = 7": [
        "mysql-routine-v84-removed-system-variable:SET expire_logs_days"
    ],
    "SELECT @@default_authentication_plugin INTO @p": [
        "mysql-routine-v84-removed-system-variable:@@default_authentication_plugin"
    ],
    "SELECT WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS('uuid:1', 1) INTO @w": [
        "mysql-routine-v84-removed-function:WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS()"
    ],
}


def _v84_failed(verdict: RoutineVerdict) -> list[str]:
    return [f for f in _failed(verdict) if "-v84-" in f]


@pytest.mark.parametrize("body", sorted(_V84_BODIES))
@pytest.mark.parametrize("source_version", ["5.7.44", "8.0.36"])
def test_a_57_or_80_routine_bound_for_84_gets_the_84_rules(body: str, source_version: str) -> None:
    verdict = _check(_proc(body), source_version=source_version, target_line="8.4")
    assert _v84_failed(verdict) == _V84_BODIES[body]


@pytest.mark.parametrize("body", sorted(_V84_BODIES))
def test_the_84_rules_never_run_on_an_80_or_84_target_from_its_own_line(body: str) -> None:
    for source, line in (("8.0.36", "8.0"), ("5.7.44", "8.0"), ("8.4.3", "8.4")):
        verdict = _check(_proc(body), source_version=source, target_line=line)
        assert not {c.rule_id for c in verdict.checks} & _v84_ids()


def test_a_57_source_bound_for_84_gets_both_lines_rules() -> None:
    verdict = _check(_proc("SELECT SQL_CACHE qualify FROM t ORDER BY 1"), target_line="8.4")
    assert [f for f in _failed(verdict) if "-v8" in f] == [
        "mysql-routine-v80-query-cache-hint:SQL_CACHE",
        "mysql-routine-v84-reserved-word:QUALIFY",
    ]


def test_with_no_line_chosen_the_84_rules_are_unknown() -> None:
    verdict = _check(_proc("SELECT qualify FROM t ORDER BY 1"), source_version="8.0.36")
    assert set(verdict.unknown_rules) == _v84_ids()
    assert {c.findings for c in verdict.checks if c.rule_id in _v84_ids()} == {
        ("target line not chosen",)
    }


def test_the_84_rules_skip_new_old_and_language() -> None:
    """A trigger's SET NEW.col and a local variable named language are not 8.4's removed
    new, old and language variables."""
    trigger = MySqlRoutine(
        routine_id="shop.t",
        kind="trigger",
        create_sql="CREATE TRIGGER t BEFORE INSERT ON o FOR EACH ROW SET NEW.a = OLD.a",
    )
    assert _v84_failed(_check(trigger, source_version="8.0.36", target_line="8.4")) == []
    local = _proc("DECLARE language INT DEFAULT 0; SET language = 1")
    assert _v84_failed(_check(local, source_version="8.0.36", target_line="8.4")) == []


def test_the_clean_57_routine_passes_every_rule_on_84() -> None:
    (entry,) = [e for e in V57 if e["file"] == "v57-clean.sql"]
    verdict = _check(_routine(entry), target_line="8.4")
    every = _version_ids() | _v84_ids()
    assert {c.rule_id: c.outcome for c in verdict.checks if c.rule_id in every} == {
        rule_id: "pass" for rule_id in every
    }


def test_the_source_version_is_a_required_keyword() -> None:
    with pytest.raises(TypeError, match="source_version"):
        check_mysql_routine(  # type: ignore[call-arg]
            _proc("SELECT 1"), target_engine="aurora_mysql", target_user=TARGET_USER
        )
