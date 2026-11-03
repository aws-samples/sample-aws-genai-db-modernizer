# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
    # The top-level session's own turns before any phase are the orchestrator's (#745).
    assert phases["orchestrator"]["inputTokens"] == 10
    assert "unattributed" not in phases
    assert {k: v for k, v in phases["assessment"].items() if k != "input_cost_usd"} == {
        "calls": 2,
        "inputTokens": 300,
        "outputTokens": None,
        "cacheReadInputTokens": 50,
        "cacheWriteInputTokens": 0,
    }
    assert phases["schema_design"]["inputTokens"] == 700
    assert phases["schema_design"]["cacheWriteInputTokens"] == 9
    assert {k: v for k, v in result["total"].items() if k != "input_cost_usd"} == {
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


# ---------------------------------------------------------------------------
# Attribution (#745 item 2), the per-subagent table and its costs (#355 item 1)
# ---------------------------------------------------------------------------

REAL_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "real_runs" / "wordpress-both"
SONNET = "claude-sonnet-5-5"


def _dispatch(msg_id: str, tool_id: str, description: str, prompt: str) -> dict[str, Any]:
    """An orchestrator turn that starts a subagent with the Agent tool."""
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": SONNET,
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Agent",
                    "input": {"description": description, "prompt": prompt},
                }
            ],
            "usage": _u(1, 1),
        },
    }


def _model_turn(msg_id: str, usage: dict[str, Any], agent: str | None = None, **kw: Any):
    rec = _turn(msg_id, usage, agent=agent, **kw)
    rec["message"]["model"] = SONNET
    return rec


def _session_result(
    cost: float,
    output: int,
    model: str = f"global.anthropic.{SONNET}",
    **tokens: int,
) -> dict[str, Any]:
    """A final result record: the CLI's cost and its cumulative modelUsage."""
    return {
        "type": "result",
        "total_cost_usd": cost,
        "usage": {"output_tokens": 1},
        "modelUsage": {
            model: {
                "inputTokens": tokens.get("inp", 0),
                "outputTokens": output,
                "cacheReadInputTokens": tokens.get("cr", 0),
                "cacheCreationInputTokens": tokens.get("cw", 0),
                "costUSD": cost,
            }
        },
    }


def _one_hour_write(tokens: int) -> dict[str, Any]:
    usage = _u(0, 1, cw=tokens)
    usage["cache_creation"] = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": tokens}
    return usage


def _fields(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        row["calls"],
        row["inputTokens"],
        row["cacheReadInputTokens"],
        row["cacheWriteInputTokens"],
    )


def _assert_sums_match(result: dict[str, Any]) -> None:
    total = _fields(result["total"])
    for table in (result["agents"], list(result["phases"].values())):
        summed = tuple(sum(col) for col in zip(*(_fields(r) for r in table), strict=True))
        assert summed == total


def test_subagent_turns_take_the_phase_their_dispatch_names(tmp_path: Path) -> None:
    """A subagent's turns before it runs a script (reading its command, skills and
    input) belong to the phase its task names, not to `unattributed` (#745)."""
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _dispatch("o1", "toolu_r", "DynamoDB review, part 1 of 3", "Follow /review-schema for job"),
            _dispatch("o2", "toolu_d", "Design ElastiCache schema", "Run /design-schema-elasticache for"),
            _dispatch(
                "o3", "toolu_x", "Fix the merge", "Fix names, then run uv run python scripts/run_schema_design.py --merge"
            ),
            _model_turn("r1", _u(10, 1), agent="toolu_r"),
            _model_turn("r2", _u(20, 1), agent="toolu_r", command="uv run python scripts/run_pe_review.py --finalize"),
            _model_turn("d1", _u(30, 1), agent="toolu_d"),
            _model_turn("x1", _u(40, 1), agent="toolu_x"),
        ],
    )  # fmt: skip
    result = token_usage.summarize(transcript)
    phases = result["phases"]
    assert phases["pe_review"]["inputTokens"] == 30
    assert phases["schema_design"]["inputTokens"] == 70
    assert phases["orchestrator"]["calls"] == 3
    assert "unattributed" not in phases
    assert result["unattributed_calls"] == 0
    labels = {row["agent"]: row["label"] for row in result["agents"]}
    assert labels["toolu_r"] == "DynamoDB review, part 1 of 3"
    assert labels["orchestrator"] == "orchestrator"


def test_a_subagent_without_a_named_phase_takes_the_first_phase_it_runs(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _dispatch("o1", "toolu_a", "Check the state", "Look at the job and report."),
            _model_turn("a1", _u(10, 1), agent="toolu_a"),
            _model_turn("a2", _u(20, 1), agent="toolu_a", command="scripts/run_synthesis.py"),
        ],
    )
    result = token_usage.summarize(transcript)
    assert result["phases"]["synthesis"]["inputTokens"] == 30
    assert result["unattributed_calls"] == 0


def test_only_turns_that_cannot_be_matched_stay_unattributed_and_are_counted(
    tmp_path: Path,
) -> None:
    """A subagent with no dispatch record and no phase of its own is the only
    case left in `unattributed`; the count is reported."""
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _model_turn("o1", _u(5, 1)),
            _model_turn("u1", _u(10, 1), agent="toolu_unknown"),
            _model_turn("u2", _u(20, 1), agent="toolu_unknown"),
        ],
    )
    result = token_usage.summarize(transcript)
    assert result["phases"]["unattributed"]["calls"] == 2
    assert result["unattributed_calls"] == 2
    assert result["phases"]["orchestrator"]["calls"] == 1
    _assert_sums_match(result)


def test_the_agent_table_has_one_row_per_agent_with_its_largest_context(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _dispatch("o1", "toolu_a", "Run synthesis", "Run /synthesize for job"),
            _model_turn("a1", _u(2, 1, cr=1000, cw=500), agent="toolu_a"),
            _model_turn("a2", _u(2, 1, cr=4000, cw=100), agent="toolu_a"),
            _model_turn("o2", _u(3, 1, cr=50)),
        ],
    )
    result = token_usage.summarize(transcript)
    rows = {row["agent"]: row for row in result["agents"]}
    assert [row["agent"] for row in result["agents"]] == ["orchestrator", "toolu_a"]
    assert rows["toolu_a"]["kind"] == "subagent"
    assert rows["toolu_a"]["calls"] == 2
    assert rows["toolu_a"]["largest_context"] == 4102  # input + cache read + cache write
    assert rows["toolu_a"]["phases"] == ["synthesis"]
    assert rows["toolu_a"]["outputTokens"] is None  # first-chunk counts only
    assert rows["orchestrator"]["kind"] == "orchestrator"
    assert rows["orchestrator"]["calls"] == 2
    _assert_sums_match(result)


def test_rows_are_priced_from_the_catalog_and_reconcile_with_its_list_price(
    tmp_path: Path,
) -> None:
    """Sonnet 5.5 on the global profile, from src/catalog/model_prices.toml: input
    $2/M, cache read $0.10/M, 5-minute write $2.50/M, output $10/M; a 1-hour write
    is 2x input, $4/M. Rows carry the input side; the session output and the
    unmatched rest close the gap to the list price of modelUsage. The CLI's own
    figure is reported beside it."""
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _dispatch("o1", "toolu_a", "Run synthesis", "Run /synthesize"),
            _model_turn("a1", _u(1_000_000, 1, cr=1_000_000, cw=1_000_000), agent="toolu_a"),
            _model_turn("a2", _one_hour_write(1_000_000), agent="toolu_a"),
            # modelUsage holds 0.3M more cache reads than the turns show: $0.03 unmatched.
            _session_result(cost=21.0, output=1_000_000, inp=1_000_001, cr=1_300_000, cw=2_000_000),
        ],
    )
    result = token_usage.summarize(transcript)
    rows = {row["agent"]: row for row in result["agents"]}
    assert rows["toolu_a"]["input_cost_usd"] == pytest.approx(2 + 0.1 + 2.5 + 4)
    assert rows["orchestrator"]["input_cost_usd"] == pytest.approx(0.000002)
    cost = result["cost"]
    assert cost["rows_input_cost_usd"] == pytest.approx(8.600002)
    assert cost["output_cost_usd"] == pytest.approx(10.0)
    assert cost["list_price_cost_usd"] == pytest.approx(18.630002)
    assert cost["unmatched_cost_usd"] == pytest.approx(0.03)
    assert cost["cli_cost_usd"] == 21.0
    assert cost["cli_minus_list_price_usd"] == pytest.approx(21.0 - 18.630002)
    assert cost["models"] == {
        f"global.anthropic.{SONNET}": {
            "cli_cost_usd": 21.0,
            "list_price_cost_usd": pytest.approx(18.630002),
        }
    }
    assert cost["unpriced_calls"] == 0 and cost["unpriced_models"] == []
    assert "model_prices.toml" in cost["price_source"]
    assert result["total"]["input_cost_usd"] == pytest.approx(8.600002)


def test_one_hour_writes_are_priced_alike_in_the_rows_and_the_model_check(
    tmp_path: Path,
) -> None:
    """1M one-hour cache writes: the row is $4.00 and so is the model's list price,
    so a 1-hour TTL run shows no false gap (it was $2.50 at the 5-minute rate)."""
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _model_turn("m1", _one_hour_write(1_000_000)),
            _session_result(cost=4.0, output=0, cw=1_000_000),
        ],
    )
    cost = token_usage.summarize(transcript)["cost"]
    assert cost["rows_input_cost_usd"] == pytest.approx(4.0)
    ((model, check),) = cost["models"].items()
    assert check["list_price_cost_usd"] == pytest.approx(4.0)
    assert cost["unmatched_cost_usd"] == pytest.approx(0.0)
    assert cost["cli_minus_list_price_usd"] == pytest.approx(0.0)


def test_the_profile_in_use_sets_the_price(tmp_path: Path) -> None:
    """The same bare model on the us profile costs the catalog's us price (+10%)."""
    transcript = _write(
        tmp_path / "t.jsonl",
        [
            _model_turn("m1", _u(1_000_000, 1)),
            _session_result(cost=2.2, output=0, model=f"us.anthropic.{SONNET}", inp=1_000_000),
        ],
    )
    result = token_usage.summarize(transcript)
    assert result["agents"][0]["input_cost_usd"] == pytest.approx(2.2)
    assert result["cost"]["list_price_cost_usd"] == pytest.approx(2.2)


def test_a_model_without_a_catalog_price_leaves_its_rows_unpriced_and_is_named(
    tmp_path: Path,
) -> None:
    rec = _turn("m1", _u(100, 1))
    rec["message"]["model"] = "some-future-model"
    transcript = _write(
        tmp_path / "t.jsonl", [rec, _session_result(1.0, 0, model="some-future-model")]
    )
    result = token_usage.summarize(transcript)
    assert result["agents"][0]["input_cost_usd"] is None
    cost = result["cost"]
    assert cost["unpriced_calls"] == 1
    assert cost["unpriced_models"] == ["some-future-model"]
    assert cost["list_price_cost_usd"] is None and cost["unmatched_cost_usd"] is None
    assert cost["models"]["some-future-model"]["list_price_cost_usd"] is None
    assert cost["cli_cost_usd"] == 1.0  # the CLI's own figure is still reported


_PRICED_TRANSCRIPTS = [
    REAL_FIXTURE / "transcript.jsonl",
    *sorted((Path(__file__).parent / "fixtures").glob("real-*.jsonl")),
]
# The hooks fixtures are trimmed to a few turns with no result record, so they name no
# profile to price by; they are left out.


@pytest.mark.parametrize("transcript", _PRICED_TRANSCRIPTS, ids=lambda p: p.name)
def test_every_model_in_a_saved_real_transcript_has_a_catalog_price(transcript: Path) -> None:
    """A model a real run used with no row in src/catalog/model_prices.toml fails
    here: add its public Bedrock price before relying on the cost table."""
    cost = token_usage.summarize(transcript)["cost"]
    assert cost["unpriced_models"] == []
    assert cost["unpriced_calls"] == 0


def test_the_explore_session_is_its_own_row_and_in_the_cli_cost(tmp_path: Path) -> None:
    run_transcript = _write(
        tmp_path / "t.jsonl",
        [
            _model_turn("o1", _u(10, 1), command="scripts/run_assessment.py"),
            _session_result(3.0, 0, inp=10),
        ],
    )
    explore = _write(
        tmp_path / "e.jsonl",
        [
            _model_turn("e1", _u(7, 1, cr=100)),
            _model_turn("e2", _u(8, 1)),
            _session_result(0.25, 0, inp=15, cr=100),
        ],
    )
    result = token_usage.summarize(run_transcript, explore=explore)
    rows = {row["agent"]: row for row in result["agents"]}
    assert rows["explore"]["kind"] == "explore"
    assert rows["explore"]["calls"] == 2
    assert rows["explore"]["phases"] == ["explore"]
    assert result["phases"]["explore"]["inputTokens"] == 15
    cost = result["cost"]
    assert cost["run_cli_cost_usd"] == 3.0
    assert cost["explore_cli_cost_usd"] == 0.25
    assert cost["cli_cost_usd"] == pytest.approx(3.25)
    assert cost["unmatched_cost_usd"] == pytest.approx(0.0)
    _assert_sums_match(result)


def test_the_saved_real_run_is_attributed_and_reconciles() -> None:
    """The replay fixture (a real both-mode wordpress run): every turn is matched,
    both tables sum to the total, and the priced rows plus the session output come
    within 2% of the list price. The CLI's figure is about 12% above the list
    price: it charges Sonnet 5.5 cache reads at $0.20/M, the catalog at $0.10/M."""
    result = token_usage.summarize(REAL_FIXTURE / "transcript.jsonl")
    assert result["unattributed_calls"] == 0
    assert "unattributed" not in result["phases"]
    _assert_sums_match(result)
    assert len(result["agents"]) == 11  # the orchestrator and its ten subagents
    cost = result["cost"]
    assert cost["cli_cost_usd"] == pytest.approx(3.5779633)
    assert cost["list_price_cost_usd"] == pytest.approx(3.201845, abs=1e-6)
    assert abs(cost["unmatched_cost_usd"]) < 0.02 * cost["list_price_cost_usd"]
    assert cost["cli_minus_list_price_usd"] == pytest.approx(0.376118, abs=1e-6)


def _command_dispatch(tmp_path: Path, prompt: str, *, task_started: bool = False) -> dict:
    """Phases of a subagent whose only turn shows no phase, dispatched with
    ``prompt`` by an Agent call, or only by a ``task_started`` record."""
    if task_started:
        start = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "a1",
            "tool_use_id": "toolu_s",
            "description": "Started task",
            "task_type": "local_agent",
            "prompt": prompt,
        }
    else:
        start = _dispatch("o1", "toolu_s", "Started task", prompt)
    transcript = _write(
        tmp_path / "t.jsonl", [start, _model_turn("s1", _u(10, 1), agent="toolu_s")]
    )
    return token_usage.summarize(transcript)


@pytest.mark.parametrize(
    ("prompt", "phase"),
    [
        ("Run /collect for the file", "collect"),
        ("Run /triage for job_id=j", "triage"),
        ("Run /analyze-dynamodb for job_id=j", "analysis"),
        ("Run /analyze for job_id=j", "analysis"),
        ("Run /assign for job_id=j", "assignment"),
        ("Run /explore --job-id j Which table?", "explore"),
        ("Run /reality-check for job_id=j", "reality_check"),
        ("Run /synthesize for job_id=j", "synthesis"),
        ("Follow /review-schema for job_id=j", "pe_review"),
        ("Follow /design-schema-dynamodb **Merge fix task**", "schema_design"),
        # A path segment is not a command; the script it names is the phase.
        ("Read artifacts/wordpress/x.json, then run scripts/run_triage.py", "triage"),
    ],
)
def test_each_command_in_a_dispatch_names_its_phase(
    tmp_path: Path, prompt: str, phase: str
) -> None:
    result = _command_dispatch(tmp_path, prompt)
    assert list(result["phases"]) == ["orchestrator", phase]
    assert result["unattributed_calls"] == 0


def test_a_dispatch_recorded_only_as_task_started_is_matched(tmp_path: Path) -> None:
    result = _command_dispatch(tmp_path, "Run /reality-check for job_id=j", task_started=True)
    assert result["phases"]["reality_check"]["calls"] == 1
    assert {r["agent"]: r["label"] for r in result["agents"]}["toolu_s"] == "Started task"
    assert "orchestrator" not in result["phases"]  # no top-level turn in this transcript


# --- token_usage_error ------------------------------------------------------


def test_an_unreadable_transcript_writes_an_error_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "artifacts"
    code = token_usage.main(
        [str(tmp_path / "missing.jsonl"), "--artifact-root", str(root), "--db", "wp", "--job", "j1"]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    written = json.loads((root / "wp" / "j1" / "_logs" / "token_usage.json").read_text())
    assert "missing.jsonl" in written["error"]


def test_a_transcript_with_no_assistant_turns_is_an_error(tmp_path: Path) -> None:
    transcript = _write(tmp_path / "t.jsonl", [{"type": "system", "subtype": "init"}])
    with pytest.raises(token_usage.TokenUsageError, match="no assistant turns"):
        token_usage.summarize(transcript)


def test_skipped_lines_are_counted(tmp_path: Path) -> None:
    transcript = _write(tmp_path / "t.jsonl", [_turn("m", _u(1, 1))])  # _write adds "not json"
    assert token_usage.summarize(transcript)["skipped_lines"] == 1


def _row(tmp_path: Path, **kw: Any) -> dict[str, Any]:
    summary = tmp_path / "summary.json"
    if not summary.exists():
        summary.write_text(
            json.dumps({"job_id": "j1", "db": "wp", "mode": "chat", "cost_usd": 3.0})
        )
    return run.build_results_row(
        mode=kw.pop("mode", "chat"),
        fixture=kw.pop("fixture", "wordpress"),
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
        **kw,
    )


def test_results_record_a_token_usage_error(tmp_path: Path) -> None:
    row = _row(tmp_path, token_usage_path=tmp_path / "nope.json")
    assert row["token_usage"] is None
    assert "not found" in row["token_usage_error"]
    failed = tmp_path / "failed.json"
    failed.write_text(json.dumps({"source": "transcript", "error": "cannot read t.jsonl"}))
    row = _row(tmp_path, token_usage_path=failed)
    assert row["token_usage"] is None
    assert row["token_usage_error"] == "cannot read t.jsonl"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _row(tmp_path, token_usage_path=bad)["token_usage_error"]
    # No step ran (no job parsed): no section and no error.
    row = _row(tmp_path)
    assert row["token_usage"] is None and row["token_usage_error"] is None


def test_results_carry_the_agent_table_and_cost(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        [_model_turn("m", _u(5, 1), command="scripts/run_collect.py"), _session_result(3.0, 10)],
    )
    path, _ = token_usage.write_token_usage(transcript, tmp_path / "a", "wp", "j1")
    row = _row(tmp_path, token_usage_path=path)
    assert row["token_usage_error"] is None
    assert row["token_usage"]["agents"][0]["agent"] == "orchestrator"
    assert row["token_usage"]["cost"]["cli_cost_usd"] == 3.0
    assert row["total_cost_usd"] == 3.0  # the CLI's figure, as before
    assert row["list_price_cost_usd"] == pytest.approx(0.0001)  # modelUsage: 10 output at $10/M
    assert row["token_usage"]["unattributed_calls"] == 0
    assert row["warnings"] == []


def test_results_warn_when_the_table_disagrees_with_total_cost_usd(tmp_path: Path) -> None:
    transcript = _write(tmp_path / "t.jsonl", [_model_turn("m", _u(5, 1)), _session_result(2.0, 0)])
    path, _ = token_usage.write_token_usage(transcript, tmp_path / "a", "wp", "j1")
    row = _row(tmp_path, token_usage_path=path)  # the summary says 3.0
    assert row["pass"] is False  # unchanged: the junit and judge files are missing
    assert any("cli_cost_usd" in w and "total_cost_usd" in w for w in row["warnings"])


# --- soft budgets -------------------------------------------------------------


def _budgets(tmp_path: Path) -> Path:
    path = tmp_path / "budgets.toml"
    path.write_text(
        "[wordpress]\nmax_total_cost_usd = 10.0\n\n[wordpress.chat]\nmax_total_cost_usd = 2.5\n\n"
        "[discourse]\nmax_total_cost_usd = 50.0\n"
    )
    return path


def test_a_run_over_its_soft_budget_gets_a_warning_and_never_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"job_id": "j1", "db": "wp", "mode": "chat", "cost_usd": 3.0}))
    junit = tmp_path / "junit.xml"
    junit.write_text(
        "<testsuite>"
        + "".join(
            f'<testcase classname="tests.e2e.{m}" name="t"/>'
            for m in ("test_pipeline", "test_report_engineering", "test_report_pdf")
        )
        + "</testsuite>"
    )
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps({"pass": True, "mean": 4.0, "scores": {}}))
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
        git_sha="abc",
        budgets_path=_budgets(tmp_path),
    )
    assert row["budget"] == {
        "key": "wordpress.chat",
        "field": "total_cost_usd",
        "limit_usd": 2.5,
        "value_usd": 3.0,
        "exceeded": True,
    }
    assert any("wordpress.chat soft budget" in w for w in row["warnings"])
    assert row["pass"] is True


def test_a_run_within_its_budget_or_without_one_has_no_warning(tmp_path: Path) -> None:
    row = _row(tmp_path, fixture="discourse", budgets_path=_budgets(tmp_path))
    assert row["budget"]["key"] == "discourse"  # no discourse.chat entry: the fixture's
    assert row["budget"]["exceeded"] is False
    assert row["warnings"] == []
    # A both-mode wordpress run has no budget of its own here: the fixture's applies.
    row = _row(tmp_path, mode="both", budgets_path=_budgets(tmp_path))
    assert (row["budget"]["key"], row["budget"]["limit_usd"]) == ("wordpress", 10.0)
    assert row["warnings"] == []
    row = _row(tmp_path, fixture="other", budgets_path=_budgets(tmp_path))
    assert row["budget"] is None and row["warnings"] == []


def test_the_committed_budgets_cover_both_samples() -> None:
    budgets = run.load_budgets(run.DEFAULT_BUDGETS)
    assert set(budgets) >= {"wordpress", "discourse", "wordpress.both", "discourse.both"}
    assert all(limit > 0 for limit in budgets.values())


def test_e2e_llm_counts_tokens_after_the_explore_session() -> None:
    script = (Path(__file__).resolve().parents[3] / "ci" / "e2e-llm.sh").read_text()
    assert script.index("step explore run_before_deadline") < script.index("step token-usage")
    assert script.index("step token-usage") < script.index("step results")
    assert '--explore "$OUT/explore-transcript.jsonl"' in script
