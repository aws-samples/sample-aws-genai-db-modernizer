# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine rewrite rules are data (#1937): strict files, one place, rendered."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.render_catalog import render_md, render_routine_rewrites_md
from src.shared.catalog import CatalogError, engines
from src.shared.catalog.patterns import patterns
from src.shared.catalog.routine_checks import routine_check_rules
from src.shared.catalog.routine_rewrites import (
    ROUTINE_REWRITES,
    ROUTINE_REWRITES_DIR,
    load_routine_rewrite_rules,
    routine_rewrite_rules,
)
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
from src.tools.routines.postgresql_checks import PostgresRoutine, check_postgresql_routine

GOOD = """
id = "{id}"
feature = "A change"
source_engines = {engines}
targets = {targets}
kinds = {kinds}
reason = "It is safe."
docs = "PostgreSQL documentation, CREATE FUNCTION"

[match]
rewrite = "{rewrite}"
{extra}
"""


def _write(
    directory: Path,
    rule_id: str = "a-rule",
    rewrite: str = "owner",
    engines: str = '["postgresql"]',
    targets: str = '["aurora_postgresql"]',
    kinds: str = '["function"]',
    extra: str = "",
) -> Path:
    path = directory / f"{rule_id}.toml"
    path.write_text(
        GOOD.format(
            id=rule_id, rewrite=rewrite, engines=engines, targets=targets, kinds=kinds, extra=extra
        ),
        encoding="utf-8",
    )
    return path


def test_the_shipped_rules_cover_each_rewrite_once() -> None:
    rules = routine_rewrite_rules()
    assert [r.id for r in rules] == sorted(p.stem for p in ROUTINE_REWRITES_DIR.glob("*.toml"))
    assert sorted(r.match.rewrite for r in rules) == sorted(ROUTINE_REWRITES)
    checks = {r.id for r in routine_check_rules()}
    for rule in rules:
        assert set(rule.match.resolves) <= checks
        assert rule.reason.endswith(".") and "\n" not in rule.reason
    (definer,) = [r for r in rules if r.match.rewrite == "definer"]
    assert definer.match.resolves == ("mysql-routine-definer",)


def test_a_valid_file_loads(tmp_path: Path) -> None:
    _write(tmp_path)
    (rule,) = load_routine_rewrite_rules(tmp_path)
    assert (rule.id, rule.match.rewrite, rule.kinds) == ("a-rule", "owner", ("function",))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"rewrite": "definer"}, "applies only to"),
        (
            {"rewrite": "owner", "engines": '["mysql"]', "targets": '["aurora_mysql"]'},
            "applies only to",
        ),
        ({"kinds": '["trigger"]'}, "only to functions and procedures"),
        ({"rewrite": "search_path", "kinds": '["function", "trigger"]'}, "only to functions"),
        ({"rewrite": "rename"}, "rewrite"),
        ({"targets": '["dynamodb"]'}, "unknown targets"),
        ({"extra": 'resolves = ["no-such-rule"]'}, "no check rule"),
        ({"extra": 'resolves = ["mysql-routine-definer"]'}, "no check rule"),
        ({"extra": 'colour = "red"'}, "colour"),
    ],
)
def test_a_bad_file_fails_at_load_time(
    tmp_path: Path, fields: dict[str, str], message: str
) -> None:
    _write(tmp_path, **fields)
    with pytest.raises(CatalogError, match=message):
        load_routine_rewrite_rules(tmp_path)


def test_the_id_must_match_the_file_name_and_the_directory_must_hold_rules(tmp_path: Path) -> None:
    path = _write(tmp_path)
    path.rename(tmp_path / "other.toml")
    with pytest.raises(CatalogError, match="does not match"):
        load_routine_rewrite_rules(tmp_path)
    with pytest.raises(CatalogError, match="no routine rewrite rule files"):
        load_routine_rewrite_rules(tmp_path / "empty")


def test_the_dependency_check_rules_are_not_the_checkers() -> None:
    """Each engine has a dependency check rule; it needs the step's moved objects, so the
    checkers leave it to the dependency module (#1937)."""
    rules = [r for r in routine_check_rules() if r.match.check == "dependencies"]
    assert sorted(r.id for r in rules) == [
        "mysql-routine-dependency-not-moved",
        "postgresql-routine-dependency-not-moved",
    ]
    mysql = MySqlRoutine(
        routine_id="s.p", kind="procedure", create_sql="CREATE PROCEDURE p() BEGIN END"
    )
    verdict = check_mysql_routine(mysql, target_engine="aurora_mysql", target_user="admin")
    assert all(c.check != "dependencies" for c in verdict.checks)
    pg = PostgresRoutine(
        routine_id="public.f()",
        kind="function",
        create_sql="CREATE FUNCTION public.f() RETURNS int LANGUAGE sql AS $$ select 1 $$",
        language="sql",
        owner_is_superuser=False,
    )
    verdict = check_postgresql_routine(pg, target_engine="aurora_postgresql", target_line="16")
    assert all(c.check != "dependencies" for c in verdict.checks)


def test_the_rule_catalog_page_lists_every_rewrite_rule() -> None:
    page = "\n".join(render_routine_rewrites_md(routine_rewrite_rules(), {}))
    for rule in routine_rewrite_rules():
        assert f"### {rule.id}" in page and f"[`{rule.id}`](#{rule.id})" in page
    assert "[`mysql-routine-definer`](#mysql-routine-definer)" in page
    assert "## Routine rewrites" in render_md(engines(), patterns())
