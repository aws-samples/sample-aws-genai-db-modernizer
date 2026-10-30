# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The settled workload a reality-check flag's tie-break reads (#746 item 2).

ADR-031 Decision 12: the settled-workload snapshot is computed once, by the
resolver (``RoutingTrace.settled_counts``), and the reality check reuses it
rather than each stage measuring "settled" differently. The placement already
does; the model-flag path recounted it from ``revised_assignments``. It now
reads the same snapshot, over the engines the check kept (a flag re-routes
only among those). No test calls a model: the flag is passed in.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from src.agents.referee import reality_check_handler as handler
from src.agents.referee.reality_check_handler import (
    apply_reality_check_llm_output,
    run_reality_check_deterministic,
)
from src.agents.referee.routing_trace import RoutingTrace
from src.agents.referee.tie_break import settled_counts
from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store


def _flag(qid: str, engine: str) -> dict[str, Any]:
    return {
        "query_ids": [qid],
        "flagged_engine": engine,
        "reason_category": "other",
        "explanation": "x",
    }


def _settled_passed(det: dict) -> dict:
    """The ``settled`` argument the flag path hands the shared routing step."""
    seen: dict = {}
    real = handler.apply_consolidation_flags

    def spy(*args, **kwargs):
        seen.update(kwargs["settled"])
        return real(*args, **kwargs)

    qid, engine = next((qa["query_id"], qa["assigned_engine"]) for qa in det["revised_assignments"])
    with patch.object(handler, "apply_consolidation_flags", side_effect=spy):
        apply_reality_check_llm_output(det, {"consolidation_corrections": [_flag(qid, engine)]})
    return seen


def test_the_deterministic_result_carries_the_snapshot_the_placement_used() -> None:
    snapshots: list[dict] = []
    real = RoutingTrace.record_settled_counts

    def keep(self, counts):
        snapshots.append(dict(counts))
        return real(self, counts)

    with patch.object(RoutingTrace, "record_settled_counts", keep):
        det = run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1)
    assert snapshots, "the resolver recorded no snapshot"
    # The counts the placement used: the resolver's snapshot, or, when it is
    # empty (this fixture's resolver settles nothing), run_reality_check's
    # recount of the input assignment.
    expected = snapshots[-1] or settled_counts(det["assignment"]["query_assignments"])
    assert expected and det["settled_counts"] == expected


def test_a_flag_reads_the_snapshot_over_the_kept_engines() -> None:
    det = run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1)
    kept = {e for e, n in det["after_distribution"].items() if n}
    # A snapshot unlike the revised counts, with an engine the check did not keep.
    det["settled_counts"] = {**{e: 40 + i for i, e in enumerate(sorted(kept))}, "opensearch": 99}
    assert _settled_passed(det) == {e: 40 + i for i, e in enumerate(sorted(kept))}


def test_a_result_without_the_snapshot_falls_back_to_the_revised_count() -> None:
    det = run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1)
    det.pop("settled_counts")
    kept = {e for e, n in det["after_distribution"].items() if n}
    expected = {e: n for e, n in settled_counts(det["revised_assignments"]).items() if e in kept}
    assert _settled_passed(det) == expected


def test_the_sanity_sweep_reads_the_same_snapshot() -> None:
    """The sweep's orphan tie-break reads the snapshot too (#746 review)."""
    det = run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1)
    det["settled_counts"] = {"dynamodb": 41, "documentdb": 40}
    seen: dict = {}
    real = handler.sanity_sweep

    def spy(*args, **kwargs):
        seen["settled"] = kwargs.get("settled")
        return real(*args, **kwargs)

    with patch.object(handler, "sanity_sweep", side_effect=spy):
        handler._run_sanity_sweep(det)
    assert seen["settled"] == {"dynamodb": 41, "documentdb": 40}


def _decision(qid: str, outcome: str, **kw: Any) -> dict:
    return {
        "query_ids": [qid],
        "flagged_engine": kw.pop("engine", "dynamodb"),
        "reason_category": "other",
        "explanation": "x",
        "outcome": outcome,
        "reason": "r",
        **kw,
    }


class TestAnIgnoredRecordAMoveSupersedes:
    """#746 review: one query can get an ``ignored`` record (a flag in another
    Bedrock batch, or on the wrong engine) and a ``moved`` one; the move wins."""

    def test_the_move_wins(self) -> None:
        out = handler._drop_superseded_ignored(
            [_decision("q1", "ignored", engine="opensearch"), _decision("q1", "moved")]
        )
        assert [(d["query_ids"], d["outcome"]) for d in out] == [(["q1"], "moved")]

    def test_a_group_member_moved_with_the_flag_counts_as_moved(self) -> None:
        out = handler._drop_superseded_ignored(
            [_decision("q2", "ignored"), _decision("q1", "moved", moved_with_group=["q2"])]
        )
        assert [d["outcome"] for d in out] == ["moved"]

    def test_an_ignored_query_nothing_moved_keeps_its_record(self) -> None:
        records = [_decision("q1", "ignored"), _decision("q2", "kept"), _decision("q3", "moved")]
        assert handler._drop_superseded_ignored(records) == records

    def test_the_flag_path_writes_the_deduped_list(self) -> None:
        det = run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1)
        qid, engine = next(
            (qa["query_id"], qa["assigned_engine"]) for qa in det["revised_assignments"]
        )
        det["flag_decisions"] = [_decision(qid, "ignored", engine="opensearch")]

        def fake(flags, revised, consolidations, *args, **kwargs):
            return revised, consolidations, [_decision(qid, "moved", engine=engine)], {}

        with patch.object(handler, "apply_consolidation_flags", side_effect=fake):
            apply_reality_check_llm_output(det, {"consolidation_corrections": [_flag(qid, engine)]})
        assert [(d["query_ids"], d["outcome"]) for d in det["flag_decisions"]] == [([qid], "moved")]
