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
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.migration_risks import RULES
from src.report.escaping import md_text
from src.shared.report_facts import risk_engine_and_body
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.deliverable_figures import norm, pdf_page_texts
from tests.e2e.designed_run import UNSUPPORTED_CACHE_READ, run_designed_wordpress
from tests.e2e.pipeline import REPO, PipelineResult, _run, run_pipeline

pytestmark = pytest.mark.deterministic

# The rules each deterministic sample raises today. A routing change that
# moves one in or out updates this with its reason in the pull request.
EXPECTED_RULES: dict[str, list[str]] = {
    "wordpress": ["sync.wave", "routing.close_calls"],
    "discourse": ["sync.wave", "routing.tie_open", "routing.close_calls"],
    # #1149: trailgear's SQL Server to Aurora PostgreSQL wave 1 has 5 gaps that need a
    # rewrite and 4 that behave differently (wordpress and discourse have one
    # informational gap each, which raises none).
    "trailgear": ["sync.wave", "routing.close_calls", "relational.gaps", "relational.gaps"],
}
# The open risks each sample lists (analysis risks and migration risks).
EXPECTED_OPEN = {"wordpress": 3, "discourse": 11, "trailgear": 11}

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
    # A 1.41 row carries no tables of its own (``source_tables`` is 1.42): the risk takes
    # what the report lists for those queries (``table_sources``, query groups), a subset.
    backfilled = data["risk_assessment"]["risks"]
    fresh_risks = report["risk_assessment"]["risks"]
    assert [{**r, "affected_tables": None} for r in backfilled] == [
        {**r, "affected_tables": None} for r in fresh_risks
    ]
    for b, f in zip(backfilled, fresh_risks, strict=True):
        assert b["affected_tables"] and set(b["affected_tables"]) <= set(f["affected_tables"])
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


OLD_1_41 = REPO / "tests/unit/report/fixtures/wordpress_1_41_flagged_report.json"


def test_a_report_from_before_the_rule_renders_the_same_risks(
    flagged: PipelineResult, tmp_path: Path
) -> None:
    """A real report main's code wrote before ``routing.flagged`` (contract 1.41, the
    same wordpress job and router output as ``flagged``) renders the register a new
    synthesis writes, and every deliverable states one risk count and level: the
    summary's risk sentence, the risk posture and the executive report's tiles."""
    report = _report(flagged)
    root = tmp_path / "artifacts"
    key = root / "wordpress/probe-flag/synthesis/v2/report.json"
    key.parent.mkdir(parents=True)
    shutil.copyfile(OLD_1_41, key)
    old = json.loads(key.read_text())
    assert not [r for r in old["risk_assessment"]["risks"] if r.get("rule") == "routing.flagged"]
    again = PipelineResult(db="wordpress", job_id="probe-flag", artifact_root=root)
    common = ["--job-id", "probe-flag", "--db", "wordpress", "--artifact-root", str(root)]
    _run("report", ["scripts/run_report.py", *common], again)
    data = json.loads(again.deliverable("assessment-data.json").read_text())
    # A 1.41 row carries no tables of its own (``source_tables`` is 1.42): the risk takes
    # what the report lists for those queries (``table_sources``, query groups), a subset.
    backfilled = data["risk_assessment"]["risks"]
    fresh_risks = report["risk_assessment"]["risks"]
    assert [{**r, "affected_tables": None} for r in backfilled] == [
        {**r, "affected_tables": None} for r in fresh_risks
    ]
    for b, f in zip(backfilled, fresh_risks, strict=True):
        assert b["affected_tables"] and set(b["affected_tables"]) <= set(f["affected_tables"])
    assert data["risk_assessment"]["overall_risk_level"] == "HIGH"
    n = len(report["risk_assessment"]["risks"])
    sentence = f"{n} open migration risks (overall: HIGH)"
    assert sentence in data["summary"] and sentence in report["summary"]
    md = again.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert md_text(sentence) in md and "overall: MEDIUM" not in md
    for r in backfilled:
        assert f"**{r['risk_id']}**" in md
    assert "Overall risk: HIGH" in md
    pdf = " ".join(pdf_page_texts(str(again.deliverable("executive-report.pdf"))))
    fresh_pdf = " ".join(pdf_page_texts(str(flagged.deliverable("executive-report.pdf"))))
    for risk in report["risk_assessment"]["risks"]:
        assert norm(risk["risk_id"]) in pdf
    assert _pdf_risk_tiles(pdf) == _pdf_risk_tiles(fresh_pdf)


def _pdf_risk_tiles(pdf: str) -> str:
    """The executive report's risk page head: the overall level and the open tile."""
    m = re.search(r"Overall risk.{0,200}", norm(pdf))
    assert m, "no risk tiles"
    return m.group(0)


# ---------------------------------------------------------------------------
# The relational move's gaps (#1149)
# ---------------------------------------------------------------------------


def test_the_gap_risks_agree_with_the_relational_gap_facts(run: PipelineResult) -> None:
    """On every sample: one ``relational.gaps`` risk per non-informational impact of
    ``facts.relational_gaps``, in Wave 1, listing exactly those gaps, and only when
    the gap check's target is wave 1's engine."""
    report = _report(run)
    gaps = report["facts"].get("relational_gaps") or {}
    wave1 = next(w for w in report["migration_waves"] if w["wave"] == 1)
    raised = [r for r in _new(report) if r["rule"] == "relational.gaps"]
    if gaps.get("target_engine") != wave1["engines"][0]:
        assert raised == [], run.db
        return
    severity = {"blocks_move": "HIGH", "needs_rewrite": "MEDIUM", "behaves_differently": "LOW"}
    want = [
        (severity[impact], n)
        for impact, n in gaps["gaps_by_impact"].items()
        if impact in severity and n
    ]
    assert [(r["severity"], len(r["details"])) for r in raised] == want, run.db
    md = run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    pdf = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    for r in raised:
        assert r["wave"] == 1 and "Wave 1 gate" in r["mitigation"]
        for detail in r["details"]:
            assert md_text(detail) in md, detail[:60]
        # The executive report states the risk, never a gap's reason or documentation.
        assert norm(r["risk_id"]) in pdf
        for g in gaps["gaps"]:
            if g["impact"] in severity:
                assert norm(g["reason"]) not in pdf, g["rule_id"]
