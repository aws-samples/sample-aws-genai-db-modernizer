"""Unit tests for the customer-facing report renderers (Increment 2).

The renderers (``render_decision_report_html``, ``render_engineering_report_md``)
and their helpers are pure functions of the synthesis ``report.json``. They are
exercised against a committed fixture — the real report from the deployed
``v2-e2e-09`` run — so the assertions check genuine reconciliation
($814.12 / 100% workload / 92 migrated tables) and the executive/engineering
content split, not a hand-built stub.

The ATX orchestrator's publish wiring (``_publish_synthesis_deliverables`` /
``run_synthesis_via_a2a``) is exercised separately in
``tests/unit/atx_orchestrator/test_synthesis_deliverables.py`` — this module
covers only the renderers themselves.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.report import renderers

FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


# =============================================================================
# Decision Report (HTML, executive)


class TestDecisionReport:
    def test_renders_html_document(self, report: dict) -> None:
        h = renderers.render_decision_report_html(report)
        low = h.lstrip().lower()
        assert low.startswith("<!doctype html") or low.startswith("<html")
        assert len(h) > 3000

    def test_offline_no_external_loads(self, report: dict) -> None:
        """Self-contained: no CDN/CSS/JS/font loads. The only URI allowed is the
        inline SVG xmlns."""
        h = renderers.render_decision_report_html(report)
        assert "https://" not in h
        assert set(re.findall(r"http://[^\s\"'>]+", h)) <= {"http://www.w3.org/2000/svg"}
        assert "<script" not in h.lower()
        assert "<link" not in h.lower()
        assert "cdn" not in h.lower()

    def test_five_engine_reconciliation(self, report: dict) -> None:
        h = renderers.render_decision_report_html(report)
        for eng in ("aurora", "elasticache", "documentdb", "dynamodb", "opensearch"):
            assert eng in h.lower()
        assert "814.12" in h
        assert "92" in h

    def test_architecture_engines_all_five_with_roles(self, report: dict) -> None:
        engines = renderers._architecture_engines(report)
        assert len(engines) == 5
        total_workload = sum(e.get("workload", 0) for e in engines)
        assert 99.0 <= total_workload <= 101.0
        total_cost = sum(e.get("cost", 0) for e in engines)
        assert abs(total_cost - 814.12) < 1.0
        roles = {e["role"] for e in engines}
        # at least the three role kinds present: retained, migration target, cache
        assert any("etain" in r for r in roles)
        assert any("igration" in r for r in roles)
        assert any("ache" in r for r in roles)

    def test_no_per_risk_list_no_tradeoffs(self, report: dict) -> None:
        h = renderers.render_decision_report_html(report)
        assert "Risk posture" in h
        assert "Key trade-offs" not in h
        # no per-risk cards in the body
        assert 'class="risk ' not in h

    def test_risk_posture_counts_and_strategies(self, report: dict) -> None:
        h = renderers.render_decision_report_html(report)
        assert "22" in h
        assert "Mitigation strategies" in h

    def test_no_empty_unknown_risks(self, report: dict) -> None:
        assert "unknown:" not in renderers.render_decision_report_html(report)


# =============================================================================
# Engineering Report (Markdown, build team)


class TestEngineeringReport:
    def test_renders_markdown(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert m.startswith("# Database Modernization")
        assert len(m) > 5000

    def test_risk_register_present_and_filtered(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert "## Risk register (22)" in m
        # the six malformed empty risks ([engine] unknown: with no body) are dropped.
        # (Note "unknown:" can still appear as a sub-type label on a KEPT risk that
        # has a real body after it, so we check the empty IDs, not the literal.)
        for rid in ("RISK-007", "RISK-008", "RISK-012", "RISK-013", "RISK-014", "RISK-015"):
            assert rid not in m

    def test_tradeoffs_by_engine(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert "## Migration trade-offs (44)" in m
        assert "### documentdb" in m

    def test_has_mermaid_fences(self, report: dict) -> None:
        m = renderers.render_engineering_report_md(report)
        assert "```mermaid" in m


# =============================================================================
# Empty-risk filter helpers


class TestRiskFilter:
    def test_filters_six_empty_risks(self, report: dict) -> None:
        risks = report["risk_assessment"]["risks"]
        kept = [r for r in risks if renderers._risk_has_content(r.get("description"))]
        assert len(risks) == 28
        assert len(kept) == 22

    def test_engine_and_body_parse(self) -> None:
        eng, body = renderers._risk_engine_and_body("[documentdb] Queries joining 3+ tables")
        assert eng == "documentdb"
        assert body == "Queries joining 3+ tables"

    def test_has_content_predicate(self) -> None:
        assert renderers._risk_has_content("[elasticache] unknown: ") is False
        assert renderers._risk_has_content("[documentdb] a real risk") is True
        assert renderers._risk_has_content("") is False


# =============================================================================
# Engine roles — an engine that won no queries is not "Retained"


@pytest.fixture
def zero_workload_report(report: dict) -> dict:
    """The real report, reshaped so DocumentDB is the engine that won nothing.

    Reproduces the observed defect: triage selected DocumentDB, analysis scored it 56%,
    then the assignment routed every query elsewhere, so schema design was skipped.
    """
    rep: dict = json.loads(json.dumps(report))
    pct = {
        "aurora_postgresql": 49.3,
        "elasticache": 29.4,
        "dynamodb": 15.1,
        "opensearch": 6.1,
        "documentdb": 0.0,
    }
    for r in rep["ranking"]:
        r["workload_percent"] = pct.get(r["target"])
    rep["recommended_architecture"]["databases"] = [
        d for d in rep["recommended_architecture"]["databases"] if d["service"] != "documentdb"
    ]
    rep.setdefault("schema_designs", {})["documentdb"] = {"status": "skipped"}
    return rep


class TestEngineRole:
    def test_zero_workload_is_evaluated_not_retained(self) -> None:
        """The defect: a skipped design used to imply "Retained" regardless of workload."""
        assert (
            renderers._engine_role("documentdb", set(), {"documentdb": {"status": "skipped"}}, 0.0)
            == "Evaluated"
        )
        assert renderers._engine_role("documentdb", set(), {}, None) == "Evaluated"

    def test_workload_carrying_engine_with_no_design_is_still_retained(self) -> None:
        """Guards against over-correcting: the source relational core keeps Retained."""
        role = renderers._engine_role(
            "aurora_postgresql", set(), {"aurora_postgresql": {"status": "not_available"}}, 49.3
        )
        assert role == "Retained"

    def test_cache_and_migration_target_keep_their_role_at_zero_workload(self) -> None:
        """Branch-order guard: the workload test must not outrank these two."""
        assert renderers._engine_role("elasticache", set(), {}, 0.0) == "Cache layer"
        assert renderers._engine_role("dynamodb", {"dynamodb"}, {}, 0.0) == "Migration target"

    def test_scope_reads_no_queries_assigned(self, zero_workload_report: dict) -> None:
        rows = {e["engine"]: e for e in renderers._architecture_engines(zero_workload_report)}
        assert rows["documentdb"]["role"] == "Evaluated"
        assert rows["documentdb"]["scope"] == "no queries assigned"
        # the engines that do the work are unaffected
        assert rows["aurora_postgresql"]["role"] == "Retained"
        assert rows["elasticache"]["role"] == "Cache layer"

    def test_evaluated_engine_excluded_from_retained_narrative(
        self, zero_workload_report: dict
    ) -> None:
        """The HTML claimed DocumentDB was "retained as the relational core"."""
        engines = renderers._architecture_engines(zero_workload_report)
        assert [e["engine"] for e in engines if e["role"] == "Retained"] == ["aurora_postgresql"]

    def test_evaluated_engine_excluded_from_wave_one(self, zero_workload_report: dict) -> None:
        """Wave 1 is built from ``no_move``, and its confidence is a ``min()`` over members.

        An engine the assignment routed nothing to must not be named in the wave nor be
        eligible to set its confidence floor. On the observed ``discourse`` report the
        printed figure does not move (ElastiCache independently sits at the same 56%), so
        this asserts membership rather than the number — the number is only distorted when
        the Evaluated engine happens to be the sole minimum, and asserting it here would
        encode a coincidence of that one report.
        """
        engines = renderers._architecture_engines(zero_workload_report)
        no_move = [e for e in engines if e["role"] in ("Retained", "Cache layer")]
        assert [e["engine"] for e in no_move] == ["aurora_postgresql", "elasticache"]
        assert "documentdb" not in {e["engine"] for e in no_move}

    def test_footer_names_and_percentage_agree(self, zero_workload_report: dict) -> None:
        """Tested positively on role, so an Evaluated engine is not named as "keeping" workload."""
        engines = renderers._architecture_engines(zero_workload_report)
        kept = [e for e in engines if e["role"] in ("Retained", "Cache layer")]
        assert [e["engine"] for e in kept] == ["aurora_postgresql", "elasticache"]
        assert abs(sum(e["workload"] for e in kept) - 78.7) < 0.05

    def test_architecture_svg_renders_the_evaluated_engine_muted(
        self, zero_workload_report: dict
    ) -> None:
        svg = renderers.architecture_svg(zero_workload_report)
        assert renderers._ROLE_STROKE["Evaluated"] in svg
        assert ">None<" not in svg


# =============================================================================
# Engine badges (accessibility)


def _srgb_channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def _contrast_ratio(hex_a: str, hex_b: str) -> float:
    lum_a, lum_b = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


class TestEngineBadgeContrast:
    """The decision report's engine badges render white text on these backgrounds
    (``_engine_badge``). axe's ``color-contrast`` rule caught the un-darkened
    "aurora" orange (#ff9900, 2.14:1) and "elasticache" red (#dc382d, 4.52:1 — a
    margin thin enough to flip pass/fail between Chromium and WebKit). Every
    badge colour, including the no-match fallback, must clear WCAG AA's 4.5:1
    for normal-weight small text.
    """

    @pytest.mark.parametrize("engine", sorted(renderers._ENGINE_BADGE))
    def test_badge_background_meets_aa_contrast_with_white_text(self, engine: str) -> None:
        color = renderers._ENGINE_BADGE[engine]
        ratio = _contrast_ratio("#ffffff", color)
        assert (
            ratio >= 4.5
        ), f"{engine} badge {color} only has {ratio:.2f}:1 contrast with white text"

    def test_fallback_badge_meets_aa_contrast_with_white_text(self) -> None:
        html = renderers._engine_badge("some-unmapped-engine")
        fallback = re.search(r"background:(#[0-9a-fA-F]{6})", html).group(1)
        assert _contrast_ratio("#ffffff", fallback) >= 4.5
