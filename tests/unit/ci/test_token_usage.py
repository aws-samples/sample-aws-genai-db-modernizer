"""Tests for ci/llm/token_usage.py: per-phase token usage from a headless transcript (#676)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ci.llm import run, token_usage


def _turn(
    msg_id: str,
    usage: dict[str, Any],
    *,
    command: str | None = None,
    skill: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": "thinking"}]
    if command:
        content.append(
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": command}}
        )
    if skill:
        content.append({"type": "tool_use", "id": "s", "name": "Skill", "input": {"skill": skill}})
    rec: dict[str, Any] = {
        "type": "assistant",
        "message": {"id": msg_id, "role": "assistant", "content": content, "usage": usage},
    }
    if agent:
        rec["parent_tool_use_id"] = agent
    return rec


def _u(i: int, o: int, cr: int = 0, cw: int = 0) -> dict[str, int]:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
    }


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n")
    return path


def test_turns_are_attributed_to_the_phase_last_run(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _turn("m0", _u(10, 1)),
            _turn("m1", _u(100, 10), command="uv run python scripts/run_assessment.py --file f"),
            _turn("m2", _u(200, 20, cr=50)),
            _turn("m3", _u(300, 30), command="uv run python scripts/run_schema_design.py --db x"),
            _turn("m4", _u(400, 40, cw=9)),
        ],
    )
    result = token_usage.summarize(transcript)
    phases = result["phases"]
    assert phases["unattributed"]["inputTokens"] == 10
    assert phases["assessment"] == {
        "calls": 2,
        "inputTokens": 300,
        "outputTokens": None,
        "cacheReadInputTokens": 50,
        "cacheWriteInputTokens": 0,
    }
    assert phases["schema_design"]["inputTokens"] == 700
    assert phases["schema_design"]["cacheWriteInputTokens"] == 9
    assert result["total"] == {
        "calls": 5,
        "inputTokens": 1010,
        "outputTokens": 0,  # no result record in this transcript
        "cacheReadInputTokens": 50,
        "cacheWriteInputTokens": 9,
    }
    assert result["source"] == "transcript"


def test_streamed_blocks_of_one_message_count_once(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        [_turn("same", _u(100, 5)), _turn("same", _u(100, 12), command="x scripts/run_triage.py")],
    )
    result = token_usage.summarize(transcript)
    assert result["total"]["calls"] == 1
    assert result["phases"]["triage"]["calls"] == 1


def test_subagents_are_tracked_separately_and_skills_set_the_phase(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _turn("o1", _u(10, 1), command="uv run python scripts/run_reality_check.py --db x"),
            _turn("a1", _u(20, 2), skill="design-schema-dynamodb", agent="toolu_a"),
            _turn(
                "b1",
                _u(30, 3),
                command="uv run python scripts/run_schema_design.py",
                agent="toolu_b",
            ),
            _turn("a2", _u(40, 4), agent="toolu_a"),
            _turn("o2", _u(50, 5)),
            _turn("b2", _u(60, 6), agent="toolu_b"),
        ],
    )
    phases = token_usage.summarize(transcript)["phases"]
    assert phases["reality_check"]["inputTokens"] == 60
    assert phases["schema_design"]["inputTokens"] == 20 + 30 + 40 + 60
    assert "unattributed" not in phases


def test_missing_or_odd_usage_counts_as_zero(tmp_path: Path) -> None:
    transcript = _write(tmp_path / "t.jsonl", [_turn("m", {"input_tokens": "x"})])
    assert token_usage.summarize(transcript)["total"]["inputTokens"] == 0
    assert token_usage.summarize(transcript)["total"]["calls"] == 1


def test_main_writes_the_job_log_and_prints_one_status_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = _write(
        tmp_path / "t.jsonl", [_turn("m", _u(5, 1), command="scripts/run_collect.py")]
    )
    root = tmp_path / "artifacts"
    assert (
        token_usage.main(
            [str(transcript), "--artifact-root", str(root), "--db", "wp", "--job", "j1"]
        )
        == 0
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and json.loads(out[0])["status"] == "ok"
    written = json.loads((root / "wp" / "j1" / "_logs" / "token_usage.json").read_text())
    assert written["phases"]["collect"]["inputTokens"] == 5


@pytest.mark.parametrize(("db", "job"), [("..", "j"), ("wp", "../x"), ("a/b", "j")])
def test_unsafe_db_or_job_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], db: str, job: str
) -> None:
    transcript = _write(tmp_path / "t.jsonl", [_turn("m", _u(1, 1))])
    code = token_usage.main(
        [str(transcript), "--artifact-root", str(tmp_path / "a"), "--db", db, "--job", job]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_results_row_carries_token_usage(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl", [_turn("m", _u(5, 1), command="scripts/run_collect.py")]
    )
    path, _ = token_usage.write_token_usage(transcript, tmp_path / "a", "wp", "j1")
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"job_id": "j1", "db": "wp", "mode": "chat"}))
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
        token_usage_path=path,
    )
    assert row["token_usage"]["total"]["inputTokens"] == 5
    assert row["token_usage"]["phases"]["collect"]["calls"] == 1
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["token_usage"] is None


def _result(output: int, model_output: int | None = None, **model_extra: int) -> dict[str, Any]:
    rec: dict[str, Any] = {"type": "result", "usage": {"output_tokens": output}}
    if model_output is not None:
        rec["modelUsage"] = {
            "m": {
                "inputTokens": 7,
                "outputTokens": model_output,
                "cacheReadInputTokens": 90,
                "cacheCreationInputTokens": 11,
                **model_extra,
            }
        }
    return rec


def test_per_phase_output_is_null_and_session_output_comes_from_model_usage(
    tmp_path: Path,
) -> None:
    """Assistant records carry first-chunk output counts (3, 4, 8...), never the
    message total, so they are not reported; the session total is the
    cumulative modelUsage on the result records (largest wins)."""
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _turn("m1", _u(10, 3), command="scripts/run_assessment.py"),
            _turn("sub", _u(20, 4), agent="toolu_a"),
            _result(output=50, model_output=900),
            _result(output=5, model_output=1000),
            _result(output=7, model_output=1000),
        ],
    )
    result = token_usage.summarize(transcript)
    assert all(p["outputTokens"] is None for p in result["phases"].values())
    assert result["total"]["outputTokens"] == 1000
    assert result["total"]["calls"] == 2  # the subagent turn is included
    assert result["session_model_usage"] == {
        "inputTokens": 7,
        "outputTokens": 1000,
        "cacheReadInputTokens": 90,
        "cacheWriteInputTokens": 11,
    }
    assert "first-chunk" in result["output_tokens_note"]


def test_session_output_falls_back_to_the_result_usage_sum(tmp_path: Path) -> None:
    transcript = _write(tmp_path / "t.jsonl", [_turn("m", _u(1, 1)), _result(50), _result(5)])
    result = token_usage.summarize(transcript)
    assert result["total"]["outputTokens"] == 55
    assert result["session_model_usage"] == {}
