"""The migration risks each sample raises, in every deliverable (#950, ADR-032).

Synthesis raises the risks the migration's shape carries from facts it already
computes (``src.agents.referee.migration_risks``). On the deterministic samples
this checks which rules each one raises, that the tables kept in sync agree
with ``facts.waves``, and that the engineering report, the executive PDF and
``assessment-data.json`` show the same risks as ``report.json``. The designed
wordpress job (schema designs, no model) adds the design rules.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.migration_risks import RULES, wave_gate_strategies
from src.report.escaping import md_text
from src.shared.report_facts import refresh_risk_facts, risk_engine_and_body
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.deliverable_figures import norm, pdf_page_texts
from tests.e2e.designed_run import UNSUPPORTED_CACHE_READ, run_designed_wordpress
from tests.e2e.pipeline import PipelineResult, _run, run_pipeline

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


# ---------------------------------------------------------------------------
# Flagged queries no other kept engine takes (#1148)
# ---------------------------------------------------------------------------


def _flagged(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in _new(report) if r["rule"] == "routing.flagged"]


def _flagged_and_held(report: dict[str, Any]) -> int:
    u = report["facts"].get("unservable") or {}
    return int(u.get("flagged_query_count") or 0) + int(u.get("held_query_count") or 0)


def test_the_flagged_risks_agree_with_the_unservable_facts(run: PipelineResult) -> None:
    """On every sample: the ``routing.flagged`` risks cover exactly the flagged and
    held queries ``facts.unservable`` counts (none on the bundled samples)."""
    report = _report(run)
    assert sum(len(r["query_ids"]) for r in _flagged(report)) == _flagged_and_held(report)


FLAG_ENGINE = "dynamodb"


@pytest.fixture(scope="module")
def flagged(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    """The wordpress job with a post-schema router output that keeps one DynamoDB
    query as terminal (no kept engine takes it) and holds another by a pin: the
    two kinds ``flagged_unservable`` lists, which no bundled sample produces."""
    root = tmp_path_factory.mktemp("risks-flagged")
    result = run_pipeline("wordpress", root, "risks-flag")
    store = LocalArtifactStore(base_dir=str(root))
    version = resolve_downstream_assignment_version(store, "wordpress", "risks-flag")
    assignment = store.read_json(f"wordpress/risks-flag/assignment/v{version}/assignment.json")
    ids = sorted(
        str(qa["query_id"])
        for qa in assignment["query_assignments"]
        if qa["assigned_engine"] == FLAG_ENGINE and qa.get("in_scope", True)
    )
    store.write_json(
        "wordpress/risks-flag/post-schema-router/router_output.json",
        {
            "job_id": "risks-flag",
            "terminal": [
                {
                    "query_id": ids[0],
                    "flagged_engine": FLAG_ENGINE,
                    "reason_category": "full_scan",
                    "cause": "no_kept_engine",
                }
            ],
            "held_queries": [ids[1]],
            "held": [
                {
                    "query_id": ids[1],
                    "flagged_engine": FLAG_ENGINE,
                    "reason_category": "client_side_join",
                }
            ],
        },
    )
    common = ["--job-id", "risks-flag", "--db", "wordpress", "--artifact-root", str(root)]
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    _run("report", ["scripts/run_report.py", *common], result)
    return result


def test_a_flagged_and_a_held_query_are_one_high_risk_in_every_deliverable(
    flagged: PipelineResult,
) -> None:
    report = _report(flagged)
    [risk] = _flagged(report)
    assert _flagged_and_held(report) == 2 == len(risk["query_ids"])
    assert risk["severity"] == "HIGH" and report["risk_assessment"]["overall_risk_level"] == "HIGH"
    wave = next(w["wave"] for w in report["migration_waves"] if FLAG_ENGINE in w["engines"])
    assert risk["wave"] == wave and f"Wave {wave} gate" in risk["mitigation"]
    assert risk["affected_tables"]
    assert "change the pin" in risk["mitigation"]
    assert report["facts"]["open_risk_count"] == len(report["risk_assessment"]["risks"])
    md = flagged.deliverable("engineering-report.md").read_text(encoding="utf-8")
    _, body = risk_engine_and_body(risk["description"])
    assert f"**{risk['risk_id']}**" in md and md_text(body) in md
    pdf = " ".join(pdf_page_texts(str(flagged.deliverable("executive-report.pdf"))))
    assert norm(risk["risk_id"]) in pdf
    for table in risk["affected_tables"]:
        assert norm(table) not in pdf, table
    data = json.loads(flagged.deliverable("assessment-data.json").read_text())
    assert data["risk_assessment"] == report["risk_assessment"]


def test_a_report_from_before_the_rule_renders_the_same_risks(
    flagged: PipelineResult, tmp_path: Path
) -> None:
    """A report synthesized before ``routing.flagged`` (the risk, its gate line
    and its counts taken out) renders the same register as a new synthesis: the
    deliverables backfill it from the report's own ``flagged_unservable``."""
    report = _report(flagged)
    [risk] = _flagged(report)
    old = copy.deepcopy(report)
    assessment = old["risk_assessment"]
    rule_risks = [r for r in assessment["risks"] if r.get("rule")]
    assessment["risks"] = [r for r in assessment["risks"] if r != risk]
    kept = [r for r in rule_risks if r != risk]
    lines = wave_gate_strategies(rule_risks)
    at = assessment["mitigation_strategies"].index(lines[0])
    assessment["mitigation_strategies"][at : at + len(lines)] = wave_gate_strategies(kept)
    old["contract_version"] = "1.40"
    old["facts"] = refresh_risk_facts(report["facts"], old)
    assert old["facts"]["open_risk_count"] == report["facts"]["open_risk_count"] - 1
    root = tmp_path / "artifacts"
    key = flagged.report_json().relative_to(flagged.artifact_root)
    shutil.copytree(flagged.job_dir(), root / flagged.db / flagged.job_id)
    (root / key).write_text(json.dumps(old))
    again = PipelineResult(db=flagged.db, job_id=flagged.job_id, artifact_root=root)
    common = ["--job-id", flagged.job_id, "--db", flagged.db, "--artifact-root", str(root)]
    _run("report", ["scripts/run_report.py", *common], again)
    data = json.loads(again.deliverable("assessment-data.json").read_text())
    assert data["risk_assessment"] == report["risk_assessment"]
    md = again.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert f"**{risk['risk_id']}**" in md
    fresh = flagged.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert _risk_section(md) == _risk_section(fresh)


def _risk_section(md: str) -> str:
    """The engineering report from its risk posture to the section after its register."""
    start = md.index("## Risk posture")
    register = md.index("## Risk register", start)
    end = md.find("\n## ", register + 1)
    return md[start : end if end > 0 else len(md)]
