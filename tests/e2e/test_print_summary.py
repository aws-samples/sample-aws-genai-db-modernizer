"""The gate block and final summary /modernize quotes, on real deterministic jobs (#1307).

``scripts/print_summary.py`` prints both from the artifacts. They mirror the UI at its depth,
and what they state is what the deliverables state (ADR-032): the executive summary is the
engineering report's, the engines' reasons are the executive PDF's, every figure is a
``report.json`` fact. Run for the designed wordpress job (a schema design for every kept
engine), the heterogeneous trailgear job and every deterministic sample.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 -- runs this repo's own print_summary.py script with fixed argv
import sys
from typing import Any

import pypdf
import pytest

from src.report.executive_report import _names_pattern, table_names, without_table_names
from src.report.facts import report_facts
from src.report.formatting import fmt_num
from src.report.printed_summary import ordered_risks, prose
from src.shared.engine_names import display_engine
from src.shared.risk_severity import severity_of
from tests.e2e.pipeline import REPO, PipelineResult

pytestmark = pytest.mark.deterministic


def _print(r: PipelineResult, flag: str, *extra: str) -> str:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script
        [
            sys.executable,
            "scripts/print_summary.py",
            "--job-id",
            r.job_id,
            "--db",
            r.db,
            flag,
            *extra,
            "--artifact-root",
            str(r.artifact_root),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_DEFAULT_REGION": "us-east-1"},
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _report(r: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(r.report_json().read_text())
    return report


def _squash(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str, level: str) -> str:
    """The lines under ``heading`` up to the next heading of the same or a higher level."""
    after = text.split(f"{level} {heading}\n", 1)[1]
    return re.split(rf"^#{{1,{len(level)}}} ", after, maxsplit=1, flags=re.MULTILINE)[0].strip()


@pytest.fixture(params=["designed_run", "trailgear_run"])
def job(request: pytest.FixtureRequest) -> PipelineResult:
    """The designed wordpress job, and the heterogeneous trailgear job (no schema designs)."""
    result: PipelineResult = request.getfixturevalue(request.param)
    return result


def test_every_deterministic_sample_prints_both_blocks(run: PipelineResult) -> None:
    gate = _print(run, "--gate")
    final = _print(run, "--final")
    assert gate.startswith(f"## Assignment for approval: {run.db} (job {run.job_id})\n")
    assert final.startswith(f"## Assessment: {run.db} (job {run.job_id})\n")
    facts = report_facts(_report(run))
    assert f"- Projected monthly cost: ${facts['projected_monthly_cost_usd']:,.2f}" in final


def test_the_executive_summary_is_the_engineering_reports(job: PipelineResult) -> None:
    printed = _section(_print(job, "--final"), "Executive summary", "###")
    engineering = (job.job_dir() / "deliverables" / "engineering-report.md").read_text()
    in_report = _section(engineering, "Executive summary", "##")
    # The report adds its fixed closing note after the summary; the printed one stops before it.
    assert in_report.startswith(printed.split("\n\n*")[0])
    assert printed.split("\n\n*")[0] == in_report.split("\n\n*")[0]


def test_the_engines_and_their_reasons_are_the_executive_pdfs(job: PipelineResult) -> None:
    report = _report(job)
    facts = report_facts(report)
    names = _names_pattern(table_names(report))
    pdf = pypdf.PdfReader(str(job.deliverable("executive-report.pdf")))
    pdf_text = _squash("\n".join(page.extract_text() for page in pdf.pages))
    final = _print(job, "--final")
    # The recommendation the PDF opens with is the architecture the summary names.
    rationale = without_table_names(
        (report.get("recommended_architecture") or {}).get("rationale"), names
    )
    if rationale:
        assert _squash(rationale) in pdf_text
    engines_section = _section(final, "Engines", "###")
    lines = [line for line in engines_section.splitlines() if line.startswith("- ")]
    assert len(lines) == len(facts["engines"])
    for line, engine in zip(lines, facts["engines"], strict=True):
        assert line.startswith(f"- {display_engine(engine['engine'])} ({engine['role']}")
        assert f"${engine['monthly_cost_usd']:,.2f}/mo" in line
        why = without_table_names(engine.get("rationale"), names)
        if why:
            assert prose(why) in line
            assert _squash(why) in pdf_text  # the PDF's "Why this architecture" row


def test_every_figure_is_a_report_fact(job: PipelineResult) -> None:
    report = _report(job)
    facts = report_facts(report)
    final = _print(job, "--final")
    glance = _section(final, "At a glance", "###")
    assert f"- Projected monthly cost: ${facts['projected_monthly_cost_usd']:,.2f}" in glance
    waves = facts["migration_wave_count"]
    assert f"- Migration: {waves} wave" in glance
    for engine in facts["engines"]:
        if engine["role"] != "Cache layer":
            assert f"{fmt_num(engine['workload_percent'], 1)}% of queries" in final
    risks = _section(final, "Risks", "###")
    assert f"Open risks: {facts['open_risk_count']}" in risks or "Open risks: none" in risks
    assert (
        f"Resolved by the assignment: {facts['resolved_risk_count']}" in risks
        or not facts["resolved_risk_count"]
    )
    level = report["risk_assessment"].get("overall_risk_level")
    if level:
        assert f"Overall risk: {level}" in risks


def test_every_critical_and_high_risk_is_listed_with_its_id(job: PipelineResult) -> None:
    final = _print(job, "--final")
    risks = ordered_risks(_report(job))
    serious = [r for r in risks if severity_of(r) in ("CRITICAL", "HIGH")]
    for r in serious:
        assert f"({r['risk_id']})" in final
    listed = [
        line for line in _section(final, "Risks", "###").splitlines() if line.startswith("- ")
    ]
    assert [line.split(",")[0].removeprefix("- ") for line in listed[:1]] in (
        [severity_of(risks[0])],
        [],
    )


def test_where_to_look_next_names_every_rendered_deliverable_and_the_pages(
    job: PipelineResult,
) -> None:
    text = _print(job, "--final", "--mode", "both")
    tail = _section(text, "Where to look next", "###")
    for path in sorted((job.job_dir() / "deliverables").iterdir()):
        assert f"{path.name}`" in tail
    assert f"/analysis/results/{job.job_id}" in tail
    assert f"/analysis/monitor/summary/{job.job_id}" in tail
    assert "Not rendered this time" not in tail
    assert "/analysis/results/" not in _print(job, "--final")  # chat starts no UI


def test_the_gate_block_shows_the_distribution_the_gate_decides_on(job: PipelineResult) -> None:
    text = _print(job, "--gate")
    rc = json.loads((job.job_dir() / "reality-check" / "output.json").read_text())
    for engine, after in rc["after_distribution"].items():
        row = next(
            line for line in text.splitlines() if line.startswith(f"| {display_engine(engine)} |")
        )
        assert f"| {rc['before_distribution'].get(engine, 0)} | {after} |" in row
    for engine in rc.get("eliminated_engines", []):
        row = next(
            line for line in text.splitlines() if line.startswith(f"| {display_engine(engine)} |")
        )
        assert row.endswith("| dropped |")
    assert f"Total: {sum(rc['after_distribution'].values())} quer" in text
    for pattern in rc.get("architectural_patterns", []):
        assert f"- {prose(pattern['name'])}" in text
    assert text.count("Recommended architecture: ") == 1
    assert "Schema design runs next for:" in text or "Engine choices to settle" in text
    # Unless chat, the block itself ends with the summary page the user checks before approving.
    with_url = _print(job, "--gate", "--mode", "both")
    assert with_url.startswith(text.rstrip())
    assert with_url.rstrip().endswith(f"/analysis/monitor/summary/{job.job_id}")


def test_both_blocks_are_stable_across_calls(job: PipelineResult) -> None:
    for flag in ("--gate", "--final"):
        assert _print(job, flag) == _print(job, flag)


def test_the_gate_and_final_label_each_cache_figure(job: PipelineResult) -> None:
    """#1252: the gate states the cache candidates, the final summary the designed cached
    reads, both from the facts; the change between them is one sentence, once, or none."""
    report = _report(job)
    cache = report_facts(report)["cache_layer"]
    gate = _print(job, "--gate")
    final = _print(job, "--final")
    if cache is None:
        assert "cache candidate" not in gate and "designed cached read" not in final
        return
    # The gate states the candidates routing put under the cache, even after synthesis has
    # rewritten the assignment's overlay to the designed count (#1252).
    assert f"has {cache['candidate_read_count']} cache candidate" in gate
    assert "cached reads" not in gate
    assert f"{cache['designed_read_count']} designed cached read" in final
    assert cache["candidate_read_count"] >= cache["designed_read_count"]
    assignments = sorted((job.job_dir() / "assignment").glob("v*/assignment.json"))
    stored = json.loads(assignments[-1].read_text())["cache_overlay"]
    assert stored["query_count"] == cache["designed_read_count"]
    assert (
        stored.get("candidate_query_count", stored["query_count"]) == cache["candidate_read_count"]
    )
    sentences = [s for s in final.splitlines() if "stay on their owner engine" in s]
    assert len(sentences) == (1 if cache["owner_only_read_count"] else 0)
    if sentences:
        assert f"{cache['candidate_read_count']} cache candidates" in sentences[0]
        assert f"{cache['designed_read_count']} designed" in sentences[0]
