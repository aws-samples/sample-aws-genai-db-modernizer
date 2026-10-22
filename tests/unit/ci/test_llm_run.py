"""Tests for ci/llm/run.py's transcript checking and results merging.

Most tests use the small synthetic stream-json fixtures in
tests/unit/ci/fixtures/ (synthetic-*.jsonl), which exercise one behavior
each in isolation. tests/unit/ci/fixtures/real-chat-wordpress-failed.jsonl
is a trimmed, scrubbed copy of a real recorded transcript (the first
internal-pipeline run) -- see ci/llm/run.py's module docstring for the
confirmed field shapes it validates (aggregation across all `result`
messages, `permission_denials` entries, nested `usage` fields).
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own scripts/binaries with fixed argv
import xml.etree.ElementTree as ET  # nosec B405 -- builds/writes synthetic JUnit fixtures; defusedxml.ElementTree has no builder/writer API, only parsing, and ci/llm/run.py (the code under test) already parses with defusedxml
from pathlib import Path

import pytest

from ci.llm import run

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# check_transcript: success paths
# ---------------------------------------------------------------------------


def test_success_chat() -> None:
    summary = run.check_transcript(FIXTURES / "synthetic-success-chat.jsonl", "chat")
    assert summary["job_id"] == "dry00001"
    assert summary["db"] == "wordpress"
    assert summary["mode"] == "chat"
    assert summary["cost_usd"] == 0.42
    assert summary["num_turns"] == 12
    assert summary["usage"] == {"input_tokens": 10000, "output_tokens": 2500}
    assert summary["duration_ms"] == 45000
    assert summary["model"] == "global.anthropic.claude-sonnet-5"


def test_success_ui() -> None:
    summary = run.check_transcript(FIXTURES / "synthetic-success-ui.jsonl", "ui")
    assert summary["job_id"] == "dry00002"
    assert summary["db"] == "wordpress"
    assert summary["mode"] == "ui"


# ---------------------------------------------------------------------------
# check_transcript: failure paths
# ---------------------------------------------------------------------------


def test_exploratory_denial_passes_with_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """synthetic-denial.jsonl's one denial (``rm -rf /tmp/whatever``) is not
    one of the pipeline's own allowlisted commands -- it's the model's own
    exploratory tool use, so it must not fail the run. It's recorded in the
    summary as ``denials`` and warned about on stderr instead."""
    summary = run.check_transcript(FIXTURES / "synthetic-denial.jsonl", "chat")
    assert summary["job_id"] == "dry00003"
    assert summary["denials"] == {"count": 1, "commands": ["Bash: rm -rf /tmp/whatever"]}
    assert "exploratory" in capsys.readouterr().err


def test_pipeline_command_denial_fails() -> None:
    """A denial of one of the pipeline's own allowlisted commands (here,
    ``uv run python scripts/run_schema_design.py ...``) means the allowlist
    itself is broken -- that must fail even though the run otherwise
    reports MODERNIZE_RESULT: complete."""
    with pytest.raises(run.TranscriptError, match="allowlist"):
        run.check_transcript(FIXTURES / "synthetic-pipeline-denial.jsonl", "chat")


def test_pipe_into_denied_command_is_a_rule_violation_not_a_broken_allowlist() -> None:
    """The shape seen in a real run: an allowlisted ``search_artifacts.py``
    piped into ``uv run python -c``. The pipe is denied because of its second
    half; the allowlist is fine. It still fails the run (the commands forbid
    it) but is named a tool-use rule violation and carried in
    ``rule_violations``, not ``denials``."""
    with pytest.raises(run.TranscriptError) as exc_info:
        run.check_transcript(FIXTURES / "synthetic-pipe-violation.jsonl", "chat")
    message = str(exc_info.value)
    assert "tool-use rule violation" in message
    assert "allowlist in .claude/settings.ci.json is broken" not in message
    partial = exc_info.value.partial
    assert partial["rule_violations"]["count"] == 1
    assert partial["rule_violations"]["commands"][0].startswith(
        "Bash: uv run python scripts/search_artifacts.py"
    )
    assert "denials" not in partial


def test_cd_chain_into_allowlisted_command_is_a_rule_violation(tmp_path: Path) -> None:
    """``cd ... && uv run python scripts/run_report.py ...`` contains an
    allowlisted command but is a forbidden ``cd`` chain: a rule violation
    (fails, named as such), never a broken allowlist."""
    path = _transcript(
        tmp_path,
        [
            _result_line(
                "MODERNIZE_RESULT: complete job_id=cmp00001 db=wordpress mode=chat",
                permission_denials=[
                    {
                        "tool_name": "Bash",
                        "tool_use_id": "tu-1",
                        "tool_input": {
                            "command": (
                                "cd /workspace/repo && uv run python scripts/run_report.py "
                                "--artifact-root ./artifacts"
                            )
                        },
                    }
                ],
            )
        ],
    )
    with pytest.raises(run.TranscriptError, match="tool-use rule violation"):
        run.check_transcript(path, "chat")


def test_compound_without_allowlisted_part_is_other_denial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A compound command with no allowlisted part is just the model's own
    exploratory tool use: warn, do not fail."""
    path = _transcript(
        tmp_path,
        [
            _result_line(
                "MODERNIZE_RESULT: complete job_id=cmp00002 db=wordpress mode=chat",
                permission_denials=[
                    {
                        "tool_name": "Bash",
                        "tool_use_id": "tu-1",
                        "tool_input": {"command": "cat artifacts/x.json | jq ."},
                    }
                ],
            )
        ],
    )
    summary = run.check_transcript(path, "chat")
    assert summary["denials"]["count"] == 1
    assert "rule_violations" not in summary
    assert "exploratory" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("a | b", ["a", "b"]),
        ("a && b || c; d", ["a", "b", "c", "d"]),
        ("a\nb", ["a", "b"]),
        ("(a; b)", ["a", "b"]),
        ("a $(b) `c`", ["a", "b", "c"]),
        ("a 'x | y; z' \"p && q\" r", ["a 'x | y; z' \"p && q\" r"]),
        ("a \\| b", ["a \\| b"]),
        ("a 2>&1 | b", ["a 2>&1", "b"]),
        ("a &> out; b", ["a &> out", "b"]),
        ("", []),
    ],
)
def test_split_compound_command(command: str, expected: list[str]) -> None:
    assert run._split_compound_command(command) == expected


def test_classify_denial_kinds() -> None:
    prefixes = ["uv run python scripts/search_artifacts.py"]
    classify = run._classify_denial
    assert classify("uv run python scripts/search_artifacts.py x y", prefixes) == "allowlist"
    assert classify("uv run python scripts/search_artifacts.py 'a|b' y", prefixes) == "allowlist"
    assert classify("uv run python scripts/search_artifacts.py x | head", prefixes) == (
        "rule_violation"
    )
    assert classify("echo hi; uv run python scripts/search_artifacts.py x", prefixes) == (
        "rule_violation"
    )
    assert classify(
        "uv run python scripts/search_artifacts.py a p && uv run python scripts/search_artifacts.py b p",
        prefixes,
    ) == ("allowlist")
    assert classify("cat <<EOF\nuv run python scripts/search_artifacts.py x\nEOF", prefixes) == (
        "rule_violation"
    )
    assert classify("echo 'uv run python scripts/search_artifacts.py x'", prefixes) == "other"
    assert classify("rm -rf /tmp/whatever", prefixes) == "other"


def test_exact_pipeline_command_denied_fails(tmp_path: Path) -> None:
    """A denial of the exact documented command (no chaining) must fail --
    this is specifically what means the allowlist itself is broken."""
    path = _transcript(
        tmp_path,
        [
            _result_line(
                "MODERNIZE_RESULT: complete job_id=exact0001 db=wordpress mode=chat",
                permission_denials=[
                    {
                        "tool_name": "Bash",
                        "tool_use_id": "tu-1",
                        "tool_input": {
                            "command": "uv run python scripts/run_report.py --artifact-root /"
                        },
                    }
                ],
            )
        ],
    )
    with pytest.raises(run.TranscriptError, match="allowlist"):
        run.check_transcript(path, "chat")


def test_error_max_turns_fails() -> None:
    with pytest.raises(run.TranscriptError, match="is_error"):
        run.check_transcript(FIXTURES / "synthetic-error-max-turns.jsonl", "chat")


def test_missing_modernize_result_fails() -> None:
    with pytest.raises(run.TranscriptError, match="MODERNIZE_RESULT"):
        run.check_transcript(FIXTURES / "synthetic-missing-modernize-result.jsonl", "chat")


def test_chat_run_that_started_ui_fails() -> None:
    with pytest.raises(run.TranscriptError, match="start_local_ui"):
        run.check_transcript(FIXTURES / "synthetic-chat-started-ui.jsonl", "chat")


def test_chat_run_that_gave_a_ui_page_names_page_in_the_failure(tmp_path: Path) -> None:
    """#1233: a chat run never gives a UI pointer; a stray ``--page`` call fails
    the run with a message that says so, not "started the UI"."""
    text = (FIXTURES / "synthetic-chat-started-ui.jsonl").read_text()
    path = tmp_path / "chat-page.jsonl"
    path.write_text(
        text.replace(
            "scripts/start_local_ui.py",
            "scripts/start_local_ui.py --page summary --job-id dry00001",
        )
    )
    with pytest.raises(run.TranscriptError, match="start_local_ui.py --page"):
        run.check_transcript(path, "chat")
    with pytest.raises(run.TranscriptError, match="should start"):
        run.check_transcript(FIXTURES / "synthetic-chat-started-ui.jsonl", "chat")


def test_modernize_result_failed_line_fails() -> None:
    with pytest.raises(
        run.TranscriptError, match=r"failed phase=synthesis reason=report rendering crashed"
    ):
        run.check_transcript(FIXTURES / "synthetic-modernize-failed.jsonl", "chat")


def test_ui_mode_never_starting_ui_fails(tmp_path: Path) -> None:
    """A transcript claiming mode=ui that never ran start_local_ui.py must fail the mode assertion."""
    path = tmp_path / "fake-ui-no-start.jsonl"
    result_rec = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "permission_denials": [],
        "result": "MODERNIZE_RESULT: complete job_id=dry00099 db=wordpress mode=ui",
    }
    path.write_text(json.dumps(result_rec) + "\n")
    with pytest.raises(run.TranscriptError, match="never ran scripts/start_local_ui.py"):
        run.check_transcript(path, "ui")


def test_mode_mismatch_fails() -> None:
    """The transcript says mode=chat; asserting --mode both against it must fail."""
    with pytest.raises(run.TranscriptError, match="does not match"):
        run.check_transcript(FIXTURES / "synthetic-success-chat.jsonl", "both")


def test_fallback_to_chat_reports_the_ui_start_failure_reason() -> None:
    """Issue #346: a `both`-mode run whose UI couldn't start falls back to
    chat (MODERNIZE_RESULT ... mode=chat). Asserting --mode both against it
    must still fail (that mode's UI path was never exercised), but with the
    specific UI-start reason rather than the generic mode-mismatch message."""
    with pytest.raises(
        run.TranscriptError,
        match=(
            r"UI start failed, run fell back to chat: port 3000 is already in use "
            r"by something this script didn't start"
        ),
    ):
        run.check_transcript(FIXTURES / "synthetic-fallback-both-to-chat.jsonl", "both")


def test_subagent_decoy_complete_cannot_override_orchestrator_failed(tmp_path: Path) -> None:
    """A subagent's assistant text (``parent_tool_use_id`` set -- the real
    fixture confirms this is how a dispatched subagent's own turns are
    marked) can echo or illustrate a MODERNIZE_RESULT line while reporting
    status. That must never be mistaken for the authoritative sentinel: the
    orchestrator's own (top-level, ``parent_tool_use_id`` null) failed
    result still decides the outcome even though the decoy comes later."""
    path = _transcript(
        tmp_path,
        [
            _result_line("MODERNIZE_RESULT: failed phase=schema_design reason=boom"),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "For reference, the orchestrator previously reported "
                                "MODERNIZE_RESULT: complete job_id=abc12345 db=wordpress "
                                "mode=chat -- that status is now stale."
                            ),
                        }
                    ],
                },
                "parent_tool_use_id": "toolu_dispatching_tool_use",
            },
        ],
    )
    with pytest.raises(run.TranscriptError, match=r"failed phase=schema_design"):
        run.check_transcript(path, "chat")


def test_subagent_decoy_failed_cannot_override_orchestrator_complete(tmp_path: Path) -> None:
    """Same as above, inverted: a subagent decoy "failed" line must not
    override the orchestrator's own top-level "complete" result."""
    path = _transcript(
        tmp_path,
        [
            _result_line("MODERNIZE_RESULT: complete job_id=real00001 db=wordpress mode=chat"),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Illustrating a failure case: MODERNIZE_RESULT: failed phase=x reason=y",
                        }
                    ],
                },
                "parent_tool_use_id": "toolu_dispatching_tool_use",
            },
        ],
    )
    summary = run.check_transcript(path, "chat")
    assert summary["job_id"] == "real00001"
    assert summary["db"] == "wordpress"


def test_no_result_line_at_all_fails(tmp_path: Path) -> None:
    path = tmp_path / "no-result.jsonl"
    path.write_text('{"type": "system", "subtype": "init"}\n')
    with pytest.raises(run.TranscriptError, match='type == "result"'):
        run.check_transcript(path, "chat")


def test_non_json_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "noisy.jsonl"
    text = (FIXTURES / "synthetic-success-chat.jsonl").read_text()
    path.write_text("not json at all\n\n" + text)
    summary = run.check_transcript(path, "chat")
    assert summary["job_id"] == "dry00001"


def test_state_file_mismatch_warns_but_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_file = tmp_path / ".modernizer-state.json"
    state_file.write_text(json.dumps({"job_id": "some-other-job", "database_name": "wordpress"}))
    summary = run.check_transcript(
        FIXTURES / "synthetic-success-chat.jsonl", "chat", state_file=state_file
    )
    assert summary["job_id"] == "dry00001"
    assert "warning" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# check-transcript CLI
# ---------------------------------------------------------------------------


def test_cli_check_transcript_success_prints_json_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run.main(
            ["check-transcript", str(FIXTURES / "synthetic-success-chat.jsonl"), "--mode", "chat"]
        )
    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == "dry00001"


def test_cli_check_transcript_failure_prints_error_json_and_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run.main(
            [
                "check-transcript",
                str(FIXTURES / "synthetic-missing-modernize-result.jsonl"),
                "--mode",
                "chat",
            ]
        )
    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload


# ---------------------------------------------------------------------------
# junit parsing
# ---------------------------------------------------------------------------


def _write_junit(path: Path, cases: list[tuple[str, str, bool]]) -> None:
    """cases: list of (classname, name, failed)."""
    root = ET.Element("testsuite")
    for classname, name, failed in cases:
        tc = ET.SubElement(root, "testcase", classname=classname, name=name, time="0.01")
        if failed:
            ET.SubElement(tc, "failure", message="boom")
    ET.ElementTree(root).write(path)


def test_parse_junit_counts_buckets_by_classname(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    _write_junit(
        path,
        [
            ("tests.e2e.test_pipeline", "test_contracts_hold", False),
            ("tests.e2e.test_report_engineering", "test_renders", False),
            ("tests.e2e.test_report_engineering", "test_links_work", True),
            ("tests.e2e.test_report_pdf", "test_pdf_has_text", False),
        ],
    )
    buckets = run.parse_junit_counts(path)
    assert buckets["contracts"] == {"total": 1, "failed": 0}
    assert buckets["engineering"] == {"total": 2, "failed": 1}
    assert buckets["pdf"] == {"total": 1, "failed": 0}
    assert buckets["ui"] == {"total": 0, "failed": 0}


def test_parse_junit_counts_treats_error_as_failed(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    root = ET.Element("testsuite")
    tc = ET.SubElement(root, "testcase", classname="tests.e2e.test_ui", name="test_smoke")
    ET.SubElement(tc, "error", message="boom")
    ET.ElementTree(root).write(path)
    buckets = run.parse_junit_counts(path)
    assert buckets["ui"] == {"total": 1, "failed": 1}


# ---------------------------------------------------------------------------
# results merging
# ---------------------------------------------------------------------------


def _write_summary(path: Path, **overrides: object) -> None:
    payload = {
        "job_id": "dry00001",
        "db": "wordpress",
        "mode": "chat",
        "cost_usd": 0.42,
        "num_turns": 12,
        "usage": {"input_tokens": 10000, "output_tokens": 2500},
        "duration_ms": 45000,
        "model": "global.anthropic.claude-sonnet-5",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload))


def _write_passing_judge(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "scores": {"grounded": 5},
                "mean": 5.0,
                "pass": True,  # nosec B105 -- "pass" is the judge's pass/fail field, not a password
            }
        )
    )


def test_results_all_green_passes(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
        git_sha="abc1234",
    )
    assert row["pass"] is True
    assert row["checks"] == {
        "transcript": True,
        "contracts": True,
        "engineering": True,
        "pdf": True,
        "ui": None,
        "judge": True,
    }
    assert row["job_id"] == "dry00001"
    assert row["db"] == "wordpress"
    assert row["tokens_in"] == 10000
    assert row["tokens_out"] == 2500
    assert row["duration_s"] == 45.0
    assert row["schema_version"] == 2
    assert row["git_sha"] == "abc1234"
    assert row["judge"] == {"mean": 5.0, "scores": {"grounded": 5}}


def test_results_ui_mode_requires_ui_junit(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary, mode="ui")
    report_junit = tmp_path / "report-junit.xml"
    _write_junit(
        report_junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    ui_junit = tmp_path / "ui-junit.xml"
    _write_junit(ui_junit, [("tests.e2e.test_ui", "test_smoke", False)])
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="ui",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[report_junit, ui_junit],
        judge_path=judge,
    )
    assert row["checks"]["ui"] is True
    assert row["pass"] is True


def test_results_ui_mode_missing_ui_junit_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary, mode="ui")
    report_junit = tmp_path / "report-junit.xml"
    _write_junit(
        report_junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="ui",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[report_junit],  # no ui-junit.xml
        judge_path=judge,
    )
    assert row["checks"]["ui"] is None
    assert row["pass"] is False


def test_results_missing_transcript_summary_is_null_and_fails(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=tmp_path / "does-not-exist.json",
        pytest_junit_paths=[junit],
        judge_path=judge,
    )
    assert row["checks"]["transcript"] is None
    assert row["pass"] is False


def test_results_transcript_error_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"error": "no result message found"}))
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
    )
    assert row["checks"]["transcript"] is False
    assert row["pass"] is False


def test_results_missing_junit_file_is_null_and_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "does-not-exist.xml"],
        judge_path=judge,
    )
    assert row["checks"]["contracts"] is None
    assert row["checks"]["engineering"] is None
    assert row["checks"]["pdf"] is None
    assert row["pass"] is False


def test_results_junit_failures_counted(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", True),  # failure
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
    )
    assert row["checks"]["engineering"] is False
    assert row["pass"] is False


def test_results_missing_judge_file_is_null_and_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=tmp_path / "does-not-exist.json",
    )
    assert row["checks"]["judge"] is None
    assert row["pass"] is False


def test_results_judge_error_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps({"error": "claude CLI exited 2"}))

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
    )
    assert row["checks"]["judge"] is False
    assert row["pass"] is False


def test_results_judge_failing_score_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    judge.write_text(
        json.dumps(
            {
                "scores": {"grounded": 1},
                "mean": 1.0,
                "pass": False,  # nosec B105 -- "pass" is the judge's pass/fail field, not a password
            }
        )
    )

    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
    )
    assert row["checks"]["judge"] is False
    assert row["pass"] is False


# ---------------------------------------------------------------------------
# results CLI
# ---------------------------------------------------------------------------


def test_cli_results_writes_file_and_exits_zero_on_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_engineering", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)
    out = tmp_path / "results.json"

    with pytest.raises(SystemExit) as exc_info:
        run.main(
            [
                "results",
                "--out",
                str(out),
                "--transcript-summary",
                str(summary),
                "--pytest-junit",
                str(junit),
                "--judge",
                str(judge),
                "--mode",
                "chat",
                "--fixture",
                "wordpress",
            ]
        )
    assert exc_info.value.code == 0
    assert json.loads(out.read_text())["pass"] is True


def test_cli_results_exits_one_on_fail(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)
    out = tmp_path / "results.json"

    with pytest.raises(SystemExit) as exc_info:
        run.main(
            [
                "results",
                "--out",
                str(out),
                "--transcript-summary",
                str(summary),
                "--pytest-junit",
                str(tmp_path / "missing.xml"),
                "--judge",
                str(judge),
                "--mode",
                "chat",
                "--fixture",
                "wordpress",
            ]
        )
    assert exc_info.value.code == 1
    assert json.loads(out.read_text())["pass"] is False


# ---------------------------------------------------------------------------
# shell sanity
# ---------------------------------------------------------------------------


def test_e2e_llm_script_has_valid_bash_syntax() -> None:
    script = REPO_ROOT / "ci" / "e2e-llm.sh"
    proc = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10  # nosec B603 B607
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# hardening: usage on failure, transcript_error, unsafe ids, tool_result
# denials, corrupt junit
# ---------------------------------------------------------------------------


def _transcript(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


def _result_line(text: str, **extra: object) -> dict:
    rec: dict = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 7,
        "total_cost_usd": 1.25,
        "usage": {"input_tokens": 11, "output_tokens": 22},
        "duration_ms": 9000,
        "model": "m",
        "permission_denials": [],
        "result": text,
    }
    rec.update(extra)
    return rec


def test_failed_transcript_still_reports_cost_turns_and_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run.main(
            [
                "check-transcript",
                str(FIXTURES / "synthetic-pipeline-denial.jsonl"),
                "--mode",
                "chat",
            ]
        )
    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "allowlist" in payload["error"]
    assert payload["cost_usd"] == 0.30
    assert payload["num_turns"] == 8
    assert payload["usage"] == {"input_tokens": 5000, "output_tokens": 1200}


def test_results_carry_transcript_error_and_usage_on_failure(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "error": "result reported 1 permission denial(s)",
                "cost_usd": 0.2,
                "num_turns": 8,
                "usage": {"input_tokens": 5, "output_tokens": 6},
                "duration_ms": 3000,
                "model": "m",
            }
        )
    )
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["checks"]["transcript"] is False
    assert row["transcript_error"] == "result reported 1 permission denial(s)"
    assert row["cost_usd"] == 0.2
    assert row["num_turns"] == 8
    assert row["tokens_in"] == 5 and row["tokens_out"] == 6
    assert row["duration_s"] == 3.0
    assert row["pass"] is False


def test_results_transcript_error_is_null_on_success(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["transcript_error"] is None


def test_results_carries_denials_summary_from_transcript_summary(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary, denials={"count": 1, "commands": ["rm -rf /tmp/whatever"]})
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["denials"] == {"count": 1, "commands": ["rm -rf /tmp/whatever"]}


def test_results_carries_rule_violations_from_transcript_summary(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    violations = {"count": 1, "commands": ["uv run python scripts/search_artifacts.py x | head"]}
    _write_summary(
        summary, error="1 tool-use rule violation(s) by the model", rule_violations=violations
    )
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["rule_violations"] == violations
    assert row["transcript_error"] == "1 tool-use rule violation(s) by the model"
    assert row["pass"] is False


def test_results_wall_duration_s_parsed_from_claude_exit_file(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    claude_exit = tmp_path / "claude-exit.txt"
    claude_exit.write_text("claude exit=0 duration=123s\n")
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
        claude_exit_path=claude_exit,
    )
    assert row["wall_duration_s"] == 123.0
    # duration_s (from the transcript's own duration_ms) is a different,
    # smaller number -- model API time only, not wall-clock.
    assert row["duration_s"] == 45.0


def test_results_wall_duration_s_is_null_when_claude_exit_missing(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["wall_duration_s"] is None


@pytest.mark.parametrize(
    "job,db",
    [("../etc", "wordpress"), ("job1", "a/b"), ("..", "wordpress"), ("job1", "x`y`")],
)
def test_unsafe_job_or_db_in_modernize_result_fails(tmp_path: Path, job: str, db: str) -> None:
    path = _transcript(
        tmp_path,
        [_result_line(f"MODERNIZE_RESULT: complete job_id={job} db={db} mode=chat")],
    )
    with pytest.raises(run.TranscriptError, match="unsafe"):
        run.check_transcript(path, "chat")


def test_tool_result_only_denial_is_an_other_denial_not_a_failure(tmp_path: Path) -> None:
    """When a result line's own ``permission_denials`` is empty but a
    ``tool_result`` block reports a denial (older/different CLI build),
    that denial text carries no command to classify -- it's treated as an
    "other" (non-pipeline) denial, which does not fail the run."""
    path = _transcript(
        tmp_path,
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": True,
                            "content": "Permission to use Read has been denied.",
                        }
                    ]
                },
            },
            _result_line("MODERNIZE_RESULT: complete job_id=j1 db=wordpress mode=chat"),
        ],
    )
    summary = run.check_transcript(path, "chat")
    assert summary["job_id"] == "j1"
    assert summary["denials"]["count"] == 1


def test_tool_result_error_without_permission_text_is_tolerated(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path,
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": True, "content": "exit code 1"},
                        {"type": "tool_result", "content": "no permission issue here"},
                    ]
                },
            },
            _result_line("MODERNIZE_RESULT: complete job_id=j1 db=wordpress mode=chat"),
        ],
    )
    assert run.check_transcript(path, "chat")["job_id"] == "j1"


def test_corrupt_junit_marks_its_buckets_false(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary, mode="ui")
    reports = tmp_path / "reports.xml"
    reports.write_text("<testsuite><testcase name='x'")  # truncated
    ui = tmp_path / "ui.xml"
    ui.write_text("not xml at all <<<")
    judge = tmp_path / "judge.json"
    _write_passing_judge(judge)

    row = run.build_results_row(
        mode="ui",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[reports, ui],
        judge_path=judge,
        git_sha="abc",
    )
    assert row["checks"]["contracts"] is False
    assert row["checks"]["engineering"] is False
    assert row["checks"]["pdf"] is False
    assert row["checks"]["ui"] is False
    assert row["pass"] is False


# ---------------------------------------------------------------------------
# aggregation across ALL `type == "result"` messages (not just the last) --
# a real run emits one per background subagent plus the orchestrator.
# ---------------------------------------------------------------------------


def test_aggregates_turns_duration_cost_and_usage_across_all_result_messages(
    tmp_path: Path,
) -> None:
    """Two result messages (e.g. one background subagent's and the
    orchestrator's): num_turns and duration_ms sum, total_cost_usd (which is
    cumulative on every line) takes the max, and usage sums numeric leaves."""
    path = _transcript(
        tmp_path,
        [
            _result_line(
                "subagent notification, not final",
                num_turns=3,
                total_cost_usd=1.0,
                duration_ms=1000,
                usage={"input_tokens": 10, "output_tokens": 20},
            ),
            _result_line(
                "MODERNIZE_RESULT: complete job_id=j1 db=wordpress mode=chat",
                num_turns=5,
                total_cost_usd=2.5,
                duration_ms=2000,
                usage={"input_tokens": 30, "output_tokens": 40},
            ),
        ],
    )
    summary = run.check_transcript(path, "chat")
    assert summary["num_turns"] == 8
    assert summary["duration_ms"] == 3000
    assert summary["cost_usd"] == 2.5  # max, not sum -- cumulative on every line
    assert summary["usage"] == {"input_tokens": 40, "output_tokens": 60}


def test_merge_usage_sums_nested_numeric_leaves() -> None:
    merged = run._merge_usage(
        [
            {"input_tokens": 2, "cache_creation": {"ephemeral_5m_input_tokens": 100}},
            {"input_tokens": 3, "cache_creation": {"ephemeral_5m_input_tokens": 50}},
        ]
    )
    assert merged == {"input_tokens": 5, "cache_creation": {"ephemeral_5m_input_tokens": 150}}


def test_allowlisted_bash_prefixes_parses_both_rule_forms(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Bash(uv run python scripts/run_schema_design.py *)",
                        "Bash(uv run python scripts/start_local_ui.py)",
                        "Edit(artifacts/**)",
                    ]
                }
            }
        )
    )
    prefixes = run._allowlisted_bash_prefixes(settings)
    assert prefixes == [
        "uv run python scripts/run_schema_design.py",
        "uv run python scripts/start_local_ui.py",
    ]


# ---------------------------------------------------------------------------
# the real (trimmed) transcript recorded on the first internal-pipeline run
# ---------------------------------------------------------------------------


def test_real_fixture_failed_run_reports_schema_design_failure_and_denials() -> None:
    """tests/unit/ci/fixtures/real-chat-wordpress-failed.jsonl is a trimmed
    copy of the first internal-pipeline run (chat/wordpress): DynamoDB
    schema design failed, and the model's 14 denials were all its own
    exploratory Bash commands the allowlist correctly blocked -- none of
    them should fail the run by themselves (see
    test_exploratory_denial_passes_with_warning), but the DynamoDB failure
    still fails check-transcript, naming its phase/reason and still
    reporting what the (11-subagent) run spent."""
    with pytest.raises(run.TranscriptError) as exc_info:
        run.check_transcript(FIXTURES / "real-chat-wordpress-failed.jsonl", "chat")
    message = str(exc_info.value)
    assert "phase=schema_design" in message
    partial = exc_info.value.partial
    assert partial["denials"]["count"] == 14
    assert partial["cost_usd"] == pytest.approx(4.0624, abs=1e-3)
    assert partial["num_turns"] > 1


# ---------------------------------------------------------------------------
# ci/e2e-llm.sh: results.json is written even when a preflight step fails
# (both cases exit before the claude call or any repo-state change)
# ---------------------------------------------------------------------------


def _run_e2e_llm(tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_REGION",
            "E2E_LLM_ARTIFACT_ROOT",
            "E2E_LLM_DRY_RUN",
        }
    }
    env.update({"E2E_OUTPUT": str(tmp_path), **extra_env})
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["bash", str(REPO_ROOT / "ci" / "e2e-llm.sh"), "chat", "wordpress"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def test_e2e_llm_rejects_artifact_root_override_outside_dry_run(tmp_path: Path) -> None:
    proc = _run_e2e_llm(tmp_path, {"E2E_LLM_ARTIFACT_ROOT": str(tmp_path / "elsewhere")})

    assert proc.returncode == 2, proc.stderr
    assert "E2E_LLM_ARTIFACT_ROOT" in proc.stderr
    results = json.loads((tmp_path / "llm-chat-wordpress" / "results.json").read_text())
    assert results == {
        "schema_version": 2,
        "pass": False,  # nosec B105 -- "pass" is the run's pass/fail field, not a password
        "error": "artifact-root-check failed before the run",
    }


def test_e2e_llm_missing_model_access_still_writes_results(tmp_path: Path) -> None:
    proc = _run_e2e_llm(tmp_path, {})

    assert proc.returncode != 0
    results = json.loads((tmp_path / "llm-chat-wordpress" / "results.json").read_text())
    assert results["pass"] is False
    assert results["error"] == "require-env failed before the run"


# ---------------------------------------------------------------------------
# collect-logs: the run's diagnostic logs reach the uploaded test-results/
# ---------------------------------------------------------------------------


def test_collect_logs_copies_the_job_log_dir_and_extra_logs(tmp_path: Path) -> None:
    log_dir = tmp_path / "artifacts" / "wordpress" / "j1" / "_logs"
    log_dir.mkdir(parents=True)
    (log_dir / "run_assessment.log").write_text("progress\n")
    ui = tmp_path / ".local-ui"
    ui.mkdir()
    (ui / "api.log").write_text("api up\n")
    out = tmp_path / "out"

    copied = run.collect_logs(
        tmp_path / "artifacts",
        out,
        db="wordpress",
        job="j1",
        extra_logs=[ui / "api.log", ui / "missing.log"],
    )

    assert [c["truncated"] for c in copied] == [False, False]
    assert (out / "job-logs" / "run_assessment.log").read_text() == "progress\n"
    assert (out / "job-logs" / "local-ui-api.log").read_text() == "api up\n"


def test_collect_logs_bounds_each_file_and_falls_back_to_the_state_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "artifacts" / "discourse" / "j2" / "_logs"
    log_dir.mkdir(parents=True)
    (log_dir / "run_assessment.log").write_bytes(b"x" * 5000 + b"END")
    state = tmp_path / ".modernizer-state.json"
    state.write_text(json.dumps({"database_name": "discourse", "job_id": "j2"}))

    copied = run.collect_logs(
        tmp_path / "artifacts", tmp_path / "out", state_file=state, max_bytes=100
    )

    assert copied[0]["truncated"] is True and copied[0]["bytes"] == 5003
    data = (tmp_path / "out" / "job-logs" / "run_assessment.log").read_bytes()
    assert data.startswith(b"[truncated: last 100 of 5003 bytes")
    assert data.endswith(b"x" * 97 + b"END")
    assert len(data.split(b"\n", 1)[1]) == 100


def test_collect_logs_without_a_job_copies_nothing(tmp_path: Path) -> None:
    assert run.collect_logs(tmp_path / "artifacts", tmp_path / "out") == []


def test_e2e_llm_collects_logs_on_exit() -> None:
    text = (REPO_ROOT / "ci" / "e2e-llm.sh").read_text()
    on_exit = text[text.index("on_exit() {") : text.index("trap on_exit EXIT")]
    assert "ci/llm/run.py collect-logs" in on_exit
    assert '--out "$OUT"' in on_exit


@pytest.mark.parametrize(
    ("db", "job"),
    [("..", "x"), ("../..", "x"), ("wordpress", ".."), ("wordpress", "../j1"), ("/etc", "x")],
)
def test_collect_logs_refuses_names_that_leave_the_artifact_root(
    tmp_path: Path, db: str, job: str
) -> None:
    root = tmp_path / "artifacts"
    (root / "wordpress" / "j1" / "_logs").mkdir(parents=True)
    (root / "wordpress" / "j1" / "_logs" / "run_assessment.log").write_text("ok\n")
    outside = tmp_path / "x" / "_logs"
    outside.mkdir(parents=True)
    (outside / "secret.log").write_text("secret\n")
    if db.startswith("/"):
        db = str(tmp_path)  # absolute path to a dir that has x/_logs under it

    assert run.collect_logs(root, tmp_path / "out", db=db, job=job) == []
    assert not (tmp_path / "out" / "job-logs").exists()


def test_collect_logs_refuses_unsafe_names_from_the_state_file(tmp_path: Path) -> None:
    (tmp_path / "x" / "_logs").mkdir(parents=True)
    (tmp_path / "x" / "_logs" / "secret.log").write_text("secret\n")
    state = tmp_path / ".modernizer-state.json"
    state.write_text(json.dumps({"database_name": "..", "job_id": "x"}))
    root = tmp_path / "artifacts"
    root.mkdir()
    assert run.collect_logs(root, tmp_path / "out", state_file=state) == []


# ---------------------------------------------------------------------------
# Non-Bash denials are named "<Tool>: <target>" (#693)
# ---------------------------------------------------------------------------


def _denied(tmp_path: Path, *denials: dict[str, object]) -> dict[str, object]:
    path = _transcript(
        tmp_path,
        [
            _result_line(
                "MODERNIZE_RESULT: complete job_id=den00001 db=wordpress mode=chat",
                permission_denials=list(denials),
            )
        ],
    )
    return run.check_transcript(path, "chat")


def test_denied_skill_outside_allowlist_is_named_not_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = _denied(
        tmp_path,
        {
            "tool_name": "Skill",
            "tool_use_id": "tu-1",
            "tool_input": {"skill": "update-config", "args": "noop"},
        },
    )
    assert summary["unlisted_tool_denials"] == {"count": 1, "commands": ["Skill: update-config"]}
    assert "denials" not in summary
    assert "Skill: update-config" in capsys.readouterr().err


def test_denied_allowlisted_skill_is_a_broken_allowlist(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path,
        [
            _result_line(
                "MODERNIZE_RESULT: complete job_id=den00002 db=wordpress mode=chat",
                permission_denials=[
                    {"tool_name": "Skill", "tool_input": {"skill": "design-schema-dynamodb"}}
                ],
            )
        ],
    )
    with pytest.raises(run.TranscriptError, match=r"allowlist.*Skill: design-schema-dynamodb"):
        run.check_transcript(path, "chat")


@pytest.mark.parametrize(
    ("denial", "label"),
    [
        ({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, "Read: /etc/hosts"),
        ({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x.txt"}}, "Write: /tmp/x.txt"),
        ({"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}}, "Edit: src/a.py"),
        (
            {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com/a"}},
            "WebFetch: https://example.com/a",
        ),
        (
            {"tool_name": "Glob", "tool_input": {"pattern": "**/*.py", "path": "src"}},
            'Glob: {"path":"src","pattern":"**/*.py"}',
        ),
        ({"tool_name": "TodoWrite", "tool_input": {}}, "TodoWrite"),
    ],
)
def test_denied_non_bash_tools_are_labeled_with_their_target(
    tmp_path: Path, denial: dict[str, object], label: str
) -> None:
    summary = _denied(tmp_path, denial)
    assert summary["unlisted_tool_denials"] == {"count": 1, "commands": [label]}


CWD = "/workspace/repo"


def _denied_path(tmp_path: Path, tool: str, file_path: str, cwd: str | None = CWD) -> dict:
    """Run check_transcript over a session whose one denial is ``tool`` on the
    absolute ``file_path`` (Claude Code always sends absolute paths)."""
    records: list[dict] = []
    if cwd is not None:
        records.append({"type": "system", "subtype": "init", "cwd": cwd})
    records.append(
        _result_line(
            "MODERNIZE_RESULT: complete job_id=den00003 db=wordpress mode=chat",
            permission_denials=[
                {"tool_name": tool, "tool_use_id": "tu-1", "tool_input": {"file_path": file_path}}
            ],
        )
    )
    return run.check_transcript(_transcript(tmp_path, records), "chat")


@pytest.mark.parametrize(
    ("tool", "file_path"),
    [
        ("Write", f"{CWD}/artifacts/wordpress/j1/x.json"),
        ("Edit", f"{CWD}/artifacts/wordpress/j1/y.json"),
        ("Edit", f"{CWD}/.modernizer-state.json"),
        ("Write", f"{CWD}/artifacts/../artifacts/wordpress/x.json"),
    ],
)
def test_denied_absolute_path_inside_allow_rule_is_a_broken_allowlist(
    tmp_path: Path, tool: str, file_path: str
) -> None:
    with pytest.raises(run.TranscriptError, match="allowlist"):
        _denied_path(tmp_path, tool, file_path)


@pytest.mark.parametrize(
    ("tool", "file_path"),
    [
        # A deny rule blocks it (Write(artifacts/.local-ui/**)): a correct denial.
        ("Write", f"{CWD}/artifacts/.local-ui/x.json"),
        ("Edit", f"{CWD}/.local-ui/state.json"),
        # ``..`` escapes the allowed directory: src/a.py is not allowlisted.
        ("Edit", f"{CWD}/artifacts/../src/a.py"),
        ("Write", f"{CWD}/src/a.py"),
        # Same relative path in another directory is not under this cwd.
        ("Write", "/elsewhere/artifacts/x.json"),
    ],
)
def test_denied_path_outside_allowlist_or_blocked_by_deny_rule_only_warns(
    tmp_path: Path, tool: str, file_path: str
) -> None:
    summary = _denied_path(tmp_path, tool, file_path)
    assert summary["unlisted_tool_denials"]["commands"] == [f"{tool}: {file_path}"]


def test_cwd_comes_from_the_init_record(tmp_path: Path) -> None:
    with pytest.raises(run.TranscriptError, match="allowlist"):
        _denied_path(tmp_path, "Write", "/other/checkout/artifacts/x.json", cwd="/other/checkout")
    summary = _denied_path(tmp_path, "Write", "/other/checkout/artifacts/x.json", cwd=CWD)
    assert "unlisted_tool_denials" in summary


def test_bash_and_skill_denials_are_reported_side_by_side(tmp_path: Path) -> None:
    summary = _denied(
        tmp_path,
        {"tool_name": "Bash", "tool_input": {"command": "cat a.json"}},
        {"tool_name": "Skill", "tool_input": {"skill": "update-config"}},
    )
    assert summary["denials"] == {"count": 1, "commands": ["Bash: cat a.json"]}
    assert summary["unlisted_tool_denials"] == {"count": 1, "commands": ["Skill: update-config"]}


def test_results_carry_unlisted_tool_denials(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary, unlisted_tool_denials={"count": 1, "commands": ["Skill: x"]})
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["unlisted_tool_denials"] == {"count": 1, "commands": ["Skill: x"]}


# --- calls refused by our PreToolUse hooks (#731) ---------------------------

_HOOK_REFUSAL = (
    "PreToolUse:Bash hook error: [uv run --no-project python -I "
    '"$CLAUDE_PROJECT_DIR/scripts/hooks/enforce_tool_use_rule.py"]: '
    "Blocked by the tool-use rule: Use Bash only for the documented commands."
)


def _hook_refused_records(command: str, reason: str = _HOOK_REFUSAL) -> list[dict]:
    """A hook-refused call shaped like the real record: the denial in the result line's
    ``permission_denials`` plus the ``tool_result`` the model saw, joined by ``tool_use_id``."""
    return [
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-1",
                        "is_error": True,
                        "content": reason,
                    }
                ]
            },
        },
        _result_line(
            "MODERNIZE_RESULT: complete job_id=hook0001 db=wordpress mode=chat",
            permission_denials=[
                {"tool_name": "Bash", "tool_use_id": "tu-1", "tool_input": {"command": command}}
            ],
        ),
    ]


def test_hook_refused_pipe_is_prevented_not_a_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = "uv run python scripts/run_schema_design.py --job-id j --status | head -c 300"
    summary = run.check_transcript(_transcript(tmp_path, _hook_refused_records(command)), "chat")
    assert summary["prevented_by_hook"] == {"count": 1, "commands": [f"Bash: {command}"]}
    assert "rule_violations" not in summary and "denials" not in summary
    assert "prevented, not a failure" in capsys.readouterr().err


def test_same_denial_without_a_hook_refusal_still_fails(tmp_path: Path) -> None:
    command = "uv run python scripts/run_schema_design.py --job-id j --status | head -c 300"
    plain = (
        "Permission to use Bash has been denied because Claude Code is running in don't ask mode."
    )
    records = _hook_refused_records(command, reason=plain)
    with pytest.raises(run.TranscriptError, match="tool-use rule violation"):
        run.check_transcript(_transcript(tmp_path, records), "chat")


def test_hook_refusal_of_another_call_does_not_excuse_a_violation(tmp_path: Path) -> None:
    command = "uv run python scripts/run_schema_design.py --job-id j --status | head -c 300"
    records = _hook_refused_records(command)
    records[0]["message"]["content"][0]["tool_use_id"] = "tu-other"
    with pytest.raises(run.TranscriptError, match="tool-use rule violation"):
        run.check_transcript(_transcript(tmp_path, records), "chat")


def test_hook_refused_edit_is_prevented(tmp_path: Path) -> None:
    records = _hook_refused_records("x")
    records[0]["message"]["content"][0][
        "content"
    ] = "PreToolUse:Edit hook error: Blocked: routing_snapshot.json is a generated baseline"
    records[1]["permission_denials"] = [
        {
            "tool_name": "Edit",
            "tool_use_id": "tu-1",
            "tool_input": {"file_path": "/x/routing_snapshot.json"},
        }
    ]
    summary = run.check_transcript(_transcript(tmp_path, records), "chat")
    assert summary["prevented_by_hook"]["count"] == 1


def test_results_carry_prevented_by_hook(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary, prevented_by_hook={"count": 2, "commands": ["Bash: a", "Bash: b"]})
    row = run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[tmp_path / "missing.xml"],
        judge_path=tmp_path / "missing.json",
        git_sha="abc",
    )
    assert row["prevented_by_hook"] == {"count": 2, "commands": ["Bash: a", "Bash: b"]}


# ---------------------------------------------------------------------------
# check_transcript: UI pointers in ui/both mode (#1242)
# ---------------------------------------------------------------------------

_SUMMARY_URL = "http://localhost:3000/analysis/monitor/summary/dry00002"
_RESULTS_URL = "http://localhost:3000/analysis/results/dry00002"


def _ui_records() -> list[dict]:
    records: list[dict] = run.iter_jsonl(FIXTURES / "synthetic-success-ui.jsonl")
    return records


def _drop_url_from_texts(records: list[dict], url: str) -> list[dict]:
    """The same run, with ``url`` removed from every assistant text and result."""
    out: list[dict] = json.loads(json.dumps(records))
    for rec in out:
        if rec.get("type") == "result":
            rec["result"] = rec["result"].replace(url, "the page")
        if rec.get("type") == "assistant":
            for block in rec["message"]["content"]:
                if block.get("type") == "text":
                    block["text"] = block["text"].replace(url, "the page")
    return out


def _fails_with(tmp_path: Path, records: list[dict], match: str) -> None:
    with pytest.raises(run.TranscriptError, match=match):
        run.check_transcript(_transcript(tmp_path, records), "ui")


def test_both_mode_run_that_never_quotes_the_gate_url_fails(tmp_path: Path) -> None:
    # The #1242 run: --page summary ran, its ui_url was never shown. A warning
    # until #1307 printed the gate and completion text; fatal since.
    records = _drop_url_from_texts(_ui_records(), _SUMMARY_URL)
    _fails_with(tmp_path, records, "missing UI pointer: ui mode never quoted the summary-page")


def test_run_that_never_asks_for_the_summary_page_fails(tmp_path: Path) -> None:
    records = [
        rec
        for rec in _ui_records()
        if "--page summary" not in json.dumps(rec) and _SUMMARY_URL not in json.dumps(rec)
    ]
    _fails_with(tmp_path, records, "--page summary")


def test_run_that_never_quotes_the_results_url_fails(tmp_path: Path) -> None:
    records = _drop_url_from_texts(_ui_records(), _RESULTS_URL)
    _fails_with(tmp_path, records, "never quoted the results-page ui_url")


def test_gate_url_quoted_only_after_schema_design_starts_fails(tmp_path: Path) -> None:
    records = _drop_url_from_texts(_ui_records(), _SUMMARY_URL)
    records[-2]["message"]["content"][0]["text"] += f"\nSummary page: {_SUMMARY_URL}"
    _fails_with(tmp_path, records, "only after schema design started")


def test_gate_url_quoted_only_by_a_subagent_does_not_count(tmp_path: Path) -> None:
    records = _drop_url_from_texts(_ui_records(), _SUMMARY_URL)
    subagent_text = {
        "type": "assistant",
        "parent_tool_use_id": "tu-agent",
        "message": {"role": "assistant", "content": [{"type": "text", "text": _SUMMARY_URL}]},
    }
    records.insert(5, subagent_text)
    _fails_with(tmp_path, records, "never quoted the summary-page ui_url")


def test_the_results_url_may_come_from_the_printed_final_summary() -> None:
    # #1307: `print_summary.py --final --mode ui` lists the results page itself.
    records = _ui_records()
    for rec in records:
        for block in (rec.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use" and "--page results" in block["input"].get(
                "command", ""
            ):
                block["input"]["command"] = (
                    "uv run python scripts/print_summary.py --job-id dry00002 "
                    "--db wordpress --final --mode ui"
                )
            elif block.get("type") == "tool_result" and _RESULTS_URL in json.dumps(block):
                block["content"][0]["text"] = (
                    "## Assessment: wordpress (job dry00002)\n\n### Where to look next\n\n"
                    f"- Results page (the interactive report): {_RESULTS_URL}\n"
                )
    assert run.check_ui_pointers(records, "ui") == (True, None)


_GATE_COMMAND = (
    "uv run python scripts/print_summary.py --job-id dry00002 --db wordpress --gate --mode ui"
)
_GATE_BLOCK = (
    "## Assignment for approval: wordpress (job dry00002)\n\n"
    "| Engine | Queries before | Queries final | Share of final | Outcome |\n\n"
    f"- Summary page (the query flow and every query's engine): {_SUMMARY_URL}\n"
)


def _gate_printed_records() -> list[dict]:
    """The UI run, with the summary page's URL from the printed gate block (#1307)
    instead of a separate ``start_local_ui.py --page summary`` call."""
    records = _ui_records()
    for rec in records:
        for block in (rec.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use" and "--page summary" in block["input"].get(
                "command", ""
            ):
                block["input"]["command"] = _GATE_COMMAND
            elif block.get("type") == "tool_result" and _SUMMARY_URL in json.dumps(block):
                block["content"][0]["text"] = _GATE_BLOCK
    return records


def test_the_summary_url_may_come_from_the_printed_gate_block() -> None:
    assert run.check_ui_pointers(_gate_printed_records(), "ui") == (True, None)


def test_a_gate_block_run_alongside_schema_design_and_never_quoted_fails(tmp_path: Path) -> None:
    # The PR 1358 e2e-llm runs: the gate block and the DynamoDB split ran in one batch of
    # tool calls and the gate was never written out; the summary URL reached the user
    # only in the final summary.
    records = _drop_url_from_texts(_gate_printed_records(), _SUMMARY_URL)
    gate_use = next(
        rec
        for rec in records
        if rec.get("type") == "assistant"
        and any(
            b.get("input", {}).get("command") == _GATE_COMMAND for b in rec["message"]["content"]
        )
    )
    split_rec = next(
        rec
        for rec in records
        if rec.get("type") == "assistant"
        and any(
            "--split" in b.get("input", {}).get("command", "") for b in rec["message"]["content"]
        )
    )
    split_use = next(b for b in split_rec["message"]["content"] if b.get("type") == "tool_use")
    gate_use["message"]["content"].append(split_use)
    split_rec["message"]["content"] = [
        b for b in split_rec["message"]["content"] if b is not split_use
    ]
    records[-2]["message"]["content"][0]["text"] += f"\nSummary page: {_SUMMARY_URL}"
    _fails_with(tmp_path, records, "only after schema design started")


@pytest.mark.passes_on_main  # guards that the new gate-block source stays the gate's only
def test_the_gate_url_counts_only_from_the_gate_block_not_the_final_summary(
    tmp_path: Path,
) -> None:
    # The final summary also lists the summary page, but only the gate's URL is the pointer
    # the user follows before approving.
    records = [
        rec
        for rec in _ui_records()
        if "--page summary" not in json.dumps(rec) and _SUMMARY_URL not in json.dumps(rec)
    ]
    for rec in records:
        for block in (rec.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use" and "--page results" in block["input"].get(
                "command", ""
            ):
                block["input"]["command"] = (
                    "uv run python scripts/print_summary.py --job-id dry00002 "
                    "--db wordpress --final --mode ui"
                )
            elif block.get("type") == "tool_result" and _RESULTS_URL in json.dumps(block):
                block["content"][0]["text"] = (
                    f"- Results page (the interactive report): {_RESULTS_URL}\n"
                    f"- Summary page (the query flow and the assignment): {_SUMMARY_URL}\n"
                )
    _fails_with(tmp_path, records, "never got a summary-page ui_url")


def test_design_schema_dispatch_also_marks_schema_design_start() -> None:
    dispatch = {
        "type": "tool_use",
        "name": "Agent",
        "input": {"prompt": "Run /design-schema-aurora-mysql for job_id=x"},
    }
    other = {
        "type": "tool_use",
        "name": "Agent",
        "input": {"prompt": "Run /reality-check for job_id=x"},
    }
    assert run._phase6_starts("assistant", dispatch)
    assert not run._phase6_starts("assistant", other)


def test_chat_mode_needs_no_ui_pointers() -> None:
    assert run.check_ui_pointers(_ui_records(), "chat") == (True, None)


@pytest.mark.passes_on_main
def test_a_run_that_quotes_both_urls_passes_without_a_pointer_warning() -> None:
    summary = run.check_transcript(FIXTURES / "synthetic-success-ui.jsonl", "ui")
    assert "ui_pointer_warning" not in summary


def test_a_missing_ui_pointer_no_longer_leaves_a_warning_field(tmp_path: Path) -> None:
    # #1307: the pointer check is fatal, so a summary never carries the old warning field,
    # and an old summary.json that still has one is not copied into results.json.
    summary_path = tmp_path / "summary.json"
    _write_summary(
        summary_path, ui_pointer_warning="ui mode never quoted the summary-page ui_url x"
    )
    row = run.build_results_row(
        mode="ui",
        fixture="wordpress",
        transcript_summary_path=summary_path,
        pytest_junit_paths=[],
        judge_path=tmp_path / "judge.json",
        git_sha="abc",
    )
    assert "ui_pointer_warning" not in row
