# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1938: a whole run turns the routine-migration artifact into risks and report facts.

Each run is the real ``run_assessment.py --all -y --llm-mode none`` on the PostgreSQL and
MySQL routine collections of #1934, so synthesis reads the artifact the run wrote. The
report's ``facts.routines`` counts the artifact's routines, the register carries the
``routines.*`` risks, nothing of the routines' text reaches report.json, and the
routines change nothing routing decided (ADR-034 Decision 8).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.contracts.synthesis_output import SynthesisOutputContract
from src.shared.report_facts import wave_readiness_rows
from src.storage.local_store import LocalArtifactStore
from tests.shared_runs import cached_run, copy_run
from tests.unit.agents.routine_collections import SEED
from tests.unit.scripts.test_run_assessment_routine_migration import (
    MARKER,
    _artifact,
    _assess,
    _job,
    _mysql_collection,
    _pg_collection,
)


@pytest.fixture(scope="module")
def pg_full(tmp_path_factory):
    root = cached_run(tmp_path_factory, "routine-risks-pg-1938",
                      lambda r: _assess(r, _pg_collection(), "--all", "-y", "--llm-mode", "none"))  # fmt: skip
    return _job(root)[0]


@pytest.fixture(scope="module")
def mysql_full(tmp_path_factory):
    root = cached_run(tmp_path_factory, "routine-risks-mysql-1938",
                      lambda r: _assess(r, _mysql_collection(), "--all", "-y", "--llm-mode", "none"))  # fmt: skip
    return _job(root)[0]


def _report(job: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads((job / "synthesis" / "v1" / "report.json").read_text())
    return report


@pytest.mark.parametrize("run", ["pg_full", "mysql_full"])
def test_the_report_counts_the_artifacts_routines(run, request) -> None:
    job = request.getfixturevalue(run)
    artifact = _artifact(job)
    report = _report(job)
    SynthesisOutputContract.model_validate(report)
    facts = report["facts"]["routines"]
    assert facts["assignment_version"] == 1 and facts["routine_count"] == artifact["total"]
    assert facts["as_is_count"] == artifact["counts"]["as_is"]
    assert facts["rewritten_count"] == artifact["counts"]["rewritten"]
    not_carried = sum(
        facts[k] for k in ("needs_repair_count", "not_exact_count", "not_checked_count",
                           "opted_out_count", "not_readable_count")
    )  # fmt: skip
    assert not_carried == artifact["counts"]["needs_attention"]
    assert facts["proof"] == "checked, not load-tested"


def _routine_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        r
        for r in report["risk_assessment"]["risks"]
        if str(r.get("rule") or "").startswith("routines.")
    ]


@pytest.mark.parametrize("run", ["pg_full", "mysql_full"])
def test_the_register_carries_the_routine_risks_in_wave_1(run, request) -> None:
    report = _report(request.getfixturevalue(run))
    risks = _routine_risks(report)
    assert risks[0]["rule"] == "routines.migrate_yourself" and risks[0]["severity"] == "HIGH"
    assert risks[-1]["rule"] == "routines.carried" and risks[-1]["severity"] == "LOW"
    assert {r["wave"] for r in risks} == {1}
    assert report["facts"]["open_risk_count"] == len(report["risk_assessment"]["risks"])
    gate = report["migration_waves"][0]["gate"]
    to_do = risks[0]
    # The to-do is listed as a to-do, never as a condition the gate closes on.
    assert f"To do before wave 1: {to_do['to_do']} ({to_do['risk_id']})." in gate
    assert not re.search(rf"Close [^.]*{to_do['risk_id']}", gate)


def test_mysql_wave_1_lists_the_to_do_and_the_routines_hold_it_not(mysql_full) -> None:
    """Maintainer decision on #1938: the to-do never holds its wave. In ``--llm-mode none``
    wave 1 has no Aurora MySQL design, so it is not designed; with the design it would
    have, the same report's wave 1 is suggested, and both lines list the to-do."""
    report = _report(mysql_full)
    to_do = report["facts"]["routines"]["migrate_yourself_line"]
    assert to_do == (
        "we could not translate 5 routines automatically; migrate them yourself before wave 1"
    )
    wave_1 = report["facts"]["waves"][0]
    assert wave_1["readiness"] == "not_designed" and wave_1["holds"] == []
    assert wave_1["to_dos"] == [to_do]
    assert wave_1["readiness_line_short"].endswith(f"To do: {to_do}.")
    designed = copy.deepcopy(report)
    designed["schema_designs"]["aurora_mysql"] = {"status": "completed"}
    row = wave_readiness_rows(designed, designed["migration_waves"])[0]
    assert (row["readiness"], row["holds"]) == ("suggested", [])
    assert row["readiness_line"] == f"Suggested: no check holds it. To do: {to_do}."


def test_mysql_routines_are_named_in_the_details_only(mysql_full) -> None:
    to_do, carried = _routine_risks(_report(mysql_full))
    for line in (
        "procedure shop.set_cap: mysql-routine-set-global",
        "procedure shop.p1: dependency_cycle, depends_on_needs_attention",
        "procedure shop.note: rebuilt around a literal (source_text_not_exact)",
        "procedure shop.secret_proc: not readable (source_text_not_collected)",
    ):
        assert line in to_do["details"]
    assert carried.get("details") is None
    for r in (to_do, carried):
        for name in ("set_cap", "p1", "p2", "note", "secret_proc", "place_order", "orders"):
            assert not re.search(rf"\b{name}\b", r["description"] + r["mitigation"])


@pytest.mark.parametrize("run", ["pg_full", "mysql_full"])
def test_no_routine_text_or_literal_reaches_the_report(run, request) -> None:
    text = (request.getfixturevalue(run) / "synthesis" / "v1" / "report.json").read_text()
    # The report states the routines it holds no text of.
    assert json.loads(text)["facts"]["routines"]["routines"]
    assert MARKER not in text and SEED not in text


@pytest.mark.parametrize("run", ["pg_full", "mysql_full"])
def test_routines_change_nothing_routing_decided(run, request, tmp_path) -> None:
    """ADR-034 Decision 8: the same job synthesized without its routine-migration
    artifact has the same engines, waves, tables and query groups."""
    src = request.getfixturevalue(run)
    job = copy_run(src.parents[2], tmp_path / "copy") / "a" / "shop" / src.name
    assignment = sorted(p.read_bytes() for p in (job / "assignment").rglob("*.json"))
    store = LocalArtifactStore(base_dir=str(job.parents[1]))
    with_routines = run_synthesis_deterministic(job.name, "shop", store, 1)
    shutil.rmtree(job / "routines")
    without = run_synthesis_deterministic(job.name, "shop", store, 1)
    assert with_routines["facts"]["routines"] is not None
    assert without["facts"]["routines"] is None
    assert not _routine_risks(without)

    def routing(result: dict[str, Any]) -> Any:
        return (
            [(r["target"], r.get("role"), r.get("routed_confidence")) for r in result["ranking"]],
            [(w.get("wave"), w.get("engines"), w.get("tables")) for w in result["migration_waves"]],
            result["table_mappings"],
            [(g.get("engine"), sorted(q["query_id"] for q in g.get("source_queries") or []))
             for g in result["query_groups"]],
        )  # fmt: skip

    assert routing(with_routines) == routing(without)
    assert sorted(p.read_bytes() for p in (job / "assignment").rglob("*.json")) == assignment
