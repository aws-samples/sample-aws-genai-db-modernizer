# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine check rules are data (#1935): strict files, one place, rendered."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.render_catalog import render_md, render_routine_checks_md
from src.shared.catalog import CatalogError, engines
from src.shared.catalog.gaps import GAP_IMPACTS, aurora_extension_lines
from src.shared.catalog.patterns import patterns
from src.shared.catalog.routine_checks import (
    ROUTINE_KINDS,
    load_routine_check_rules,
    routine_check_rules,
)

GOOD = """
id = "{id}"
feature = "A feature"
source_engines = ["postgresql"]
targets = ["aurora_postgresql"]
kinds = ["function"]
impact = "needs_rewrite"
reason = "It does not load."
fix_hint = "Rewrite it."
docs = "PostgreSQL documentation, CREATE FUNCTION"

[match]
{match}
"""


def _write(
    directory: Path, rule_id: str = "a-rule", match: str = "parses = true", **lines: str
) -> Path:
    text = GOOD.format(id=rule_id, match=match)
    for key, value in lines.items():
        text = "\n".join(
            f"{key} = {value}" if line.startswith(f"{key} = ") else line
            for line in text.splitlines()
        )
    path = directory / f"{rule_id}.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_shipped_rules_cover_each_check_once_for_postgresql() -> None:
    # The catalog is shared with MySQL's routine checks (#1936): only PostgreSQL's here.
    rules = [r for r in routine_check_rules() if r.source_engines == ("postgresql",)]
    assert sorted(r.match.check for r in rules) == ["extensions", "language", "ownership", "parse"]
    for rule in rules:
        assert rule.source_engines == ("postgresql",)
        assert rule.targets == ("aurora_postgresql",)
        assert rule.impact in GAP_IMPACTS
        assert set(rule.kinds) <= set(ROUTINE_KINDS)
        for sentence in (rule.reason, rule.fix_hint):
            assert sentence.endswith(".") and "\n" not in sentence


def test_the_language_rule_passes_only_plpgsql_sql_and_offered_trusted_languages() -> None:
    (rule,) = [r for r in routine_check_rules() if r.match.check == "language"]
    assert rule.match.languages == ("plpgsql", "sql")
    untrusted = {"plpythonu", "plpython2u", "plpython3u", "plperlu", "pltclu", "c", "internal"}
    assert not untrusted & set(rule.match.trusted_extension_languages)
    # Every trusted language it names is one Aurora PostgreSQL offers on some line.
    offered = set().union(
        *(set(e.extensions) for e in aurora_extension_lines()["aurora_postgresql"].lines.values())
    )
    assert set(rule.match.trusted_extension_languages) <= offered


def test_a_valid_file_loads(tmp_path: Path) -> None:
    _write(tmp_path)
    (rule,) = load_routine_check_rules(tmp_path)
    assert (rule.id, rule.match.check, rule.kinds) == ("a-rule", "parse", ("function",))


@pytest.mark.parametrize(
    ("match", "message"),
    [
        ("", "exactly one of"),
        ("parses = false", "exactly one of"),
        ('parses = true\nlanguages = ["sql"]', "exactly one of"),
        (
            'extensions = "offered_on_target_line"\nsecurity_definer_owned_by_superuser = true',
            "exactly one of",
        ),
        ('extensions = "anywhere"', "extensions"),
        ('trusted_extension_languages = ["plv8"]\nparses = true', "goes with languages"),
        ('languages = ["PLPGSQL"]', "lower case"),
        ('languages = ["sql", "sql"]', "listed twice"),
        ('languages = ["sql"]\ntrusted_extension_languages = ["sql"]', "listed twice"),
        ("parses = true\nsurprise = 1", "surprise"),
    ],
)
def test_a_bad_match_fails_to_load(tmp_path: Path, match: str, message: str) -> None:
    _write(tmp_path, match=match)
    with pytest.raises(CatalogError, match=message):
        load_routine_check_rules(tmp_path)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("kinds", '["view"]', "kinds"),
        ("kinds", "[]", "kinds"),
        ("kinds", '["function", "function"]', "twice"),
        ("source_engines", '["informix"]', "unknown source engines"),
        ("targets", '["dynamodb"]', "unknown targets"),
        ("impact", '"fatal"', "impact"),
        ("reason", '"Two\\nlines."', "reason"),
        ("fix_hint", '""', "fix_hint"),
    ],
)
def test_a_bad_field_fails_to_load(tmp_path: Path, key: str, value: str, message: str) -> None:
    _write(tmp_path, **{key: value})
    with pytest.raises(CatalogError, match=message):
        load_routine_check_rules(tmp_path)


def test_the_id_must_match_the_file_name(tmp_path: Path) -> None:
    _write(tmp_path).rename(tmp_path / "other.toml")
    with pytest.raises(CatalogError, match="does not match the file name"):
        load_routine_check_rules(tmp_path)


@pytest.mark.parametrize(
    "match",
    [
        'extensions = "offered_on_target_line"',
        'languages = ["sql"]\ntrusted_extension_languages = ["plv8"]',
    ],
)
def test_a_rule_that_reads_the_extension_list_needs_one(tmp_path: Path, match: str) -> None:
    _write(tmp_path, match=match)
    load_routine_check_rules(tmp_path)
    with pytest.raises(CatalogError, match="needs one; aurora_postgresql has none"):
        load_routine_check_rules(tmp_path, extension_lines={})


def test_an_empty_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="no routine check rule files"):
        load_routine_check_rules(tmp_path)


def test_the_rule_catalog_page_lists_every_routine_rule_with_its_fix() -> None:
    page = "\n".join(render_routine_checks_md(routine_check_rules(), {}))
    for rule in routine_check_rules():
        assert f"### {rule.id}" in page
        assert f"[`{rule.id}`](#{rule.id})" in page
        assert "Fix: " in page
    assert "## Routine checks" in render_md(engines(), patterns())


@pytest.mark.parametrize("kind", ["trigger", "event"])
@pytest.mark.parametrize(
    "match", ['languages = ["sql"]', "security_definer_owned_by_superuser = true"]
)
def test_a_language_or_ownership_rule_cannot_apply_to_a_trigger(
    tmp_path: Path, match: str, kind: str
) -> None:
    """A trigger (or a MySQL event) has no language and no owner of its own: such a rule
    would always fail with an empty finding, or always pass (review of #1961)."""
    _write(tmp_path, match=match, kinds=f'["function", "{kind}"]')
    with pytest.raises(CatalogError, match=f"cannot apply to a {kind}"):
        load_routine_check_rules(tmp_path)
    _write(tmp_path, match=match, kinds='["function", "procedure"]')
    load_routine_check_rules(tmp_path)


def test_the_language_fix_hint_names_only_languages_the_rule_passes() -> None:
    """pg_tle builds extensions in trusted languages, not new languages, so the hint does
    not offer it (review of #1961)."""
    (rule,) = [r for r in routine_check_rules() if r.match.check == "language"]
    assert "Trusted Language Extensions" not in rule.fix_hint
    for name in ("PL/pgSQL", "SQL", "PL/Perl", "PL/Tcl", "PL/v8"):
        assert name in rule.fix_hint


_MYSQL_MATCHES = {
    "parses": "parses = true",
    "declare": "declare_order = true",
    "handler": "valid_handlers = true",
    "definer": 'definer_not_target_user = true\nalways_definer_kinds = ["trigger", "event"]',
    "starts": 'statement_starts = ["SHUTDOWN"]',
    "contains": 'statement_contains = ["INTO OUTFILE"]',
    "scopes": 'set_scopes = ["GLOBAL"]',
    "variables": 'set_variables = ["sql_log_bin"]',
    "modes": 'sql_modes = ["NO_AUTO_CREATE_USER"]',
    "unknown": 'unknown_sql_modes = true\nknown_sql_modes = ["ANSI_QUOTES"]',
}


def test_the_page_renders_the_mysql_checks_the_shared_catalog_holds(tmp_path: Path) -> None:
    """The catalog is shared with MySQL's routine checks (#1936): the page words every
    MySQL check, and only a PostgreSQL rule's row carries this PR's claim marker."""
    for name, match in _MYSQL_MATCHES.items():
        _write(
            tmp_path,
            rule_id=f"mysql-{name}",
            match=match,
            source_engines='["mysql"]',
            targets='["aurora_mysql"]',
            kinds='["procedure", "function", "trigger", "event"]',
        )
    rules = load_routine_check_rules(tmp_path)
    assert {r.match.check for r in rules} == {
        "parse",
        "declare_order",
        "handler",
        "definer",
        "statement",
        "sql_mode",
    }
    page = "\n".join(render_routine_checks_md(rules + routine_check_rules(), {}))
    rows = {line.split("`")[1]: line for line in page.splitlines() if line.startswith("| [`")}
    for name in _MYSQL_MATCHES:
        row = rows[f"mysql-{name}"]
        assert "claim:" not in row and row.count("|") == 7
    assert "always a trigger or event" in rows["mysql-definer"]
    assert "`SHUTDOWN`" in rows["mysql-starts"] and "`sql_log_bin`" in rows["mysql-variables"]
    assert "EVENT statement" in rows["mysql-parses"]
    for rule in routine_check_rules():
        assert "claim:" in rows[rule.id]
