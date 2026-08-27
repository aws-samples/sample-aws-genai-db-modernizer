"""Unit tests for the customer-facing report renderers and the orchestrator
publish wiring (Increment 2).

The renderers (``render_decision_report_html``, ``render_engineering_report_md``)
and their helpers are pure functions of the synthesis ``report.json``. They are
exercised against a committed fixture — the real report from the deployed
``v2-e2e-09`` run — so the assertions check genuine reconciliation
($814.12 / 100% workload / 92 migrated tables) and the executive/engineering
content split, not a hand-built stub.

``_publish_synthesis_deliverables`` (in ``tools``) is tested with a fake store
and a patched ``publish`` to confirm: two rendered files are written to S3, all
three deliverables are published as CUSTOMER_OUTPUT, and the whole step is
non-fatal when the report cannot be read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from src.atx_orchestrator import artifacts
from src.atx_orchestrator.tools import run_synthesis_via_a2a

FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


@pytest.fixture(scope="module")
def report() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


# =============================================================================
# Decision Report (HTML, executive)


class TestDecisionReport:
    def test_renders_html_document(self, report: dict) -> None:
        h = artifacts.render_decision_report_html(report)
        low = h.lstrip().lower()
        assert low.startswith("<!doctype html") or low.startswith("<html")
        assert len(h) > 3000

    def test_offline_no_external_loads(self, report: dict) -> None:
        """Self-contained: no CDN/CSS/JS/font loads. The only URI allowed is the
        inline SVG xmlns."""
        h = artifacts.render_decision_report_html(report)
        assert "https://" not in h
        assert set(re.findall(r"http://[^\s\"'>]+", h)) <= {"http://www.w3.org/2000/svg"}
        assert "<script" not in h.lower()
        assert "<link" not in h.lower()
        assert "cdn" not in h.lower()

    def test_five_engine_reconciliation(self, report: dict) -> None:
        h = artifacts.render_decision_report_html(report)
        for eng in ("aurora", "elasticache", "documentdb", "dynamodb", "opensearch"):
            assert eng in h.lower()
        assert "814.12" in h
        assert "92" in h

    def test_architecture_engines_all_five_with_roles(self, report: dict) -> None:
        engines = artifacts._architecture_engines(report)
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
        h = artifacts.render_decision_report_html(report)
        assert "Risk posture" in h
        assert "Key trade-offs" not in h
        # no per-risk cards in the body
        assert 'class="risk ' not in h

    def test_risk_posture_counts_and_strategies(self, report: dict) -> None:
        h = artifacts.render_decision_report_html(report)
        assert "22" in h
        assert "Mitigation strategies" in h

    def test_no_empty_unknown_risks(self, report: dict) -> None:
        assert "unknown:" not in artifacts.render_decision_report_html(report)


# =============================================================================
# Engineering Report (Markdown, build team)


class TestEngineeringReport:
    def test_renders_markdown(self, report: dict) -> None:
        m = artifacts.render_engineering_report_md(report)
        assert m.startswith("# Database Modernization")
        assert len(m) > 5000

    def test_risk_register_present_and_filtered(self, report: dict) -> None:
        m = artifacts.render_engineering_report_md(report)
        assert "## Risk register (22)" in m
        # the six malformed empty risks ([engine] unknown: with no body) are dropped.
        # (Note "unknown:" can still appear as a sub-type label on a KEPT risk that
        # has a real body after it, so we check the empty IDs, not the literal.)
        for rid in ("RISK-007", "RISK-008", "RISK-012", "RISK-013", "RISK-014", "RISK-015"):
            assert rid not in m

    def test_tradeoffs_by_engine(self, report: dict) -> None:
        m = artifacts.render_engineering_report_md(report)
        assert "## Migration trade-offs (44)" in m
        assert "### documentdb" in m

    def test_has_mermaid_fences(self, report: dict) -> None:
        m = artifacts.render_engineering_report_md(report)
        assert "```mermaid" in m


# =============================================================================
# Empty-risk filter helpers


class TestRiskFilter:
    def test_filters_six_empty_risks(self, report: dict) -> None:
        risks = report["risk_assessment"]["risks"]
        kept = [r for r in risks if artifacts._risk_has_content(r.get("description"))]
        assert len(risks) == 28
        assert len(kept) == 22

    def test_engine_and_body_parse(self) -> None:
        eng, body = artifacts._risk_engine_and_body("[documentdb] Queries joining 3+ tables")
        assert eng == "documentdb"
        assert body == "Queries joining 3+ tables"

    def test_has_content_predicate(self) -> None:
        assert artifacts._risk_has_content("[elasticache] unknown: ") is False
        assert artifacts._risk_has_content("[documentdb] a real risk") is True
        assert artifacts._risk_has_content("") is False


# =============================================================================
# Orchestrator publish wiring


class _FakeStore:
    def __init__(self, data: dict) -> None:
        self.data: dict[str, dict] = dict(data)
        self.text_writes: dict[str, tuple[str, str]] = {}

    def read_json(self, path: str) -> dict:
        return self.data[path]

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        self.text_writes[path] = (content, content_type)


class TestSynthesisDeliverables:
    KEY = "discourse/job-x/synthesis/v1/report.json"

    def test_writes_two_files_and_publishes_three(self, report: dict) -> None:
        payload = {"report_artifact": self.KEY, "engines_ranked": 5}
        store = _FakeStore({self.KEY: report})
        captured: dict = {}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.artifacts.publish",
                side_effect=lambda items: captured.update({"items": items}) or {},
            ),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse", assignment_version=1)

        # payload flows through unchanged
        assert json.loads(out)["report_artifact"] == self.KEY

        # exactly the two rendered deliverables written to S3 (report.json untouched)
        keys = list(store.text_writes)
        assert len(keys) == 2
        assert any(k.endswith("decision-report-discourse.html") for k in keys)
        assert any(k.endswith("engineering-report-discourse.md") for k in keys)

        # three published, in order, all CUSTOMER_OUTPUT
        items = captured["items"]
        assert [it[1] for it in items] == ["HTML", "MARKDOWN", "JSON"]
        assert [it[2] for it in items] == [
            "Decision Report",
            "Engineering Report",
            "Assessment Data",
        ]
        assert {it[3] for it in items} == {"CUSTOMER_OUTPUT"}

    def test_non_fatal_when_report_unreadable(self) -> None:
        payload = {"report_artifact": "missing/key.json"}
        store = _FakeStore({})  # read_json raises KeyError
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse", assignment_version=1)
        assert json.loads(out)["report_artifact"] == "missing/key.json"

    def test_non_fatal_when_no_report_artifact(self) -> None:
        payload = {"engines_ranked": 5}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store") as make_store,
        ):
            out = run_synthesis_via_a2a("job-x", "discourse", assignment_version=1)
        # bailed before constructing a store
        make_store.assert_not_called()
        assert json.loads(out) == payload
