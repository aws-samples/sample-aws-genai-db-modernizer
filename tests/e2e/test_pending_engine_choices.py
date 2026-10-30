# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pending engine choices per table, and ``--auto``'s "decide for me", end to end (#1003).

On discourse (the sample with open ties), the deliverables present one choice
per table, and every surface reads the same choices. On a copy of the job,
``scripts/run_assessment.py --choose`` settles one table and ``--decide-for-me``
the rest, as the approval gate does under ``--auto``; synthesis and the reports
then mark each decision "decided automatically, review if needed".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from tests.e2e.deliverable_figures import pdf_page_texts
from tests.e2e.pipeline import REPO, PipelineResult, _env

JOB = "choices"


def _script(cwd: Path, *args: str) -> list[dict]:
    env = {**_env(), "PYTHONPATH": str(REPO)}
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, str(REPO / args[0]), *args[1:]],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    lines = [json.loads(x) for x in proc.stdout.strip().splitlines() if x.startswith("{")]
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return lines


def _latest(job: Path) -> dict:
    versions = sorted(
        job.glob("assignment/v*/assignment.json"), key=lambda p: int(p.parent.name[1:])
    )
    latest: dict = json.loads(versions[-1].read_text())
    return latest


@pytest.fixture(scope="module")
def plain(discourse_run: PipelineResult) -> dict:
    report: dict = json.loads(discourse_run.report_json().read_text())
    return report


@pytest.fixture(scope="module")
def decided(discourse_run: PipelineResult, tmp_path_factory: pytest.TempPathFactory) -> dict:
    cwd = Path(tmp_path_factory.mktemp("choices"))
    root = cwd / "artifacts"
    shutil.copytree(discourse_run.job_dir(), root / "discourse" / JOB)
    job = root / "discourse" / JOB
    common = ["--job-id", JOB, "--db", "discourse", "--artifact-root", str(root)]
    before = _latest(job)
    chose = _script(
        cwd, "scripts/run_assessment.py", *common, "--choose", "theme_fields=documentdb"
    )
    after_pick = _latest(job)
    rest = _script(cwd, "scripts/run_assessment.py", *common, "--decide-for-me")
    after_auto = _latest(job)
    _script(cwd, "scripts/run_synthesis.py", *common, "--llm-mode", "none")
    report_step = _script(cwd, "scripts/run_report.py", *common)[-1]
    report = json.loads((root / report_step["report"]).read_text())
    files = {
        Path(p).name: Path(p) if os.path.isabs(p) else cwd / p for p in report_step["deliverables"]
    }
    return {
        "before": before,
        "after_pick": after_pick,
        "after_auto": after_auto,
        "chose": next(x for x in chose if x.get("phase") == "engine_choices"),
        "rest": next(x for x in rest if x.get("phase") == "engine_choices"),
        "report": report,
        "md": files["engineering-report.md"].read_text(),
        "pdf": " ".join(pdf_page_texts(str(files["executive-report.pdf"]))),
        "state_written": (cwd / ".modernizer-state.json").exists(),
    }


@pytest.mark.deterministic
def test_every_open_tie_is_in_exactly_one_choice_per_table(plain: dict) -> None:
    choices = plain["pending_engine_choices"]
    tied = {t["query_id"]: t for t in plain["tied_queries"]}
    listed = [q for c in choices for q in c["query_ids"]]
    assert sorted(listed) == sorted(tied) and len(listed) == len(set(listed))
    for c in choices:
        assert {tuple(tied[q]["tied_engines"]) for q in c["query_ids"]} == {
            tuple(c["tied_engines"])
        }
        for e in c["options"]:
            # The unrounded score is the same score before the fractions are dropped.
            assert e["score"] <= e["unrounded_score"] < e["score"] + 2, (c["choice_id"], e)
    facts = plain["facts"]
    assert facts["pending_choice_count"] == len(choices) < facts["tied_query_count"]
    assert facts["pending_choice_query_count"] == facts["tied_query_count"] == len(tied)
    # The issue's finding: level at whole points, apart before rounding, and that
    # difference is what decides those choices.
    apart = [c for c in choices if len({e["unrounded_score"] for e in c["options"]}) > 1]
    assert apart
    for c in apart:
        best = max(c["options"], key=lambda e: e["unrounded_score"])
        assert (c["decide_for_me_engine"], c["decide_for_me_rule"]) == (
            best["engine"],
            "unrounded_score",
        )
    # Fewer decisions than queries: the ties sit on a few tables (#1003).
    assert {c["table"] for c in choices} >= {"discourse.theme_fields", "discourse.incoming_emails"}


@pytest.mark.deterministic
def test_the_deliverables_present_one_choice_per_table(
    discourse_run: PipelineResult, plain: dict
) -> None:
    n, q = plain["facts"]["pending_choice_count"], plain["facts"]["pending_choice_query_count"]
    md = discourse_run.deliverable("engineering-report.md").read_text()
    assert f"## Pending engine choices ({n} tables, {q} queries)" in md
    assert "## Unresolved engine ties" not in md
    for c in plain["pending_engine_choices"]:
        table = c["table"].rsplit(".", 1)[-1]
        assert re.search(rf"^### `{re.escape(table)}` \(", md, re.M), table
    pdf = " ".join(pdf_page_texts(str(discourse_run.deliverable("executive-report.pdf"))))
    assert f"Pending engine choices ({n} tables, {q} queries)" in pdf


@pytest.mark.deterministic
def test_a_table_pick_settles_that_table_only(decided: dict) -> None:
    pick = decided["chose"]
    assert pick["picked"] == {"discourse.theme_fields": "documentdb"} and pick["decided"] == []
    qas = {qa["query_id"]: qa for qa in decided["after_pick"]["query_assignments"]}
    was_tied = {
        qa["query_id"] for qa in decided["before"]["query_assignments"] if qa["tied_engines"]
    }
    theme = [
        qa
        for qid, qa in qas.items()
        if qid in was_tied and qa["source_tables"] == ["discourse.theme_fields"]
    ]
    assert theme and {qa["assigned_engine"] for qa in theme} == {"documentdb"}
    assert all(qa["customer_override"] and not qa["tied_engines"] for qa in theme)
    remaining = {c["choice"] for c in pick["pending_choices"]}
    assert "discourse.theme_fields" not in remaining and remaining
    assert decided["after_pick"]["auto_decided_choices"] == []
    assert (decided["after_pick"]["status"], decided["after_pick"]["source"]) == (
        "customer_modified",
        "customer_gate",
    )
    # The run's state file belongs to no job here, so it is never written.
    assert decided["state_written"] is False


@pytest.mark.deterministic
def test_decide_for_me_settles_every_other_choice_by_the_documented_rule(
    decided: dict, plain: dict
) -> None:
    rest = decided["rest"]
    assert rest["pending_choices"] == []
    by_choice = {c["choice_id"]: c for c in plain["pending_engine_choices"]}
    expected = {cid: c["decide_for_me_engine"] for cid, c in by_choice.items()}
    expected.pop("discourse.theme_fields")
    assert {d["choice"]: d["engine"] for d in rest["decided"]} == expected
    after = decided["after_auto"]
    # Decided by the rule: the version is not a customer edit (#1003).
    assert (after["status"], after["source"]) == ("auto_decided", "auto_decided")
    assert not [qa for qa in after["query_assignments"] if qa["tied_engines"]]
    recorded = {d["choice_id"]: d for d in after["auto_decided_choices"]}
    assert set(recorded) == set(expected)
    for cid, d in recorded.items():
        assert d["engine"] == expected[cid]
        assert set(d["unrounded_scores"]) == set(by_choice[cid]["tied_engines"])
        assert d["rule"] == by_choice[cid]["decide_for_me_rule"]
    moves = after["moves"]
    assert moves and {(m["pass"], m["reason_code"], m["actor"]) for m in moves} <= {
        ("auto_pick", "tie.decided_for_you", "rule"),
        ("cache_owner_normalize", "cache.legacy_owner", "rule"),
    }
    for m in (m for m in moves if m["pass"] == "auto_pick"):
        assert m["runner_up"]["engine"] in by_choice[m["evidence"]["choice_id"]]["tied_engines"]
        assert len(m["evidence"]["choice_unrounded_scores"]) == 2


@pytest.mark.deterministic
def test_the_deliverables_mark_each_decision_for_review(decided: dict) -> None:
    report = decided["report"]
    facts = report["facts"]
    n = len(decided["after_auto"]["auto_decided_choices"])
    q = sum(len(d["query_ids"]) for d in decided["after_auto"]["auto_decided_choices"])
    assert report["pending_engine_choices"] == [] and report["tied_queries"] == []
    assert facts["auto_decided_choice_count"] == n and facts["auto_decided_query_count"] == q
    heading = f"Decided automatically, review if needed ({n} tables, {q} queries)"
    assert f"## {heading}" in decided["md"]
    assert heading in decided["pdf"]
    assert "Pending engine choices" not in decided["md"] + decided["pdf"]
    # The table the customer picked is theirs, not an automatic decision.
    assert "`theme_fields`" not in decided["md"].split(f"## {heading}")[1].split("\n## ")[0]


@pytest.mark.deterministic
def test_the_decided_artifacts_have_no_internal_wording(decided: dict) -> None:
    """The decisions' reasons and the auto_pick assignment reasons are customer
    text: no engine id, rule code or internal reference (#572's guard)."""
    from src.tools.validation.customer_text_guard import scan_json

    for name, artifact in (
        ("report.json", decided["report"]),
        ("assignment", decided["after_auto"]),
    ):
        findings = scan_json(artifact)
        assert findings == [], (name, [(f.kind, f.path, f.excerpt) for f in findings])


@pytest.mark.deterministic
def test_every_deliverable_figure_agrees_after_the_decisions(decided: dict) -> None:
    """The figures harness (#1003 review): the decided counts in the engineering
    report and the PDF are the stored ones, with every other figure."""
    from tests.e2e.deliverable_figures import engineering_figures, pdf_figures
    from tests.e2e.test_deliverable_figures import (
        _engineering_required,
        _pdf_required,
        _problems,
    )

    report = decided["report"]
    assert _problems(report, engineering_figures(decided["md"]), _engineering_required) == []
    assert _problems(report, pdf_figures(decided["pdf"]), _pdf_required) == []
