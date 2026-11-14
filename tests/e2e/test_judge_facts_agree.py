# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every figure the rubric judge's facts block shares with the report's own
facts layer (``report.json`` ``facts``, ADR-032) is the same number (#952).

The judge is told its facts are the ground truth, so a judge fact that
recounts a figure differently (#581's wave-3 kept-in-sync count: 118 against
the report's 67 on discourse) would make it mark a correct deliverable as a
defect. Runs on every sample job, and on an external (e2e-llm) job.
"""

from __future__ import annotations

import json

from ci.llm import judge, judge_facts
from tests.e2e.pipeline import PipelineResult


def _facts(run: PipelineResult) -> tuple[dict, dict]:
    paths = judge.locate_deliverables(run.artifact_root, run.db, run.job_id)
    report = json.loads(paths["report_json"].read_text())  # type: ignore[union-attr]

    def optional(key: str) -> dict | None:
        path = paths.get(key)
        if path is None:
            return None
        data: dict = json.loads(path.read_text())
        return data

    built = judge_facts.build_facts(report, optional("llm_input"), optional("assignment"))
    return built, report["facts"]


def test_judge_facts_equal_the_reports_own_figures(run: PipelineResult) -> None:
    built, layer = _facts(run)
    totals = built["totals"]
    assert totals["open_risks"] == layer["open_risk_count"]
    assert totals["resolved_risks"] == layer["resolved_risk_count"]
    assert totals["migration_wave_count"] == layer["migration_wave_count"]
    assert totals["projected_monthly_cost_usd"] == layer["projected_monthly_cost_usd"]

    margins = built["routing_margins"]
    assert margins["close_call_query_count"] == layer["close_calls"]["close_call_query_count"]
    assert margins["settled_tie_count"] == layer["close_calls"]["settled_tie_count"]

    assert built["wave_figures"] == [
        {k: w.get(k) for k in judge_facts._WAVE_FIGURE_KEYS} for w in layer["waves"]
    ]

    # #2162: each migration target's Scope row figures, verbatim.
    assert built["scope_figures"] == [
        {k: e.get(k) for k in judge_facts._SCOPE_FIGURE_KEYS}
        for e in layer["engines"]
        if e.get("engine_source_tables") is not None
    ]

    risk = built["migration_risk_facts"]
    assert risk["close_call_query_count"] == layer["close_calls"]["close_call_query_count"]
    assert risk["unservable_queries"] == layer["unservable"]["unservable_query_count"]

    kept = {w["wave"]: w["kept_in_sync_tables"] for w in risk["kept_in_sync"]}
    expected = {
        w["wave"]: w["kept_in_sync_tables"]
        for w in layer["waves"]
        if w.get("kept_in_sync") and w.get("kept_in_sync_tables") is not None
    }
    assert kept == expected
    for wave in layer["waves"]:
        if wave.get("kept_in_sync"):
            listed = next(w for w in risk["kept_in_sync"] if w["wave"] == wave["wave"])
            assert len(listed["tables"]) == len(wave["kept_in_sync"])
    shared = {w["wave"]: w["entries"] for w in risk["shared_in_wave"]}
    assert shared == {
        w["wave"]: len(w["shared_in_wave"]) for w in layer["waves"] if w.get("shared_in_wave")
    }


def test_judge_adoption_facts_are_the_paths_the_report_shows(run: PipelineResult) -> None:
    """#1136: the paths shown, each with the report's own engines and queries off the
    relational engine, and its cost wherever the judge states one."""
    built, layer = _facts(run)
    adoption = layer.get("adoption")
    if adoption is None:  # a report written before facts.adoption
        assert built["adoption"] is None
        return
    block = built["adoption"]
    shown = adoption.get("summary_path_ids") or []  # absent from a block saved before #2172
    assert block["paths_shown"] == shown
    assert block["path_count"] == adoption.get("path_count")
    stated = {p["path_id"]: p for p in adoption["paths"]}
    assert [p["path_id"] for p in block["paths"]] == [
        p["path_id"] for p in adoption["paths"] if p["path_id"] in shown
    ]
    for path in block["paths"]:
        own = stated[path["path_id"]]
        assert path["engines"] == own["engines"]
        assert path["queries_off_relational"] == own["queries_moved"]
        assert path["monthly_cost_usd"] in (None, own["monthly_cost_usd"])
    assert (block["one_step"] is None) == (adoption.get("one_step") is None)
