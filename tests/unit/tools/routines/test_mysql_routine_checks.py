# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""MySQL routines parsed and checked against Aurora MySQL (#1936).

The corpus is ``tests/fixtures/routines/mysql/``: synthetic routines, one or more per
catalog rule, plus delimiter, trigger, event, dynamic-SQL and unparseable cases, each
with its expected verdict in ``manifest.toml``.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlglot import exp
from sqlglot.errors import SqlglotError

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.routine_checks import (
    ROUTINE_CHECKS_DIR,
    RoutineCheckRule,
    load_routine_check_rules,
    routine_check_rules,
    version_change_scope,
)
from src.tools.routines.mysql_builtins import is_builtin_function
from src.tools.routines.mysql_checks import (
    MAX_NESTING,
    MySqlRoutine,
    check_mysql_routine,
    split_client_statements,
)
from src.tools.routines.verdict import RoutineCheckResult, RoutineVerdict
from src.tools.sql.facade import RoutineTokenError, routine_statement_refs, routine_tokens
from tests.unit.tools.sql._timing import assert_cost_ratio, assert_linear

CORPUS = Path(__file__).resolve().parents[3] / "fixtures" / "routines" / "mysql"
MANIFEST: list[dict[str, Any]] = tomllib.loads((CORPUS / "manifest.toml").read_text())["routine"]
TARGET_USER = "admin"


def _routine(entry: dict[str, Any], **overrides: Any) -> MySqlRoutine:
    fields: dict[str, Any] = {
        "routine_id": "shop." + Path(entry["file"]).stem.replace("-", "_"),
        "kind": entry["kind"],
        "create_sql": (CORPUS / entry["file"]).read_text(encoding="utf-8"),
        "sql_mode": entry.get("sql_mode", "STRICT_TRANS_TABLES"),
        "security_type": entry.get("security_type"),
        "definer": entry.get("definer"),
    }
    return MySqlRoutine(**(fields | overrides))


def _proc(text: str, **fields: Any) -> MySqlRoutine:
    base: dict[str, Any] = {"routine_id": "shop.p", "kind": "procedure", "create_sql": text}
    return MySqlRoutine(**(base | fields))


def _check(routine: MySqlRoutine, **kwargs: Any) -> RoutineVerdict:
    kwargs.setdefault("source_version", None)
    return check_mysql_routine(routine, target_engine="aurora_mysql", **kwargs)


def _ids(verdict: RoutineVerdict) -> list[str]:
    """Every failed rule with each of its findings, as ``<rule id>:<finding>``, sorted."""
    return sorted(
        f"{c.rule_id}:{finding}"
        for c in verdict.checks
        if c.outcome == "fail"
        for finding in c.findings
    )


def _deps(verdict: RoutineVerdict) -> list[str]:
    return [f"{d.kind}:{d.schema_name or ''}.{d.name}" for d in verdict.dependencies]


# --------------------------------------------------------------------------- the corpus


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["file"])
def test_each_corpus_routine_gets_its_expected_verdict(entry: dict[str, Any]) -> None:
    verdict = _check(
        _routine(entry),
        target_user=TARGET_USER,
        source_version=entry.get("source_version"),
        target_line=entry.get("target_line"),
    )
    assert verdict.status == entry["status"]
    assert verdict.parsed is entry["parsed"]
    assert _ids(verdict) == entry["findings"]
    assert _deps(verdict) == entry["dependencies"]
    assert verdict.dependencies_complete is entry.get("dependencies_complete", True)
    assert verdict.target_engine == "aurora_mysql" and verdict.kind == entry["kind"]
    # One result per MySQL rule, in check order, passed or failed, but for the version-change
    # rules this move does not cross (#1952).
    lines = (entry["target_line"],) if entry.get("target_line") else ("8.0", "8.4")
    crossed = {
        r.id
        for r in _mysql_rules()
        if version_change_scope(r, "mysql", entry.get("source_version"), lines)
        != "not_a_version_change"
    }
    assert {c.rule_id for c in verdict.checks} == crossed
    assert verdict.parsed is (verdict.parse_failure is None)


def _mysql_rules() -> list[RoutineCheckRule]:
    """The MySQL rules the checker runs: the dependency check needs the step's moved
    objects, so it runs in ``src/tools/routines/dependencies.py`` (#1937)."""
    return [
        r
        for r in routine_check_rules()
        if "mysql" in r.source_engines and r.match.check != "dependencies"
    ]


def test_every_mysql_rule_has_a_corpus_routine_that_trips_it() -> None:
    tripped = {f.split(":", 1)[0] for e in MANIFEST for f in e["findings"]}
    assert {r.id for r in _mysql_rules()} <= tripped


def test_every_corpus_file_is_in_the_manifest() -> None:
    assert sorted(p.name for p in CORPUS.glob("*.sql")) == sorted(e["file"] for e in MANIFEST)


def test_the_corpus_has_passing_triggers_events_functions_and_procedures() -> None:
    passing = {e["kind"] for e in MANIFEST if e["status"] == "as_is" and e["parsed"]}
    assert passing == {"procedure", "function", "trigger", "event"}


# --------------------------------------------------------------------------- literals


def _without_catalog_text(verdict: RoutineVerdict) -> str:
    """The verdict as JSON without the catalog's own sentences (reason, fix hint)."""
    data = verdict.model_dump(mode="json")
    for check in data["checks"]:
        check.pop("reason")
        check.pop("fix_hint")
    return json.dumps(data)


_LITERAL = re.compile(r"'((?:[^'\\]|\\.|'')*)'")


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["file"])
def test_a_verdict_never_carries_a_literal_from_the_routine(entry: dict[str, Any]) -> None:
    text = (CORPUS / entry["file"]).read_text(encoding="utf-8")
    dumped = _without_catalog_text(_check(_routine(entry), target_user=TARGET_USER))
    for literal in _LITERAL.findall(text):
        if len(literal) >= 4:
            assert literal not in dumped, entry["file"]


def test_a_named_definer_is_not_echoed() -> None:
    entry = next(e for e in MANIFEST if e["file"] == "definer-named.sql")
    dumped = json.dumps(_check(_routine(entry), target_user=TARGET_USER).model_dump(mode="json"))
    assert "legacy_ops" not in dumped and "10.0." not in dumped


# --------------------------------------------------------------------------- delimiters


def test_without_a_delimiter_line_the_text_is_one_statement() -> None:
    text = "CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END;"
    assert split_client_statements(text) == ["CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END"]


def test_a_delimiter_line_splits_like_the_mysql_client() -> None:
    text = "DELIMITER $$\nCREATE PROCEDURE p()\nBEGIN SELECT '$$'; END$$\nDELIMITER ;\n"
    assert split_client_statements(text) == ["CREATE PROCEDURE p()\nBEGIN SELECT '$$'; END"]


def test_the_word_delimiter_inside_a_body_is_not_a_delimiter_line() -> None:
    text = "CREATE PROCEDURE p() BEGIN SELECT 1 AS delimiter; END"
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert verdict.parsed and verdict.status == "as_is"


def test_a_delimiter_inside_a_comment_or_quote_does_not_split() -> None:
    text = (
        "DELIMITER //\nCREATE PROCEDURE p() BEGIN\n  -- not here //\n"
        '  /* nor // here */ SELECT "//", `a//b` FROM t;\nEND//\nDELIMITER ;'
    )
    (stmt,) = split_client_statements(text)
    assert stmt.startswith("CREATE PROCEDURE") and stmt.endswith("END")


def test_executable_comments_are_unwrapped() -> None:
    text = "/*!50003 CREATE*/ /*!50020 DEFINER=`admin`@`%`*/ /*!50003 PROCEDURE p() SELECT 1 */"
    (stmt,) = split_client_statements(text)
    assert "CREATE" in stmt and "/*" not in stmt
    assert _check(_proc(text), target_user=TARGET_USER).status == "as_is"


def test_a_delimiter_line_with_no_delimiter_is_unparseable() -> None:
    verdict = _check(_proc("DELIMITER\nCREATE PROCEDURE p() SELECT 1"))
    assert _ids(verdict) == ["mysql-routine-unparseable:delimiter"]


def test_a_text_with_only_a_drop_is_unparseable() -> None:
    verdict = _check(_proc("DROP PROCEDURE IF EXISTS p;"))
    assert _ids(verdict) == ["mysql-routine-unparseable:no create"]


# --------------------------------------------------------------------------- definer


@pytest.mark.parametrize(
    "definer,target,named",
    [
        ("`admin`@`%`", "admin", False),
        ("'admin'@'%'", "admin@%", False),
        ("admin@localhost", "admin", False),
        ("admin@localhost", "admin@%", True),
        ("`Admin`@`%`", "admin", True),
        ("CURRENT_USER", None, False),
        ("CURRENT_USER()", None, False),
        ("`admin`@`%`", None, True),
        ("`app`@`%`", "admin", True),
    ],
)
def test_the_definer_check(definer: str, target: str | None, named: bool) -> None:
    text = f"CREATE DEFINER={definer} PROCEDURE p() SELECT 1"
    verdict = _check(_proc(text), target_user=target)
    assert (_ids(verdict) == ["mysql-routine-definer:named definer"]) is named
    assert verdict.parsed


def test_a_text_without_a_definer_clause_uses_the_collected_definer() -> None:
    text = "CREATE PROCEDURE p() SELECT 1"
    assert not _check(_proc(text), target_user=TARGET_USER).failed_rules
    verdict = _check(_proc(text, definer="app@%"), target_user=TARGET_USER)
    assert _ids(verdict) == ["mysql-routine-definer:named definer"]
    (failed,) = [c for c in verdict.checks if c.outcome == "fail"]
    assert failed.impact == "needs_rewrite"


def test_sql_security_invoker_skips_the_definer_check_only_for_procedures_and_functions() -> None:
    proc = _check(
        _proc("CREATE DEFINER=`app`@`%` PROCEDURE p() SELECT 1", security_type="INVOKER"),
        target_user=TARGET_USER,
    )
    assert not proc.failed_rules
    trigger = _check(
        _proc(
            "CREATE DEFINER=`app`@`%` TRIGGER t BEFORE DELETE ON orders FOR EACH ROW SET @x = 1",
            kind="trigger",
            security_type="INVOKER",
        ),
        target_user=TARGET_USER,
    )
    assert _ids(trigger) == ["mysql-routine-definer:named definer"]


def test_the_text_s_sql_security_wins_over_the_metadata() -> None:
    text = "CREATE DEFINER=`app`@`%` PROCEDURE p() SQL SECURITY DEFINER SELECT 1"
    verdict = _check(_proc(text, security_type="INVOKER"), target_user=TARGET_USER)
    assert _ids(verdict) == ["mysql-routine-definer:named definer"]


# --------------------------------------------------------------------------- privileges


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("SET GLOBAL max_connections = 10", ["mysql-routine-set-global:SET GLOBAL"]),
        ("SET @@global.max_connections = 10", ["mysql-routine-set-global:SET GLOBAL"]),
        ("SET PERSIST_ONLY x = 1", ["mysql-routine-set-global:SET PERSIST_ONLY"]),
        ("SET a = 1, GLOBAL b = 2", ["mysql-routine-set-global:SET GLOBAL"]),
        ("SET SESSION sql_log_bin = 0", ["mysql-routine-sql-log-bin:SET sql_log_bin"]),
        (
            "SET @@GLOBAL.sql_log_bin = 0",
            ["mysql-routine-set-global:SET GLOBAL", "mysql-routine-sql-log-bin:SET sql_log_bin"],
        ),
        ("CHANGE REPLICATION SOURCE TO SOURCE_HOST = 'h'", None),
        ("START REPLICA", ["mysql-routine-replication-control:START REPLICA"]),
        ("PURGE BINARY LOGS BEFORE NOW()", ["mysql-routine-replication-control:PURGE BINARY LOGS"]),
        ("UNINSTALL COMPONENT 'file://component_x'", None),
        ("RESTART", ["mysql-routine-server-shutdown:RESTART"]),
        ("SELECT 1 INTO DUMPFILE '/tmp/x'", ["mysql-routine-file-output:INTO DUMPFILE"]),
        # Look-alikes that pass.
        ("SET SESSION wait_timeout = 60", []),
        ("SET @global = 1", []),
        ("SET v_global = 'SET GLOBAL x = 1'", []),
        ("SELECT 'CHANGE MASTER TO' AS note", []),
        ("INSERT INTO install_plugin_log (note) VALUES ('INSTALL PLUGIN')", []),
        ("SELECT id INTO v_outfile FROM orders LIMIT 1", []),
    ],
)
def test_super_only_statements(statement: str, expected: list[str] | None) -> None:
    text = f"CREATE DEFINER=`admin`@`%` PROCEDURE p() BEGIN DECLARE v_outfile INT; {statement}; END"
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert verdict.parsed
    if expected is None:
        failed = [c for c in verdict.checks if c.outcome == "fail"]
        assert len(failed) == 1 and failed[0].impact == "needs_rewrite"
    else:
        assert _ids(verdict) == sorted(expected)


def test_a_super_only_statement_in_a_handler_or_nested_block_is_found() -> None:
    text = (
        "CREATE PROCEDURE p() BEGIN DECLARE CONTINUE HANDLER FOR SQLEXCEPTION "
        "BEGIN SET GLOBAL read_only = 0; END; "
        "IF 1 THEN WHILE 0 DO SHUTDOWN; END WHILE; END IF; END"
    )
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert _ids(verdict) == [
        "mysql-routine-server-shutdown:SHUTDOWN",
        "mysql-routine-set-global:SET GLOBAL",
    ]


# --------------------------------------------------------------------------- modes


@pytest.mark.parametrize(
    "sql_mode,expected",
    [
        ("", []),
        ("ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION", []),
        ("traditional,ansi", []),
        ("NO_AUTO_CREATE_USER", ["mysql-routine-mode-removed:NO_AUTO_CREATE_USER"]),
        (
            "MSSQL, NO_KEY_OPTIONS",
            ["mysql-routine-mode-removed:MSSQL", "mysql-routine-mode-removed:NO_KEY_OPTIONS"],
        ),
        ("SIMULTANEOUS_ASSIGNMENT", ["mysql-routine-mode-unknown:unknown mode"]),
        ("PAD_CHAR_TO_FULL_LENGTH", ["mysql-routine-mode-deprecated:PAD_CHAR_TO_FULL_LENGTH"]),
    ],
)
def test_sql_modes(sql_mode: str, expected: list[str]) -> None:
    verdict = _check(
        _proc("CREATE PROCEDURE p() SELECT 1", sql_mode=sql_mode), target_user=TARGET_USER
    )
    assert _ids(verdict) == expected


def test_an_informational_mode_finding_keeps_the_routine_as_is() -> None:
    verdict = _check(
        _proc("CREATE PROCEDURE p() SELECT 1", sql_mode="PAD_CHAR_TO_FULL_LENGTH"),
        target_user=TARGET_USER,
    )
    assert verdict.failed_rules and verdict.status == "as_is"


def test_the_mode_check_runs_on_an_unparseable_routine() -> None:
    verdict = _check(
        _proc("CREATE PROCEDURE p() BEGIN", sql_mode="NO_AUTO_CREATE_USER"),
        target_user=TARGET_USER,
    )
    assert not verdict.parsed
    assert "mysql-routine-mode-removed:NO_AUTO_CREATE_USER" in _ids(verdict)


# --------------------------------------------------------------------------- structure


@pytest.mark.parametrize(
    "body,evidence",
    [
        ("BEGIN SELECT 1", "unbalanced block"),
        ("BEGIN SELECT 1 END", "unbalanced block"),
        ("BEGIN IF 1 THEN SELECT 1; END; END", "unbalanced block"),
        ("BEGIN LOOP SELECT 1; END WHILE; END", "unbalanced block"),
        ("BEGIN SELECT (1; END", "unbalanced parentheses"),
        ("BEGIN LEAVE nowhere; END", "unknown label"),
        ("a: BEGIN b: LOOP LEAVE a; END LOOP c; END a", "end label"),
        ("BEGIN END; SELECT 1", "unbalanced block"),
        ("BEGIN END IF", "end label"),
    ],
)
def test_bodies_that_do_not_close_are_unparseable(body: str, evidence: str) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() {body}"), target_user=TARGET_USER)
    assert not verdict.parsed and not verdict.dependencies_complete
    assert _ids(verdict) == [f"mysql-routine-unparseable:{evidence}"]
    assert verdict.status == "needs_attention"


@pytest.mark.parametrize(
    "body",
    [
        "BEGIN END",
        "BEGIN NOT ATOMIC SELECT 1; END",
        "a: BEGIN b: LOOP LEAVE b; END LOOP; LEAVE a; END a",
        "BEGIN REPEAT SELECT 1; UNTIL CASE WHEN 1 THEN 1 END END REPEAT; END",
        "BEGIN IF CASE WHEN 1 THEN 1 ELSE 0 END THEN SELECT 1; END IF; END",
        "BEGIN CASE WHEN 1 THEN SELECT 1; WHEN 2 THEN BEGIN END; END CASE; END",
        "BEGIN DECLARE a, b INT DEFAULT 0; DECLARE c CONDITION FOR 1062; "
        "DECLARE CONTINUE HANDLER FOR c, SQLWARNING, NOT FOUND BEGIN END; SELECT a; END",
        "BEGIN DECLARE c CONDITION FOR SQLSTATE VALUE '45000'; "
        "BEGIN DECLARE EXIT HANDLER FOR c SELECT 1; END; END",
        "BEGIN BEGIN DECLARE x INT; END; BEGIN DECLARE x INT; END; END",
    ],
)
def test_bodies_that_close_parse(body: str) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() {body}"), target_user=TARGET_USER)
    assert verdict.parsed, _ids(verdict)
    assert not verdict.failed_rules


@pytest.mark.parametrize(
    "text,evidence",
    [
        ("CREATE TABLE t (a INT)", "header"),
        ("CREATE PROCEDURE p SELECT 1", "header"),
        ("CREATE FUNCTION f() RETURNS INT", "no body"),
        ("CREATE TRIGGER t DURING INSERT ON x FOR EACH ROW SET @a = 1", "header"),
        ("CREATE DEFINER app PROCEDURE p() SELECT 1", "header"),
        ("SELECT 1", "more than one statement"),
        ("CREATE PROCEDURE p() SELECT 1 /* never closed", "unterminated comment"),
        ("CREATE FUNCTION p() RETURNS INT RETURN 1", "kind"),
    ],
)
def test_headers_that_do_not_read_are_unparseable(text: str, evidence: str) -> None:
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert _ids(verdict) == [f"mysql-routine-unparseable:{evidence}"]


@pytest.mark.parametrize(
    "body,evidence",
    [
        (
            "BEGIN DECLARE h CONDITION FOR 1; DECLARE c CURSOR FOR SELECT 1; "
            "DECLARE EXIT HANDLER FOR h BEGIN END; DECLARE d CURSOR FOR SELECT 2; END",
            "cursor after handler",
        ),
        ("BEGIN IF 1 THEN DECLARE x INT; END IF; END", "declare after statement"),
        ("DECLARE x INT", "declare outside a block"),
    ],
)
def test_declare_order(body: str, evidence: str) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() {body}"), target_user=TARGET_USER)
    assert _ids(verdict) == [f"mysql-routine-declare-order:{evidence}"]


@pytest.mark.parametrize(
    "condition,evidence",
    [
        ("SQLSTATE '4500'", "sqlstate"),
        ("SQLSTATE 45000", "sqlstate"),
        ("0", "error code"),
        ("SQLEXCEPTION, SQLEXCEPTION", "duplicate handler"),
    ],
)
def test_invalid_handlers(condition: str, evidence: str) -> None:
    text = f"CREATE PROCEDURE p() BEGIN DECLARE EXIT HANDLER FOR {condition} BEGIN END; END"
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert _ids(verdict) == [f"mysql-routine-handler:{evidence}"]


def test_a_condition_declared_in_an_inner_block_is_not_seen_outside() -> None:
    text = (
        "CREATE PROCEDURE p() BEGIN BEGIN DECLARE c CONDITION FOR 1062; END; "
        "BEGIN DECLARE EXIT HANDLER FOR c BEGIN END; END; END"
    )
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert _ids(verdict) == ["mysql-routine-handler:undeclared condition"]


# --------------------------------------------------------------------------- dependencies


def test_dependencies_are_recorded_without_variables_ctes_or_builtins() -> None:
    text = (
        "CREATE PROCEDURE shop.p(IN a INT) BEGIN DECLARE v INT; "
        "WITH recent AS (SELECT id FROM orders) SELECT COUNT(*) INTO v FROM recent, dual; "
        "SET v = (SELECT MAX(id) FROM billing.invoices), @x = loyalty_points(a, NOW()); "
        "REPLACE INTO order_cache (id) SELECT id FROM orders; "
        "IF EXISTS (SELECT 1 FROM refunds) THEN CALL p(a); CALL shop.p(a); END IF; "
        "SELECT LAST_INSERT_ID(), ROW_COUNT(), ST_Distance(a, a); END"
    )
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert verdict.parsed and verdict.dependencies_complete
    assert _deps(verdict) == [
        "routine:.loyalty_points",
        "table:.order_cache",
        "table:.orders",
        "table:.refunds",
        "table:billing.invoices",
    ]


def test_a_recursive_call_in_another_schema_is_a_dependency() -> None:
    text = "CREATE PROCEDURE p() CALL archive.p()"
    assert _deps(_check(_proc(text))) == ["routine:archive.p"]


def test_a_statement_sqlglot_cannot_read_leaves_dependencies_incomplete_without_a_finding() -> None:
    text = "CREATE PROCEDURE p() BEGIN LOCK TABLES orders WRITE; UNLOCK TABLES; END"
    verdict = _check(_proc(text), target_user=TARGET_USER)
    assert verdict.parsed and verdict.status == "as_is"
    assert not verdict.failed_rules


def test_the_builtin_list() -> None:
    assert is_builtin_function("now") and is_builtin_function("ST_Contains")
    assert not is_builtin_function("loyalty_points")


# --------------------------------------------------------------------------- the catalog


def test_the_bundled_routine_rules_load_and_validate() -> None:
    rules = routine_check_rules()
    assert [r.id for r in rules] == sorted(p.stem for p in ROUTINE_CHECKS_DIR.glob("*.toml"))
    mysql = [r for r in rules if "mysql" in r.source_engines]
    assert mysql and all(r.id.startswith("mysql-") for r in mysql)
    assert all(r.source_engines == ("mysql",) and r.targets == ("aurora_mysql",) for r in mysql)
    assert {r.match.check for r in mysql} == {
        "parse",
        "declare_order",
        "handler",
        "definer",
        "statement",
        "sql_mode",
        "dynamic_sql",
        "reserved_word",
        "group_by",
        "dependencies",
    }
    for rule in mysql:
        assert rule.reason.endswith(".") and rule.fix_hint.endswith(".")


_GOOD = (
    'id = "x-rule"\nfeature = "X"\nsource_engines = ["mysql"]\ntargets = ["aurora_mysql"]\n'
    'kinds = ["procedure", "event"]\nimpact = "needs_rewrite"\nreason = "Why."\n'
    'fix_hint = "How."\ndocs = "Manual"\n'
)


def _rule_file(tmp_path: Path, match: str, head: str = _GOOD) -> None:
    (tmp_path / "x-rule.toml").write_text(f"{head}\n[match]\n{match}\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("match", "message"),
    [
        ('statement_starts = ["SET GLOBAL"]\nsql_modes = ["ANSI"]', "exactly one of"),
        ("declare_order = true\nvalid_handlers = true", "exactly one of"),
        ('known_sql_modes = ["ANSI"]\nsql_modes = ["X"]', "goes with unknown_sql_modes"),
        ('always_definer_kinds = ["event"]\nparses = true', "goes with definer_not_target"),
        ('statement_starts = ["set global"]', "upper case"),
        ('statement_starts = ["SET  GLOBAL"]', "upper case"),
        ('set_variables = ["SQL_LOG_BIN"]', "lower case"),
        ('sql_modes = ["ANSI", "ANSI"]', "listed twice"),
        ('definer_not_target_user = true\nalways_definer_kinds = ["view"]', "always_definer"),
    ],
)
def test_a_bad_mysql_match_fails_to_load(tmp_path: Path, match: str, message: str) -> None:
    _rule_file(tmp_path, match)
    with pytest.raises(CatalogError, match=message):
        load_routine_check_rules(tmp_path)


def test_an_event_rule_loads(tmp_path: Path) -> None:
    _rule_file(tmp_path, 'statement_starts = ["LOCK TABLES"]')
    (rule,) = load_routine_check_rules(tmp_path)
    assert rule.kinds == ("procedure", "event") and rule.match.check == "statement"


def test_rules_come_from_the_catalog_not_the_code(tmp_path: Path) -> None:
    _rule_file(tmp_path, 'statement_starts = ["LOCK TABLES"]')
    rules = load_routine_check_rules(tmp_path)
    verdict = _check(
        _proc("CREATE PROCEDURE p() BEGIN SET GLOBAL x = 1; LOCK TABLES t READ; END"),
        rules=rules,
    )
    assert _ids(verdict) == ["x-rule:LOCK TABLES"]
    (result,) = verdict.checks
    assert (result.reason, result.fix_hint, result.impact) == ("Why.", "How.", "needs_rewrite")


def test_a_rule_for_another_kind_or_engine_does_not_run(tmp_path: Path) -> None:
    _rule_file(tmp_path, 'statement_starts = ["SELECT"]')
    rules = load_routine_check_rules(tmp_path)
    assert _check(_proc("CREATE PROCEDURE p() SELECT 1"), rules=rules).checks
    function = _proc("CREATE FUNCTION p() RETURNS INT RETURN 1", kind="function")
    assert not _check(function, rules=rules).checks
    other = (
        _GOOD.replace('["mysql"]', '["postgresql"]')
        .replace("aurora_mysql", "aurora_postgresql")
        .replace('["procedure", "event"]', '["procedure"]')
    )
    (tmp_path / "x-rule.toml").unlink()
    _rule_file(tmp_path, "parses = true", head=other)
    assert not _check(
        _proc("CREATE PROCEDURE p() SELECT 1"), rules=load_routine_check_rules(tmp_path)
    ).checks


@pytest.mark.parametrize(
    ("engine", "line", "message"),
    [
        ("aurora_postgresql", None, "is not the Aurora engine for mysql"),
        ("dynamodb", None, "is not the Aurora engine for mysql"),
        ("aurora_mysql", "5.7", "is not listed"),
    ],
)
def test_the_target_must_be_a_listed_aurora_mysql_line(
    engine: str, line: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        check_mysql_routine(
            _proc("CREATE PROCEDURE p() SELECT 1"),
            target_engine=engine,
            target_line=line,
            source_version=None,
        )


def test_a_listed_target_line_is_recorded() -> None:
    verdict = _check(_proc("CREATE PROCEDURE p() SELECT 1"), target_line="8.0")
    assert verdict.target_line == "8.0"


# --------------------------------------------------------------------------- the verdict


def _verdict(*impacts_and_outcomes: tuple[str, str]) -> RoutineVerdict:
    checks = tuple(
        RoutineCheckResult(
            rule_id=f"r{i}",
            check="statement",
            outcome=outcome,  # type: ignore[arg-type]
            impact=impact,  # type: ignore[arg-type]
            reason="x",
            fix_hint="y",
        )
        for i, (impact, outcome) in enumerate(impacts_and_outcomes)
    )
    return RoutineVerdict(
        routine_id="r",
        kind="event",
        target_engine="t",
        target_line=None,
        parse_failure=None,
        checks=checks,
    )


def test_needs_attention_follows_failed_rules_that_matter() -> None:
    assert _verdict().status == "as_is"
    assert _verdict(("blocks_move", "pass")).status == "as_is"
    informational = _verdict(("informational", "fail"))
    assert informational.status == "as_is" and informational.failed_rules == ("r0",)
    for impact in ("blocks_move", "needs_rewrite", "behaves_differently"):
        verdict = _verdict(("informational", "fail"), (impact, "fail"))
        assert verdict.needs_attention and verdict.status == "needs_attention"


_COMPUTED = {"parsed", "failed_rules", "unknown_rules", "needs_attention", "status"}


def test_a_verdict_round_trips_through_json() -> None:
    entry = next(e for e in MANIFEST if e["file"] == "clean-procedure.sql")
    verdict = _check(_routine(entry), target_user=TARGET_USER)
    dumped = verdict.model_dump(mode="json", exclude=_COMPUTED)
    assert RoutineVerdict.model_validate(dumped) == verdict
    assert verdict.routine_id == "shop.clean_procedure"


def test_an_unparseable_verdict_says_where_without_text() -> None:
    verdict = _check(_proc("CREATE PROCEDURE p() a: BEGIN SELECT 'x'; END b"))
    assert verdict.parse_failure is not None
    assert (verdict.parse_failure.stage, verdict.parse_failure.detail) == ("body", "end label")
    header = _check(_proc("CREATE PROCEDURE p SELECT 1"))
    assert header.parse_failure is not None and header.parse_failure.stage == "statement"
    kind = _check(_proc("CREATE FUNCTION p() RETURNS INT RETURN 1"))
    assert kind.parse_failure is not None and kind.parse_failure.stage == "kind"


def test_the_check_is_pure() -> None:
    entry = next(e for e in MANIFEST if e["file"] == "replication-control.sql")
    routine = _routine(entry)
    assert _check(routine, target_user=TARGET_USER) == _check(routine, target_user=TARGET_USER)


# --------------------------------------------------------------------------- why our own reader


def _sqlglot_reading(sql: str) -> str:
    """How sqlglot's MySQL dialect reads a whole CREATE: ``full``, ``partial`` (a tree
    with bare Command nodes in it), ``command`` or ``error``."""
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except SqlglotError:
        return "error"
    if tree is None or isinstance(tree, exp.Command):
        return "command"
    return "partial" if any(tree.find_all(exp.Command)) else "full"


#: The parsed corpus routines sqlglot cannot read by itself, and how it fails: IN/OUT
#: parameters, DECLARE, handlers, IF/CASE/LOOP, a function's RETURNS ... CHARSET, triggers,
#: events, CHANGE MASTER, PREPARE, KILL. It reads the others, plain BEGIN ... END bodies of
#: simple statements, in full.
SQLGLOT_CANNOT_READ = {
    "clean-procedure.sql": "error",
    "clean-function.sql": "error",
    "trigger.sql": "command",
    "event.sql": "error",
    "delimiter-dump-trigger.sql": "partial",
    "replication-control.sql": "error",
    "dynamic-sql.sql": "error",
    "dynamic-sql-literal.sql": "error",
    "dynamic-sql-variable.sql": "error",
    "kill.sql": "error",
    # The MySQL 5.7 routines (#1952): IN parameters, DECLARE, cursors, events, and the
    # syntax 8.0 removed (GROUP BY ... DESC, SQL_CACHE).
    "v57-clean.sql": "error",
    "v57-reserved-words.sql": "error",
    "v57-found-rows.sql": "error",
    "v57-group-by.sql": "error",
    "v57-removed-functions.sql": "error",
    "v57-sql-cache.sql": "command",
    "v57-removed-variables.sql": "partial",
    "v80-to-84.sql": "error",
}


def test_sqlglot_alone_cannot_read_most_routine_shapes_in_the_corpus() -> None:
    """The evidence for the body-structure reader."""
    cannot: dict[str, str] = {}
    for entry in MANIFEST:
        if not entry["parsed"]:
            continue
        (stmt,) = [
            s
            for s in split_client_statements((CORPUS / entry["file"]).read_text())
            if "CREATE" in s.upper()
        ]
        reading = _sqlglot_reading(stmt)
        if reading != "full":
            cannot[entry["file"]] = reading
    assert cannot == SQLGLOT_CANNOT_READ


def test_sqlglot_alone_does_not_tell_a_broken_body_from_a_valid_one() -> None:
    """Every structure fixture that MySQL refuses either errors in sqlglot just as the
    valid clean-procedure does, or sqlglot reads it in full: its answer is not a check."""
    for entry in MANIFEST:
        if entry["parsed"] or "quote" in entry["file"] or "two-creates" in entry["file"]:
            continue
        stmt = split_client_statements((CORPUS / entry["file"]).read_text())[-1]
        assert _sqlglot_reading(stmt) in {"error", "full"}, entry["file"]


@pytest.mark.passes_on_main
def test_every_dml_statement_of_the_parsed_corpus_reads_with_sqlglot() -> None:
    """The other half: once the reader has split a body, sqlglot reads every statement it
    gets in the corpus (only the dynamic-SQL routines' dependencies stay incomplete, and
    those of the 5.7 routines whose statements use syntax or functions 8.0 removed, #1952)."""
    incomplete = [
        e["file"] for e in MANIFEST if e["parsed"] and not e.get("dependencies_complete", True)
    ]
    assert incomplete == [
        "dynamic-sql.sql",
        "dynamic-sql-literal.sql",
        "dynamic-sql-variable.sql",
        "v57-group-by.sql",
        "v57-removed-functions.sql",
        "v57-sql-cache.sql",
        "v80-to-84.sql",
    ]


def test_a_delimiter_line_inside_a_string_does_not_split_the_body() -> None:
    text = "CREATE PROCEDURE p() BEGIN SELECT 'a\nDELIMITER $$\nb'; SELECT 2; END"
    (stmt,) = split_client_statements(text)
    assert stmt == text
    assert _check(_proc(text), target_user=TARGET_USER).parsed


# --------------------------------------------------------------------------- the facade


def test_the_checker_reads_sql_only_through_the_facade() -> None:
    """No sqlglot import outside the facade (#557); the checker uses its routine helpers."""
    source = (
        Path(__file__).resolve().parents[4] / "src/tools/routines/mysql_checks.py"
    ).read_text()
    assert "import sqlglot" not in source and "from sqlglot" not in source
    assert "routine_tokens" in source and "routine_statement_refs" in source


def test_the_facade_reads_a_statement_s_tables_and_qualified_calls() -> None:
    refs = routine_statement_refs(
        "SELECT billing.tax(1), f2(2) FROM a.b JOIN c, dual WHERE x IN (SELECT 1 FROM d)", "mysql"
    )
    assert refs.read
    assert sorted(refs.tables, key=str) == [("a", "b"), (None, "c"), (None, "d")]
    assert sorted(refs.calls, key=str) == [("billing", "tax"), (None, "f2")]


@pytest.mark.parametrize(
    "sql", ["SELECT " + "(" * 200 + "1" + ")" * 200, "SELECT FROM WHERE", "CALL p()", ""]
)
def test_the_facade_never_raises_on_a_statement(sql: str) -> None:
    assert not routine_statement_refs(sql, "mysql").read


def test_a_statement_the_facade_does_not_parse_reads_no_regex_guess() -> None:
    """Over the facade's size cap the statement falls back; the routine reader takes no
    regex guess from it, it reads as not read."""
    long = "SELECT a FROM t WHERE b IN (" + ", ".join(["1"] * 40_000) + ")"
    refs = routine_statement_refs(long, "mysql")
    assert not refs.read and refs.tables == ()


def test_the_facade_tokenizer_raises_its_own_error_without_the_text() -> None:
    with pytest.raises(RoutineTokenError) as exc:
        routine_tokens("SELECT 'SECRET_MARKER", "mysql")
    assert "SECRET_MARKER" not in str(exc.value)


# --------------------------------------------------------------------------- never raises


_BODY = "CREATE PROCEDURE p() BEGIN SELECT 1; {} END"


def test_the_reader_is_linear_in_runs_of_whitespace() -> None:
    """A long whitespace run reads in linear time (it was quadratic: 32,000 spaces took
    15 s), and about as fast as text of the same length, against a same-process
    reference (AGENTS.md, Before asking for review, item 8)."""
    n = 40_000
    assert_linear(lambda k: _BODY.format(" " * k), split_client_statements, sizes=(n, 8 * n))
    assert_linear(lambda k: _BODY.format("\n" + " " * k), split_client_statements, sizes=(n, 8 * n))
    spaces, plain = _BODY.format(" " * n), _BODY.format("x" * n)
    assert_cost_ratio(
        lambda: split_client_statements(spaces), lambda: split_client_statements(plain), 3.0
    )


def test_deep_nesting_is_unparseable_not_a_crash() -> None:
    deep = "BEGIN " * 500 + "SELECT 1; " + "END; " * 499 + "END"
    verdict = _check(_proc(f"CREATE PROCEDURE p() {deep}"))
    assert verdict.parse_failure is not None
    assert verdict.parse_failure.detail == "nesting too deep"
    ok = "BEGIN " * (MAX_NESTING - 1) + "SELECT 1; " + "END; " * (MAX_NESTING - 2) + "END"
    assert _check(_proc(f"CREATE PROCEDURE p() {ok}")).parsed


def test_deeply_nested_parentheses_leave_dependencies_incomplete() -> None:
    nested = "(" * 200 + "SELECT 1 FROM t" + ")" * 200
    verdict = _check(_proc(f"CREATE PROCEDURE p() BEGIN SET @x = {nested}; END"))
    assert verdict.parsed and not verdict.dependencies_complete


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("CREATE PROCEDURE p() BEGIN REPLACE; END", "incomplete statement"),
        ("CREATE PROCEDURE p() BEGIN SELECT; END", "incomplete statement"),
        ("CREATE PROCEDURE p() BEGIN CALL ``(); END", "empty name"),
        ("CREATE PROCEDURE p() CALL", "incomplete statement"),
        ("CREATE PROCEDURE ``() SELECT 1", "empty name"),
    ],
)
def test_odd_text_is_unparseable_never_an_exception(text: str, detail: str) -> None:
    verdict = _check(_proc(text))
    assert verdict.parse_failure is not None and verdict.parse_failure.detail == detail


def test_a_trigger_on_an_empty_table_name_is_unparseable() -> None:
    text = "CREATE TRIGGER t BEFORE INSERT ON `` FOR EACH ROW SET @a = 1"
    verdict = _check(_proc(text, kind="trigger"))
    assert verdict.parse_failure is not None and verdict.parse_failure.detail == "empty name"


_PIECES = [*"abcXYZ019 \n\t;:,.()'\"`\\@#-*/="]
_PIECES += ["BEGIN ", "END ", "IF ", "THEN ", "LOOP ", "DECLARE ", "CALL ", "SET ", "SELECT "]
_PIECES += ["FROM ", "PREPARE ", "HANDLER ", "FOR ", "/*!50003 ", "*/", "REPLACE ", "\nDELIMITER "]
_SQLISH = st.lists(st.sampled_from(_PIECES), max_size=60).map("".join)


@settings(max_examples=300, deadline=None)
@given(body=_SQLISH, mode=st.sampled_from(["", "NO_BACKSLASH_ESCAPES"]))
def test_any_text_returns_a_verdict(body: str, mode: str) -> None:
    for text in (body, f"CREATE PROCEDURE p() {body}", f"CREATE PROCEDURE p() BEGIN {body} END"):
        verdict = _check(_proc(text, sql_mode=mode), target_user=TARGET_USER)
        assert verdict.parsed in (True, False)


# --------------------------------------------------------------------------- inputs


def test_a_bad_input_never_repeats_itself_in_the_error() -> None:
    with pytest.raises(ValidationError) as wrong_type:
        MySqlRoutine(routine_id="s.p", kind="procedure", create_sql=["SECRET_MARKER"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError) as extra:
        MySqlRoutine(
            routine_id="s.p", kind="procedure", create_sql="x", body="SECRET_MARKER"  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError) as verdict:
        RoutineVerdict.model_validate(
            {
                "routine_id": "r",
                "kind": "SECRET_MARKER",
                "target_engine": "t",
                "target_line": None,
                "parse_failure": None,
                "checks": [],
            }
        )
    for exc in (wrong_type, extra, verdict):
        assert "SECRET_MARKER" not in str(exc.value)


@pytest.mark.parametrize("target_user", ["'", "`", "a@", "a b"])
def test_an_unreadable_target_user_is_a_value_error(target_user: str) -> None:
    with pytest.raises(ValueError, match="not a MySQL account name") as exc:
        _check(_proc("CREATE PROCEDURE p() SELECT 1"), target_user=target_user)
    assert type(exc.value) is ValueError


# --------------------------------------------------------------------------- dynamic SQL


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("PREPARE s FROM 'SELECT 1'; EXECUTE s;", []),
        ("PREPARE s FROM 'SHUTDOWN'; EXECUTE s;", ["mysql-routine-server-shutdown:SHUTDOWN"]),
        (
            "SET @q = CONCAT('SELECT * FROM ', 't'); PREPARE s FROM @q; EXECUTE s;",
            ["mysql-routine-dynamic-sql:dynamic SQL"],
        ),
        (
            "SET @q = 'SELECT 1'; SET @q = 'SET GLOBAL x = 1'; PREPARE s FROM @q; EXECUTE s;",
            ["mysql-routine-dynamic-sql:dynamic SQL", "mysql-routine-set-global:SET GLOBAL"],
        ),
        ("SET @q = 'SET GLOBAL x = 1'; SELECT @q;", []),
    ],
)
def test_dynamic_sql(body: str, expected: list[str]) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() BEGIN {body} END"), target_user=TARGET_USER)
    assert verdict.parsed and _ids(verdict) == expected
    assert verdict.dependencies_complete is ("PREPARE" not in body)
    if any(not e.startswith("mysql-routine-mode") for e in expected):
        assert verdict.status == "needs_attention"


# --------------------------------------------------------------------------- dependencies


def test_a_declare_default_s_dependencies_are_recorded() -> None:
    text = (
        "CREATE PROCEDURE p() BEGIN DECLARE v INT DEFAULT (SELECT next_id(1) FROM counters); "
        "DECLARE w INT; SELECT v; END"
    )
    verdict = _check(_proc(text))
    assert verdict.dependencies_complete
    assert _deps(verdict) == ["routine:.next_id", "table:.counters"]


def test_a_qualified_call_keeps_its_schema() -> None:
    text = "CREATE PROCEDURE shop.p() BEGIN SET @x = billing.tax(1), @y = tax(2); END"
    assert _deps(_check(_proc(text))) == ["routine:.tax", "routine:billing.tax"]


def test_a_qualified_call_to_a_builtin_name_is_a_routine() -> None:
    text = "CREATE PROCEDURE p() SELECT util.now()"
    assert _deps(_check(_proc(text))) == ["routine:util.now"]


# --------------------------------------------------------------------------- reading


def test_no_backslash_escapes_reads_a_backslash_as_a_character() -> None:
    text = "CREATE PROCEDURE p() BEGIN SELECT 'a\\' AS v; END"
    assert not _check(_proc(text)).parsed
    verdict = _check(_proc(text, sql_mode="STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES"))
    assert verdict.parsed and not verdict.failed_rules


def test_a_delimiter_column_in_a_show_create_body_is_not_a_delimiter_line() -> None:
    text = "CREATE PROCEDURE p()\nBEGIN\n  UPDATE t SET\ndelimiter = 1;\n  SELECT 2;\nEND"
    assert split_client_statements(text) == [text]
    assert _check(_proc(text)).parsed


def test_a_delimiter_line_before_the_create_is_still_honoured() -> None:
    text = "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END//\nDELIMITER ;"
    assert split_client_statements(text) == ["CREATE PROCEDURE p() BEGIN SELECT 1; END"]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ("b: BEGIN ITERATE b; END b", "iterate on a block label"),
        (
            "outer_block: BEGIN DECLARE CONTINUE HANDLER FOR SQLEXCEPTION LEAVE outer_block; "
            "SELECT 1; END outer_block",
            "unknown label",
        ),
        ("l: LOOP BEGIN ITERATE x; END; END LOOP l", "unknown label"),
    ],
)
def test_labels_follow_mysql_s_scoping(body: str, detail: str) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() {body}"))
    assert verdict.parse_failure is not None and verdict.parse_failure.detail == detail


@pytest.mark.parametrize(
    "body",
    [
        "l: LOOP b: BEGIN ITERATE l; END b; END LOOP l",
        "w: WHILE 1 DO ITERATE w; END WHILE w",
        "r: REPEAT ITERATE r; UNTIL 1 END REPEAT r",
        "BEGIN DECLARE CONTINUE HANDLER FOR SQLEXCEPTION h: BEGIN LEAVE h; END; SELECT 1; END",
    ],
)
def test_labels_mysql_accepts(body: str) -> None:
    assert _check(_proc(f"CREATE PROCEDURE p() {body}")).parsed


def test_the_unknown_mode_check_gives_the_same_answer_with_any_subset_of_rules() -> None:
    """A mode another catalog rule names is known even when that rule does not run."""
    unknown_only = [r for r in routine_check_rules() if r.id == "mysql-routine-mode-unknown"]
    removed = _proc("CREATE PROCEDURE p() SELECT 1", sql_mode="NO_AUTO_CREATE_USER")
    assert _ids(_check(removed, rules=unknown_only)) == []
    assert _ids(_check(removed)) == ["mysql-routine-mode-removed:NO_AUTO_CREATE_USER"]
    unknown = _proc("CREATE PROCEDURE p() SELECT 1", sql_mode="NOT_A_MODE")
    assert _ids(_check(unknown, rules=unknown_only)) == ["mysql-routine-mode-unknown:unknown mode"]


def test_an_unexpected_failure_while_reading_is_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(toks: object) -> None:
        raise TypeError("planted")

    monkeypatch.setattr("src.tools.routines.mysql_checks._header", boom)
    verdict = _check(_proc("CREATE PROCEDURE p() SELECT 1"))
    assert verdict.parse_failure is not None and verdict.parse_failure.detail == "unreadable"
    assert not verdict.dependencies_complete


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("KILL QUERY 7", ["mysql-routine-kill:KILL"]),
        ("SET @c = LOAD_FILE('/tmp/x')", ["mysql-routine-load-file:LOAD_FILE()"]),
        (
            "SET gtid_next = 'AUTOMATIC'",
            ["mysql-routine-restricted-session-variable:SET gtid_next"],
        ),
        ("SET @binlog_format = 'ROW'", []),
        ("SELECT 'KILL' AS k, load_file_count FROM t", []),
        ("SELECT id INTO OUTFILE S3 's3://b/k' FROM t", []),
        ("SELECT id FROM t INTO OUTFILE '/tmp/o'", ["mysql-routine-file-output:INTO OUTFILE"]),
    ],
)
def test_more_privileged_statements(statement: str, expected: list[str]) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() BEGIN {statement}; END"), target_user=TARGET_USER)
    assert verdict.parsed and _ids(verdict) == expected


# --------------------------------------------------------------------------- review round 3


def _prepares(k: int) -> str:
    """k assignments to one user variable, and k PREPAREs from it."""
    sets = "".join(f"SET @v = 'SELECT {i}'; " for i in range(k))
    prepares = "".join(f"PREPARE s{i} FROM @v; " for i in range(k))
    return f"CREATE PROCEDURE p() BEGIN {sets}{prepares} END"


def test_many_prepares_from_variables_check_in_linear_time() -> None:
    """Each prepared variable's literals are read once, so k assignments times k
    PREPAREs cost linear time (400 of each took 4.5 s), against a same-process reference:
    the same number of plain statements."""

    def reference(k: int) -> str:
        return "CREATE PROCEDURE p() BEGIN " + "SELECT 1; " * (2 * k) + "END"

    assert_linear(lambda k: _prepares(k), lambda text: _check(_proc(text)), sizes=(100, 800))
    assert_cost_ratio(
        lambda: _check(_proc(_prepares(400))), lambda: _check(_proc(reference(400))), 4.0
    )


@pytest.mark.parametrize(
    "body",
    [
        "SELECT 1; REPLACE INTO t SELECT LOAD_FILE('/x');",
        "IF 1 THEN REPLACE INTO t SELECT LOAD_FILE('/x'); END IF;",
        "SELECT 1; CALL q(LOAD_FILE('/x'));",
    ],
)
def test_a_command_s_words_are_checked_wherever_it_stands(body: str) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() BEGIN {body} END"), target_user=TARGET_USER)
    assert _ids(verdict) == ["mysql-routine-load-file:LOAD_FILE()"]


def test_a_command_s_words_are_checked_right_after_begin() -> None:
    verdict = _check(
        _proc("CREATE PROCEDURE p() BEGIN REPLACE INTO t SELECT 1 INTO OUTFILE '/o'; END")
    )
    assert _ids(verdict) == ["mysql-routine-file-output:INTO OUTFILE"]


@pytest.mark.parametrize(
    "body",
    [
        "BEGIN CALL q((SELECT a FROM t2), f2(1)); END",
        "BEGIN SELECT 1; CALL q((SELECT a FROM t2), f2(1)); END",
        "BEGIN IF 1 THEN CALL q((SELECT a FROM t2), f2(1)); END IF; END",
        "CALL q((SELECT a FROM t2), f2(1))",
    ],
)
def test_a_call_s_arguments_are_read_for_dependencies(body: str) -> None:
    verdict = _check(_proc(f"CREATE PROCEDURE p() {body}"))
    assert verdict.dependencies_complete
    assert _deps(verdict) == ["routine:.f2", "routine:.q", "table:.t2"]


def test_a_call_with_unreadable_arguments_leaves_dependencies_incomplete() -> None:
    verdict = _check(_proc("CREATE PROCEDURE p() BEGIN CALL q(SELECT FROM WHERE); END"))
    assert "routine:.q" in _deps(verdict) and not verdict.dependencies_complete


@pytest.mark.parametrize(
    ("definer", "target", "named"),
    [
        ("app@10.0.0.%", "admin", True),
        ("app@%.example.com", "admin", True),
        ("my-app@%", "admin", True),
        ("app@my-host", "admin", True),
        ("admin@10.0.0.%", "admin@10.0.0.%", False),
        ("admin@10.0.0.%", "admin", False),
        ("`admin`@`10.0.0.%`", "'admin'@'10.0.0.%'", False),
        ("'app'@'%'", "admin", True),
        ('"admin"@"%"', "`admin`@`%`", False),
        ("admin@%", "admin@10.0.0.%", True),
        # An unquoted name splits at its last @: the user part may hold one.
        ("ops@corp@%", "`ops@corp`@`%`", False),
    ],
)
def test_a_collected_definer_reads_in_every_account_form(
    definer: str, target: str, named: bool
) -> None:
    verdict = _check(_proc("CREATE PROCEDURE p() SELECT 1", definer=definer), target_user=target)
    assert (_ids(verdict) == ["mysql-routine-definer:named definer"]) is named
    assert verdict.status == ("needs_attention" if named else "as_is")


@pytest.mark.parametrize("definer", ["a b@c", "@h", "app@", "'app'@", "'app"])
def test_an_unreadable_collected_definer_needs_review(definer: str) -> None:
    verdict = _check(
        _proc("CREATE PROCEDURE p() SELECT 1", definer=definer), target_user=TARGET_USER
    )
    assert _ids(verdict) == ["mysql-routine-definer:unreadable definer"]
    assert verdict.status == "needs_attention"


def test_an_unreadable_collected_definer_needs_review_even_for_invoker() -> None:
    routine = _proc("CREATE PROCEDURE p() SELECT 1", definer="a b@c", security_type="INVOKER")
    assert _ids(_check(routine)) == ["mysql-routine-definer:unreadable definer"]


@pytest.mark.parametrize("target", ["admin@10.0.0.%", "`admin`@`%`", "'admin'@'%.example.com'"])
def test_a_target_user_in_any_account_form_is_accepted(target: str) -> None:
    assert _check(_proc("CREATE PROCEDURE p() SELECT 1"), target_user=target).parsed


def test_an_unknown_dialect_raises_the_facade_s_own_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dialect lookup is inside the tokenizer's error handling too."""
    monkeypatch.setattr("src.tools.sql.facade.sqlglot_dialect", lambda engine: "no_such_dialect")
    with pytest.raises(RoutineTokenError):
        routine_tokens("SELECT 1", "mysql")
