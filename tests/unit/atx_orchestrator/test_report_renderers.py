"""Unit tests for the customer-facing report renderers and the orchestrator
publish wiring (Increment 2).

The renderers (``render_decision_report_html``, ``render_engineering_report_md``)
and their helpers are pure functions of the synthesis ``report.json``. They are
exercised against a committed fixture — the real report from the deployed
``v2-e2e-09`` run — so the assertions check genuine reconciliation
($814.12 / 100% workload / 92 migrated tables) and the executive/engineering
content split, not a hand-built stub.

``_publish_synthesis_deliverables`` (in ``tools``) is tested with a fake store
and a patched ``publish`` to confirm: three text deliverables plus the executive
deck and its PDF are written to S3 under the canonical artifact names, all five
deliverables are published as CUSTOMER_OUTPUT each carrying an explicit download
filename, and the whole step is non-fatal when the report cannot be read.

``_FakeStore`` implements ``write_bytes`` deliberately. While it did not, the
executive-summary block raised ``AttributeError`` straight into its own
``except`` and the fifth deliverable was never exercised at all.
"""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from src.atx_orchestrator.runtime import artifacts, pptx_report
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
            artifacts._engine_role("documentdb", set(), {"documentdb": {"status": "skipped"}}, 0.0)
            == "Evaluated"
        )
        assert artifacts._engine_role("documentdb", set(), {}, None) == "Evaluated"

    def test_workload_carrying_engine_with_no_design_is_still_retained(self) -> None:
        """Guards against over-correcting: the source relational core keeps Retained."""
        role = artifacts._engine_role(
            "aurora_postgresql", set(), {"aurora_postgresql": {"status": "not_available"}}, 49.3
        )
        assert role == "Retained"

    def test_cache_and_migration_target_keep_their_role_at_zero_workload(self) -> None:
        """Branch-order guard: the workload test must not outrank these two."""
        assert artifacts._engine_role("elasticache", set(), {}, 0.0) == "Cache layer"
        assert artifacts._engine_role("dynamodb", {"dynamodb"}, {}, 0.0) == "Migration target"

    def test_scope_reads_no_queries_assigned(self, zero_workload_report: dict) -> None:
        rows = {e["engine"]: e for e in artifacts._architecture_engines(zero_workload_report)}
        assert rows["documentdb"]["role"] == "Evaluated"
        assert rows["documentdb"]["scope"] == "no queries assigned"
        # the engines that do the work are unaffected
        assert rows["aurora_postgresql"]["role"] == "Retained"
        assert rows["elasticache"]["role"] == "Cache layer"

    def test_evaluated_engine_excluded_from_retained_narrative(
        self, zero_workload_report: dict
    ) -> None:
        """The HTML claimed DocumentDB was "retained as the relational core"."""
        engines = artifacts._architecture_engines(zero_workload_report)
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
        engines = artifacts._architecture_engines(zero_workload_report)
        no_move = [e for e in engines if e["role"] in ("Retained", "Cache layer")]
        assert [e["engine"] for e in no_move] == ["aurora_postgresql", "elasticache"]
        assert "documentdb" not in {e["engine"] for e in no_move}

    def test_footer_names_and_percentage_agree(self, zero_workload_report: dict) -> None:
        """Tested positively on role, so an Evaluated engine is not named as "keeping" workload."""
        engines = artifacts._architecture_engines(zero_workload_report)
        kept = [e for e in engines if e["role"] in ("Retained", "Cache layer")]
        assert [e["engine"] for e in kept] == ["aurora_postgresql", "elasticache"]
        assert abs(sum(e["workload"] for e in kept) - 78.7) < 0.05

    def test_architecture_svg_renders_the_evaluated_engine_muted(
        self, zero_workload_report: dict
    ) -> None:
        svg = artifacts.architecture_svg(zero_workload_report)
        assert artifacts._ROLE_STROKE["Evaluated"] in svg
        assert ">None<" not in svg


# =============================================================================
# Orchestrator publish wiring


class _FakeStore:
    def __init__(self, data: dict) -> None:
        self.data: dict[str, dict] = dict(data)
        self.text_writes: dict[str, tuple[str, str]] = {}
        self.byte_writes: dict[str, bytes] = {}

    def read_json(self, path: str) -> dict:
        return self.data[path]

    def exists(self, path: str) -> bool:
        return path in self.data

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.data if k.startswith(prefix)]

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        self.text_writes[path] = (content, content_type)

    # The executive summary writes binary. Without this the whole block raises
    # AttributeError into its own ``except`` and the fifth deliverable silently
    # never renders -- which is exactly how it went untested before.
    def write_bytes(self, path: str, content: bytes, content_type: str = "") -> None:
        self.byte_writes[path] = content


class TestSynthesisDeliverables:
    KEY = "discourse/job-x/synthesis/v1/report.json"

    def test_writes_five_files_and_publishes_five(self, report: dict) -> None:
        payload = {"response": {"report_artifact": self.KEY, "engines_ranked": 5}}
        store = _FakeStore({self.KEY: report})
        captured: dict = {}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch(
                "src.atx_orchestrator.runtime.artifacts.publish",
                side_effect=lambda items: captured.update({"items": items}) or {},
            ),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse")

        # payload flows through unchanged
        assert json.loads(out)["response"]["report_artifact"] == self.KEY

        # provenance stems are date-stamped; derive the day rather than freezing time
        day = datetime.now(UTC).strftime("%Y%m%d")

        # exactly the three rendered deliverables written to S3 (report.json untouched)
        keys = list(store.text_writes)
        assert len(keys) == 3
        # canonical stem: {database}_{artifact}_{job8}_{YYYYMMDD}.{ext}
        assert any(re.search(r"/discourse_decision-report_job-x_\d{8}\.html$", k) for k in keys)
        assert any(re.search(r"/discourse_engineering-report_job-x_\d{8}\.md$", k) for k in keys)
        assert any(re.search(r"/discourse_analysis-report_job-x_\d{8}\.html$", k) for k in keys)
        assert self.KEY not in keys

        # the executive summary writes both halves as binary under fixed names --
        # the deck stays on S3 as the editable source, only the PDF is registered
        assert sorted(k.rsplit("/", 1)[-1] for k in store.byte_writes) == [
            "summary-executive-report.pdf",
            "summary-executive-report.pptx",
        ]
        assert (
            store.byte_writes[f"{self.KEY.rsplit('/', 1)[0]}/summary-executive-report.pdf"][:5]
            == b"%PDF-"
        )

        # five published, in order, all CUSTOMER_OUTPUT
        items = captured["items"]
        assert [it[1] for it in items] == ["HTML", "MARKDOWN", "JSON", "HTML", "PDF"]
        assert [it[2] for it in items] == [
            "Decision Report — discourse",
            "Engineering Report — discourse",
            "Assessment Data (raw) — discourse",
            "Interactive Analysis Report — discourse",
            "Executive Summary Report — discourse",
        ]
        assert {it[3] for it in items} == {"CUSTOMER_OUTPUT"}

        # Every item carries an explicit download filename (5th element). Without it
        # publish() cannot set fileMetadata.path and the customer's download is named
        # after the artifact UUID -- a regression no other assertion here would catch.
        assert all(len(it) == 5 for it in items)
        assert items[-1][4] == "summary-executive-report.pdf"
        assert [it[4] for it in items[:4]] == [
            f"discourse_decision-report_job-x_{day}.html",
            f"discourse_engineering-report_job-x_{day}.md",
            f"discourse_assessment-data_job-x_{day}.json",
            f"discourse_analysis-report_job-x_{day}.html",
        ]

    def test_non_fatal_when_report_unreadable(self) -> None:
        payload = {"response": {"report_artifact": "missing/key.json"}}
        store = _FakeStore({})  # read_json raises KeyError
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse")
        assert json.loads(out)["response"]["report_artifact"] == "missing/key.json"

    def test_non_fatal_when_no_report_artifact(self) -> None:
        payload = {"response": {"engines_ranked": 5}}
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload),
            patch("src.atx_orchestrator.tools._make_store"),
        ):
            out = run_synthesis_via_a2a("job-x", "discourse")
        # No report_artifact -> deliverables publishing is skipped; the payload
        # flows through unchanged. (A store is constructed once for version
        # resolution, which is best-effort and never fatal.)
        assert json.loads(out) == payload


# =============================================================================
# Risk panels are grouped by engine, not by risk id


class TestRisksByEngine:
    """The deck used to print one row per risk_id. RISK-005 means nothing to an executive
    reader, and one engine on two rows read as two unrelated problems."""

    RISKS = [
        {
            "risk_id": "RISK-005",
            "severity": "HIGH",
            "description": "[documentdb] Correlated subqueries. (0% resolved, 159 remaining)",
            "mitigation": "Pre-denormalize at write time.",
        },
        {
            "risk_id": "RISK-006",
            "severity": "HIGH",
            "description": "[documentdb] Recursive queries. (0% resolved, 1 remaining)",
            "mitigation": "Use Materialized Path.",
        },
        {
            "risk_id": "RISK-010",
            "severity": "HIGH",
            "description": "[dynamodb] Complex GROUP BY. (0% resolved, 108 remaining)",
            "mitigation": "Pre-compute aggregates on write.",
        },
    ]

    def test_one_entry_per_engine(self) -> None:
        out = pptx_report._risks_by_engine(self.RISKS)
        assert [g["engine"] for g in out] == ["documentdb", "dynamodb"]

    def test_query_counts_sum_within_an_engine(self) -> None:
        out = {g["engine"]: g for g in pptx_report._risks_by_engine(self.RISKS)}
        assert out["documentdb"]["queries"] == 160  # 159 + 1
        assert out["dynamodb"]["queries"] == 108

    def test_ordered_by_exposure_then_name(self) -> None:
        """Worst-affected engine leads; ties break on name so the deck stays deterministic."""
        out = pptx_report._risks_by_engine(self.RISKS)
        assert [g["queries"] for g in out] == [160, 108]
        tied = [
            {"description": "[zeta] a.", "mitigation": "x"},
            {"description": "[alpha] b.", "mitigation": "y"},
        ]
        assert [g["engine"] for g in pptx_report._risks_by_engine(tied)] == ["alpha", "zeta"]

    def test_mitigations_combined_and_deduplicated(self) -> None:
        out = {g["engine"]: g for g in pptx_report._risks_by_engine(self.RISKS)}
        assert "Pre-denormalize at write time." in out["documentdb"]["fix"]
        assert "Use Materialized Path." in out["documentdb"]["fix"]
        dupes = [
            {"description": "[x] one.", "mitigation": "same fix"},
            {"description": "[x] two.", "mitigation": "same fix"},
        ]
        assert pptx_report._risks_by_engine(dupes)[0]["fix"] == "same fix"

    def test_risk_ids_never_reach_the_output(self) -> None:
        out = pptx_report._risks_by_engine(self.RISKS)
        blob = json.dumps(out)
        for rid in ("RISK-005", "RISK-006", "RISK-010"):
            assert rid not in blob

    def test_engineless_risks_group_under_one_entry(self) -> None:
        out = pptx_report._risks_by_engine([{"description": "no prefix here.", "mitigation": ""}])
        assert out[0]["engine"] == ""


# =============================================================================
# Appendix A — the complete risk register


class TestRiskAppendix:
    def test_appendix_slides_are_appended(self, report: dict) -> None:
        assert pptx_report.slide_risk_appendix in pptx_report.SLIDES

    def test_panel_height_grows_with_content(self) -> None:
        """Pagination is by measured height: one engine with nine findings needs far more
        room than one with a single finding, so a fixed panel count would clip or waste."""
        small = {"whats": ["Short."], "fixes": ["Fix."], "count": 1}
        big = {"whats": ["A sentence." * 20], "fixes": ["A mitigation." * 20], "count": 9}
        assert pptx_report._appendix_panel_height(big) > pptx_report._appendix_panel_height(small)

    def test_every_engine_and_severity_appears_somewhere(self, report: dict) -> None:
        """Slide 6 caps panels and names the rest in its footnote; the appendix must not cap."""
        deck = pptx_report.render_executive_summary_pptx(report, None)
        from pptx import Presentation

        prs = Presentation(io.BytesIO(deck))
        appendix = " ".join(
            sh.text_frame.text
            for s in prs.slides
            for sh in s.shapes
            if sh.has_text_frame and "Appendix A" not in sh.text_frame.text
        )
        risks = report["risk_assessment"]["risks"]
        for sev in ("HIGH", "MEDIUM"):
            group = [r for r in risks if str(r.get("severity", "")).upper() == sev]
            for g in pptx_report._risks_by_engine(group):
                label = artifacts_engine_label(g["engine"])
                assert label in appendix, f"{sev} {g['engine']} missing from the deck"

    def test_appendix_text_is_never_truncated(self, report: dict) -> None:
        """The executive slide abbreviates with "(+N more)"; the appendix shows everything."""
        deck = pptx_report.render_executive_summary_pptx(report, None)
        from pptx import Presentation

        prs = Presentation(io.BytesIO(deck))
        pages = [
            s
            for s in prs.slides
            if any(
                sh.has_text_frame and sh.text_frame.text.startswith("Appendix A") for sh in s.shapes
            )
        ]
        assert pages, "no appendix slides rendered"
        blob = " ".join(sh.text_frame.text for s in pages for sh in s.shapes if sh.has_text_frame)
        assert "more)" not in blob
        assert "…" not in blob


def artifacts_engine_label(engine: str) -> str:
    return pptx_report.ENGINE_LABEL.get(engine, engine) or "General"
