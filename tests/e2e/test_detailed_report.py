"""The detailed engineering report on the bundled samples (#1364).

Every run writes ``engineering-report-detailed.md`` into its deliverables folder, listed
apart in ``index.md``: the full routing detail read from the job's saved assignments,
access-pattern record and analyses. These checks tie it to those artifacts: every query
of the effective assignment and every write group is there, and on discourse each
write-group member its engine serves poorly (#1362) is marked with its intent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.report.escaping import md_cell
from tests.e2e.pipeline import PipelineResult
from tests.e2e.test_carried_writes import EXPECTED as CARRIED

DETAILED = "engineering-report-detailed.md"


def _detailed(r: PipelineResult) -> str:
    return r.deliverable(DETAILED).read_text(encoding="utf-8")


def _effective_assignment(r: PipelineResult) -> dict[str, Any]:
    report = json.loads(r.report_json().read_text())
    version = int(report["assignment_summary"]["version"])
    path = r.job_dir() / "assignment" / f"v{version}" / "assignment.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_the_folder_index_lists_the_detailed_report_apart(run: PipelineResult) -> None:
    index = (run.job_dir() / "deliverables" / "index.md").read_text(encoding="utf-8")
    main, audit = index.split("## For auditing the routing")
    assert DETAILED not in main and f"`{DETAILED}`" in audit


def test_every_query_and_write_group_of_the_assignment_is_listed(run: PipelineResult) -> None:
    md = _detailed(run)
    assignment = _effective_assignment(run)
    qids = [str(qa["query_id"]) for qa in assignment["query_assignments"]]
    assert md.count("\n### Query `") == len(qids)
    for qid in qids:
        assert f"### Query `{qid}`" in md
    groups = [t for t, m in (assignment.get("write_groups") or {}).items() if len(set(m)) > 1]
    for table in groups:
        assert f"### Write group `{table}`" in md
    assert f"## Write groups ({len(groups)})" in md


@pytest.mark.deterministic
def test_a_fresh_run_has_every_routing_record(run: PipelineResult) -> None:
    """A run of this release saves every record the detailed report reads: it states no
    absence, and every query has a move history."""
    md = _detailed(run)
    assert "## Records not available" not in md
    assert "Move history:" in md and "| Engine | Routed score | Pattern |" in md


@pytest.mark.deterministic
def test_each_carried_write_is_marked_in_its_write_group(discourse_run: PipelineResult) -> None:
    """report.json keeps the #1362 records, and the detailed report marks each member under
    its write group with its intent (``design.carried_writes``, the same function)."""
    report = json.loads(discourse_run.report_json().read_text())
    carried = {c["query_id"]: c for c in report["carried_writes"]}
    assert {q: (c["table_id"], c["intent_name"]) for q, c in carried.items()} == CARRIED[
        "discourse"
    ]
    md = _detailed(discourse_run)
    for qid, c in carried.items():
        section = md.split(f"### Write group `{c['table_id']}`", 1)[1].split("\n### ", 1)[0]
        row = next(line for line in section.splitlines() if line.startswith(f"| `{qid}` |"))
        assert "| yes |" in row and md_cell(c["intent_name"]) in row


def _detail(r: PipelineResult) -> dict[str, Any]:
    from src.report.deliverables import _collector_output
    from src.shared.routing_detail import load_routing_detail
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(r.artifact_root))
    key = str(r.report["report"])
    return load_routing_detail(store, key, store.read_json(key), _collector_output(store, key))


@pytest.mark.deterministic
def test_the_penalty_is_the_one_routing_applied(run: PipelineResult) -> None:
    """The penalty column is the scorer's own anti-pattern map (the biggest penalty per
    query and engine, ADR-031 Decision 8), for every query and analysed engine."""
    from src.agents.referee.query_scoring import ScoringContext

    detail = _detail(run)
    root = run.job_dir()
    analyses = {
        e: json.loads((root / f"analysis-{e}" / "analysis.json").read_text())
        for e in detail["analysed_engines"]
    }
    ctx = ScoringContext.from_pipeline(
        analyses,
        json.loads((root / "referee-triage" / "triage.json").read_text()),
        json.loads((root / "collector" / "output.json").read_text()),
    )
    pairs = [(q["query_id"], d) for q in detail["queries"] for d in q["dimensions"]]
    assert any(len(d["intrinsic_findings"]) > 1 for _, d in pairs) or run.db != "discourse"
    for qid, d in pairs:
        assert d["intrinsic_penalty"] == ctx.anti_pattern_map.get((qid, d["engine"]), 0), (
            qid,
            d["engine"],
        )


@pytest.mark.deterministic
def test_close_calls_and_settled_ties_match_the_facts(run: PipelineResult) -> None:
    report = json.loads(run.report_json().read_text())
    close = report["facts"]["close_calls"]
    detail = _detail(run)
    assert (detail["close_call_count"], detail["settled_tie_count"]) == (
        close["close_call_query_count"],
        close["settled_tie_count"],
    )


@pytest.mark.deterministic
def test_poor_fits_are_the_carried_writes_and_no_placeholder_is_a_table(
    run: PipelineResult,
) -> None:
    """Every member marked as fitting poorly, in any group, is a carried write (the
    ``design.carried_writes`` risk's own statements), and every carried write is marked."""
    report = json.loads(run.report_json().read_text())
    detail = _detail(run)
    marked = {
        m["query_id"]
        for g in [*detail["write_groups"], *detail["co_dependency_groups"]]
        for m in g["members"]
        if m["poor_fit"]
    }
    assert marked == {c["query_id"] for c in report["carried_writes"]}
    assert "unknown" not in {t["table"] for t in detail["tables"]}
