"""``assess()`` keeps the caller's stdout clean: the phases' progress lines go to the
``src.assessment.progress`` logger instead (found by the library demo,
``docs/examples/library/demo_assess.py``).

A library that prints its progress mixes it into whatever its caller writes to stdout
(a JSON document, a report, a demo's own lines). The phases report progress with
``src.shared.progress.say``: it prints for the command line, and logs inside
``to_logger``, which ``assess()`` wraps its run in. ``sys.stdout`` is never replaced,
so another thread's output during a run is untouched.
"""

from __future__ import annotations

import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest

from src.assessment import assess
from src.shared.progress import in_context, say, to_logger
from tests.unit.assessment.conftest import collection

PROGRESS_LOGGER = "src.assessment.progress"


def _request(run_through: str = "reality_check") -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "collection": collection("wordpress"),
        "options": {"run_through": run_through},
    }


def _logged(caplog: pytest.LogCaptureFixture, name: str = PROGRESS_LOGGER) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == name]


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_a_run_writes_nothing_to_stdout(
    run_through: str, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        assess(_request(run_through))
    assert capsys.readouterr().out == ""
    progress = _logged(caplog)
    assert any(line.startswith("[triage] Starting triage for wordpress") for line in progress)
    if run_through == "reality_check":
        assert any(line.startswith("[reality-check]") for line in progress)
    # One record per line, no blank ones.
    assert all(line.strip() and "\n" not in line for line in progress)


def test_the_runner_names_the_logger_it_uses() -> None:
    from src.assessment.runner import PROGRESS_LOGGER as RUNNER_LOGGER

    assert RUNNER_LOGGER == PROGRESS_LOGGER


def test_another_threads_print_during_a_run_still_reaches_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A thread the run did not start prints while the run is in progress: its line
    goes to stdout, and the run's own progress still goes to the logger."""
    from src.assessment import runner

    in_run, printed = threading.Event(), threading.Event()

    def other() -> None:
        in_run.wait(timeout=30)
        print("[caller] a line from another thread")
        printed.set()

    def phase(job: Any) -> None:
        runner._ingest_and_analyse(job)
        in_run.set()
        assert printed.wait(timeout=30)
        say("[run] after the other thread printed")

    monkeypatch.setattr(runner, "PHASES", (("analysis", phase),))
    thread = threading.Thread(target=other)
    thread.start()
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        assess(_request("analysis"))
    thread.join(timeout=30)
    assert capsys.readouterr().out == "[caller] a line from another thread\n"
    assert "[run] after the other thread printed" in _logged(caplog)


def test_stdout_is_untouched_by_a_run_and_by_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``sys.stdout`` is never replaced, and once a run ends, normally or by raising,
    say() prints again: the logger setting does not outlive the run."""
    from src.assessment import runner

    before = sys.stdout
    assess(_request("analysis"))
    assert sys.stdout is before
    capsys.readouterr()
    say("[after] a normal run")
    assert capsys.readouterr().out == "[after] a normal run\n"

    def fail(job: Any) -> None:
        assert sys.stdout is before
        say("[fail] half a line", end="")
        raise RuntimeError("phase failed")

    monkeypatch.setattr(runner, "PHASES", (("analysis", fail),))
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        with pytest.raises(RuntimeError, match="phase failed"):
            assess(_request("analysis"))
    assert sys.stdout is before
    # The unfinished line is still logged, with the run's job id.
    records = [r for r in caplog.records if r.name == PROGRESS_LOGGER]
    assert [r.getMessage() for r in records] == ["[fail] half a line"]
    capsys.readouterr()
    say("[after] a raising run")
    assert capsys.readouterr().out == "[after] a raising run\n"


def test_each_record_names_the_runs_job(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        assess(_request("analysis"), job_id="jobname1")
    records = [r for r in caplog.records if r.name == PROGRESS_LOGGER]
    assert records and {getattr(r, "job_id", None) for r in records} == {"jobname1"}


def test_both_design_pools_hand_the_run_on(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The real schema design path, with only the agent call replaced: the runner's
    design pool (one thread per engine) and schema design's group pool (DynamoDB's 40
    statements split into groups of at most 20) both log their threads' progress.
    Without ``in_context`` at either pool, those threads' lines would reach stdout."""
    from src.agents.schema_design.group_splitter import MAX_GROUP_SIZE
    from src.assessment.runner import AssessmentRunError
    from src.storage.local_store import LocalArtifactStore
    from tests.assess_models import DESIGNER, MODEL_ENTRY_POINTS

    placed = assess(_request("reality_check"))
    assert placed.summary.statements_per_engine["dynamodb"] > MAX_GROUP_SIZE
    capsys.readouterr()

    def agent(target_type: str, **kw: Any) -> Any:
        say(f"[agent] {target_type} on {threading.current_thread().name}")
        raise RuntimeError("no model in this test")

    request = _request("schema_design")
    request["options"]["deterministic"] = False
    others = [(target, value) for target, value in MODEL_ENTRY_POINTS if target != DESIGNER]
    with ExitStack() as stack:
        for target, value in others:
            stack.enter_context(patch(target, return_value=value))
        stack.enter_context(
            patch("src.agents.schema_design.handler._dispatch_schema_agent", side_effect=agent)
        )
        stack.enter_context(caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER))
        with pytest.raises(AssessmentRunError, match="schema design failed"):
            assess(request, store=LocalArtifactStore(base_dir=str(tmp_path)), job_id="pools1")
    assert capsys.readouterr().out == ""
    lines = _logged(caplog)
    # The runner's pool: every designed engine's first line, from its worker thread.
    assert any(line.startswith("[schema-design/aurora_mysql]") for line in lines)
    assert any("splitting into groups" in line for line in lines)
    # The group pool: the agent ran in schema design's own worker threads.
    agents = [line for line in lines if line.startswith("[agent] dynamodb on ")]
    assert agents and all("ThreadPoolExecutor" in line for line in agents)


# --------------------------------------------------------------------------- say()


def test_say_prints_exactly_like_print_outside_a_run(capsys: pytest.CaptureFixture[str]) -> None:
    say("[a]", 1, "two", sep="-", end="!\n")
    say("[b] plain")
    say()
    say("[c] to stderr", file=sys.stderr)
    out, err = capsys.readouterr()
    assert out == "[a]-1-two!\n[b] plain\n\n"
    assert err == "[c] to stderr\n"


def test_partial_lines_are_joined_and_flushed(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("tests.progress")
    with caplog.at_level(logging.INFO, logger="tests.progress"):
        with to_logger(logger):
            say("[a] one", end="")
            say(" piece\n[b] two\n")
            say()
            say("[n] end None ends the line", end=None)
            say("[c] tail", end="")
            say("[d] still printed", file=sys.stderr)
    assert _logged(caplog, "tests.progress") == [
        "[a] one piece",
        "[b] two",
        "[n] end None ends the line",
        "[c] tail",
    ]
    out, err = capsys.readouterr()
    assert out == "" and err == "[d] still printed\n"


def test_pool_work_follows_the_run_only_through_in_context(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("tests.progress")
    with caplog.at_level(logging.INFO, logger="tests.progress"):
        with to_logger(logger), ThreadPoolExecutor(max_workers=2) as pool:
            handed = [pool.submit(in_context(say), f"[pool] {i}") for i in range(4)]
            bare = pool.submit(say, "[bare] not handed through")
            for future in [*handed, bare]:
                future.result()
    assert sorted(_logged(caplog, "tests.progress")) == [f"[pool] {i}" for i in range(4)]
    assert capsys.readouterr().out == "[bare] not handed through\n"


def test_a_designed_run_writes_nothing_to_stdout(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Schema design, the post-schema routing and synthesis log
    their progress as well (the model entry points are the shared stand-ins)."""
    from tests.assess_models import models_patched

    request = _request("synthesis")
    request["options"]["deterministic"] = False
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER), models_patched():
        assess(request)
    assert capsys.readouterr().out == ""
    progress = _logged(caplog)
    assert any(line.startswith("[synthesis]") for line in progress)
