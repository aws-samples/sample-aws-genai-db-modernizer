"""The migration risks each sample raises, in every deliverable (#950, ADR-032).

Synthesis raises the risks the migration's shape carries from facts it already
computes (``src.agents.referee.migration_risks``). On the deterministic samples
this checks which rules each one raises, that the tables kept in sync agree
with ``facts.waves``, and that the engineering report, the executive PDF and
``assessment-data.json`` show the same risks as ``report.json``. The designed
wordpress job (schema designs, no model) adds the design rules.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from src.agents.referee.migration_risks import RULES
from src.report.escaping import md_text
from src.shared.report_facts import risk_engine_and_body
from tests.e2e.deliverable_figures import norm, pdf_page_texts
from tests.e2e.designed_run import UNSUPPORTED_CACHE_READ, run_designed_wordpress
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

# The rules each deterministic sample raises today. A routing change that
# moves one in or out updates this with its reason in the pull request.
EXPECTED_RULES: dict[str, list[str]] = {
    "wordpress": ["sync.wave", "routing.close_calls"],
    "discourse": ["sync.wave", "routing.tie_open", "routing.close_calls"],
    "trailgear": ["sync.wave", "routing.close_calls"],
}
# The open risks each sample lists (analysis risks and migration risks).
EXPECTED_OPEN = {"wordpress": 3, "discourse": 11, "trailgear": 9}

DESIGNED_RULES = [
    "sync.wave",
    "routing.close_calls",
    "cache.unsupported",
    "design.app_layer",
    "design.trade_offs",
    "design.trade_offs",
    "design.trade_offs",
]


def _report(r: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(r.report_json().read_text())
    return report


def _new(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in report["risk_assessment"]["risks"] if x.get("rule")]


def test_each_sample_raises_its_migration_risks(run: PipelineResult) -> None:
    report = _report(run)
    risks = _new(report)
    assert [r["rule"] for r in risks] == EXPECTED_RULES[run.db], run.db
    assert len(report["risk_assessment"]["risks"]) == EXPECTED_OPEN[run.db], run.db
    for r in risks:
        assert r["rule"] in RULES
        assert r["affected_tables"] and r["query_ids"], r["risk_id"]
        assert r["wave"] and f"Wave {r['wave']}" in r["mitigation"], r["risk_id"]
    strategies = " ".join(report["risk_assessment"]["mitigation_strategies"])
    for r in risks:
        assert r["risk_id"] in strategies, r["risk_id"]


def test_the_sync_risk_agrees_with_the_wave_and_source_facts(run: PipelineResult) -> None:
    """Each wave's one sync risk covers every table the wave keeps in sync from
    outside it and every replicated table of ``facts.table_sources``."""
    report = _report(run)
    sync = [r for r in _new(report) if r["rule"] == "sync.wave"]
    synced = {t for r in sync for t in r["affected_tables"]}
    sources = report["facts"]["table_sources"]
    assert {t["table"] for t in sources["replicated_tables"]} <= synced, run.db
    replicas = re.search(
        r"a copy of (\d+) tables? that CDC", " ".join(r["description"] for r in sync)
    )
    assert replicas and int(replicas.group(1)) == sources["replicated_table_count"], run.db
    for wave in report["facts"]["waves"]:
        if wave["migrating"]:
            kept = {o["table"] for o in wave["kept_in_sync"]}
            assert kept <= synced, (run.db, wave["wave"], kept - synced)


def test_every_deliverable_shows_the_same_risks(run: PipelineResult) -> None:
    report = _report(run)
    risks = _new(report)
    md = run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    pdf = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    data = json.loads(run.deliverable("assessment-data.json").read_text())
    assert data["risk_assessment"]["risks"] == report["risk_assessment"]["risks"]
    for r in risks:
        _, body = risk_engine_and_body(r["description"])
        assert f"**{r['risk_id']}**" in md and md_text(body) in md, r["risk_id"]
        assert f"`{r['affected_tables'][0]}`" in md, r["risk_id"]
        # Every risk has its card, a low one included (maintainer decision on #950).
        assert norm(r["risk_id"]) in pdf, r["risk_id"]
    # The PDF names no table: the risk cards print their counts, not their tables.
    for r in risks:
        for table in r["affected_tables"]:
            assert norm(table) not in pdf, (r["risk_id"], table)


@pytest.fixture(scope="module")
def designed(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    return run_designed_wordpress(tmp_path_factory.mktemp("risks-designed"), "risks-desi")


def test_the_designed_job_adds_the_design_risks(designed: PipelineResult) -> None:
    report = _report(designed)
    risks = _new(report)
    assert [r["rule"] for r in risks] == DESIGNED_RULES
    app = next(r for r in risks if r["rule"] == "design.app_layer")
    assert app["wave"] == 1 and "1 trigger" in app["description"]
    trade_offs = [r for r in risks if r["rule"] == "design.trade_offs"]
    assert all(r["severity"] == "MEDIUM" for r in trade_offs)
    assert "Cache-aside with a short TTL" in [d for r in trade_offs for d in r["details"]]
    # The register is no longer empty on the designed job, which was LOW with no open risk.
    assert report["risk_assessment"]["overall_risk_level"] == "MEDIUM"
    md = designed.deliverable("engineering-report.md").read_text(encoding="utf-8")
    for r in risks:
        assert f"**{r['risk_id']}**" in md, r["risk_id"]
    # A long design text is listed in full in the engineering report and never quoted,
    # clipped or whole, in the executive PDF (the e2e-llm PDF check found "…").
    cache = next(r for r in risks if r["rule"] == "cache.unsupported")
    assert any(UNSUPPORTED_CACHE_READ.rstrip(".") in d for d in cache["details"])
    assert md_text(cache["details"][0]) in md
    pdf = " ".join(pdf_page_texts(str(designed.deliverable("executive-report.pdf"))))
    assert "…" not in pdf and "..." not in pdf
    assert norm(cache["risk_id"]) in pdf and "Lookup by post title" not in pdf
