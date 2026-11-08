# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``assess()`` records each phase's duration and engines, and enforces the opt-in
per-run ceilings (#145).

The phases are stand-ins that report model calls through ``record_usage`` (the hook
the model wrapper uses) and move a fake clock; no model is called. The real-phase
check (engines recorded by the actual phase code) is
``test_a_model_run_records_each_phases_duration_and_engines`` below, on the
wordpress sample with the model entry points patched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.assessment import RunLimitConfigError, runner
from src.assessment.runner import AssessmentRunError, assess
from src.shared import model_usage
from src.shared.run_limits import (
    ENV_MAX_RUN_MODEL_CALLS,
    ENV_MAX_RUN_SECONDS,
    ENV_MAX_RUN_TOKENS,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "assess45"
OPUS = "us.anthropic.claude-opus-5-5"
REQUEST = {
    "database_name": DB,
    "collection": {"metadata": {}},
    "options": {"run_through": "synthesis", "deterministic": False},
}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    """Four stand-in phases, each 10 s and one 100-token call; the result is a stub."""
    for name in (ENV_MAX_RUN_TOKENS, ENV_MAX_RUN_MODEL_CALLS, ENV_MAX_RUN_SECONDS):
        monkeypatch.delenv(name, raising=False)
    clock = _Clock()
    monkeypatch.setattr(model_usage, "_clock", clock)
    ran: list[str] = []

    def stand_in(name: str):
        def work(job: Any) -> None:
            ran.append(name)
            model_usage.record_engines(["dynamodb", "aurora_mysql"])
            model_usage.record_usage(OPUS, {"inputTokens": 90, "outputTokens": 10})
            clock.now += 10.0

        return work

    monkeypatch.setattr(
        runner,
        "PHASES",
        tuple((name, stand_in(name)) for name, _ in runner.PHASES),
    )
    monkeypatch.setattr(runner, "load_run_state", MagicMock(return_value="state"))
    monkeypatch.setattr(runner, "build_result", MagicMock(return_value="result"))
    store = LocalArtifactStore(base_dir=str(tmp_path))

    def run() -> dict[str, Any]:
        assert assess(REQUEST, store=store, job_id=JOB) == "result"
        record: dict[str, Any] = store.read_json(f"{DB}/{JOB}/{model_usage.USAGE_KEY}")
        return record

    run.ran = ran  # type: ignore[attr-defined]
    run.store = store  # type: ignore[attr-defined]
    yield run
    model_usage.LEDGER.reset()


def _record(run) -> dict[str, Any]:
    record: dict[str, Any] = run.store.read_json(f"{DB}/{JOB}/{model_usage.USAGE_KEY}")
    return record


def test_with_no_ceiling_every_phase_runs_and_is_timed(fake_run) -> None:
    record = fake_run()
    assert fake_run.ran == ["analysis", "reality_check", "schema_design", "synthesis"]
    run = record["runs"][-1]
    assert run["phases"] == {
        name: {"seconds": 10.0, "engines": ["dynamodb", "aurora_mysql"]}
        for name in ("analysis", "reality_check", "schema_design", "synthesis")
    }
    assert run["wall_clock_seconds"] == 40.0
    assert "ceiling_exceeded" not in run


def test_exactly_at_every_ceiling_the_run_completes(fake_run, monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAX_RUN_TOKENS, "400")
    monkeypatch.setenv(ENV_MAX_RUN_MODEL_CALLS, "4")
    monkeypatch.setenv(ENV_MAX_RUN_SECONDS, "40")
    fake_run()
    assert len(fake_run.ran) == 4


@pytest.mark.parametrize(
    ("name", "value", "ceiling", "figure"),
    [
        (ENV_MAX_RUN_TOKENS, "250", "tokens", "300 tokens"),
        (ENV_MAX_RUN_MODEL_CALLS, "2", "model_calls", "3 model calls"),
        (
            ENV_MAX_RUN_SECONDS,
            "29.5",
            "seconds",
            "wall-clock time since it started was 30.0 seconds",
        ),
    ],
)
def test_just_over_a_ceiling_the_run_stops_after_the_phase_and_fails(
    fake_run, monkeypatch, name: str, value: str, ceiling: str, figure: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(AssessmentRunError) as raised:
        fake_run()
    # Stopped at the phase boundary: the phase that crossed it finished, no later one ran.
    assert fake_run.ran == ["analysis", "reality_check", "schema_design"]
    message = str(raised.value)
    assert name in message and value in message and figure in message
    assert "in the schema_design phase" in message and "no result built" in message
    # The record of the failed run says which ceiling stopped it.
    run = _record(fake_run)["runs"][-1]
    assert run["ceiling_exceeded"]["ceiling"] == ceiling
    assert run["ceiling_exceeded"]["phase"] == "schema_design"
    assert list(run["phases"]) == ["analysis", "reality_check", "schema_design"]


@pytest.mark.parametrize("raw", ["lots", "0", "-1"])
def test_a_bad_ceiling_fails_before_any_phase_runs(fake_run, monkeypatch, raw: str) -> None:
    monkeypatch.setenv(ENV_MAX_RUN_MODEL_CALLS, raw)
    with pytest.raises(RunLimitConfigError, match=ENV_MAX_RUN_MODEL_CALLS):
        fake_run()
    assert fake_run.ran == []
    assert not fake_run.store.exists(f"{DB}/{JOB}/{model_usage.USAGE_KEY}")


def test_a_ceiling_is_checked_after_the_last_phase_too(fake_run, monkeypatch) -> None:
    """Going over in the last phase still fails the run: never a silent overrun."""
    monkeypatch.setenv(ENV_MAX_RUN_TOKENS, "399")
    with pytest.raises(AssessmentRunError, match="in the synthesis phase"):
        fake_run()
    assert len(fake_run.ran) == 4


def test_a_model_run_records_each_phases_duration_and_engines(tmp_path, monkeypatch) -> None:
    """The real phases on the wordpress sample (model entry points patched): each phase
    is timed, and records the engines it processed."""
    from tests.unit.assessment.conftest import collection
    from tests.unit.assessment.test_schema_design_synthesis import _Models

    for name in (ENV_MAX_RUN_TOKENS, ENV_MAX_RUN_MODEL_CALLS, ENV_MAX_RUN_SECONDS):
        monkeypatch.delenv(name, raising=False)
    models = _Models()
    real_designer = models.designer.side_effect

    def designer(*args: Any, **kwargs: Any) -> Any:
        model_usage.record_usage(OPUS, {"inputTokens": 10, "outputTokens": 1})
        return real_designer(*args, **kwargs)

    models.designer = MagicMock(side_effect=designer)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    patches = models.patches()
    for p in patches:
        p.start()
    try:
        result = assess(
            {
                "database_name": DB,
                "collection": collection(DB),
                "options": {"run_through": "synthesis", "deterministic": False},
            },
            store=store,
            job_id=JOB,
        )
    finally:
        for p in reversed(patches):
            p.stop()
    phases = store.read_json(f"{DB}/{JOB}/{model_usage.USAGE_KEY}")["runs"][-1]["phases"]
    assert list(phases) == ["analysis", "reality_check", "schema_design", "synthesis"]
    assert all(row["seconds"] >= 0 for row in phases.values())
    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    analysed = [a["agent_type"] for a in triage["selected_agents"]]
    assert phases["analysis"]["engines"] == analysed
    designed = list(result.summary.designed_engines)
    assert designed and set(designed) <= set(phases["schema_design"]["engines"])
    kept = phases["reality_check"]["engines"]
    assert kept and set(kept) <= set(analysed)
    # Schema design starts from the engines the reality check kept.
    assert phases["schema_design"]["engines"][: len(kept)] == kept
    assert set(phases["synthesis"]["engines"]) == set(designed)


def test_a_second_concurrent_ceilinged_run_in_the_process_fails_clearly(
    fake_run, monkeypatch
) -> None:
    """With a ceiling set, a second ``assess()`` while one runs would mix the two runs'
    counts against the ceiling, so it is refused; after the first ends, runs work."""
    import threading

    from src.assessment import ConcurrentRunError

    monkeypatch.setenv(ENV_MAX_RUN_SECONDS, "1000")

    entered, release = threading.Event(), threading.Event()
    first_phase = runner.PHASES[0][1]

    def blocking(job: Any) -> None:
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            release.wait(timeout=30)
        first_phase(job)

    monkeypatch.setattr(runner, "PHASES", (("analysis", blocking), *runner.PHASES[1:]))
    errors: list[BaseException] = []

    def first() -> None:
        try:
            fake_run()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=30)
    try:
        with pytest.raises(ConcurrentRunError, match="one ceilinged assess\\(\\) call at a time"):
            assess(REQUEST, job_id="other1")
    finally:
        release.set()
        thread.join(timeout=30)
    assert errors == []
    # The refused call touched nothing of the first run's, which completed in full.
    assert _record(fake_run)["runs"][-1]["totals"]["calls"] == 4
    fake_run()  # the process's run is free again


def test_the_lock_is_released_when_a_run_fails(fake_run, monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAX_RUN_TOKENS, "1")
    with pytest.raises(AssessmentRunError):
        fake_run()
    monkeypatch.setenv(ENV_MAX_RUN_TOKENS, "1000")  # a ceilinged run takes the lock again
    fake_run()


@pytest.mark.passes_on_main  # it fails on main only on a slow runner (#2047)
def test_schema_design_is_timed_as_wall_clock_over_its_concurrent_designers(
    tmp_path, monkeypatch
) -> None:
    """Real designer threads (the phase's pool) on a hand-moved clock: each designer
    works for 10 s at the same moment, so the phase takes 10 s of wall clock, not the
    30 s of summed thread time. No real time is measured, so a slow runner cannot move it."""
    import threading

    from tests.unit.assessment.conftest import collection
    from tests.unit.assessment.test_schema_design_synthesis import _Models

    for name in (ENV_MAX_RUN_TOKENS, ENV_MAX_RUN_MODEL_CALLS, ENV_MAX_RUN_SECONDS):
        monkeypatch.delenv(name, raising=False)
    clock = _Clock()
    monkeypatch.setattr(model_usage, "_clock", clock)
    models = _Models()
    real_designer = models.designer.side_effect
    work_s = 10.0
    # All three designers must be inside the pool at once to pass the barrier; a pool
    # that ran them one after the other would time out here.
    barrier = threading.Barrier(3, timeout=30)
    spans: list[float] = []
    threads: set[int] = set()
    lock = threading.Lock()

    def designer(*args: Any, **kwargs: Any) -> Any:
        start = clock()
        if barrier.wait() == 0:  # exactly one designer moves the shared clock
            clock.now += work_s
        barrier.wait()  # nobody reads the clock until it has moved
        model_usage.record_usage(OPUS, {"inputTokens": 10})
        out = real_designer(*args, **kwargs)
        with lock:
            spans.append(clock() - start)
            threads.add(threading.get_ident())
        return out

    models.designer = MagicMock(side_effect=designer)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    patches = models.patches()
    for p in patches:
        p.start()
    try:
        assess(
            {
                "database_name": DB,
                "collection": collection(DB),
                "options": {"run_through": "schema_design", "deterministic": False},
            },
            store=store,
            job_id=JOB,
        )
    finally:
        for p in reversed(patches):
            p.stop()
    seconds = store.read_json(f"{DB}/{JOB}/{model_usage.USAGE_KEY}")["runs"][-1]["phases"][
        "schema_design"
    ]["seconds"]
    assert len(threads) == 3 and spans == [work_s] * 3, (threads, spans)
    assert seconds == work_s, (seconds, spans)  # wall clock; summed thread time is 30


def test_a_forked_child_starts_with_no_run_in_progress() -> None:
    """The run lock is reset in a forked child: a child forked while the parent holds
    it (a ceilinged run in progress) can still run (os.register_at_fork)."""
    import os
    import warnings

    with model_usage.exclusive_run():  # the parent's run is in progress
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # multi-threaded fork
            pid = os.fork()
        if pid == 0:  # the child
            try:
                with model_usage.exclusive_run():
                    pass
            except BaseException:  # noqa: BLE001 - the exit code says it
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_the_ceiling_errors_are_part_of_the_package_surface() -> None:
    import src.assessment as package

    assert {"ConcurrentRunError", "RunLimitConfigError"} <= set(package.__all__)
    assert issubclass(package.RunLimitConfigError, ValueError)
    # Refused before anything ran: a RuntimeError, not a failed run.
    assert issubclass(package.ConcurrentRunError, RuntimeError)
    assert not issubclass(package.ConcurrentRunError, AssessmentRunError)
