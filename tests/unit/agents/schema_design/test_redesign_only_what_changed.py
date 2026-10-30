# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A post-schema move redesigns only the parts it changed (#1650).

The deterministic wordpress run is approved (v2) and DynamoDB is designed on it by the
real grouped path (``run_schema_design_auto``: split into parts, one designer call per
part, merge), with the designer call replayed. A PE flag then moves one statement to
Aurora MySQL (v3, ``LocalOrchestrator._run_post_schema_routing``). On v3 the splitter
keeps v2's parts, so only the part that held the moved statement (and its placement
unit) changes: DynamoDB's designer runs once per changed part, never for the others,
whose drafts and design traces are carried forward byte for byte, and the merge covers
every statement DynamoDB keeps. No test calls a model.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
from src.agents.schema_design import handler
from src.agents.schema_design.carry_forward import carried_parts
from src.agents.schema_design.pattern_input import input_query_ids
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, run_pipeline
from tests.shared_runs import cached_run, copy_run

DB, JOB = "wordpress", "job-redesign-changed"
FLAGGED = "b05af520"  # INSERT INTO wp_actionscheduler_logs: moves to Aurora MySQL
TEMPLATE = json.loads(
    (
        REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
    ).read_text()
)


class _Designer:
    """Stands in for one designer call (``handler._dispatch_schema_agent``): one access
    pattern per statement of the part's input. Records which version's part it
    designed, by the statements the part holds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, frozenset[str]]] = []

    def __call__(self, target_type: str, collector_path: str | None = None, **_: Any):
        collector = json.loads(Path(str(collector_path)).read_text())
        ids = input_query_ids(collector)
        self.calls.append((target_type, frozenset(ids)))
        design = copy.deepcopy(TEMPLATE)
        pattern = design["access_patterns"][0]
        table = design["table_definitions"][0]["table_name"]
        design["access_patterns"] = [
            {**pattern, "pattern_id": f"DDB-AP-{i + 1}", "query_ids": [qid], "table_name": table}
            for i, qid in enumerate(ids)
        ]
        out = DynamoDBModelOutputContract.model_validate(design)
        return out.model_dump_json(), json.dumps(
            {"trace": [], "token_usage": {"calls": len(self.calls)}}
        )


def _parts(store: LocalArtifactStore, version: int) -> dict[int, frozenset[str]]:
    base = f"{DB}/{JOB}/schema-dynamodb/v{version}"
    manifest = store.read_json(f"{base}/groups_manifest.json")
    return {
        g["group_index"]: frozenset(
            input_query_ids(
                store.read_json(f"{base}/input_group_{g['group_index']}.json")["collector_output"]
            )
        )
        for g in manifest["groups"]
    }


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-wordpress-{JOB}",
        lambda d: run_pipeline("wordpress", d / "artifacts", JOB),
    )
    root = copy_run(cached, tmp_path_factory.mktemp("changed") / "run") / "artifacts"
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, DB, JOB)
    designer = _Designer()
    real_auto = handler.run_schema_design_auto

    def redesign(job_id, database_name, engine, store, assignment_version=0, llm_mode=""):
        # DynamoDB through the real grouped path; the other engines need no design here.
        if engine == "dynamodb":
            real_auto(job_id, database_name, engine, store, assignment_version, llm_mode)

    with patch.object(handler, "_dispatch_schema_agent", side_effect=designer):
        real_auto(JOB, DB, "dynamodb", store, 2, llm_mode="bedrock")
        first_round = len(designer.calls)
        v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
        flagged = next(
            qa["query_id"] for qa in v2["query_assignments"] if qa["query_id"].startswith(FLAGGED)
        )
        trace_key = f"{DB}/{JOB}/schema-dynamodb/v2/design_trace.json"
        trace = store.read_json(trace_key)
        trace["routing_flags"] = [
            {
                "query_ids": [flagged],
                "flagged_engine": "dynamodb",
                "reason_category": "client_side_join",
                "explanation": "needs a server-side join",
            }
        ]
        store.write_json(trace_key, trace)
        with patch.object(handler, "run_schema_design_auto", side_effect=redesign):
            LocalOrchestrator(store, llm_mode="bedrock")._run_post_schema_routing(JOB, DB)
    v3 = store.read_json(f"{DB}/{JOB}/assignment/v3/assignment.json")
    moved = {r["query_id"] for r in v3["post_schema_reroutes"]}
    return {
        "store": store,
        "flagged": flagged,
        "moved": moved,
        "first_round": designer.calls[:first_round],
        "redesign": designer.calls[first_round:],
        "v2_parts": _parts(store, 2),
        "v3_parts": _parts(store, 3),
        "v3": v3,
    }


def test_the_flag_moved_a_statement_after_approval(run) -> None:
    assert run["flagged"] in run["moved"]
    on_dynamodb = {
        qa["query_id"]
        for qa in run["v3"]["query_assignments"]
        if qa["assigned_engine"] == "dynamodb" and qa.get("in_scope", True)
    }
    assert not run["moved"] & on_dynamodb


def test_only_the_parts_holding_the_move_are_designed_again(run) -> None:
    v2_parts = run["v2_parts"]
    assert len(v2_parts) >= 3, "the fixture must split DynamoDB into several parts"
    changed = [p for p in v2_parts.values() if p & run["moved"]]
    assert len(run["first_round"]) == len(v2_parts)
    # One designer call per changed part, with exactly that part's other statements.
    assert len(run["redesign"]) == len(changed) < len(v2_parts)
    assert Counter(ids for _, ids in run["redesign"]) == Counter(
        p - run["moved"] for p in changed if p - run["moved"]
    )


def test_every_other_part_is_kept_as_it_was(run) -> None:
    unchanged = {p for p in run["v2_parts"].values() if not p & run["moved"]}
    assert unchanged <= set(run["v3_parts"].values())
    carried = carried_parts(run["store"], DB, JOB, "dynamodb", 3)
    assert {run["v3_parts"][new] for new in carried} == unchanged


def test_carried_forward_designs_are_unchanged(run) -> None:
    store = run["store"]
    carried = carried_parts(store, DB, JOB, "dynamodb", 3)
    assert carried
    for new, old in carried.items():
        # The draft byte for byte; the trace (with its review) as the contract reads it.
        draft = "schema_draft_group"
        before = store.read_bytes(f"{DB}/{JOB}/schema-dynamodb/v2/{draft}_{old}.json")
        assert store.read_bytes(f"{DB}/{JOB}/schema-dynamodb/v3/{draft}_{new}.json") == before
        trace = "design_trace_group"
        before_trace = store.read_json(f"{DB}/{JOB}/schema-dynamodb/v2/{trace}_{old}.json")
        assert store.read_json(f"{DB}/{JOB}/schema-dynamodb/v3/{trace}_{new}.json") == before_trace


def test_the_merged_design_serves_every_statement_dynamodb_keeps(run) -> None:
    store = run["store"]
    merged = store.read_json(f"{DB}/{JOB}/schema-dynamodb/v3/schema_output.json")
    served = {str(q) for ap in merged["access_patterns"] for q in ap["query_ids"]}
    on_dynamodb = {
        qa["query_id"]
        for qa in run["v3"]["query_assignments"]
        if qa["assigned_engine"] == "dynamodb" and qa.get("in_scope", True)
    }
    assert served == on_dynamodb


# ---------------------------------------------------------------------------
# Claude Code path: the headless reroute step, then the --split line
# ---------------------------------------------------------------------------


def _script(root: Path, *args: str) -> dict:
    import subprocess
    import sys

    done = subprocess.run(
        [
            sys.executable,
            "scripts/run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--artifact-root",
            str(root),
            *args,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    line: dict = json.loads(done.stdout.strip().splitlines()[-1])
    return line


@pytest.fixture(scope="module")
def external(tmp_path_factory) -> dict:
    from src.agents.referee.post_schema_routing_handler import PostSchemaRouting
    from src.agents.schema_design.pe_review_external import REVIEW_KEY, missing_reviews

    cached = cached_run(
        tmp_path_factory,
        f"pipeline-wordpress-{JOB}",
        lambda d: run_pipeline("wordpress", d / "artifacts", JOB),
    )
    root = copy_run(cached, tmp_path_factory.mktemp("external") / "run") / "artifacts"
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, DB, JOB)
    with patch.object(handler, "_dispatch_schema_agent", side_effect=_Designer()):
        handler.run_schema_design_auto(JOB, DB, "dynamodb", store, 2, llm_mode="bedrock")
    base2 = f"{DB}/{JOB}/schema-dynamodb/v2"
    v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    flagged = next(
        qa["query_id"] for qa in v2["query_assignments"] if qa["query_id"].startswith(FLAGGED)
    )
    # Every part's review is recorded (as /review-schema records it), one of them
    # flagging the statement.
    parts = _parts(store, 2)
    for idx, ids in parts.items():
        key = f"{base2}/design_trace_group_{idx}.json"
        trace = store.read_json(key)
        trace[REVIEW_KEY] = {"source": "external", "verdict": "approved"}
        if flagged in ids:
            trace["routing_flags"] = [
                {
                    "query_ids": [flagged],
                    "flagged_engine": "dynamodb",
                    "reason_category": "client_side_join",
                    "explanation": "needs a server-side join",
                }
            ]
        store.write_json(key, trace)
    handler.run_schema_merge(JOB, DB, "dynamodb", store, assignment_version=2)
    status = PostSchemaRouting(store, lambda *a: None).run_external_step(JOB, DB)
    split = _script(root, "--engine", "dynamodb", "--split")
    split_again = _script(root, "--engine", "dynamodb", "--split")  # a resumed session
    unreviewed_before = missing_reviews(store, DB, JOB, 3, ["dynamodb"])
    # The changed part's subagent writes its draft: only its review is then missing.
    redo = [g for g in split["groups"] if g["carried_from"] is None]
    for group in redo:
        Path(group["draft"]).write_text(
            json.dumps(
                json.loads(_Designer()("dynamodb", collector_path=_collector_file(group))[0])
            )
        )
    unreviewed_after = missing_reviews(store, DB, JOB, 3, ["dynamodb"])
    return {
        "status": status,
        "split": split,
        "split_again": split_again,
        "redo": redo,
        "unreviewed_before": unreviewed_before,
        "unreviewed_after": unreviewed_after,
        "parts": parts,
    }


def _collector_file(group: dict) -> str:
    """The part's collector output, as the Bedrock designer is given it."""
    import tempfile

    data = json.loads(Path(group["input_file"]).read_text())["collector_output"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(data, handle)
    return handle.name


def test_the_headless_reroute_asks_for_dynamodb_again(external) -> None:
    assert external["status"]["status"] == "awaiting_redesign"
    assert "dynamodb" in external["status"]["redesign_engines"]
    assert external["split"]["assignment_version"] == 3


def test_the_split_line_marks_every_unchanged_part_carried(external) -> None:
    split = external["split"]
    assert split["carried_count"] == len(split["groups"]) - len(external["redo"])
    assert len(external["redo"]) == 1 < len(split["groups"])
    for group in split["groups"]:
        assert group["draft_exists"] is (group["carried_from"] is not None)


def test_re_running_the_split_keeps_every_carried_part(external) -> None:
    again = external["split_again"]
    assert again["carried_count"] == external["split"]["carried_count"] > 0
    assert [g["carried_from"] for g in again["groups"]] == [
        g["carried_from"] for g in external["split"]["groups"]
    ]


def test_only_the_redesigned_part_waits_for_a_review(external) -> None:
    # Carried parts keep their recorded review; the part to redo has no draft yet.
    assert external["unreviewed_before"] == []
    [redo] = external["redo"]
    assert external["unreviewed_after"] == [f"dynamodb group {redo['group_index']}"]


def test_the_merged_trace_counts_only_the_tokens_this_version_spent(run) -> None:
    store = run["store"]
    base = f"{DB}/{JOB}/schema-dynamodb/v3"
    carried = carried_parts(store, DB, JOB, "dynamodb", 3)
    spent = sum(
        store.read_json(f"{base}/design_trace_group_{g}.json")["token_usage"]["calls"]
        for g in run["v3_parts"]
        if g not in carried
    )
    assert store.read_json(f"{base}/design_trace.json")["token_usage"] == {"calls": spent}


def test_splitting_the_same_version_again_keeps_the_carried_parts(run) -> None:
    """A retry, a resumed session or a recycled run re-runs the split (#1650 review)."""
    store = run["store"]
    before = carried_parts(store, DB, JOB, "dynamodb", 3)
    handler.run_schema_split(JOB, DB, "dynamodb", store, assignment_version=3)
    assert carried_parts(store, DB, JOB, "dynamodb", 3) == before != {}


def test_a_part_designed_afresh_is_no_longer_carried_and_needs_its_own_review(
    tmp_path,
) -> None:
    from src.agents.schema_design.carry_forward import carry_forward_parts, stale_review
    from src.agents.schema_design.design_trace import write_design_trace
    from src.agents.schema_design.pe_review_external import REVIEW_KEY, is_recorded

    store = LocalArtifactStore(base_dir=str(tmp_path))
    old, new = f"{DB}/{JOB}/schema-dynamodb/v2", f"{DB}/{JOB}/schema-dynamodb/v3"
    for base in (old, new):
        store.write_json(
            f"{base}/groups_manifest.json",
            {"groups": [{"group_index": 0, "primary_tables": ["a"]}]},
        )
        store.write_json(f"{base}/input_group_0.json", {"collector_output": {"queries": {}}})
    store.write_json(f"{old}/schema_draft_group_0.json", {"access_patterns": []})
    write_design_trace(
        store,
        f"{old}/design_trace_group_0.json",
        {"trace": [], REVIEW_KEY: {"source": "external", "verdict": "approved"}},
    )
    assert carry_forward_parts(store, DB, JOB, "dynamodb", 2, 3) == {0: 0}
    assert is_recorded(store, DB, JOB, "dynamodb", 3, 0)
    # A designer writes a fresh draft over the carried one: its review is of another design.
    store.write_json(f"{new}/schema_draft_group_0.json", {"access_patterns": [{"x": 1}]})
    assert carry_forward_parts(store, DB, JOB, "dynamodb", 2, 3) == {}
    assert stale_review(store, new, 0)
    assert not is_recorded(store, DB, JOB, "dynamodb", 3, 0)
    # Its own review, once recorded, counts again.
    write_design_trace(
        store,
        f"{new}/design_trace_group_0.json",
        {"trace": [], REVIEW_KEY: {"source": "external", "verdict": "approved", "n": 2}},
    )
    assert is_recorded(store, DB, JOB, "dynamodb", 3, 0)


@pytest.mark.parametrize("other", [["a"], ["b", "c"]])
def test_a_change_to_the_other_parts_tables_is_a_change_to_the_input(tmp_path, other) -> None:
    """The headless designer is told the other parts' tables (/modernize 6b)."""
    from src.agents.schema_design.carry_forward import carry_forward_parts

    store = LocalArtifactStore(base_dir=str(tmp_path))
    old, new = f"{DB}/{JOB}/schema-dynamodb/v2", f"{DB}/{JOB}/schema-dynamodb/v3"
    for base, tables in ((old, ["b", "c"]), (new, other)):
        store.write_json(
            f"{base}/groups_manifest.json",
            {
                "groups": [
                    {"group_index": 0, "primary_tables": ["x"]},
                    {"group_index": 1, "primary_tables": tables},
                ]
            },
        )
        for g in (0, 1):
            store.write_json(
                f"{base}/input_group_{g}.json", {"collector_output": {"queries": {"g": g}}}
            )
    store.write_json(f"{old}/schema_draft_group_0.json", {"access_patterns": []})
    carried = carry_forward_parts(store, DB, JOB, "dynamodb", 2, 3)
    assert (0 in carried) is (other == ["b", "c"])


def test_a_retried_redesign_designs_nothing_again_and_keeps_the_carried_parts(run) -> None:
    """A recycled Bedrock run re-splits and finds every part's draft in place."""
    store = run["store"]
    before = carried_parts(store, DB, JOB, "dynamodb", 3)
    designer = _Designer()
    with patch.object(handler, "_dispatch_schema_agent", side_effect=designer):
        handler.run_schema_design_auto(JOB, DB, "dynamodb", store, 3, llm_mode="bedrock")
    assert designer.calls == []
    assert carried_parts(store, DB, JOB, "dynamodb", 3) == before != {}


class _NoDeleteStore(LocalArtifactStore):
    """A store that cannot delete (S3 without delete rights, AWS Transform's)."""

    def delete(self, path: str) -> bool:
        return False


def _one_part(store, base: str, query: str, draft: bool) -> None:
    from src.agents.schema_design.design_trace import write_design_trace
    from src.agents.schema_design.pe_review_external import REVIEW_KEY

    store.write_json(
        f"{base}/groups_manifest.json", {"groups": [{"group_index": 0, "primary_tables": ["a"]}]}
    )
    store.write_json(f"{base}/input_group_0.json", {"collector_output": {"queries": {"q": query}}})
    if draft:
        store.write_json(f"{base}/schema_draft_group_0.json", {"access_patterns": []})
        write_design_trace(
            store,
            f"{base}/design_trace_group_0.json",
            {"trace": [], REVIEW_KEY: {"source": "external", "verdict": "approved"}},
        )


@pytest.mark.parametrize("store_class", [LocalArtifactStore, _NoDeleteStore])
def test_a_re_split_that_changes_a_carried_parts_input_drops_its_copy(
    tmp_path, store_class
) -> None:
    from src.agents.schema_design.carry_forward import carry_forward_parts, is_dropped_copy
    from src.agents.schema_design.pe_review_external import is_recorded

    store = store_class(base_dir=str(tmp_path))
    old, new = f"{DB}/{JOB}/schema-dynamodb/v2", f"{DB}/{JOB}/schema-dynamodb/v3"
    _one_part(store, old, "same", draft=True)
    _one_part(store, new, "same", draft=False)
    assert carry_forward_parts(store, DB, JOB, "dynamodb", 2, 3) == {0: 0}
    # The re-split gives part 0 another input: the copy is of another design.
    _one_part(store, new, "changed", draft=False)
    assert carry_forward_parts(store, DB, JOB, "dynamodb", 2, 3) == {}
    assert carried_parts(store, DB, JOB, "dynamodb", 3) == {}
    draft = f"{new}/schema_draft_group_0.json"
    if store_class is LocalArtifactStore:
        assert not store.exists(draft)
        assert not store.exists(f"{new}/design_trace_group_0.json")
    else:
        # It could not be deleted: it is never reused as the part's draft or review.
        assert store.exists(draft) and is_dropped_copy(store, new, 0)
        assert not is_recorded(store, DB, JOB, "dynamodb", 3, 0)


def test_a_dropped_copy_stops_counting_once_the_part_has_its_own_draft(tmp_path) -> None:
    """The group loop and --status treat a dropped copy as no draft, never a fresh one."""
    from src.agents.schema_design.carry_forward import is_dropped_copy

    store = _NoDeleteStore(base_dir=str(tmp_path))
    new = f"{DB}/{JOB}/schema-dynamodb/v3"
    store.write_json(f"{new}/schema_draft_group_0.json", {"access_patterns": []})
    import hashlib

    digest = hashlib.sha256(store.read_bytes(f"{new}/schema_draft_group_0.json")).hexdigest()
    store.write_json(
        f"{new}/carried_forward.json",
        {
            "from_version": 2,
            "parts": [{"part": 0, "from_part": 0, "draft_sha256": digest, "dropped": True}],
        },
    )
    assert is_dropped_copy(store, new, 0)
    store.write_json(f"{new}/schema_draft_group_0.json", {"access_patterns": [{"own": 1}]})
    assert not is_dropped_copy(store, new, 0)
