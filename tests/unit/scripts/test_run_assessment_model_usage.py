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
    assert set(record) == {"totals", "by_model", "by_phase", "runs"}
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


@pytest.mark.passes_on_main  # a guard: main already behaves this way
def test_a_run_with_no_model_call_writes_nothing(run):
    def deterministic(store) -> None:
        run_assessment._start_phase("analysis")

    assert not run(deterministic).exists()


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
