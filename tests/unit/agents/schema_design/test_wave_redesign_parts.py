# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A redesign round redesigns and reviews only the DynamoDB parts it has to (#2119).

A round on a DynamoDB design in parts sets aside only the parts its failed checks name:
the parts holding a check's queries, the parts whose last review held the design (the
``open_review`` hold names them), and every part sharing a placement group with one of
those. The parts it keeps keep their draft and their recorded review: ``--split`` marks
them ``carried_from`` and no review is asked for them. A check that names no query, and
every other engine, keeps the whole engine. Maintainer decision on #2119 (2026-11-11):
"we need to redesign the flagged parts and skip the review of things that we already
reviewed, unless that is part of a co-dependency group".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.agents.schema_design import pe_review_external as pe
from src.agents.schema_design import wave_redesign as wr
from src.agents.schema_design.wave_redesign import (
    fail_round,
    plan_round,
    read_log,
    redesign_input,
    request_key,
)
from src.contracts.synthesis_output import WaveRedesignLog
from src.storage.local_store import LocalArtifactStore

# The post-schema reroute run (v2 -> v3, DynamoDB in parts), a module fixture.
from tests.unit.agents.schema_design.test_redesign_only_what_changed import run  # noqa: F401

DB, JOB, V = "shop", "job-1", 2
REPO = Path(__file__).resolve().parents[4]
BASE = f"{DB}/{JOB}/schema-dynamodb/v{V}/"
DESIGN = BASE + "schema_output.json"
REPORT = f"{DB}/{JOB}/synthesis/v{V}/report.json"

#: Three parts: part 1 holds q1 and a query pattern whose member is q1b.
PARTS = {0: ["q0"], 1: ["q1"], 2: ["q2"]}

APPROVED = {"source": "external", "verdict": "approved", "change_requests": []}
BLOCKED = {
    "source": "external",
    "verdict": "needs_revision",
    "change_requests": [
        {
            "severity": "blocker",
            "category": "key_design",
            "target": "Orders",
            "requested_change": "x",
        }
    ],
}


def _risk(query_ids: list[str]) -> dict[str, Any]:
    return {
        "risk_id": "RISK-001",
        "risk_type": "PERFORMANCE_DEGRADATION",
        "severity": "HIGH",
        "rule": "design.hot_partition",
        "description": "[dynamodb] Every order read lands on one key.",
        "mitigation": "Spread the orders over more key values.",
        "query_ids": query_ids,
    }


def _hold(check: str) -> dict[str, Any]:
    return {
        "check": check,
        "engine": "dynamodb",
        "risk_id": "RISK-001" if check == "open_high_risk" else None,
        "severity": "HIGH" if check == "open_high_risk" else None,
        "line": f"a {check} hold on dynamodb",
    }


def _report(
    holds: list[dict[str, Any]],
    *,
    query_ids: list[str] | None = None,
    reviewed: int = 3,
    open_reviews: int = 0,
) -> dict[str, Any]:
    return {
        "schema_designs": {
            "dynamodb": {
                "status": "completed",
                "reviewed_count": reviewed,
                "open_review_count": open_reviews,
                "failed_review_count": 0,
                "unsupported_query_count": 0,
            }
        },
        "risk_assessment": {"risks": [_risk(query_ids or [])]},
        "facts": {
            "waves": [
                {
                    "wave": 1,
                    "engines": ["dynamodb"],
                    "readiness": "held" if holds else "suggested",
                    "holds": holds,
                }
            ]
        },
    }


def _part_input(ids: list[str]) -> dict[str, Any]:
    first, *members = ids
    patterns: dict[str, Any] = {"query_patterns": [{"query_id": first}]}
    if members:
        # One input entry standing for a query pattern of several statements (#1602).
        patterns["query_pattern_groups"] = [{"query_ids": [first, *members]}]
    return {"group_index": 0, "collector_output": {"queries": patterns}}


def _job(
    store: LocalArtifactStore,
    parts: dict[int, list[str]] = PARTS,
    reviews: dict[int, dict[str, Any]] | None = None,
    co_dependency_groups: list[list[str]] | None = None,
) -> None:
    """A DynamoDB design in ``parts``, each with its draft and its recorded review."""
    store.write_json(
        BASE + "groups_manifest.json",
        {
            "groups": [
                {"group_index": i, "input_file": f"input_group_{i}.json", "primary_tables": []}
                for i in parts
            ]
        },
    )
    for i, ids in parts.items():
        store.write_json(BASE + f"input_group_{i}.json", _part_input(ids))
        store.write_json(BASE + f"schema_draft_group_{i}.json", {"part": i, "draft": "first"})
        review = (reviews or {}).get(i, APPROVED)
        store.write_json(BASE + f"design_trace_group_{i}.json", {"pe_review": review})
    store.write_json(DESIGN, {"design": "first"})
    store.write_json(
        f"{DB}/{JOB}/assignment/v{V}/assignment.json",
        {
            "query_assignments": [
                {"query_id": q, "assigned_engine": "dynamodb"}
                for ids in parts.values()
                for q in ids
            ],
            "co_dependency_groups": co_dependency_groups or [],
        },
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(REPORT, {"wave_redesign": None})
    monkeypatch.setattr(
        wr,
        "_write_report_log",
        lambda store, db, job_id, version, log: store.write_json(
            REPORT, {"wave_redesign": dict(log)}
        ),
    )
    return s


def _redesign(store: LocalArtifactStore, parts: list[int], *, review: bool = True) -> None:
    """The round's designer and reviewer: a new draft (and review) for ``parts``, merged."""
    for i in parts:
        store.write_json(BASE + f"schema_draft_group_{i}.json", {"part": i, "draft": "second"})
        if review:
            store.write_json(BASE + f"design_trace_group_{i}.json", {"pe_review": APPROVED})
    store.write_json(DESIGN, {"design": "second"})


# ---------------------------------------------------------------------------
# Which parts a round redesigns
# ---------------------------------------------------------------------------


def test_a_check_naming_a_query_in_one_part_redesigns_only_that_part(
    store: LocalArtifactStore,
) -> None:
    _job(store)
    status = plan_round(
        store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [1]}
    for i in (0, 2):
        assert store.read_json(BASE + f"schema_draft_group_{i}.json")["draft"] == "first"
        assert store.exists(BASE + f"design_trace_group_{i}.json")
    assert not store.exists(BASE + "schema_draft_group_1.json")
    assert not store.exists(BASE + "design_trace_group_1.json")
    request = store.read_json(request_key(DB, JOB, "dynamodb", V))
    assert request["parts"] == [1]
    assert [e["part"] for e in request["kept_parts"]] == [0, 2]


def test_a_check_naming_a_member_of_a_query_pattern_selects_its_part(
    store: LocalArtifactStore,
) -> None:
    """A part's input lists one entry per query pattern; its members are its queries too."""
    _job(store, parts={0: ["q0"], 1: ["q1", "q1b"], 2: ["q2"]})
    status = plan_round(
        store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1b"]), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [1]}


def test_a_review_hold_redesigns_only_the_part_its_review_held(
    store: LocalArtifactStore,
) -> None:
    _job(store, reviews={2: BLOCKED})
    status = plan_round(
        store, DB, JOB, _report([_hold("open_review")], open_reviews=1), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [2]}
    [check] = read_log(store, DB, JOB, V)["rounds"][0]["requests"][0]["checks"]
    # The hold names its part and that part's statements.
    assert check["parts"] == [2] and check["query_ids"] == ["q2"]
    given = redesign_input(store, DB, JOB, "dynamodb", V)
    assert given is not None and "Queries: q2" in given["instructions"]


def test_a_review_hold_and_a_query_check_redesign_both_parts(
    store: LocalArtifactStore,
) -> None:
    _job(store, reviews={2: BLOCKED})
    holds = [_hold("open_high_risk"), _hold("open_review")]
    status = plan_round(
        store, DB, JOB, _report(holds, query_ids=["q0"], open_reviews=1), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [0, 2]}


def test_a_co_dependency_group_spanning_parts_redesigns_every_part_it_touches(
    store: LocalArtifactStore,
) -> None:
    """The split clusters a co-dependency group's tables, but cuts a cluster over the
    part size into several parts, so a group can span parts: they go together."""
    _job(store, co_dependency_groups=[["q1", "q2"]])
    status = plan_round(
        store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [1, 2]}
    assert store.read_json(BASE + "schema_draft_group_0.json")["draft"] == "first"
    assert not store.exists(BASE + "design_trace_group_2.json")


def test_a_write_group_spanning_parts_redesigns_them_together(store: LocalArtifactStore) -> None:
    _job(store)
    key = f"{DB}/{JOB}/assignment/v{V}/assignment.json"
    assignment = store.read_json(key)
    assignment["write_groups"] = {"shop.orders": ["q0", "q1"]}
    store.write_json(key, assignment)
    status = plan_round(
        store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [0, 1]}


def test_a_routine_unit_spanning_parts_redesigns_them_together(
    store: LocalArtifactStore,
) -> None:
    _job(store)
    key = f"{DB}/{JOB}/assignment/v{V}/assignment.json"
    assignment = store.read_json(key)
    assignment["routine_units"] = [
        {"routine_id": "shop.place_order", "kind": "procedure", "query_ids": ["q1", "q2"]}
    ]
    store.write_json(key, assignment)
    status = plan_round(
        store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [1, 2]}


def test_a_check_naming_more_queries_than_the_designer_is_shown_picks_every_part(
    store: LocalArtifactStore,
) -> None:
    """The designer is shown at most MAX_QUERY_IDS_PER_CHECK ids; the parts are picked
    from all of them, so a part whose statements all sit past the cap is redesigned too."""
    sizes = {0: 20, 1: 20, 2: 10, 3: 10}
    parts: dict[int, list[str]] = {}
    n = 0
    for i, size in sizes.items():
        parts[i] = []
        for _ in range(size):
            parts[i].append(f"q{n:02d}")
            n += 1
    _job(store, parts=parts)
    every = [q for ids in parts.values() for q in ids]
    assert len(every) > wr.MAX_QUERY_IDS_PER_CHECK
    status = plan_round(
        store, DB, JOB, _report([_hold("open_high_risk")], query_ids=every), V, parts=True
    )
    assert status["parts"] == {"dynamodb": [0, 1, 2, 3]}
    [check] = read_log(store, DB, JOB, V)["rounds"][0]["requests"][0]["checks"]
    assert len(check["query_ids"]) == wr.MAX_QUERY_IDS_PER_CHECK


@pytest.mark.passes_on_main  # a fact of the split main already has: why the closure is needed
def test_the_splitter_can_cut_a_co_dependency_group_across_parts() -> None:
    """Why the closure is needed: a co-dependency cluster over the part size is cut into
    chunks, so its statements land in more than one part."""
    from src.agents.schema_design.group_splitter import build_groups

    queries: list[dict[str, Any]] = [
        {"query_id": f"q{i}", "tables_accessed": [f"shop.t{i % 3}"], "sql_template": f"S{i}"}
        for i in range(30)
    ]
    groups = build_groups(
        queries, "shop", co_dependency_groups=[[str(q["query_id"]) for q in queries]], max_size=20
    )
    assert len(groups) == 2
    assert all(g["queries"] for g in groups)


@pytest.mark.passes_on_main  # guards the whole-engine rule main already has
def test_a_check_with_no_query_redesigns_the_whole_engine(store: LocalArtifactStore) -> None:
    _job(store, reviews={2: BLOCKED})
    holds = [_hold("open_review"), _hold("unserved_patterns")]
    status = plan_round(store, DB, JOB, _report(holds, open_reviews=1), V, parts=True)
    assert status["parts"] == {"dynamodb": None}
    assert store.list_prefix(BASE) == [request_key(DB, JOB, "dynamodb", V)]


@pytest.mark.parametrize("engine", ["documentdb", "opensearch"])
def test_another_engine_stays_whole_engine(store: LocalArtifactStore, engine: str) -> None:
    """DocumentDB and OpenSearch can be designed in groups in Bedrock, but are reviewed
    as one design: a round redesigns them whole."""
    base = f"{DB}/{JOB}/schema-{engine}/v{V}/"
    store.write_json(
        base + "groups_manifest.json",
        {"groups": [{"group_index": 0, "input_file": "input_group_0.json"}]},
    )
    store.write_json(base + "input_group_0.json", _part_input(["q1"]))
    store.write_json(base + "schema_output.json", {"collections": []})
    report = _report([{**_hold("open_high_risk"), "engine": engine}], query_ids=["q1"])
    report["schema_designs"] = {engine: report["schema_designs"]["dynamodb"]}
    report["risk_assessment"]["risks"][0]["rule"] = None
    report["facts"]["waves"][0]["engines"] = [engine]
    status = plan_round(store, DB, JOB, report, V, parts=True)
    assert status["parts"] == {engine: None}


# ---------------------------------------------------------------------------
# Kept parts keep their review; redesigned parts are reviewed again
# ---------------------------------------------------------------------------


def _group_entries(store: LocalArtifactStore) -> list[dict[str, Any]]:
    sys.path.insert(0, str(REPO))
    from scripts.run_schema_design import _group_entries as entries

    return entries(store, BASE.rstrip("/"), store.read_json(BASE + "groups_manifest.json"))


def test_the_split_line_marks_the_kept_parts_carried_and_their_reviews_are_not_asked(
    store: LocalArtifactStore,
) -> None:
    _job(store)
    plan_round(store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True)
    entries = _group_entries(store)
    assert [e["carried_from"] for e in entries] == [0, None, 2]
    # Before the redesigned part's draft is in, no review is asked; then only its own.
    assert pe.missing_reviews(store, DB, JOB, V, ["dynamodb"]) == []
    _redesign(store, [1], review=False)
    asked = pe.prepare_pe_review_llm_input(store, DB, JOB, V, ["dynamodb"])
    assert [r["group"] for r in asked["reviews"]] == [1]
    assert {r["group"] for r in asked["recorded"]} == {0, 2}
    assert wr.pending_reviews(store, DB, JOB, V, read_log(store, DB, JOB, V)) == [
        "dynamodb group 1"
    ]


def test_a_kept_part_drafted_again_loses_its_kept_review(store: LocalArtifactStore) -> None:
    _job(store)
    plan_round(store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True)
    store.write_json(BASE + "schema_draft_group_0.json", {"part": 0, "draft": "other"})
    assert [e["carried_from"] for e in _group_entries(store)] == [None, None, 2]
    assert "dynamodb group 0" in pe.missing_reviews(store, DB, JOB, V, ["dynamodb"])


def test_a_parts_round_keeps_a_reviewed_better_design_fixed(store: LocalArtifactStore) -> None:
    """``fixed`` still needs every review recorded and none open: the kept parts'
    reviews count with the redesigned part's own."""
    _job(store, reviews={1: BLOCKED})
    plan_round(store, DB, JOB, _report([_hold("open_review")], open_reviews=1), V, parts=True)
    _redesign(store, [1])
    assert plan_round(store, DB, JOB, _report([]), V, parts=True)["status"] == "complete"
    assert read_log(store, DB, JOB, V)["rounds"][0]["outcome"] == {"dynamodb": "fixed"}
    assert store.read_json(BASE + "schema_draft_group_1.json")["draft"] == "second"
    assert store.read_json(BASE + "schema_draft_group_0.json")["draft"] == "first"


# ---------------------------------------------------------------------------
# Never a worse design: put-back for a parts round
# ---------------------------------------------------------------------------


def test_a_redesigned_part_with_no_review_of_its_own_is_put_back(
    store: LocalArtifactStore,
) -> None:
    """The kept parts' reviews make the report's ``reviewed_count`` positive; the part
    the round redesigned still needs its own review before its design is kept."""
    _job(store)
    plan_round(store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True)
    _redesign(store, [1], review=False)
    status = plan_round(store, DB, JOB, _report([]), V, parts=True)
    assert status == {"status": "awaiting_synthesis", "restored": ["dynamodb"], "rounds": 1}
    assert read_log(store, DB, JOB, V)["rounds"][0]["outcome"] == {"dynamodb": "not_reviewed"}
    assert store.read_json(BASE + "schema_draft_group_1.json")["draft"] == "first"
    assert store.read_json(DESIGN) == {"design": "first"}


def test_put_back_of_a_parts_round_removes_what_the_round_wrote_for_its_parts(
    store: LocalArtifactStore,
) -> None:
    """A redesigned part that had no review before gets one in the round: putting the
    earlier design back removes it, so the earlier draft never shows the new review."""
    _job(store)
    store.delete(BASE + "design_trace_group_1.json")
    plan_round(store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True)
    _redesign(store, [1])
    held = _report([_hold("open_high_risk")], query_ids=["q1"])
    status = plan_round(store, DB, JOB, held, V, parts=True)
    assert status["status"] == "awaiting_synthesis"
    assert read_log(store, DB, JOB, V)["rounds"][0]["outcome"] == {"dynamodb": "did_not_improve"}
    assert store.read_json(BASE + "schema_draft_group_1.json")["draft"] == "first"
    assert not store.exists(BASE + "design_trace_group_1.json")
    assert store.read_json(BASE + "design_trace_group_0.json") == {"pe_review": APPROVED}
    assert not store.exists(request_key(DB, JOB, "dynamodb", V))


def test_a_rolled_back_parts_round_restores_only_its_parts(store: LocalArtifactStore) -> None:
    _job(store)
    store.delete(BASE + "design_trace_group_1.json")
    plan_round(store, DB, JOB, _report([_hold("open_high_risk")], query_ids=["q1"]), V, parts=True)
    store.write_json(BASE + "design_trace_group_1.json", {"pe_review": APPROVED})
    fail_round(store, DB, JOB, V, "redesign round 1 failed: schema_design_dynamodb")
    assert store.read_json(BASE + "schema_draft_group_1.json")["draft"] == "first"
    assert not store.exists(BASE + "design_trace_group_1.json")
    assert store.read_json(DESIGN) == {"design": "first"}


# ---------------------------------------------------------------------------
# rounds.json, report.json and old logs
# ---------------------------------------------------------------------------


def test_the_report_keeps_the_parts_a_review_hold_names() -> None:
    """``report.json``'s ``wave_redesign`` is the log as written: a check's parts too."""
    log = {
        "max_rounds": 2,
        "rounds": [
            {
                "round": 1,
                "assignment_version": V,
                "engines": ["dynamodb"],
                "requests": [
                    {
                        "engine": "dynamodb",
                        "waves": [1],
                        "checks": [
                            {"check": "open_review", "line": "x", "query_ids": ["q2"], "parts": [2]}
                        ],
                    }
                ],
                "parts": {"dynamodb": [2]},
                "outcome": {"dynamodb": "fixed"},
            }
        ],
    }
    dumped = WaveRedesignLog.model_validate(log).model_dump(mode="json")
    assert dumped["rounds"][0]["requests"][0]["checks"][0]["parts"] == [2]


@pytest.mark.passes_on_main  # an old log loads on main too; this guards that it still does
def test_an_old_rounds_json_still_loads_and_closes(store: LocalArtifactStore) -> None:
    """A parts round saved before #2119 (Bedrock): no ``parts`` on its checks and no
    kept parts in its request. It closes by the same rule, puts back only its parts, and
    the log still validates."""
    _job(store)
    archive = f"{DB}/{JOB}/wave-redesign/v{V}/round_1/schema-dynamodb/"
    for name in ("schema_output.json", "schema_draft_group_1.json", "design_trace_group_1.json"):
        store.write_bytes(archive + name, store.read_bytes(BASE + name))
    store.write_json(BASE + "schema_draft_group_1.json", {"part": 1, "draft": "second"})
    store.write_json(DESIGN, {"design": "second"})
    old: dict[str, Any] = {
        "max_rounds": 2,
        "rounds": [
            {
                "round": 1,
                "assignment_version": V,
                "engines": ["dynamodb"],
                "requests": [
                    {
                        "engine": "dynamodb",
                        "waves": [1],
                        "checks": [{"check": "open_high_risk", "query_ids": ["q1"]}],
                    }
                ],
                "before": {"dynamodb": [1, 0]},
                "parts": {"dynamodb": [1]},
                "outcome": None,
            }
        ],
    }
    store.write_json(wr.log_key(DB, JOB, V), old)
    store.write_json(
        request_key(DB, JOB, "dynamodb", V),
        {"round": 1, "max_rounds": 2, **old["rounds"][0]["requests"][0]},
    )
    held = _report([_hold("open_high_risk")], query_ids=["q1"])
    assert plan_round(store, DB, JOB, held, V, parts=True)["status"] == "awaiting_synthesis"
    log = read_log(store, DB, JOB, V)
    assert log["rounds"][0]["outcome"] == {"dynamodb": "did_not_improve"}
    assert store.read_json(BASE + "schema_draft_group_1.json")["draft"] == "first"
    assert store.read_json(BASE + "schema_draft_group_0.json")["draft"] == "first"
    WaveRedesignLog.model_validate(log)


@pytest.mark.passes_on_main  # an old report loads on main too; this guards that it still does
def test_an_old_report_whose_round_checks_have_no_parts_still_loads() -> None:
    """A report.json written before #2119: its wave_redesign checks have no ``parts``."""
    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.shared.contract_versioning import load_contract

    path = REPO / "docs/examples/wordpress/sample-run/synthesis/v2/report.json"
    data = json.loads(path.read_text())
    data["wave_redesign"] = {
        "max_rounds": 2,
        "rounds": [
            {
                "round": 1,
                "assignment_version": 2,
                "engines": ["dynamodb"],
                "requests": [
                    {
                        "engine": "dynamodb",
                        "waves": [1],
                        "checks": [{"check": "open_review", "line": "x", "query_ids": []}],
                    }
                ],
                "parts": {"dynamodb": None},
                "outcome": {"dynamodb": "did_not_improve"},
            }
        ],
    }
    report = load_contract(SynthesisOutputContract, data, artifact=str(path))
    assert report.wave_redesign is not None
    [check] = report.wave_redesign.rounds[0].requests[0].checks
    assert check.parts is None


# ---------------------------------------------------------------------------
# The headless round, on the deterministic wordpress run
# ---------------------------------------------------------------------------

WP_DB, WP_JOB = "wordpress", "job-redesign-changed"


def _script(root: Path, script: str, *args: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, f"scripts/{script}", "--job-id", WP_JOB, "--db", WP_DB]
        + ["--artifact-root", str(root), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    line: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return line


@pytest.fixture(scope="module")
def headless(tmp_path_factory) -> dict[str, Any]:
    """WordPress's DynamoDB design in parts, every part reviewed, and a report whose
    wave is held by a risk on one statement of one part: ``/modernize`` 7b's round."""
    from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
    from src.agents.schema_design import handler
    from tests.e2e.pipeline import run_pipeline
    from tests.shared_runs import cached_run, copy_run
    from tests.unit.agents.schema_design.test_redesign_only_what_changed import _Designer

    cached = cached_run(
        tmp_path_factory,
        f"pipeline-wordpress-{WP_JOB}",
        lambda d: run_pipeline("wordpress", d / "artifacts", WP_JOB),
    )
    root = copy_run(cached, tmp_path_factory.mktemp("parts") / "run") / "artifacts"
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, WP_DB, WP_JOB)
    with patch.object(handler, "_dispatch_schema_agent", side_effect=_Designer()):
        handler.run_schema_design_auto(WP_JOB, WP_DB, "dynamodb", store, 2, llm_mode="bedrock")
    base = f"{WP_DB}/{WP_JOB}/schema-dynamodb/v2"
    manifest = store.read_json(f"{base}/groups_manifest.json")
    parts = {}
    for g in manifest["groups"]:
        idx = g["group_index"]
        data = store.read_json(f"{base}/input_group_{idx}.json")
        from src.agents.schema_design.pattern_input import input_query_ids

        parts[idx] = input_query_ids(data["collector_output"])
        trace = store.read_json(f"{base}/design_trace_group_{idx}.json")
        trace["pe_review"] = APPROVED
        store.write_json(f"{base}/design_trace_group_{idx}.json", trace)
    handler.run_schema_merge(WP_JOB, WP_DB, "dynamodb", store, assignment_version=2)
    target = sorted(parts)[1]
    report = _report([_hold("open_high_risk")], query_ids=[parts[target][0]])
    store.write_json(f"{WP_DB}/{WP_JOB}/synthesis/v2/report.json", report)
    drafts = {i: store.read_bytes(f"{base}/schema_draft_group_{i}.json") for i in parts}
    status = _script(root, "run_wave_redesign.py")
    split = _script(root, "run_schema_design.py", "--engine", "dynamodb", "--split")
    inputs = {i: store.read_json(f"{base}/input_group_{i}.json") for i in parts}
    return {
        "store": store,
        "base": base,
        "parts": parts,
        "target": target,
        "drafts": drafts,
        "status": status,
        "split": split,
        "inputs": inputs,
    }


def test_the_headless_check_redesigns_only_the_part_holding_the_query(headless) -> None:
    status = headless["status"]
    assert status["status"] == "awaiting_redesign"
    assert status["parts"] == {"dynamodb": [headless["target"]]}
    assert len(headless["parts"]) > 1


def test_the_split_marks_every_other_part_carried_from_itself(headless) -> None:
    split = headless["split"]
    target = headless["target"]
    assert split["carried_count"] == len(headless["parts"]) - 1
    for group in split["groups"]:
        idx = group["group_index"]
        assert group["carried_from"] == (None if idx == target else idx)
        assert group["draft_exists"] is (idx != target)


def test_the_kept_parts_keep_their_draft_and_input(headless) -> None:
    store, base = headless["store"], headless["base"]
    for idx, draft in headless["drafts"].items():
        if idx == headless["target"]:
            continue
        assert store.read_bytes(f"{base}/schema_draft_group_{idx}.json") == draft
        assert "redesign_request" not in headless["inputs"][idx]
    assert "redesign_request" in headless["inputs"][headless["target"]]


def test_only_the_redesigned_part_is_reviewed_again(headless) -> None:
    store = headless["store"]
    target = headless["target"]
    assert pe.missing_reviews(store, WP_DB, WP_JOB, 2, ["dynamodb"]) == []
    # The kept parts' reviews are recorded already: none is asked again.
    asked = pe.prepare_pe_review_llm_input(store, WP_DB, WP_JOB, 2, ["dynamodb"])
    assert asked["reviews"] == []
    assert {r["group"] for r in asked["recorded"]} == set(headless["parts"]) - {target}
    store.write_json(f"{headless['base']}/schema_draft_group_{target}.json", {"draft": "new"})
    assert pe.missing_reviews(store, WP_DB, WP_JOB, 2, ["dynamodb"]) == [f"dynamodb group {target}"]


# ---------------------------------------------------------------------------
# A parts round on a version a post-schema reroute wrote
# ---------------------------------------------------------------------------


def test_a_parts_round_put_back_on_a_rerouted_version_keeps_the_part_carried(
    run: dict[str, Any],  # noqa: F811
) -> None:
    """On v3 (a post-schema reroute of v2) a carried part is redesigned in a round: the
    round's --split gives it another input, so the reroute record drops its copy. Put
    back, the part has its carried draft again and reads carried, with its review: no
    designer and no review is asked for it outside a round."""
    from src.agents.schema_design.carry_forward import carried_parts

    rerouted = run
    store = rerouted["store"]
    db, job = "wordpress", "job-redesign-changed"
    root = Path(store.base_dir)
    base = f"{db}/{job}/schema-dynamodb/v3"
    carried = carried_parts(store, db, job, "dynamodb", 3)
    assert carried
    target = sorted(carried)[0]
    for idx in rerouted["v3_parts"]:
        key = f"{base}/design_trace_group_{idx}.json"
        trace = store.read_json(key)
        trace["pe_review"] = APPROVED
        store.write_json(key, trace)
    query = sorted(rerouted["v3_parts"][target])[0]
    held = _report([_hold("open_high_risk")], query_ids=[query])
    held["facts"]["waves"][0]["engines"] = ["dynamodb"]
    store.write_json(f"{db}/{job}/synthesis/v3/report.json", held)

    def script(name: str, *args: str) -> dict[str, Any]:
        done = subprocess.run(
            [sys.executable, f"scripts/{name}", "--job-id", job, "--db", db]
            + ["--artifact-root", str(root), *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        line: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
        return line

    status = script("run_wave_redesign.py")
    assert status["status"] == "awaiting_redesign"
    assert target in status["parts"]["dynamodb"]
    split = script("run_schema_design.py", "--engine", "dynamodb", "--split")
    assert {g["group_index"]: g["carried_from"] for g in split["groups"]}[target] is None
    # The round's design of the part, reviewed, merged; it clears nothing: put back.
    store.write_json(f"{base}/schema_draft_group_{target}.json", {"table_definitions": []})
    store.write_json(f"{base}/design_trace_group_{target}.json", {"pe_review": APPROVED})
    assert script("run_wave_redesign.py")["status"] == "awaiting_synthesis"
    status_line = script("run_schema_design.py", "--engine", "dynamodb", "--status")
    assert target not in (status_line.get("drafts_missing") or [])
    entries = _group_entries_at(store, base)
    entry = next(e for e in entries if e["group_index"] == target)
    assert entry["draft_state"] == "ok"
    assert entry["carried_from"] is not None
    assert pe.missing_reviews(store, db, job, 3, ["dynamodb"]) == []


def test_a_parts_round_the_run_closes_keeps_the_rerouted_part_carried(
    run: dict[str, Any], tmp_path: Path  # noqa: F811
) -> None:
    """#2122: the same parts round on v3, closed with --stop after its split and a draft
    of the part (no review, no synthesis): the part reads carried again, with its review,
    and the round is stopped."""
    import shutil

    from src.agents.schema_design.carry_forward import carried_parts

    db, job = "wordpress", "job-redesign-changed"
    # A copy of the module's run, without the rounds another test ran on it.
    root = tmp_path / "artifacts"
    shutil.copytree(run["store"].base_dir, root)
    shutil.rmtree(root / db / job / "wave-redesign", ignore_errors=True)
    store = LocalArtifactStore(base_dir=str(root))
    base = f"{db}/{job}/schema-dynamodb/v3"
    carried = carried_parts(store, db, job, "dynamodb", 3)
    target = sorted(carried)[0]
    for idx in run["v3_parts"]:
        key = f"{base}/design_trace_group_{idx}.json"
        trace = store.read_json(key)
        trace["pe_review"] = APPROVED
        store.write_json(key, trace)
    held = _report([_hold("open_high_risk")], query_ids=[sorted(run["v3_parts"][target])[0]])
    held["facts"]["waves"][0]["engines"] = ["dynamodb"]
    store.write_json(f"{db}/{job}/synthesis/v3/report.json", held)

    def script(name: str, *args: str) -> dict[str, Any]:
        done = subprocess.run(
            [sys.executable, f"scripts/{name}", "--job-id", job, "--db", db]
            + ["--artifact-root", str(root), *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        line: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
        return line

    assert script("run_wave_redesign.py")["status"] == "awaiting_redesign"
    script("run_schema_design.py", "--engine", "dynamodb", "--split")
    store.write_json(f"{base}/schema_draft_group_{target}.json", {"table_definitions": []})
    stopped = script("run_wave_redesign.py", "--stop")
    assert stopped["status"] == "awaiting_synthesis" and stopped["restored"] == ["dynamodb"]
    rnd = read_log(store, db, job, 3)["rounds"][-1]
    assert rnd["outcome"] == {"dynamodb": "stopped"} and rnd["stopped_by"] == "closed"
    assert target in carried_parts(store, db, job, "dynamodb", 3)
    entry = next(e for e in _group_entries_at(store, base) if e["group_index"] == target)
    assert entry["draft_state"] == "ok" and entry["carried_from"] is not None
    assert pe.missing_reviews(store, db, job, 3, ["dynamodb"]) == []
    assert not store.exists(request_key(db, job, "dynamodb", 3))


def _group_entries_at(store: LocalArtifactStore, base: str) -> list[dict[str, Any]]:
    sys.path.insert(0, str(REPO))
    from scripts.run_schema_design import _group_entries as entries

    return entries(store, base, store.read_json(f"{base}/groups_manifest.json"))
