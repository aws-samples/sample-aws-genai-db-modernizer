"""Tests for ci/llm/run.py's transcript checking and results merging.

Uses the small synthetic stream-json fixtures in tests/unit/ci/fixtures/
(synthetic-*.jsonl) rather than a recorded real transcript -- see
ci/llm/run.py's module docstring for why (Step 1's spike, recording real
transcript shapes against the installed CLI, was explicitly skipped to avoid
spending model tokens; the field names here are assumptions from public docs
to be confirmed on the first internal-pipeline run).
"""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
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


def test_denial_present_fails() -> None:
    with pytest.raises(run.TranscriptError, match="permission denial"):
        run.check_transcript(FIXTURES / "synthetic-denial.jsonl", "chat")


def test_error_max_turns_fails() -> None:
    with pytest.raises(run.TranscriptError, match="is_error"):
        run.check_transcript(FIXTURES / "synthetic-error-max-turns.jsonl", "chat")


def test_missing_modernize_result_fails() -> None:
    with pytest.raises(run.TranscriptError, match="MODERNIZE_RESULT"):
        run.check_transcript(FIXTURES / "synthetic-missing-modernize-result.jsonl", "chat")


def test_chat_run_that_started_ui_fails() -> None:
    with pytest.raises(run.TranscriptError, match="start_local_ui"):
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
        run.main(["check-transcript", str(FIXTURES / "synthetic-denial.jsonl"), "--mode", "chat"])
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
            ("tests.e2e.test_report_html", "test_renders", False),
            ("tests.e2e.test_report_html", "test_links_work", True),
            ("tests.e2e.test_report_pdf", "test_pdf_has_text", False),
        ],
    )
    buckets = run.parse_junit_counts(path)
    assert buckets["contracts"] == {"total": 1, "failed": 0}
    assert buckets["html"] == {"total": 2, "failed": 1}
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
    path.write_text(json.dumps({"scores": {"grounded": 5}, "mean": 5.0, "pass": True}))


def test_results_all_green_passes(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_html", "test_x", False),
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
        "html": True,
        "pdf": True,
        "ui": None,
        "judge": True,
    }
    assert row["job_id"] == "dry00001"
    assert row["db"] == "wordpress"
    assert row["tokens_in"] == 10000
    assert row["tokens_out"] == 2500
    assert row["duration_s"] == 45.0
    assert row["schema_version"] == 1
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
            ("tests.e2e.test_report_html", "test_x", False),
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
            ("tests.e2e.test_report_html", "test_x", False),
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
            ("tests.e2e.test_report_html", "test_x", False),
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
            ("tests.e2e.test_report_html", "test_x", False),
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
    assert row["checks"]["html"] is None
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
            ("tests.e2e.test_report_html", "test_x", True),  # failure
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
    assert row["checks"]["html"] is False
    assert row["pass"] is False


def test_results_missing_judge_file_is_null_and_fails(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    _write_summary(summary)
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        [
            ("tests.e2e.test_pipeline", "test_x", False),
            ("tests.e2e.test_report_html", "test_x", False),
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
            ("tests.e2e.test_report_html", "test_x", False),
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
            ("tests.e2e.test_report_html", "test_x", False),
            ("tests.e2e.test_report_pdf", "test_x", False),
        ],
    )
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps({"scores": {"grounded": 1}, "mean": 1.0, "pass": False}))

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
            ("tests.e2e.test_report_html", "test_x", False),
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
        run.main(["check-transcript", str(FIXTURES / "synthetic-denial.jsonl"), "--mode", "chat"])
    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "permission denial" in payload["error"]
    assert payload["cost_usd"] == 0.20
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


def test_tool_result_permission_error_counts_as_a_denial(tmp_path: Path) -> None:
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
    with pytest.raises(run.TranscriptError, match="permission"):
        run.check_transcript(path, "chat")


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
    assert row["checks"]["html"] is False
    assert row["checks"]["pdf"] is False
    assert row["checks"]["ui"] is False
    assert row["pass"] is False
