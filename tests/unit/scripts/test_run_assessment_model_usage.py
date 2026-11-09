# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_assessment.py`` records the run's model usage the way ``assess()`` does (#1675).

It also sends the structured-output re-ask lines to the run log (compact mode), and a
later run of the same job adds a run to the record instead of overwriting it.

A Bedrock run of the script used to leave no ``_logs/model_usage.json``, so its cost
could not be read without extra tooling. The script now uses the same recorder as
``assess()`` (``src/shared/model_usage.py``): it clears the ledger when it starts,
counts each call under the phase running (``_start_phase``), and writes the totals
when it ends, also when a phase fails; a run that called no model writes nothing.
``scripts/usage_cost.py`` prices the file.

The phases are replaced by a stand-in that reports calls through ``record_usage``,
the hook the model wrapper and the direct boto3 calls use; no model is called.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_assessment, usage_cost
from src.shared import model_usage
from src.shared.models import MODEL_IDS
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "abcd1234"
OPUS, SONNET = MODEL_IDS["opus-5.5"], MODEL_IDS["sonnet-5.5"]


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Call ``main()`` with the phases replaced by ``phases(store)``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_assessment, "_JOB", {})
    monkeypatch.setattr(run_assessment, "_CONSOLE", None)
    root: Path = tmp_path / "artifacts"

    def _main(phases, *extra: str) -> Path:
        def fake_run(args) -> None:
            store = LocalArtifactStore(base_dir=str(root))
            run_assessment._attach_log(str(root), DB, JOB)
            run_assessment._set_job_context(store, DB, JOB)
            phases(store)

        monkeypatch.setattr(run_assessment, "_run", fake_run)
        monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--job-id", JOB, "--db", DB, *extra])
        try:
            run_assessment.main()
        except SystemExit:
            pass
        return root / DB / JOB / "_logs" / "model_usage.json"

    yield _main
    model_usage.LEDGER.reset()  # the ledger is process-wide: leave it empty


def _bedrock_run(store) -> None:
    run_assessment._start_phase("reality_check")
    model_usage.record_usage(SONNET, {"inputTokens": 1000, "outputTokens": 100})
    run_assessment._start_phase("schema_design")
    model_usage.record_usage(
        OPUS,
        {
            "inputTokens": 2000,
            "outputTokens": 500,
            "cacheReadInputTokens": 30000,
            "cacheWriteInputTokens": 4000,
        },
    )
    model_usage.record_usage(OPUS, {"inputTokens": 100, "outputTokens": 50})
    run_assessment._start_phase("synthesis")


@pytest.mark.parametrize("mode", [(), ("--verbose",)], ids=["compact", "verbose"])
def test_a_run_that_calls_a_model_writes_the_assess_record(run, mode):
    model_usage.record_usage(OPUS, {"inputTokens": 999999})  # a previous run's: cleared
    path = run(_bedrock_run, *mode)
    record = json.loads(path.read_text())
    assert record["by_phase"] == {
        "reality_check": {
            SONNET: {
                "calls": 1,
                "inputTokens": 1000,
                "outputTokens": 100,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 0,
            }
        },
        "schema_design": {
            OPUS: {
                "calls": 2,
                "inputTokens": 2100,
                "outputTokens": 550,
                "cacheReadInputTokens": 30000,
                "cacheWriteInputTokens": 4000,
            }
        },
    }
    assert record["totals"]["calls"] == 3
    # The same shape assess() writes: one recorder, one file format.
    assert set(record) == {
        "totals",
        "by_model",
        "by_phase",
        "runs",
        "wall_clock_seconds",
        "phases",
    }
    assert [r["run"] for r in record["runs"]] == [1]
    assert record["runs"][0]["by_phase"] == record["by_phase"]
    assert path.relative_to(path.parents[2]).as_posix() == f"{JOB}/{model_usage.USAGE_KEY}"


def test_usage_cost_prices_the_record(run, capsys):
    path = run(_bedrock_run)
    cost = usage_cost.compute(json.loads(path.read_text()), usage_cost.load_prices())
    prices = usage_cost.load_prices()
    opus, sonnet = prices[OPUS], prices[SONNET]
    expected_design = (
        2100 * opus["input"]
        + 550 * opus["output"]
        + 30000 * opus["cache_read"]
        + 4000 * opus["cache_write"]
    ) / 1e6
    assert cost["by_phase"]["schema_design"]["total"] == pytest.approx(expected_design)
    assert cost["by_phase"]["reality_check"]["total"] == pytest.approx(
        (1000 * sonnet["input"] + 100 * sonnet["output"]) / 1e6
    )
    capsys.readouterr()
    assert usage_cost.main([str(path)]) == 0
    assert "schema_design: $" in capsys.readouterr().out


def test_a_failed_run_still_records_what_it_spent(run):
    def failing(store) -> None:
        run_assessment._start_phase("schema_design")
        model_usage.record_usage(OPUS, {"inputTokens": 10, "outputTokens": 5})
        run_assessment._error("schema_design", "designer failed")

    path = run(failing)
    record = json.loads(path.read_text())
    assert record["by_phase"]["schema_design"][OPUS]["calls"] == 1


def test_an_unexpected_error_still_records_and_propagates(run):
    def crashing(store) -> None:
        run_assessment._start_phase("synthesis")
        model_usage.record_usage(SONNET, {"inputTokens": 10})
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run(crashing)
    path = Path("artifacts") / DB / JOB / "_logs" / "model_usage.json"
    assert json.loads(path.read_text())["by_phase"]["synthesis"][SONNET]["calls"] == 1


def test_a_run_with_no_model_call_records_its_timings_with_zero_counts(run, monkeypatch):
    """Every run of the script is recorded (#145), so the default /modernize path
    (external mode) and --llm-mode none runs are measurable per phase too."""
    clock = _Clock()
    monkeypatch.setattr(model_usage, "_clock", clock)

    def deterministic(store) -> None:
        run_assessment._start_phase("analysis")
        clock.now += 3.0

    record = json.loads(run(deterministic).read_text())
    assert record["totals"]["calls"] == 0 and record["by_phase"] == {}
    assert record["runs"][-1]["phases"] == {"analysis": {"seconds": 3.0, "engines": []}}
    # usage_cost.py prices it as $0.
    assert usage_cost.compute(record, usage_cost.load_prices())["total_usd"] == 0


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_write_error_never_hides_the_run(run, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(model_usage, "write_usage", broken)
    path = run(_bedrock_run, "--verbose")  # main() returns normally
    assert not path.exists()


def test_a_second_run_of_the_job_adds_a_run_and_sums_the_totals(run, capsys):
    run(_bedrock_run)

    def resumed(store) -> None:
        run_assessment._start_phase("synthesis")
        model_usage.record_usage(SONNET, {"inputTokens": 7, "outputTokens": 3})

    path = run(resumed)
    record = json.loads(path.read_text())
    assert [r["run"] for r in record["runs"]] == [1, 2]
    assert record["runs"][1]["by_phase"] == {
        "synthesis": {
            SONNET: {
                "calls": 1,
                "inputTokens": 7,
                "outputTokens": 3,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 0,
            }
        }
    }
    # The first run is kept, and the top level is the sum of both.
    assert record["by_phase"]["schema_design"][OPUS]["calls"] == 2
    assert record["by_phase"]["synthesis"][SONNET]["calls"] == 1
    assert record["totals"]["calls"] == 4
    cost = usage_cost.compute(record, usage_cost.load_prices())
    assert [r["run"] for r in cost["runs"]] == [1, 2]
    assert sum(r["total_usd"] for r in cost["runs"]) == pytest.approx(cost["total_usd"])
    capsys.readouterr()
    assert usage_cost.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "run 1: $" in out and "run 2: $" in out


def test_a_record_from_before_runs_were_kept_becomes_run_one(run):
    root = Path("artifacts") / DB / JOB / "_logs"
    root.mkdir(parents=True)
    legacy = {
        "totals": {"calls": 1, "inputTokens": 5},
        "by_model": {OPUS: {"calls": 1, "inputTokens": 5}},
        "by_phase": {"schema_design": {OPUS: {"calls": 1, "inputTokens": 5}}},
    }
    (root / "model_usage.json").write_text(json.dumps(legacy))
    record = json.loads(run(_bedrock_run).read_text())
    assert [r["run"] for r in record["runs"]] == [1, 2]
    assert record["runs"][0]["by_phase"] == legacy["by_phase"]
    assert record["by_phase"]["schema_design"][OPUS]["calls"] == 3
    assert record["by_phase"]["schema_design"][OPUS]["inputTokens"] == 2105


def test_a_reask_lands_in_the_run_log_and_not_on_stdout(run, capsys):
    """A real Strands designer whose first output fails validation, inside the run."""
    from tests.unit.shared.test_prompt_cache import (
        SCHEMA_MODELS,
        _designer_turns,
        _run_designer,
        _stream,
    )

    load, valid = _designer_turns()
    invalid = _stream(
        [{"toolUse": ("_Design", {"tables": "customer_secret_table"})}],
        "tool_use",
        {"inputTokens": 40, "outputTokens": 15},
    )

    def designing(store) -> None:
        run_assessment._start_phase("schema_design")
        _run_designer(SCHEMA_MODELS[0], conversation=True, responses=[load, invalid, valid])

    capsys.readouterr()
    run(designing)
    stdout = capsys.readouterr().out
    log = (Path("artifacts") / DB / JOB / "_logs" / "run_assessment.log").read_text()
    lines = [line for line in log.splitlines() if "structured output asked again" in line]
    assert len(lines) == 1
    assert lines[0].startswith("[model] ")
    assert "_Design input failed validation, 1 error(s) at: tables" in lines[0]
    assert "customer_secret_table" not in log
    # Compact mode: stdout keeps only the JSON status and pointer lines.
    assert "asked again" not in stdout
    assert all(line.startswith("{") for line in stdout.splitlines() if line.strip())
    # The route is only for the run: afterwards the logger is back as it was.
    from src.shared.role_bedrock_model import REASK_LOGGER

    assert REASK_LOGGER.handlers == []


# --------------------------------------------------------------------------- phase timings and ceilings (#145)

CEILINGS = (
    "MODERNIZER_MAX_RUN_TOKENS",
    "MODERNIZER_MAX_RUN_MODEL_CALLS",
    "MODERNIZER_MAX_RUN_SECONDS",
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    for name in CEILINGS:
        monkeypatch.delenv(name, raising=False)
    fake = _Clock()
    monkeypatch.setattr(model_usage, "_clock", fake)
    return fake


def _timed_run(clock):
    """Three phases of 5, 60 and 2 seconds, one call each (2,200 tokens in all)."""

    def phases(store) -> None:
        store.write_json(f"{DB}/{JOB}/collector/output.json", {})  # a real job's marker
        for name, seconds, model, tokens in (
            ("reality_check", 5.0, SONNET, 1000),
            ("schema_design", 60.0, OPUS, 1000),
            ("synthesis", 2.0, SONNET, 200),
        ):
            run_assessment._start_phase(name)
            model_usage.record_usage(model, {"inputTokens": tokens})
            clock.now += seconds

    return phases


def _status_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


@pytest.mark.parametrize("mode", [(), ("--verbose",)], ids=["compact", "verbose"])
def test_each_phase_is_timed_in_the_record(run, clock, mode):
    record = json.loads(run(_timed_run(clock), *mode).read_text())
    phases = record["runs"][-1]["phases"]
    assert {name: row["seconds"] for name, row in phases.items()} == {
        "reality_check": 5.0,
        "schema_design": 60.0,
        "synthesis": 2.0,
    }
    assert record["runs"][-1]["wall_clock_seconds"] == 67.0
    assert record["phases"] == phases


def test_with_no_ceiling_set_the_run_completes(run, clock, capsys):
    path = run(_timed_run(clock))
    lines = _status_lines(capsys.readouterr().out)
    assert not [line for line in lines if line.get("status") == "error"]
    assert "ceiling_exceeded" not in json.loads(path.read_text())["runs"][-1]


def test_exactly_at_the_ceilings_the_run_completes(run, clock, capsys, monkeypatch):
    monkeypatch.setenv("MODERNIZER_MAX_RUN_TOKENS", "2200")
    monkeypatch.setenv("MODERNIZER_MAX_RUN_MODEL_CALLS", "3")
    monkeypatch.setenv("MODERNIZER_MAX_RUN_SECONDS", "67")
    run(_timed_run(clock))
    lines = _status_lines(capsys.readouterr().out)
    assert not [line for line in lines if line.get("status") == "error"]


@pytest.mark.parametrize(
    ("name", "value", "ceiling", "phase", "figure"),
    [
        ("MODERNIZER_MAX_RUN_TOKENS", "1999", "tokens", "schema_design", "2,000 tokens"),
        ("MODERNIZER_MAX_RUN_MODEL_CALLS", "1", "model_calls", "schema_design", "2 model calls"),
        ("MODERNIZER_MAX_RUN_SECONDS", "4.5", "seconds", "reality_check", "5.0 seconds"),
    ],
)
def test_just_over_a_ceiling_the_run_stops_with_a_failed_status(
    run, clock, capsys, monkeypatch, name, value, ceiling, phase, figure
):
    monkeypatch.setenv(name, value)
    path = run(_timed_run(clock))
    lines = _status_lines(capsys.readouterr().out)
    (failed,) = [line for line in lines if line.get("status") == "error"]
    assert failed["phase"] == phase
    assert name in failed["message"] and value in failed["message"]
    assert figure in failed["message"]
    assert failed["ceiling"] == ceiling
    # Stopped at the phase boundary: no later phase started.
    record = json.loads(path.read_text())
    run_record = record["runs"][-1]
    assert list(run_record["phases"])[-1] == phase
    assert run_record["ceiling_exceeded"]["ceiling"] == ceiling
    # The job's status marker says it failed, and why.
    marker = json.loads((path.parents[1] / "_status.json").read_text())
    assert marker["status"] == "FAILED" and marker["phase"] == phase
    assert marker["error_type"] == "RunLimitExceeded"


@pytest.mark.parametrize("mode", [(), ("--verbose",)], ids=["compact", "verbose"])
def test_a_ceiling_crossed_in_the_last_phase_still_fails(run, clock, capsys, monkeypatch, mode):
    monkeypatch.setenv("MODERNIZER_MAX_RUN_TOKENS", "2199")
    run(_timed_run(clock), *mode)
    (failed,) = [
        line for line in _status_lines(capsys.readouterr().out) if line.get("status") == "error"
    ]
    assert failed["phase"] == "synthesis" and failed["ceiling"] == "tokens"


@pytest.mark.parametrize("raw", ["ten", "0", "-3"])
def test_a_bad_ceiling_fails_fast_before_the_run(clock, capsys, monkeypatch, tmp_path, raw):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODERNIZER_MAX_RUN_SECONDS", raw)
    started: list[str] = []
    monkeypatch.setattr(run_assessment, "_run", lambda args: started.append("ran"))
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--job-id", JOB, "--db", DB])
    with pytest.raises(SystemExit) as raised:
        run_assessment.main()
    assert raised.value.code == 2
    assert started == []
    err = capsys.readouterr().err
    assert "MODERNIZER_MAX_RUN_SECONDS" in err and repr(raw) in err


def test_external_mode_counts_no_call_so_a_call_ceiling_never_trips(
    run, clock, capsys, monkeypatch
):
    """``--llm-mode external``: Claude Code answers the model's part outside this
    process, so the ledger counts nothing and a token or call ceiling has nothing to
    count; only the wall-clock ceiling applies, to the script's own time."""
    monkeypatch.setenv("MODERNIZER_MAX_RUN_MODEL_CALLS", "1")
    monkeypatch.setenv("MODERNIZER_MAX_RUN_TOKENS", "1")

    def external(store) -> None:
        run_assessment._start_phase("reality_check")  # writes llm_input.json and stops

    path = run(external, "--llm-mode", "external")
    lines = _status_lines(capsys.readouterr().out)
    assert not [line for line in lines if line.get("status") == "error"]
    record = json.loads(path.read_text())  # recorded, with no call counted
    assert record["totals"]["calls"] == 0 and "reality_check" in record["runs"][-1]["phases"]


def test_the_real_phases_record_the_engines_they_processed(monkeypatch, tmp_path, clock):
    """The script's own phases on the wordpress sample (no model: one call is reported
    from the assignment phase so the record is written; the model phases after the
    reality check are stand-ins naming the engines they designed)."""
    import zipfile

    repo = Path(__file__).resolve().parents[3]
    with zipfile.ZipFile(repo / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(tmp_path / "input")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_assessment, "_JOB", {})
    monkeypatch.setattr(run_assessment, "_CONSOLE", None)
    real_assignment = run_assessment.phase_assignment

    def assignment(store, job_id, db):
        model_usage.record_usage(SONNET, {"inputTokens": 1})
        return real_assignment(store, job_id, db)

    designed: list[str] = []

    def schema_design(store, job_id, db, llm_mode):
        designed.extend(run_assessment._surviving_engines(store, job_id, db, ["dynamodb"]))
        return {"status": "complete", "artifacts": {e: "x" for e in designed}}

    monkeypatch.setattr(run_assessment, "phase_assignment", assignment)
    monkeypatch.setattr(run_assessment, "phase_schema_design", schema_design)
    monkeypatch.setattr(run_assessment, "phase_synthesis", lambda *a, **k: None)
    monkeypatch.setattr(run_assessment, "phase_report", lambda *a, **k: {"status": "complete"})
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assessment.py",
            "--file",
            str(tmp_path / "input" / "wordpress-collection.json"),
            "--db",
            "wordpress",
            "--llm-mode",
            "none",
            "--all",
            "-y",
            "--artifact-root",
            str(artifacts),
        ],
    )
    try:
        run_assessment.main()
    finally:
        model_usage.LEDGER.reset()
    state = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    job = artifacts / "wordpress" / state["job_id"]
    phases = json.loads((job / "_logs" / "model_usage.json").read_text())["runs"][-1]["phases"]
    triage = json.loads((job / "referee-triage" / "triage.json").read_text())
    selected = [a["agent_type"] for a in triage["selected_agents"]]
    assert list(phases) == [
        "collect",
        "triage",
        "analysis",
        "assignment",
        "reality_check",
        "routine_migration",  # #1934: timed like any phase, on the version it reads
        "engine_choices",
        "schema_design",
        "synthesis",
        "report",
    ]
    assert phases["collect"]["engines"] == []
    assert phases["triage"]["engines"] == selected
    assert phases["analysis"]["engines"] == selected
    assert phases["assignment"]["engines"] == selected
    surviving = state["selected_engines"]
    assert surviving and set(surviving) <= set(selected)
    assert phases["reality_check"]["engines"] == surviving
    # Schema design and synthesis: the engines the reality check kept, as assess().
    assert phases["schema_design"]["engines"] == surviving
    assert phases["synthesis"]["engines"] == surviving
    assert phases["report"]["engines"] == []


def test_a_new_run_never_checks_a_phase_left_by_an_earlier_one(run, clock, capsys, monkeypatch):
    """Time spent before the first phase (argument checks) is no phase's: a stale phase
    name from an earlier run in the process is not blamed for it."""
    monkeypatch.setattr(run_assessment, "_CURRENT_PHASE", "synthesis")
    monkeypatch.setenv("MODERNIZER_MAX_RUN_SECONDS", "1")

    def no_phase(store) -> None:
        clock.now += 5.0

    run(no_phase, "--verbose")
    lines = _status_lines(capsys.readouterr().out)
    assert not [line for line in lines if line.get("status") == "error"]


# --------------------------------------------------------------------------- one line per phase (#145 review)


def _real_main(monkeypatch, tmp_path, *argv: str) -> int:
    """``main()`` with the real ``_run``; returns the exit code (0 when it returned)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_assessment, "_JOB", {})
    monkeypatch.setattr(run_assessment, "_CONSOLE", None)
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", *argv])
    try:
        run_assessment.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        model_usage.LEDGER.reset()
    return 0


def _seed_job(tmp_path: Path) -> Path:
    """A job a resume or a choice can run on: collected, with its state file."""
    job = tmp_path / "artifacts" / DB / JOB
    (job / "collector").mkdir(parents=True)
    (job / "collector" / "output.json").write_text("{}")
    (tmp_path / run_assessment.STATE_FILE).write_text(
        json.dumps(
            {
                "job_id": JOB,
                "database_name": DB,
                "current_phase": "reality_check",
                "selected_engines": ["dynamodb", "aurora_mysql"],
                "phase_status": {"reality_check": "awaiting_llm"},
            }
        )
    )
    return job


def _assert_one_failed_line(out: str, phase: str, phase_result: str, ceiling: str) -> dict:
    lines = [line for line in _status_lines(out) if line.get("phase") == phase]
    assert len(lines) == 1, lines  # the phase's own line carries the failure
    (line,) = lines
    assert line["status"] == "error" and line["phase_result"] == phase_result
    assert line["ceiling"] == ceiling and f"in the {phase} phase" in line["message"]
    assert "output was kept and the run is marked failed" in line["message"]
    return line


def _assert_failed_everywhere(tmp_path, job: Path, phase: str) -> None:
    state = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    assert state["phase_status"][phase] == "failed" and state["current_phase"] == phase
    marker = json.loads((job / "_status.json").read_text())
    assert (marker["status"], marker["phase"], marker["error_type"]) == (
        "FAILED",
        phase,
        "RunLimitExceeded",
    )
    run_record = json.loads((job / "_logs" / "model_usage.json").read_text())["runs"][-1]
    assert run_record["ceiling_exceeded"]["phase"] == phase


@pytest.mark.parametrize("mode", [(), ("--verbose",)], ids=["compact", "verbose"])
def test_a_resumed_reality_check_over_a_ceiling_prints_one_failed_line(
    monkeypatch, tmp_path, capsys, clock, mode
):
    monkeypatch.setenv("MODERNIZER_MAX_RUN_MODEL_CALLS", "1")
    job = _seed_job(tmp_path)

    def finalize(store, job_id, db):
        for _ in range(2):
            model_usage.record_usage(SONNET, {"inputTokens": 10})
        run_assessment._output("reality_check", {"status": "complete", "finalized": True})

    monkeypatch.setattr(run_assessment, "phase_reality_check_finalize", finalize)
    monkeypatch.setattr(run_assessment, "_surviving_engines", lambda *a: ["dynamodb"])
    code = _real_main(
        monkeypatch, tmp_path, "--job-id", JOB, "--db", DB, "--resume-reality-check", *mode
    )
    assert code == 1
    line = _assert_one_failed_line(
        capsys.readouterr().out, "reality_check", "complete", "model_calls"
    )
    assert line["finalized"] is True  # the phase's own fields are kept
    _assert_failed_everywhere(tmp_path, job, "reality_check")
    phases = json.loads((job / "_logs" / "model_usage.json").read_text())["runs"][-1]["phases"]
    assert phases["reality_check"]["engines"] == ["dynamodb"]  # what the resume kept


def test_settling_engine_choices_over_a_ceiling_prints_one_failed_line(
    monkeypatch, tmp_path, capsys, clock
):
    monkeypatch.setenv("MODERNIZER_MAX_RUN_SECONDS", "2")
    job = _seed_job(tmp_path)

    def choices(store, job_id, db, choose, decide_for_me):
        clock.now += 3.0
        run_assessment._output("engine_choices", {"status": "complete", "decided": 1})

    monkeypatch.setattr(run_assessment, "phase_engine_choices", choices)
    monkeypatch.setattr(run_assessment, "_surviving_engines", lambda *a: ["dynamodb"])
    code = _real_main(monkeypatch, tmp_path, "--job-id", JOB, "--db", DB, "--decide-for-me")
    assert code == 1
    _assert_one_failed_line(capsys.readouterr().out, "engine_choices", "complete", "seconds")
    _assert_failed_everywhere(tmp_path, job, "engine_choices")


def test_an_awaiting_llm_line_over_a_ceiling_is_the_one_failed_line(
    monkeypatch, tmp_path, capsys, clock
):
    """The default /modernize path: external mode stops at ``awaiting_llm``; over the
    wall-clock ceiling, that line is the reality check's failure, and no second line
    for the phase follows."""
    import zipfile

    repo = Path(__file__).resolve().parents[3]
    with zipfile.ZipFile(repo / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(tmp_path / "input")
    monkeypatch.setenv("MODERNIZER_MAX_RUN_SECONDS", "10")
    real = run_assessment.phase_reality_check

    def reality_check(store, job_id, db, llm_mode):
        clock.now += 11.0
        return real(store, job_id, db, llm_mode)

    monkeypatch.setattr(run_assessment, "phase_reality_check", reality_check)
    code = _real_main(
        monkeypatch,
        tmp_path,
        "--file",
        str(tmp_path / "input" / "wordpress-collection.json"),
        "--db",
        DB,
        "--artifact-root",
        str(tmp_path / "artifacts"),
    )
    assert code == 1
    out = capsys.readouterr().out
    _assert_one_failed_line(out, "reality_check", "awaiting_llm", "seconds")
    # Every earlier phase kept its own single, successful line.
    earlier = [line for line in _status_lines(out) if line.get("phase") != "reality_check"]
    assert [line["phase"] for line in earlier if "phase" in line] == [
        "collect",
        "triage",
        "analysis",
        "assignment",
    ]
    assert all(line["status"] == "complete" for line in earlier if "phase" in line)
    state = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    _assert_failed_everywhere(
        tmp_path, tmp_path / "artifacts" / DB / state["job_id"], "reality_check"
    )


def test_a_phase_that_prints_no_line_still_fails_with_one_line(run, clock, capsys, monkeypatch):
    monkeypatch.setenv("MODERNIZER_MAX_RUN_TOKENS", "5")

    def silent(store) -> None:
        run_assessment._start_phase("schema_design")
        model_usage.record_usage(OPUS, {"inputTokens": 6})
        run_assessment._start_phase("synthesis")  # schema_design printed nothing

    run(silent)
    lines = [line for line in _status_lines(capsys.readouterr().out) if line.get("phase")]
    assert [(line["phase"], line["status"]) for line in lines] == [("schema_design", "error")]
    assert "phase_result" not in lines[0]


def test_the_modernize_command_handles_a_ceiling_line():
    """/modernize reads a ceiling failure as the phase's one line and never retries it."""
    text = (
        Path(__file__).resolve().parents[3] / ".claude" / "commands" / "modernize.md"
    ).read_text()
    for name in CEILINGS:
        assert name in text
    assert "`ceiling` field" in text and "`phase_result`" in text
    assert "never retry or skip it" in text


def test_time_after_a_checked_line_counts_against_the_next_phase(run, clock, capsys, monkeypatch):
    """A phase whose line passed gets no second line: what is spent after it is the
    next phase's, whose own line then carries the failure."""
    monkeypatch.setenv("MODERNIZER_MAX_RUN_SECONDS", "4")

    def phases(store) -> None:
        store.write_json(f"{DB}/{JOB}/collector/output.json", {})
        run_assessment._start_phase("reality_check")
        run_assessment._output("reality_check", {"status": "complete"})
        clock.now += 5.0  # bookkeeping after the line
        run_assessment._start_phase("synthesis")
        run_assessment._output("synthesis", {"status": "complete"})

    run(phases)
    lines = [line for line in _status_lines(capsys.readouterr().out) if line.get("phase")]
    assert [(line["phase"], line["status"]) for line in lines] == [
        ("reality_check", "complete"),
        ("synthesis", "error"),
    ]


def test_a_line_checked_in_an_earlier_run_does_not_exempt_a_phase(run, clock, capsys, monkeypatch):
    monkeypatch.setattr(run_assessment, "_LINE_CHECKED", "schema_design")
    monkeypatch.setenv("MODERNIZER_MAX_RUN_TOKENS", "5")

    def silent(store) -> None:
        run_assessment._start_phase("schema_design")
        model_usage.record_usage(OPUS, {"inputTokens": 6})

    run(silent)
    lines = [line for line in _status_lines(capsys.readouterr().out) if line.get("phase")]
    assert [(line["phase"], line["status"]) for line in lines] == [("schema_design", "error")]
