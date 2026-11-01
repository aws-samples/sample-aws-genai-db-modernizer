# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Random review-gate sequences against the real pointer logic (#1742).

Hypothesis walks random sequences of what the customer, the agent and the
platform do around the routing-review gate. The steps are: raise a table; submit
the latest or an older table; close a table from outside (the model's MCP tool);
make the next close fail, or let the customer submit during it; make the client
unavailable for one step; start from a legacy pointer; finalize from the table
(feasible, infeasible, or accepting the risks) or with a chat edit; and reopen.
The Agentic API client is a stub that keeps each task's status and submission.
The gate's tools, ``hitl.close_hitl_task`` and ``hitl.read_assignment_submission``
run for real.

After every step three things hold:

* No wait lasts forever: when finalize says it is waiting for the table, that
  table can still be submitted.
* No routing is approved twice: a table submission's edits are applied at most
  once, and a finalize that reads no submission writes no version from a table.
* No submission is read from a task of an earlier round.

The run is derandomized (the same examples every run), so a required check is
never flaky. Set ``HITL_PROPERTY_RANDOM=1`` to draw fresh random examples, for
example with ``--hypothesis-seed``.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from src.atx_orchestrator import tools
from src.atx_orchestrator.runtime import hitl
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-hitl-props"
ENGINES = ("dynamodb", "opensearch")

FINALIZE = {
    # action: (chat edit, infeasible, accept_feasibility_risks)
    "finalize_table": (False, False, False),
    "finalize_table_infeasible": (False, True, False),
    "finalize_table_accept": (False, True, True),
    "finalize_chat": (True, False, False),
    "finalize_chat_infeasible": (True, True, False),
}
ACTIONS = (
    "raise",
    "submit_dynamodb",
    "submit_opensearch",
    "submit_older",
    "close_outside",
    "flaky_close",
    "submit_during_close",
    "client_unavailable",
    "legacy_start",
    "reopen",
    *FINALIZE,
)


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Platform:
    """The tasks a job holds, and a stub Agentic API client over them."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.round = 1
        self.next_close = ""  # "", "fail" or "submit"
        self.unavailable = False

    def raise_table(self, **_kwargs) -> str:
        task_id = f"t{len(self.tasks) + 1}"
        self.tasks[task_id] = {"status": "AWAITING_HUMAN_INPUT", "rows": None, "round": self.round}
        return task_id

    def is_open(self, task_id: str | None) -> bool:
        return bool(task_id) and self.tasks[task_id]["status"] in hitl._OPEN_STATUSES

    def submit(self, task_id: str, engine: str = "opensearch") -> None:
        self.tasks[task_id].update(
            status="SUBMITTED", rows=[{"query_id": "q2", "new_engine": engine, "in_scope": "yes"}]
        )

    def get_hitl_task(self, hitlTaskId: str, **_kwargs) -> dict:
        task = self.tasks.get(hitlTaskId)
        if task is None:
            raise _ClientError("ResourceNotFoundException")
        out: dict = {"hitlTaskStatus": task["status"]}
        if task["rows"] is not None:
            out["humanArtifact"] = {"content": json.dumps({"items": task["rows"]})}
        return {"hitlTask": out}

    def close_hitl_task(self, hitlTaskId: str, **_kwargs) -> dict:
        task = self.tasks[hitlTaskId]
        mode, self.next_close = self.next_close, ""
        if mode == "fail":
            raise _ClientError("ThrottlingException")
        if mode == "submit" and task["status"] in hitl._OPEN_STATUSES:
            self.submit(hitlTaskId)  # the customer submits during the close
        if task["status"] not in hitl._OPEN_STATUSES:
            raise _ClientError("TerminalResourceException")
        task["status"] = "CANCELLED"
        return {"hitlTaskStatus": "CANCELLED"}

    def resolve(self):
        if self.unavailable:
            raise RuntimeError("no runtime client")
        client = MagicMock()
        client.get_hitl_task.side_effect = self.get_hitl_task
        client.close_hitl_task.side_effect = self.close_hitl_task
        return client, {}


def _seed(store) -> None:
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            QueryAssignment(
                query_id=qid,
                assigned_engine=engine,
                confidence=80,
                source_tables=[table],
                assignment_reason=f"routed to {engine}",
            )
            for qid, engine, table in (
                ("q1", "dynamodb", "t.users"),
                ("q2", "dynamodb", "t.posts"),
                ("q3", "opensearch", "t.docs"),
            )
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {"query_id": "q1", "tables_accessed": ["t.users"]},
                    {"query_id": "q2", "tables_accessed": ["t.posts"]},
                    {"query_id": "q3", "tables_accessed": ["t.docs"]},
                ]
            },
            "database_schema": {"tables": []},
        },
    )
    for engine in ENGINES:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})


def _versions(store) -> int:
    return max(
        v for v in range(1, 60) if store.exists(f"{DB}/{JOB}/assignment/v{v}/assignment.json")
    )


def _chat_edit(store) -> str:
    """The effective routing as review markdown, with q1 moved to the other engine."""
    from src.agents.referee.assignment_review import render_assignment_review
    from src.storage.assignment_versioning import resolve_effective_assignment_version

    version = resolve_effective_assignment_version(store, DB, JOB)
    assignment = Assignment.model_validate(
        store.read_json(f"{DB}/{JOB}/assignment/v{version}/assignment.json")
    )
    current = {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}["q1"]
    target = ENGINES[1] if current == ENGINES[0] else ENGINES[0]
    lines = render_assignment_review(assignment).splitlines()
    for i, line in enumerate(lines):
        if line.startswith("| q1 |"):
            cells = line.split("|")
            cells[4] = f" {target} "
            lines[i] = "|".join(cells)
    return "\n".join(lines)


def _infeasible():
    from src.contracts.feasibility_models import (
        FeasibilityFinding,
        FindingKind,
        FindingSeverity,
    )

    blocking = FeasibilityFinding(
        kind=FindingKind.READ_WRITE_SPLIT,
        severity=FindingSeverity.BLOCKING,
        table="t.posts",
        message="stub blocking finding",
    )
    return patch(
        "src.agents.referee.feasibility_review.review_assignment_feasibility",
        return_value=[blocking],
    )


def run_gate(actions: list[str]) -> list[dict]:
    """Run ``actions`` against the real gate; check the invariants after each step.

    Returns one record per step: the action, the tool's JSON result (or None for
    a step that calls no tool) and the platform, for the sequence tests.
    """
    platform = _Platform()
    applied: set[str] = set()  # task ids whose submission edits were applied
    reads: list[str] = []
    real_read = hitl.read_assignment_submission
    steps: list[dict] = []

    def recording_read(task_id: str):
        reads.append(task_id)
        return real_read(task_id)

    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        store = upgrade_store(LocalArtifactStore(base_dir=tmp))
        _seed(store)
        stack.enter_context(patch("src.atx_orchestrator.tools._make_store", return_value=store))
        stack.enter_context(
            patch.object(hitl, "_resolve_client_and_context", side_effect=platform.resolve)
        )
        stack.enter_context(
            patch.object(hitl, "raise_assignment_table", side_effect=platform.raise_table)
        )
        stack.enter_context(
            patch.object(hitl, "read_assignment_submission", side_effect=recording_read)
        )

        for action in actions:
            tasks = list(platform.tasks)
            latest = tasks[-1] if tasks else None
            out: dict | None = None
            reads.clear()
            before = _versions(store)
            if action == "raise":
                out = json.loads(tools.open_detailed_routing_review(JOB, DB))
            elif action in ("submit_dynamodb", "submit_opensearch"):
                if platform.is_open(latest):
                    platform.submit(latest, action.removeprefix("submit_"))
            elif action == "submit_older":
                older = [t for t in tasks[:-1] if platform.is_open(t)]
                if older:
                    platform.submit(older[0])
            elif action == "close_outside":
                if platform.is_open(latest):
                    platform.tasks[latest]["status"] = "CANCELLED"
            elif action == "flaky_close":
                platform.next_close = "fail"
            elif action == "submit_during_close":
                platform.next_close = "submit"
            elif action == "client_unavailable":
                platform.unavailable = True
                continue  # applies to the next step only
            elif action == "legacy_start":
                if not tasks:
                    # A job saved before the pointer lifecycle, waiting on a table.
                    store.write_json(
                        f"{DB}/{JOB}/assignment/review/pending_hitl.json",
                        {"hitl_task_id": platform.raise_table(), "assignment_version": 1},
                    )
            elif action == "reopen":
                out = json.loads(tools.reopen_assignment_review(JOB, DB))
                platform.round += 1
            else:
                chat, infeasible, accept = FINALIZE[action]
                markdown = _chat_edit(store) if chat else ""
                with ExitStack() as call:
                    if infeasible:
                        call.enter_context(_infeasible())
                    out = json.loads(
                        tools.finalize_assignment_review(
                            JOB, DB, markdown, accept_feasibility_risks=accept
                        )
                    )
                if out.get("status") == "awaiting_review":
                    # 1. No wait lasts forever: the awaited table can still be submitted.
                    pending = tools._awaited_pending_hitl(store, DB, JOB)
                    assert pending is not None
                    assert platform.is_open(pending["hitl_task_id"])
                if not chat and _versions(store) > before:
                    # 2. A submission's edits are applied at most once.
                    assert len(set(reads)) == 1, reads
                    (task_id,) = set(reads)
                    assert task_id not in applied, (task_id, actions)
                    applied.add(task_id)
            platform.unavailable = False
            # 3. No submission is read from a task of an earlier round.
            for task_id in reads:
                assert platform.tasks[task_id]["round"] == platform.round, (task_id, actions)
            if not reads and action not in FINALIZE:
                assert _versions(store) == before  # only finalize writes a version
            steps.append({"action": action, "out": out, "platform": platform})
    return steps


@settings(
    max_examples=200,
    deadline=None,
    derandomize=not os.environ.get("HITL_PROPERTY_RANDOM"),
    suppress_health_check=[HealthCheck.too_slow],
)
@given(st.lists(st.sampled_from(ACTIONS), min_size=1, max_size=12))
# An infeasible table submission re-read for the next finalize: its edits are
# applied once (the #1750 third review's shrunk sequence), also when accepting.
@example(["raise", "submit_opensearch", "finalize_table_infeasible", "finalize_table"])
@example(["raise", "submit_opensearch", "finalize_table_infeasible", "finalize_table_accept"])
# The #1750 re-review's sequence: an infeasible table submission, fixed in chat,
# then reopen and continue (round 2 must not re-apply round 1's table edit).
@example(
    [
        "raise",
        "submit_opensearch",
        "finalize_table_infeasible",
        "finalize_chat",
        "reopen",
        "finalize_table",
    ]
)
# An infeasible table submission, never fixed, then reopen and continue: the
# reopen must retire the consumed pointer.
@example(["raise", "submit_opensearch", "finalize_table_infeasible", "reopen", "finalize_table"])
# A table approval, then reopen and continue.
@example(["raise", "submit_opensearch", "finalize_table", "reopen", "finalize_table"])
# A close that fails, a late submit, then reopen and continue.
@example(["raise", "flaky_close", "finalize_chat", "submit_opensearch", "reopen", "finalize_table"])
def test_random_gate_sequences_keep_the_three_invariants(actions: list[str]) -> None:
    run_gate(actions)


def _reported(step: dict) -> list[str]:
    out = step["out"] or {}
    found = out.get("superseded_submissions_ignored") or []
    if out.get("superseded_submission_ignored"):
        found = [out["superseded_submission_ignored"], *found]
    return found


class TestASubmissionAfterAFailedCloseIsReported:
    """The #1750 third review's four sequences: each dropped submission is
    reported in the result of the step that retires or retries its table."""

    def test_reopen_fails_to_close_then_submit_then_reopen_again(self) -> None:
        steps = run_gate(["raise", "flaky_close", "reopen", "submit_opensearch", "reopen"])
        assert _reported(steps[-1]) == ["t1"]
        assert "not applied" in steps[-1]["out"]["message"]

    def test_chat_approval_fails_to_close_then_submit_then_reopen(self) -> None:
        steps = run_gate(["raise", "flaky_close", "finalize_chat", "submit_opensearch", "reopen"])
        assert _reported(steps[2]) == []
        assert _reported(steps[-1]) == ["t1"]

    def test_reopen_fails_to_close_then_submit_then_raise(self) -> None:
        steps = run_gate(["raise", "flaky_close", "reopen", "submit_opensearch", "raise"])
        assert steps[-1]["out"]["hitl_task_id"] == "t2"
        assert _reported(steps[-1]) == ["t1"]

    def test_raise_fails_to_close_the_old_table_then_it_is_submitted(self) -> None:
        steps = run_gate(["raise", "flaky_close", "raise", "submit_older", "finalize_table"])
        assert steps[-1]["out"]["status"] == "awaiting_review"  # t2 is still open
        assert _reported(steps[-1]) == ["t1"]

    def test_a_retried_close_closes_a_table_nobody_submitted(self) -> None:
        steps = run_gate(["raise", "flaky_close", "raise", "finalize_table"])
        assert steps[-1]["platform"].tasks["t1"]["status"] == "CANCELLED"
        assert _reported(steps[-1]) == []

    def test_a_report_is_made_once(self) -> None:
        steps = run_gate(
            ["raise", "flaky_close", "reopen", "submit_opensearch", "reopen", "reopen"]
        )
        assert _reported(steps[-2]) == ["t1"]
        assert _reported(steps[-1]) == []
