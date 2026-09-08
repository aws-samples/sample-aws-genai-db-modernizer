"""The committed report template must match what ExportReport.js says it is.

``src/ui/src/utils/ExportReport.js`` is the single source of truth for the
interactive report's look. ``scripts/sync_report_template.py`` lifts its static
parts into ``src/atx_orchestrator/runtime/templates/``. Without this test a UI
change would silently leave the ATX report rendering last month's layout, and the
"matches the WebApp export exactly" property would be a claim rather than an
invariant.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "sync_report_template.py"


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_report_template", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sync():
    return _load_sync_module()


def test_committed_templates_match_export_report_js(sync):
    for path, expected in sync.build().items():
        assert path.exists(), (
            f"{path.relative_to(REPO_ROOT)} is missing. "
            "Run: uv run python scripts/sync_report_template.py"
        )
        assert path.read_text(encoding="utf-8") == expected, (
            f"{path.relative_to(REPO_ROOT)} is out of date with ExportReport.js. "
            "Run: uv run python scripts/sync_report_template.py"
        )


def test_css_is_the_single_source_of_the_palette(sync):
    """Every colour the report uses has to be reachable from the CSS :root block.

    The badges pick their colour with ``[data-engine]`` rules and the charts read the
    same custom properties back at runtime, so a re-theme is one edit here. A hex
    literal reappearing in the JS or the Python renderer would silently split that.
    """
    css = sync.extract_css(sync._read_source())
    for token in ("--indigo-900", "--indigo-400", "--indigo-100"):
        assert token in css, f"palette ramp is missing {token}"
    for engine in ("dynamodb", "documentdb", "elasticache", "opensearch", "aurora_postgresql"):
        assert f"--engine-{engine}:" in css, f"no palette token for {engine}"
        assert f'.badge[data-engine="{engine}"]' in css, f"no badge rule for {engine}"
    assert "--engine-fallback:" in css and "--chart-neutral:" in css
    assert "Amazon Ember" in css
    assert ".stat-card" in css and ".modal-overlay" in css and ".tab-button" in css


def test_engine_labels_match_the_python_renderer(sync):
    """Display names are the one thing both sides still declare independently.

    The colours moved into the CSS, but ENGINE_LABELS is injected as a JS const by
    whichever side renders -- the React exporter or analysis_report.py. A label added
    to one and not the other would show the raw engine key ("aurora_postgresql") in
    half the report, so pin them together here.
    """
    from src.atx_orchestrator.runtime import analysis_report as ar

    source = "\n".join(sync._read_source())
    block = re.search(r"const ENGINE_LABELS = \{(.*?)\};", source, re.S)
    assert block, "ExportReport.js no longer declares ENGINE_LABELS"
    js_labels = dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))

    assert js_labels == ar.ENGINE_LABELS
    for engine in (
        "dynamodb",
        "documentdb",
        "opensearch",
        "elasticache",
        "aurora_postgresql",
        "aurora_mysql",
    ):
        assert engine in js_labels, f"no display name for {engine}"


def test_badges_share_the_container_corner_radius(sync):
    css = sync.extract_css(sync._read_source())
    assert "--radius-container:" in css
    assert css.count("border-radius: var(--radius-container)") >= 3


def test_script_reads_its_colours_from_the_palette(sync):
    body = sync.extract_script_body(sync._read_source())
    assert "function paletteColor" in body
    assert "engineColor(" in body and "opColor(" in body
    assert "#0972d3" not in body and "#5f6b7a" not in body


def test_script_body_has_every_client_function(sync):
    body = sync.extract_script_body(sync._read_source())
    for fn in (
        "extractPatterns",
        "buildSourceTableGroups",
        "getOpCategory",
        "filterPatterns",
        "buildTable",
        "buildSourceTableTable",
        "createCharts",
        "buildCostBreakdown",
        "buildQueryFlow",
        "buildTradeoffs",
        "showPatternDetails",
        "showSourceTableDetails",
        "showQueryJourney",
        "switchBrowseMode",
        "clearAllFilters",
    ):
        assert f"function {fn}" in body or f"{fn} =" in body, f"missing {fn}"
    assert "DOMContentLoaded" in body


def test_script_body_excludes_the_dynamic_declarations(sync):
    """DATA and the palette consts are emitted by the Python renderer, not the template."""
    body = sync.extract_script_body(sync._read_source())
    assert "const DATA = " not in body
    assert "const ENGINE_COLORS = " not in body
    assert "<script>" not in body and "</script>" not in body


def test_shell_placeholders_are_all_present(sync):
    shell = sync.extract_shell(sync._read_source())
    for placeholder in (
        "__CSS__",
        "__SCRIPT__",
        "__CHART_JS__",
        "__META__",
        "__BANNER__",
        "__PROVENANCE__",
        "__TITLE__",
        "__JOB_ID__",
        "__EXPORT_DATE__",
        "__DATABASE_NAME__",
        "__SUMMARY__",
        "__ENGINE_BADGES__",
        "__PROJECTED_COST__",
        "__TOTAL_PATTERNS__",
    ):
        assert placeholder in shell, f"missing {placeholder}"
    assert "${" not in shell, "an unmapped JS interpolation survived into the template"


def test_shell_keeps_every_report_section(sync):
    shell = sync.extract_shell(sync._read_source())
    for header in (
        "Database Modernization Analysis Report",
        "Executive Summary",
        "Cost Breakdown",
        "Query Flow",
        "Access Pattern Explorer",
        "Trade-offs and Design Decisions",
        "Principal Engineer Notes",
    ):
        assert header in shell, f"missing section: {header}"


def test_shell_keeps_the_aws_transform_attribution(sync):
    """The brand mark and its legend are the only provenance a shared HTML file carries.

    The report leaves the WebApp as a standalone file, so nothing else tells a reader who
    produced it. The glyph is filled with ``var(--color-brand)`` because an SVG
    presentation attribute cannot take a custom property -- a plain ``fill=`` would
    reintroduce the hex literal the palette test forbids everywhere else.
    """
    shell = sync.extract_shell(sync._read_source())
    assert "Generated by AWS Transform" in shell
    assert 'class="report-brand"' in shell
    assert "fill: var(--color-brand)" in shell
    assert "#01a88d" not in shell.lower(), "brand colour belongs in the CSS palette"
    assert "--color-brand:" in sync.extract_css(sync._read_source())


def test_unescape_handles_the_escapes_export_report_uses(sync):
    f = sync._unescape_js_single_quoted
    assert f(r"\'") == "'"
    assert f(r"\\") == "\\"
    assert f(r"a\nb") == "a\nb"
    assert f(r"A") == "A"
    assert f(r"\x41") == "A"


def test_check_mode_passes_on_committed_state(sync, monkeypatch):
    monkeypatch.setattr("sys.argv", ["sync_report_template.py", "--check"])
    assert sync.main() == 0
