"""The relational-move gap rules are data (#471): strict files, one place, rendered."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.render_catalog import render_gaps_md
from src.contracts.collector_output import DatabaseEngine
from src.shared.catalog import CatalogError
from src.shared.catalog.gaps import (
    AURORA_TARGETS,
    GAP_IMPACT_LABELS,
    GAP_IMPACTS,
    SOURCE_ENGINES,
    aurora_version_lines,
    gap_rules,
    load_aurora_versions,
    load_gap_rules,
    parse_version_line,
)

GOOD = """
id = "{id}"
feature = "A feature"
source_engines = ["mysql"]
targets = ["aurora_mysql"]
impact = "needs_rewrite"
reason = "Rewrite it."
docs = "MySQL 8.0 Reference Manual"

[match]
{match}
"""


def _write(
    directory: Path, rule_id: str = "a-rule", match: str = 'calls = ["X"]', **kw: str
) -> Path:
    text = GOOD.format(id=rule_id, match=match)
    for key, value in kw.items():
        text = text.replace(next(ln for ln in text.splitlines() if ln.startswith(key)), value)
    path = directory / f"{kw.get('file', rule_id)}.toml"
    path.write_text(text)
    return path


def test_the_catalog_loads_and_covers_every_source_engine() -> None:
    rules = gap_rules()
    assert rules == tuple(sorted(rules, key=lambda r: r.id))
    assert {e for r in rules for e in r.source_engines} == SOURCE_ENGINES
    assert {t for r in rules for t in r.targets} == AURORA_TARGETS
    assert {r.impact for r in rules} == set(GAP_IMPACTS)


def test_the_source_engines_are_the_collectors() -> None:
    assert SOURCE_ENGINES == {e.value for e in DatabaseEngine}


def test_every_rule_cites_its_documentation_and_reads_as_one_sentence() -> None:
    for rule in gap_rules():
        assert rule.docs.strip(), rule.id
        assert rule.reason.endswith("."), rule.id
        assert "\n" not in rule.reason


def test_every_homogeneous_pair_has_version_rules() -> None:
    versions = aurora_version_lines()
    for target, entry in versions.items():
        for source in entry.source_engines:
            checks = {
                r.match.version
                for r in gap_rules()
                if r.match.version and source in r.source_engines and target in r.targets
            }
            assert checks == {"older_than_target", "newer_than_target", "unknown"}, (source, target)


def test_a_valid_file_loads(tmp_path: Path) -> None:
    _write(tmp_path)
    (rule,) = load_gap_rules(tmp_path, versions=aurora_version_lines())
    assert rule.id == "a-rule" and rule.match.calls == ("X",)


@pytest.mark.parametrize(
    ("match", "message"),
    [
        ("", "needs at least one matcher"),
        ('calls = ["X"]\nsurprise = 1', "surprise"),
        ('always = true\ncalls = ["X"]', "no other matcher"),
        ('version = "older_than_target"\nwords = ["X"]', "no other matcher"),
        ('syntax = ["CONNECT"]', "one token"),
        ('version = "sideways"', "version"),
    ],
)
def test_a_bad_match_fails_to_load(tmp_path: Path, match: str, message: str) -> None:
    _write(tmp_path, match=match)
    with pytest.raises(CatalogError, match=message):
        load_gap_rules(tmp_path, versions=aurora_version_lines())


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('impact = "catastrophic"', "impact"),
        ('source_engines = ["sybase"]', "unknown source engines"),
        ('targets = ["dynamodb"]', "unknown targets"),
        ('reason = ""', "reason"),
        ('docs = ""', "docs"),
    ],
)
def test_a_bad_field_fails_to_load(tmp_path: Path, line: str, message: str) -> None:
    key = line.split(" ", 1)[0]
    _write(tmp_path, **{key: line})
    with pytest.raises(CatalogError, match=message):
        load_gap_rules(tmp_path, versions=aurora_version_lines())


def test_the_id_must_match_the_file_name(tmp_path: Path) -> None:
    (tmp_path / "other.toml").write_text(GOOD.format(id="a-rule", match='calls = ["X"]'))
    with pytest.raises(CatalogError, match="does not match the file name"):
        load_gap_rules(tmp_path, versions=aurora_version_lines())


def test_a_version_rule_needs_a_homogeneous_pair(tmp_path: Path) -> None:
    text = GOOD.format(id="v", match='version = "older_than_target"').replace(
        'source_engines = ["mysql"]', 'source_engines = ["sqlserver"]'
    )
    (tmp_path / "v.toml").write_text(text)
    with pytest.raises(CatalogError, match="homogeneous pair"):
        load_gap_rules(tmp_path, versions=aurora_version_lines())


def test_an_empty_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="no gap rule files"):
        load_gap_rules(tmp_path, versions=aurora_version_lines())


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '[engines.dynamodb]\nsource_engines = ["mysql"]\nlines = ["8.0"]\ndocs = "d"',
            "unknown Aurora",
        ),
        (
            '[engines.aurora_mysql]\nsource_engines = ["mysql"]\nlines = ["eight"]\ndocs = "d"',
            "version line",
        ),
        ('[engines.aurora_mysql]\nsource_engines = ["mysql"]\nlines = []\ndocs = "d"', "lines"),
    ],
)
def test_bad_version_lines_fail_to_load(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "aurora_versions.toml"
    path.write_text(body)
    with pytest.raises(CatalogError, match=message):
        load_aurora_versions(path)


@pytest.mark.parametrize(
    ("engine", "version", "line"),
    [
        ("mysql", "8.0.45", (8, 0)),
        ("mysql", "5.7.44-log", (5, 7)),
        ("mariadb", "10.6.12-MariaDB", (10, 6)),
        ("postgresql", "PostgreSQL 16.10 on x86_64-pc-linux-gnu, compiled by gcc", (16,)),
        ("postgresql", "17", (17,)),
        ("postgresql", "9.6.24", (9, 6)),
        ("mysql", "8", None),
        ("mysql", "unknown", None),
        ("postgresql", "", None),
        ("postgresql", None, None),
    ],
)
def test_version_lines(engine: str, version: str | None, line: tuple[int, ...] | None) -> None:
    assert parse_version_line(engine, version) == line


def test_the_rule_catalog_page_lists_every_rule() -> None:
    page = "\n".join(render_gaps_md(gap_rules(), aurora_version_lines(), {}))
    for rule in gap_rules():
        assert f"### {rule.id}" in page
        assert f"[`{rule.id}`](#{rule.id})" in page


def test_the_results_page_names_each_impact_as_the_reports_do() -> None:
    """#1120: the UI's gap list labels each impact with the words the engineering
    and executive reports use (``GAP_IMPACT_LABELS``), one string per impact."""
    en = json.loads(
        (Path(__file__).resolve().parents[3] / "src/ui/src/locales/en.json").read_text()
    )
    prefix = "analysis-results.waves.gap-impact."
    shown = {k[len(prefix) :]: v for k, v in en.items() if k.startswith(prefix)}
    assert shown == dict(GAP_IMPACT_LABELS)
    assert set(GAP_IMPACT_LABELS) == set(GAP_IMPACTS)
