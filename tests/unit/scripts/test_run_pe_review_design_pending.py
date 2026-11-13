# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_pe_review.py`` never writes a review of a design still being written (#2200).

``/modernize`` starts each engine's review as soon as that engine's design is complete,
while the other designs still run. The orchestrator knows a design is complete from its
``run_schema_design.py --status`` line; this guard makes the same check in the script, so
a review started early can never read a design whose subagent is still writing or
finalizing it: a design of the run with no review recorded whose ``--status`` is pending
(``not_finalized`` or ``finalize_pending``; ``not_split`` or ``drafts_pending`` for
DynamoDB), including one that has written nothing yet, makes the line ``design_pending``
(``designs_pending``), with or without ``--engine``, and no request is written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_pe_review, run_schema_design
from src.storage.local_store import LocalArtifactStore

DB, JOB, V = "shop", "job-1", 1
PREFIX = f"{DB}/{JOB}"
DOC = f"{PREFIX}/schema-documentdb/v{V}"
DDB = f"{PREFIX}/schema-dynamodb/v{V}"


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(f"{PREFIX}/collector/output.json", {"queries": {"query_patterns": []}})
    s.write_json(
        f"{PREFIX}/assignment/v{V}/assignment.json",
        {
            "version": V,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "documentdb", "in_scope": True},
                {"query_id": "q4", "assigned_engine": "dynamodb", "in_scope": True},
                {"query_id": "q5", "assigned_engine": "dynamodb", "in_scope": True},
            ],
        },
    )
    s.write_json(f"{PREFIX}/llm_responses/schema_design_documentdb.json", {"collections": []})
    s.write_json(
        f"{DOC}/schema_output.json",
        {"collections": [{"collection_name": "orders"}], "access_patterns": []},
    )
    s.write_json(
        f"{DDB}/groups_manifest.json",
        {"groups": [{"group_index": 0}, {"group_index": 1}], "total_groups": 2},
    )
    for idx, qid in ((0, "q4"), (1, "q5")):
        s.write_json(
            f"{DDB}/input_group_{idx}.json",
            {
                "collector_output": {
                    "queries": {"query_patterns": [{"query_id": qid, "tables_accessed": ["t"]}]},
                    "tables": [{"table_name": "t"}],
                }
            },
        )
    return s


def _finalized(store: LocalArtifactStore, status: str = "complete") -> None:
    """The record ``--finalize`` writes, on the response and output there now."""
    store.write_json(
        f"{DOC}/{run_schema_design._FINALIZE_RECORD}",
        {
            "status": status,
            "errors": [],
            "warnings": [],
            "response_sha256": run_schema_design._digest(
                store, f"{PREFIX}/llm_responses/schema_design_documentdb.json"
            ),
            "output_sha256": run_schema_design._digest(store, f"{DOC}/schema_output.json"),
        },
    )


def _draft(store: LocalArtifactStore, idx: int) -> None:
    store.write_json(
        f"{DDB}/schema_draft_group_{idx}.json",
        {"table_definitions": [], "access_patterns": [{"query_ids": [f"q{idx + 4}"]}]},
    )


def _run(monkeypatch, capsys, store: LocalArtifactStore, *extra: str) -> tuple[int, dict]:
    argv = ["run_pe_review.py", "--job-id", JOB, "--db", DB]
    argv += ["--artifact-root", str(store.base_dir), *extra]
    monkeypatch.setattr(sys, "argv", argv)
    code = 0
    try:
        run_pe_review.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    return code, json.loads(lines[0])


def _requests(store: LocalArtifactStore) -> list[str]:
    folder = Path(store.base_dir) / PREFIX / "llm_requests"
    return sorted(p.name for p in folder.glob("pe_review_*.json")) if folder.exists() else []


def test_a_design_written_but_not_finalized_gets_no_review(monkeypatch, capsys, store) -> None:
    code, line = _run(monkeypatch, capsys, store, "--engine", "documentdb")
    assert code == 0
    assert line["status"] == "design_pending"
    assert line["engine"] == "documentdb"
    assert line["design_status"] == "not_finalized"
    assert line["assignment_version"] == V
    assert "reviews" not in line
    assert _requests(store) == []


def test_a_design_changed_since_its_finalize_gets_no_review(monkeypatch, capsys, store) -> None:
    _finalized(store)
    store.write_json(f"{DOC}/schema_output.json", {"collections": [], "access_patterns": []})
    code, line = _run(monkeypatch, capsys, store, "--engine", "documentdb")
    assert code == 0 and line["status"] == "design_pending"
    assert line["design_status"] == "finalize_pending"
    assert _requests(store) == []


@pytest.mark.passes_on_main  # a complete design is reviewed, as before
def test_a_finalized_design_gets_its_review(monkeypatch, capsys, store) -> None:
    _finalized(store)
    code, line = _run(monkeypatch, capsys, store, "--engine", "documentdb")
    assert code == 0 and line["status"] == "awaiting_llm"
    assert [(r["engine"], r["group"]) for r in line["reviews"]] == [("documentdb", None)]
    assert _requests(store) == ["pe_review_documentdb.json"]


@pytest.mark.passes_on_main  # the guard holds only a pending design
def test_a_design_that_failed_validation_is_not_held_as_pending(monkeypatch, capsys, store) -> None:
    """Only a pending design is held: a finished one that failed its checks is the
    phase failure's business, as before."""
    _finalized(store, status="validation_failed")
    code, line = _run(monkeypatch, capsys, store, "--engine", "documentdb")
    assert code == 0 and line["status"] == "awaiting_llm"


def test_dynamodb_waits_for_every_part_draft(monkeypatch, capsys, store) -> None:
    _draft(store, 0)
    code, line = _run(monkeypatch, capsys, store, "--engine", "dynamodb")
    assert code == 0 and line["status"] == "design_pending"
    assert line["design_status"] == "drafts_pending"
    assert line["drafts_missing"] == [1]
    assert _requests(store) == []
    _draft(store, 1)
    code, line = _run(monkeypatch, capsys, store, "--engine", "dynamodb")
    assert code == 0 and line["status"] == "awaiting_llm"
    assert [r["group"] for r in line["reviews"]] == [0, 1]


def _assign_aurora(store: LocalArtifactStore) -> None:
    """Add an Aurora MySQL query: an engine in the run's schema-design list."""
    key = f"{PREFIX}/assignment/v{V}/assignment.json"
    assignment = store.read_json(key)
    assignment["query_assignments"].append(
        {"query_id": "q6", "assigned_engine": "aurora_mysql", "in_scope": True}
    )
    store.write_json(key, assignment)


def _record_review(store: LocalArtifactStore, engine: str, group: int | None = None) -> None:
    from src.agents.schema_design import pe_review_external as pe

    key = pe.trace_key(DB, JOB, engine, V, group)
    trace = store.read_json(key) if store.exists(key) else {}
    store.write_json(key, {**trace, pe.REVIEW_KEY: {"verdict": "APPROVED"}})


def test_without_an_engine_an_unfinalized_design_holds_the_whole_check(
    monkeypatch, capsys, store
) -> None:
    """Review of #2208, scenario A: the final check (and the "not sure" fallback) ran
    while DocumentDB was still finalizing and wrote a review request for it."""
    _draft(store, 0)
    _draft(store, 1)
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "design_pending"
    assert line["designs_pending"] == [{"engine": "documentdb", "design_status": "not_finalized"}]
    assert "reviews" not in line
    assert _requests(store) == []
    _finalized(store)
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "awaiting_llm"
    assert [(r["engine"], r["group"]) for r in line["reviews"]] == [
        ("documentdb", None),
        ("dynamodb", 0),
        ("dynamodb", 1),
    ]


def test_without_an_engine_a_design_not_started_is_never_complete(
    monkeypatch, capsys, store
) -> None:
    """Review of #2208, scenario B: an Aurora design that had written nothing yet let the
    final check print ``complete``, and 6e ran with that design still in flight."""
    _assign_aurora(store)
    _finalized(store)
    _draft(store, 0)
    _draft(store, 1)
    for engine, group in (("documentdb", None), ("dynamodb", 0), ("dynamodb", 1)):
        _record_review(store, engine, group)
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "design_pending"
    assert line["designs_pending"] == [{"engine": "aurora_mysql", "design_status": "not_finalized"}]
    assert line["next"].startswith("run each listed engine's run_schema_design.py --status")


@pytest.mark.parametrize(
    ("engine", "design_status"), [("aurora_mysql", "not_finalized"), ("dynamodb", "not_split")]
)
def test_with_an_engine_a_design_not_started_is_pending_not_complete(
    monkeypatch, capsys, store, engine, design_status
) -> None:
    """Review of #2208, item 4: an engine the run designs whose design has written
    nothing yet (DynamoDB before its split) printed ``complete``."""
    _assign_aurora(store)
    if engine == "dynamodb":
        store.delete(f"{DDB}/groups_manifest.json")
    code, line = _run(monkeypatch, capsys, store, "--engine", engine)
    assert code == 0 and line["status"] == "design_pending"
    assert line["engine"] == engine and line["design_status"] == design_status


@pytest.mark.passes_on_main  # the guard must not hold a reviewed design
def test_a_recorded_review_counts_as_done_even_if_the_design_moved_since(
    monkeypatch, capsys, store
) -> None:
    """A design carried forward with its review (a reroute, a redesign round) is done:
    its review is recorded, so its status line never holds the check."""
    _draft(store, 0)
    _draft(store, 1)
    for engine, group in (("documentdb", None), ("dynamodb", 0), ("dynamodb", 1)):
        _record_review(store, engine, group)
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "complete"
    code, line = _run(monkeypatch, capsys, store, "--engine", "dynamodb")
    assert code == 0 and line["status"] == "complete"
    assert line["recorded"] == [
        {"engine": "dynamodb", "group": 0},
        {"engine": "dynamodb", "group": 1},
    ]


def test_design_pending_names_its_next_step(monkeypatch, capsys, store) -> None:
    code, line = _run(monkeypatch, capsys, store, "--engine", "documentdb")
    assert code == 0 and line["status"] == "design_pending"
    assert "--status" in line["next"] and "phase failure" in line["next"]


@pytest.mark.passes_on_main  # a design never started, or skipped, still has nothing to review
@pytest.mark.parametrize("output", [None, {"status": "skipped", "reason": "nothing assigned"}])
def test_an_undesigned_or_skipped_engine_is_complete_not_pending(
    monkeypatch, capsys, store, output
) -> None:
    if output is not None:
        store.write_json(f"{PREFIX}/schema-opensearch/v{V}/schema_output.json", output)
    code, line = _run(monkeypatch, capsys, store, "--engine", "opensearch")
    assert code == 0 and line["status"] == "complete" and line["reviews"] == []


def _skip(monkeypatch, capsys, store: LocalArtifactStore, engine: str) -> tuple[int, dict]:
    argv = ["run_schema_design.py", "--job-id", JOB, "--db", DB, "--engine", engine]
    argv += ["--artifact-root", str(store.base_dir), "--skip"]
    monkeypatch.setattr(sys, "argv", argv)
    code = 0
    try:
        run_schema_design.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, json.loads(lines[-1])


def test_a_design_the_user_skipped_lets_the_final_check_go_on(monkeypatch, capsys, store) -> None:
    """Review round 2 of #2208: an interactive "skip" left the failed design in the run's
    list, so every final check printed design_pending for it and asked again. Skip writes
    the skipped placeholder, which the check treats as done, and the design gets no review."""
    _draft(store, 0)
    _draft(store, 1)
    code, line = _run(monkeypatch, capsys, store)
    assert line["status"] == "design_pending"
    code, skipped = _skip(monkeypatch, capsys, store, "documentdb")
    assert code == 0 and skipped["status"] == "skipped" and skipped["engine"] == "documentdb"
    assert store.read_json(f"{DOC}/schema_output.json")["status"] == "skipped"
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "awaiting_llm"
    assert [(r["engine"], r["group"]) for r in line["reviews"]] == [
        ("dynamodb", 0),
        ("dynamodb", 1),
    ]
    for engine, group in (("dynamodb", 0), ("dynamodb", 1)):
        _record_review(store, engine, group)
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "complete"


def test_a_skipped_dynamodb_design_with_missing_parts_lets_the_check_go_on(
    monkeypatch, capsys, store
) -> None:
    _finalized(store)
    _draft(store, 0)  # part 2 never wrote its draft
    code, skipped = _skip(monkeypatch, capsys, store, "dynamodb")
    assert code == 0 and skipped["status"] == "skipped"
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "awaiting_llm"
    assert [r["engine"] for r in line["reviews"]] == ["documentdb"]


def test_a_complete_design_is_never_skipped(monkeypatch, capsys, store) -> None:
    _finalized(store)
    code, line = _skip(monkeypatch, capsys, store, "documentdb")
    assert code == 1 and line["status"] == "error"
    assert store.read_json(f"{DOC}/schema_output.json").get("status") != "skipped"


@pytest.mark.passes_on_main  # main had no guard; this keeps the guard from over-holding
def test_a_leftover_design_of_an_engine_the_run_does_not_design_never_holds_the_check(
    monkeypatch, capsys, store
) -> None:
    """Review round 2 of #2208: an unfinalized folder for an engine outside
    ``schema_design_engines`` held the final check forever."""
    _finalized(store)
    _draft(store, 0)
    _draft(store, 1)
    store.write_json(f"{PREFIX}/schema-opensearch/v{V}/schema_output.json", {"indices": []})
    code, line = _run(monkeypatch, capsys, store)
    assert code == 0 and line["status"] == "awaiting_llm"
    assert "designs_pending" not in line
