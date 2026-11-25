# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Opening a redesign round is crash safe (#2157).

``plan_round`` moved each engine's design aside before it recorded the round open, so an
interrupt between the two left a schema folder without its design and no open round for
``run_wave_redesign.py --stop`` to close. The round now records its whole opening
(``wave-redesign/v<N>/opening.json``) before anything moves and applies it step by step;
the next read of the log finishes it. Each test below stops the opening at every write
or delete it makes, then checks that the job is in one of the two states an
uninterrupted run passes through: no round, with every design in place, or an open
round with its designs set aside that ``stop_round`` puts back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents.schema_design import wave_edit as we
from src.agents.schema_design import wave_redesign as wr
from src.agents.schema_design.wave_redesign import (
    _open_round,
    opening_key,
    plan_round,
    read_log,
    request_key,
    run_rounds,
    stop_round,
)
from src.shared.contract_versioning import is_internal_artifact
from src.storage.local_store import LocalArtifactStore

DB, JOB, V = "shop", "job-1", 2
REPORT = f"{DB}/{JOB}/synthesis/v{V}/report.json"
ENGINES = ("documentdb", "dynamodb")
#: Each engine's design: several files, so an interrupt can fall between two of them.
FILES = ("schema_output.json", "design_trace.json", "notes.json")


def _design_key(engine: str, name: str) -> str:
    return f"{DB}/{JOB}/schema-{engine}/v{V}/{name}"


def _hold(engine: str) -> dict[str, Any]:
    return {"check": "open_review", "engine": engine, "risk_id": None, "severity": None}


def _report() -> dict[str, Any]:
    """Wave 2 (DynamoDB) and wave 3 (DocumentDB) each held by an open review."""
    return {
        "schema_designs": {
            e: {
                "status": "completed",
                "reviewed_count": 1,
                "open_review_count": 1,
                "failed_review_count": 0,
                "unsupported_query_count": 0,
            }
            for e in ENGINES
        },
        "risk_assessment": {"risks": []},
        "facts": {
            "waves": [
                {"wave": 1, "engines": ["aurora_mysql"], "readiness": "suggested", "holds": []},
                {
                    "wave": 2,
                    "engines": ["dynamodb"],
                    "readiness": "held",
                    "holds": [_hold("dynamodb")],
                },
                {
                    "wave": 3,
                    "engines": ["documentdb"],
                    "readiness": "held",
                    "holds": [_hold("documentdb")],
                },
            ]
        },
    }


class Interrupted(BaseException):
    """The simulated interrupt (a ``BaseException``, as ``KeyboardInterrupt`` is)."""


class CrashingStore(LocalArtifactStore):
    """A local store that raises :class:`Interrupted` on its ``crash_at``-th write or
    delete, or on the first write or delete of a key holding ``crash_on``, once, and
    counts every one (both None: never)."""

    def __init__(self, base_dir: str, *, can_delete: bool = True) -> None:
        super().__init__(base_dir=base_dir)
        self.crash_at: int | None = None
        self.crash_on: str | None = None
        self.count = 0
        self.can_delete = can_delete

    def _step(self, path: str) -> None:
        self.count += 1
        if self.count == self.crash_at or (self.crash_on and self.crash_on in path):
            self.crash_at = self.crash_on = None
            raise Interrupted(f"interrupted at step {self.count} ({path})")

    def write_json(self, path: str, data: dict) -> None:
        self._step(path)
        super().write_json(path, data)

    def write_bytes(self, path: str, data: bytes) -> None:
        self._step(path)
        super().write_bytes(path, data)

    def delete(self, path: str) -> bool:
        self._step(path)
        # A store that cannot delete (S3 without delete rights) keeps the file.
        return super().delete(path) if self.can_delete else False


def _job(root: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> CrashingStore:
    store = CrashingStore(str(root), **kw)
    for engine in ENGINES:
        for name in FILES:
            store.write_json(_design_key(engine, name), {"engine": engine, "file": name})
    store.write_json(REPORT, {"wave_redesign": None})
    monkeypatch.setattr(
        wr,
        "_write_report_log",
        lambda store, db, job_id, version, log: store.write_json(
            REPORT, {"wave_redesign": dict(log)}
        ),
    )
    store.count = 0
    return store


def _designs(store: LocalArtifactStore) -> dict[str, bytes]:
    return {
        key: store.read_bytes(key)
        for engine in ENGINES
        for key in store.list_prefix(f"{DB}/{JOB}/schema-{engine}/v{V}/")
    }


def _steps(root: Path, monkeypatch: pytest.MonkeyPatch, open_round: Any) -> int:
    """How many writes and deletes an uninterrupted opening makes."""
    store = _job(root / "count", monkeypatch)
    open_round(store)
    return store.count


def _assert_consistent(store: LocalArtifactStore, before: dict[str, bytes]) -> None:
    """The job after a resume: no round and every design in place, or an open round with
    every design set aside and its request written; closing it puts every design back."""
    log = read_log(store, DB, JOB, V)
    assert not store.exists(opening_key(DB, JOB, V)) or log["rounds"]
    rnd = _open_round(log)
    if rnd is None:
        assert log["rounds"] == []
        assert _designs(store) == before
        assert not store.list_prefix(f"{DB}/{JOB}/wave-redesign/v{V}/round_")
        return
    assert sorted(rnd["engines"]) == sorted(ENGINES)
    for engine in ENGINES:
        assert not store.list_prefix(f"{DB}/{JOB}/schema-{engine}/v{V}/schema_")
        assert store.exists(request_key(DB, JOB, engine, V))
        for name in FILES:
            archived = f"{DB}/{JOB}/wave-redesign/v{V}/round_1/schema-{engine}/{name}"
            assert store.read_bytes(archived) == before[_design_key(engine, name)]
    closed = stop_round(store, DB, JOB, V, "the run was interrupted")
    assert closed["status"] == "awaiting_synthesis"
    assert sorted(closed["restored"]) == sorted(ENGINES)
    assert _designs(store) == before
    assert _open_round(read_log(store, DB, JOB, V)) is None


def _plan(store: LocalArtifactStore) -> dict[str, Any]:
    return plan_round(store, DB, JOB, _report(), V)


def test_an_uninterrupted_opening_sets_every_design_aside_and_leaves_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _job(tmp_path, monkeypatch)
    before = _designs(store)
    assert _plan(store)["status"] == "awaiting_redesign"
    assert not store.exists(opening_key(DB, JOB, V))
    assert is_internal_artifact(opening_key(DB, JOB, V))
    _assert_consistent(store, before)


def test_an_interrupt_at_any_step_of_the_opening_leaves_a_consistent_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    total = _steps(tmp_path, monkeypatch, _plan)
    # The record, every file of both engines moved (a write and a delete each), the two
    # requests, the log and the record's delete.
    assert total >= 2 + 2 * 2 * len(FILES)
    for step in range(1, total + 1):
        store = _job(tmp_path / f"step-{step}", monkeypatch)
        before = _designs(store)
        store.crash_at = step
        with pytest.raises(Interrupted):
            _plan(store)
        _assert_consistent(store, before)


def test_an_interrupt_after_the_first_engine_moved_resumes_to_an_open_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's case: one engine's design is aside, the log names no round. The next
    read of the log finishes the opening, so --stop has a round to close."""
    store = _job(tmp_path, monkeypatch)
    before = _designs(store)
    # Every file of the first engine (documentdb) is aside; the second's first is not.
    store.crash_on = "round_1/schema-dynamodb/"
    with pytest.raises(Interrupted):
        _plan(store)
    assert not store.exists(_design_key("documentdb", "schema_output.json"))
    assert store.exists(_design_key("dynamodb", "schema_output.json"))
    rnd = _open_round(read_log(store, DB, JOB, V))
    assert rnd is not None and rnd["round"] == 1
    assert not store.exists(_design_key("dynamodb", "schema_output.json"))
    assert not store.exists(opening_key(DB, JOB, V))
    _assert_consistent(store, before)


def test_the_in_process_loop_puts_the_design_back_after_an_interrupt_while_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_rounds`` closes an open round when anything stops it (#2122): an interrupt
    during the opening now leaves one to close, at every step."""
    total = _steps(tmp_path, monkeypatch, _plan)
    for step in range(1, total + 1):
        store = _job(tmp_path / f"loop-{step}", monkeypatch)
        before = _designs(store)
        store.crash_at = step
        with pytest.raises(Interrupted):
            run_rounds(
                store,
                DB,
                JOB,
                version=V,
                read_report=_report,
                design=lambda engine: None,
                synthesize=lambda: None,
            )
        assert _designs(store) == before, f"step {step}"
        assert _open_round(read_log(store, DB, JOB, V)) is None


def test_a_record_cut_short_is_dropped_with_every_design_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is written before anything moves: one that does not read moved
    nothing."""
    store = _job(tmp_path, monkeypatch)
    before = _designs(store)
    store.write_bytes(opening_key(DB, JOB, V), b'{"round": 1, "mov')
    assert read_log(store, DB, JOB, V)["rounds"] == []
    assert not store.exists(opening_key(DB, JOB, V))
    assert _designs(store) == before


def test_a_store_that_cannot_delete_never_opens_the_round_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a store that keeps what it deletes (S3 without delete rights) the record stays
    after the round opened: the log holds its round, so it is never applied again."""
    store = _job(tmp_path, monkeypatch, can_delete=False)
    assert _plan(store)["status"] == "awaiting_redesign"
    assert store.exists(opening_key(DB, JOB, V))
    stop_round(store, DB, JOB, V, "closed")
    log = read_log(store, DB, JOB, V)
    assert [r["round"] for r in log["rounds"]] == [1]
    assert _open_round(log) is None


def test_a_wave_edit_round_opens_crash_safe_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/wave-edit`` opens its rounds through the same record (#2067)."""

    def start(store: LocalArtifactStore) -> dict[str, Any]:
        log = read_log(store, DB, JOB, V)
        requests = {e: {"engine": e, "waves": [2], "checks": [_hold(e)]} for e in ENGINES}
        return we._start(store, DB, JOB, _report(), V, 2, 1, requests, log, 1)

    total = _steps(tmp_path, monkeypatch, start)
    for step in range(1, total + 1):
        store = _job(tmp_path / f"edit-{step}", monkeypatch)
        before = _designs(store)
        store.crash_at = step
        with pytest.raises(Interrupted):
            start(store)
        log = read_log(store, DB, JOB, V)
        rnd = _open_round(log)
        if rnd is None:
            assert _designs(store) == before, f"step {step}"
            continue
        assert rnd["wave_edit"] == 2
        assert not any(k.endswith("schema_output.json") for k in _designs(store))
        assert wr.fail_round(store, DB, JOB, V, "interrupted")["status"] == "rolled_back"
        assert _designs(store) == before, f"step {step}"
