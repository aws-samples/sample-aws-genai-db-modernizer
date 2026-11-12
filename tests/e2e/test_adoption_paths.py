# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The adoption paths on every sample's real run (#1130, epic #1128).

Synthesis computes ``facts.adoption`` from ``facts.slice_plan``: ``relational_only``,
``through_step_<k>`` and ``full_target`` (the default), the paths the relational engine's
gates keep from being offered, and the one-step form. Here every contract validator
passes on each sample, the full target agrees with the headline figures and the waves,
the relational move only agrees with ``facts.no_migration``, every path is a projection of
the assignment, and paths whose figures do not add up to the plan are refused.

``replay_stale``: the saved replay fixtures' report.json predates the computed paths.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.agents.referee.write_groups import assignment_sources_of_truth
from src.contracts.synthesis_output import AdoptionFacts, ReportFacts
from src.shared.cache_engines import CACHE_ENGINES
from src.tools.validation.customer_text_guard import scan_json
from tests.e2e.pipeline import PipelineResult

MOVING_STEPS = {"slice", "engine_wave"}

# replay_stale: the saved replay fixtures' report.json was synthesized before the paths
# were computed (#1130), so its adoption block is empty; a fresh e2e-llm run and the
# deterministic runs compute it. Drop the marker when the fixtures are refreshed.
pytestmark = pytest.mark.replay_stale


def _report(run: PipelineResult) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(Path(run.report_json()).read_text(encoding="utf-8"))
    return out


def _assignment(run: PipelineResult, report: dict[str, Any]) -> dict[str, Any]:
    version = report["assignment_summary"]["version"]
    path = run.job_dir() / "assignment" / f"v{version}" / "assignment.json"
    out: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return out


def _paths(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["path_id"]: p for p in report["facts"]["adoption"]["paths"]}


def _cross_engine(report: dict[str, Any]) -> bool:
    return bool(report["migration_waves"][0].get("homogeneity") == "cross_engine")


def test_the_adoption_paths_pass_every_contract_validator(run: PipelineResult) -> None:
    report = _report(run)
    adoption = report["facts"]["adoption"]
    assert AdoptionFacts.model_validate(adoption).model_dump(mode="json") == adoption
    # The cross-checks against the slice plan and the report's own figures.
    ReportFacts.model_validate(report["facts"])
    assert scan_json(adoption, "$.facts.adoption") == []
    steps = report["facts"]["slice_plan"]["plan_steps"]
    ids = [p["path_id"] for p in adoption["paths"]]
    lead = next(i for i, s in enumerate(steps) if s["kind"] in MOVING_STEPS)
    expected = [
        "relational_only",
        *(f"through_step_{k}" for k in range(lead + 1, len(steps))),
        "full_target",
    ]
    assert ids == expected, run.db
    assert adoption["default_path_id"] == "full_target"
    assert adoption["not_offered"] == [], run.db
    # The capped view (#1133): every path when they fit, else the ends and three steps.
    shown = adoption["summary_path_ids"]
    assert len(shown) == min(len(ids), 5) and shown[0] == ids[0] and shown[-1] == ids[-1]
    assert adoption["cost_basis"] == "full_target_estimates", run.db


def test_the_full_target_agrees_with_the_headline_and_the_waves(run: PipelineResult) -> None:
    report = _report(run)
    facts = report["facts"]
    full = _paths(report)["full_target"]
    assert full["monthly_cost_usd"] == facts["projected_monthly_cost_usd"], run.db
    assert set(full["engines"]) == {e["engine"] for e in facts["engines"]}, run.db
    # A cross-engine source's move to Aurora is itself a migration (wave 1's queries).
    wave_one = report["migration_waves"][0]["query_count"] if _cross_engine(report) else 0
    assert full["queries_moved"] == facts["moving_query_count"] - wave_one, run.db
    moving_waves = [
        (w, row)
        for w, row in zip(report["migration_waves"], facts["waves"], strict=True)
        if row["migrating"] and w["wave"] != 1
    ]
    assert full["queries_moved"] == sum(w["query_count"] for w, _ in moving_waves), run.db
    # The tables the waves move off Aurora, less those nobody writes: the wave builder
    # still gives such a table to its plurality reader (#2166), the plan to its writer.
    written = assignment_sources_of_truth(_assignment(run, report))
    wave_tables = set()
    for wave, row in moving_waves:
        kept = {
            o["table"] for o in wave.get("table_owners") or [] if o["owner"] not in wave["engines"]
        }
        assert len(set(wave["tables"]) - kept) == row["moving_tables"], (run.db, wave["wave"])
        wave_tables |= set(wave["tables"]) - kept
    assert full["tables_migrate"] == len(wave_tables & set(written)), run.db


def test_the_relational_move_only_agrees_with_no_migration(run: PipelineResult) -> None:
    report = _report(run)
    facts = report["facts"]
    path = _paths(report)["relational_only"]
    relational = facts["slice_plan"]["plan_steps"][0]["engine"]
    expected = set(facts["no_migration"]["engines"])
    if _cross_engine(report):
        assert relational not in expected
        expected.add(relational)
    assert set(path["engines"]) == expected, run.db
    assert path["queries_moved"] == 0 and path["tables_migrate"] == 0
    assert path["queries_by_engine"] == {relational: facts["in_scope_query_count"]}
    assert path["cost_delta_vs_relational_usd"] == 0.0
    assert path["first_gate"].endswith("The cutover is your responsibility.")


def test_every_path_is_a_projection_of_the_assignment(run: PipelineResult) -> None:
    """A query keeps its engine when the path takes the step that moves it, and stays on
    the relational engine otherwise; the full target is the assignment itself."""
    report = _report(run)
    plan = report["facts"]["slice_plan"]
    relational = plan["plan_steps"][0]["engine"]
    step_of = {}
    units = {u["key"]: u for u in plan["move_units"]}
    for step in plan["plan_steps"]:
        for stage in step["step_stages"]:
            for key in stage["unit_keys"]:
                if units[key]["kind"] in ("owner", "replica_reads", "read_model"):
                    step_of.update(dict.fromkeys(units[key]["query_ids"], step["step_number"]))
    engine_of = {
        qa["query_id"]: qa["assigned_engine"]
        for qa in _assignment(run, report)["query_assignments"]
        if qa.get("in_scope", True)
        and qa.get("assigned_engine") not in (None, "unservable")
        and qa["assigned_engine"] not in CACHE_ENGINES
    }
    for path in report["facts"]["adoption"]["paths"]:
        projected: dict[str, int] = {}
        for qid, engine in engine_of.items():
            kept = qid in step_of and step_of[qid] <= path["stops_after_step"]
            on = engine if kept else relational
            projected[on] = projected.get(on, 0) + 1
        assert path["queries_by_engine"] == projected, (run.db, path["path_id"])
    full = _paths(report)["full_target"]
    assert full["queries_by_engine"] == {
        e: sum(1 for x in engine_of.values() if x == e) for e in set(engine_of.values())
    }


def test_the_one_step_form_states_the_plan_s_shape(run: PipelineResult) -> None:
    report = _report(run)
    plan = report["facts"]["slice_plan"]
    one = report["facts"]["adoption"]["one_step"]
    full = _paths(report)["full_target"]
    assert one["staged_step_count"] == plan["step_count"] == full["step_count"]
    assert one["staged_stage_count"] == one["gate_count"] == full["stage_count"]
    assert one["staged_temporary_stream_count"] == full["temporary_stream_count"]
    checks = {c["rule"]: c["observed"] for c in one["one_step_checks"]}
    assert checks["no_open_critical_high_risk"] == sum(
        report["facts"]["critical_high_risks_by_engine"].values()
    )
    moving = {s["engine"] for s in plan["plan_steps"] if s["kind"] in MOVING_STEPS}
    assert checks["at_most_one_purpose_built_target"] == len(moving)
    # Every sample has a stage c (a reverse replica), so none suits one step.
    assert checks["no_reverse_replica_stage"] >= 1 and one["suits_one_step"] is False


def _refused(facts: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ReportFacts.model_validate(facts)


def test_paths_that_do_not_add_up_to_the_plan_are_refused(run: PipelineResult) -> None:
    facts = _report(run)["facts"]
    full = len(facts["adoption"]["paths"]) - 1
    # A query counted on the wrong engine: the total still adds up, the split does not.
    broken = copy.deepcopy(facts)
    by_engine = broken["adoption"]["paths"][full]["queries_by_engine"]
    relational, other = list(by_engine)[:2]
    by_engine[relational] -= 1
    by_engine[other] += 1
    _refused(broken, "queries_by_engine")
    # One table too many, a dollar off, a reverse stream left out.
    for field, change in (
        ("tables_migrate", 1),
        ("monthly_cost_usd", 1.0),
        ("temporary_stream_count", -1),
    ):
        broken = copy.deepcopy(facts)
        broken["adoption"]["paths"][full][field] += change
        _refused(broken, field)
    # A path that stops one step early but keeps the full target's id.
    broken = copy.deepcopy(facts)
    broken["adoption"]["paths"] = broken["adoption"]["paths"][:-1]
    broken["adoption"]["paths"][-1]["path_id"] = "full_target"
    broken["adoption"]["path_count"] -= 1
    _refused(broken, "full_target takes every step")
    # The one-step form with a stage c the plan does not have.
    broken = copy.deepcopy(facts)
    check = next(
        c
        for c in broken["adoption"]["one_step"]["one_step_checks"]
        if c["rule"] == "no_reverse_replica_stage"
    )
    check["observed"] += 1
    _refused(broken, "one-step checks")
