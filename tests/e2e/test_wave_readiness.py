# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis states whether the plan suggests each wave (#1992), and every deliverable
says the same.

The check is one function of the report (``wave_readiness_rows``); the engineering report
and the executive PDF print each wave's readiness line, the final chat summary each wave
the plan does not suggest (``facts.wave_readiness_notes``), and the executive summary's
Decision, rendered from the facts, names each one and approves only a suggested wave.
The results page is checked in ``tests/e2e/test_ui.py``.

A held wave on a bundled sample is a design-quality defect, listed in ``KNOWN_HELD``
with what holds it, so a new one fails here until it is fixed or listed. Without a
model no wave is designed, so on the deterministic samples every wave a check does not
hold is "not designed yet", and no wave is put up for approval.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.report.engineering_report import summary_paragraphs
from src.report.printed_summary import render_final_summary
from src.shared.report_facts import wave_readiness_rows
from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
from tests.e2e.pipeline import PipelineResult

# The waves the deterministic samples hold, and what holds each one. discourse: the
# DocumentDB analysis's correlated-subquery flag on a query now on DynamoDB, which
# DynamoDB's own analysis flags too (a write wider than one transaction); with no wave 1
# design yet, the wave also waits on wave 1. wordpress and trailgear hold none: their
# HIGH is an Aurora note on a query now on DynamoDB that DynamoDB does not flag.
KNOWN_HELD: dict[str, dict[int, list[str]]] = {
    "wordpress": {},
    "discourse": {3: ["open_high_risk", "earlier_wave"]},
    "trailgear": {},
}


def _report(run: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(run.report_json().read_text())
    return report


def _check_facts(report: dict[str, Any]) -> list[dict[str, Any]]:
    facts = report["facts"]
    rows = wave_readiness_rows(report, report["migration_waves"], facts.get("rate_windows"))
    stored = [{k: w[k] for k in rows[0]} for w in facts["waves"]]
    assert stored == rows
    brief = facts["executive_brief"]
    assert brief["held_waves"] == [r["wave"] for r in rows if r["readiness"] == "held"]
    assert brief["decision_wave"] == next(
        (r["wave"] for r in rows if r["readiness"] == "suggested"), None
    )
    assert facts["wave_readiness_notes"] == [
        {"wave": r["wave"], "readiness": r["readiness"], "line": r["readiness_line"]}
        for r in rows
        if r["readiness"] != "suggested"
    ]
    return rows


# A replayed or headless job may predate #1992's facts: the deterministic run checks it.
@pytest.mark.deterministic
def test_the_facts_state_each_waves_readiness_from_the_report(run: PipelineResult) -> None:
    rows = _check_facts(_report(run))
    held = {r["wave"]: [h["check"] for h in r["holds"]] for r in rows if r["readiness"] == "held"}
    if run.db in KNOWN_HELD:
        assert held == KNOWN_HELD[run.db]
        # No model, no design: nothing is suggested.
        assert {r["readiness"] for r in rows} <= {"held", "not_designed"}


def _check_deliverables(run: PipelineResult) -> None:
    report = _report(run)
    rows = report["facts"]["waves"]
    md = md_plain(run.deliverable("engineering-report.md").read_text())
    pdf = " ".join(" ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))).split())
    chat = md_plain(
        render_final_summary(
            report, database_name=run.db, job_id=run.job_id, deliverables_dir="d/", deliverables=[]
        )
    )
    decision = next(p for p in summary_paragraphs(report)[0] if p.startswith("Decision:"))
    for row in rows:
        line = row["readiness_line"]
        # Once per wave that states it (waves in the same state can share a line).
        times = sum(1 for r in rows if r["readiness_line"] == line)
        assert md.count(f"- Readiness: {line}") == times, row["wave"]
        assert pdf.count(" ".join(f"Readiness: {line}".split())) == times, row["wave"]
        n = row["wave"]
        if row["readiness"] == "suggested":
            assert f"- Wave {n}: " not in chat
        else:
            assert f"- Wave {n}: {line}" in chat
            assert f"approve wave {n}." not in decision.lower()


@pytest.mark.deterministic
def test_every_deliverable_states_the_same_readiness(run: PipelineResult) -> None:
    _check_deliverables(run)


@pytest.mark.deterministic
def test_the_designed_job_states_its_readiness(designed_run: PipelineResult) -> None:
    """With designs (and no reviews) the rule runs on real design facts too."""
    _check_facts(_report(designed_run))
    _check_deliverables(designed_run)


@pytest.mark.deterministic
def test_no_redesign_round_runs_without_a_model(run: PipelineResult) -> None:
    """Without a model no wave is designed, so nothing goes back to a design: the
    redesign step /modernize runs after synthesis prints complete, with 0 rounds."""
    import subprocess  # nosec B404 -- this repo's own script, fixed argv
    import sys

    from tests.e2e.pipeline import REPO

    proc = subprocess.run(  # nosec B603
        [
            sys.executable,
            "scripts/run_wave_redesign.py",
            "--job-id",
            run.job_id,
            "--db",
            run.db,
            "--artifact-root",
            str(run.artifact_root),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "status": "complete",
        "rounds": 0,
        "max_rounds": 2,
    }


def _script(root: Path, db: str, job: str, *extra: str) -> dict[str, Any]:
    import subprocess  # nosec B404 -- this repo's own script, fixed argv
    import sys

    from tests.e2e.pipeline import REPO

    proc = subprocess.run(  # nosec B603
        [
            sys.executable,
            "scripts/run_wave_redesign.py",
            "--job-id",
            job,
            "--db",
            db,
            "--artifact-root",
            str(root),
            *extra,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return out


@pytest.mark.deterministic
def test_one_redesign_round_end_to_end_with_a_stub_designer(
    designed_run: PipelineResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One full round on the designed job: its DynamoDB design marks an assigned query
    unsupported, so the wave is held; the round sends the engine back with the failed
    check in its design input (every mode's: the external input, the split's part
    inputs and the in-process designer's revision context), a stub designer serves the
    query, its review approves it, synthesis runs again and the wave is suggested. The
    assignment never moves, ``rounds.json`` and ``report.json`` agree, and every
    deliverable states the same line."""
    import shutil

    from src.agents.schema_design import handler
    from src.agents.schema_design.design_trace import write_design_trace
    from src.storage.assignment_versioning import resolve_downstream_assignment_version
    from src.storage.local_store import LocalArtifactStore
    from tests.e2e.pipeline import _run

    db, job = designed_run.db, designed_run.job_id
    root = tmp_path / "artifacts"
    shutil.copytree(designed_run.job_dir(), root / db / job)
    store = LocalArtifactStore(base_dir=str(root))
    version = resolve_downstream_assignment_version(store, db, job)
    base = f"{db}/{job}/schema-dynamodb/v{version}"
    assignment = store.read_json(f"{db}/{job}/assignment/v{version}/assignment.json")
    engines_before = {
        q["query_id"]: q.get("assigned_engine") for q in assignment["query_assignments"]
    }
    qid = next(q for q, e in engines_before.items() if e == "dynamodb")
    served = store.read_json(f"{base}/schema_output.json")
    held = {
        **served,
        "unsupported_patterns": [
            *(served.get("unsupported_patterns") or []),
            {"query_ids": [qid], "pattern_type": "aggregation", "recommendation": "no key"},
        ],
    }
    store.write_json(f"{base}/schema_output.json", held)
    approved = {"trace": [{"type": "pe_review", "iteration": 0, "verdict": "APPROVED"}]}
    write_design_trace(store, f"{base}/design_trace.json", approved)
    common = ["--job-id", job, "--db", db, "--artifact-root", str(root)]
    result = PipelineResult(db=db, job_id=job, artifact_root=root)
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)

    def latest() -> dict[str, Any]:
        from src.agents.schema_design.wave_redesign import latest_report

        return latest_report(store, db, job) or {}

    def dynamodb_row() -> dict[str, Any]:
        return next(w for w in latest()["facts"]["waves"] if "dynamodb" in w["engines"])

    assert dynamodb_row()["readiness"] == "held"
    assert [h["check"] for h in dynamodb_row()["holds"]] == ["unsupported_queries"]

    status = _script(root, db, job)
    assert status["status"] == "awaiting_redesign" and status["engines"] == ["dynamodb"]
    assert not store.exists(f"{base}/schema_output.json")
    # The external design input carries the failed check, as do the split's part inputs.
    given = handler.prepare_schema_design_input(job, db, "dynamodb", store, version)
    assert given["redesign_request"]["checks"][0]["check"] == "unsupported_queries"
    _run(
        "split",
        ["scripts/run_schema_design.py", *common, "--engine", "dynamodb", "--split"],
        result,
    )
    parts = store.read_json(f"{base}/groups_manifest.json")["groups"]
    for part in parts:
        assert "redesign_request" in store.read_json(f"{base}/{part['input_file']}")
    # The in-process designer is told the same checks: a stub serves the query.
    told: list[str] = []

    def stub(target_type: str, **paths: Any) -> tuple[str, str]:
        told.append(
            json.loads(Path(paths["revision_context_path"]).read_text())[
                "failed_checks_instructions"
            ]
        )
        return json.dumps(served), json.dumps(approved)

    monkeypatch.setattr(handler, "_dispatch_schema_agent", stub)
    handler.run_schema_design(job, db, "dynamodb", store, assignment_version=version)
    assert told and "marks 1 query it is assigned as unsupported" in told[0]

    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    assert _script(root, db, job)["status"] == "complete"
    rounds = store.read_json(f"{db}/{job}/wave-redesign/v{version}/rounds.json")
    assert [r["outcome"] for r in rounds["rounds"]] == [{"dynamodb": "fixed"}]
    from src.contracts.synthesis_output import WaveRedesignLog

    assert latest()["wave_redesign"] == WaveRedesignLog.model_validate(rounds).model_dump(
        mode="json"
    )
    assert rounds["rounds"][0]["requests"][0]["checks"][0]["query_ids"] == [qid]
    line = dynamodb_row()["readiness_line"]
    assert line == "Suggested: no check holds it after it was redesigned once."
    # Routing never moved: no new assignment version, the same engine per query.
    assert resolve_downstream_assignment_version(store, db, job) == version
    after = store.read_json(f"{db}/{job}/assignment/v{version}/assignment.json")
    assert {q["query_id"]: q.get("assigned_engine") for q in after["query_assignments"]} == (
        engines_before
    )
    assert not store.exists(f"{db}/{job}/assignment/v{version + 1}/assignment.json")
    # Every deliverable states the same line.
    _run("report", ["scripts/run_report.py", *common], result)
    md = md_plain(result.deliverable("engineering-report.md").read_text())
    pdf = " ".join(
        " ".join(pdf_page_texts(str(result.deliverable("executive-report.pdf")))).split()
    )
    assert f"- Readiness: {line}" in md
    assert " ".join(f"Readiness: {line}".split()) in pdf
