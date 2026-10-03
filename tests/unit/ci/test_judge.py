"""ci/llm/judge.py grades a job's rendered deliverables through the same
`claude -p ... --output-format json` CLI invocation the headless run uses.

No real model is ever called here: CLAUDE_BIN is pointed at a tiny stub
script written per-test into tmp_path that records the argv it was invoked
with and prints a canned `--output-format json` result -- the same shape the
real CLI's non-interactive output has (an outer object with a string
`result` field holding the model's reply).
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from ci.llm import judge

DB, JOB = "acme", "job-1"

PASSING_SCORES = {k: 5 for k in judge.CRITERIA}
ALL_TWOS = {k: 2 for k in judge.CRITERIA}


def _build_job_dir(artifact_root: Path, *, db: str = DB, job: str = JOB, version: int = 1) -> Path:
    synthesis_dir = artifact_root / db / job / "synthesis" / f"v{version}"
    synthesis_dir.mkdir(parents=True)
    report = {
        "database_name": db,
        "job_id": job,
        "ranking": [{"engine": "dynamodb", "schema_design_available": True}],
    }
    (synthesis_dir / "report.json").write_text(json.dumps(report))
    (synthesis_dir / f"{db}_decision-report_{job}_20261001.html").write_text(
        "<html><body><h1>Decision</h1><p>DynamoDB selected. Cost: $500/mo.</p></body></html>"
    )
    (synthesis_dir / f"{db}_engineering-report_{job}_20261001.md").write_text(
        "# Engineering Report\n\nDynamoDB. Cost: $500/mo.\n"
    )
    return synthesis_dir


def _write_stub(
    tmp_path: Path, outer_result: dict, argv_file: Path, *, name: str = "fake_claude.py"
) -> Path:
    """Write a stub CLAUDE_BIN script that records its argv to `argv_file` (as
    a JSON list) and prints `json.dumps(outer_result)` on stdout."""
    stub = tmp_path / name
    payload_text = json.dumps(outer_result)
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"with open({str(argv_file)!r}, 'w') as f:\n"
        "    json.dump(sys.argv, f)\n"
        f"print({payload_text!r})\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _outer(result_text: str, **extra) -> dict:
    outer = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "total_cost_usd": 0.01,
        "num_turns": 1,
        "session_id": "sess-abc",
        "model": "claude-sonnet-5",
    }
    outer.update(extra)
    return outer


def _inner(scores: dict) -> str:
    return json.dumps({"scores": scores, "notes": {k: f"note-{k}" for k in scores}})


def test_pass_case(tmp_path: Path) -> None:
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    stub = _write_stub(tmp_path, _outer(_inner(PASSING_SCORES)), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )

    assert code == 0, result
    assert result["pass"] is True
    assert result["scores"] == PASSING_SCORES
    assert result["mean"] == 5.0
    assert result["model"] == "claude-sonnet-5"
    assert result["cost_usd"] == 0.01
    assert set(result["notes"]) == set(judge.CRITERIA)


def test_fail_on_min_score(tmp_path: Path) -> None:
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    scores = dict(PASSING_SCORES)
    scores["tone"] = 1  # below default min_score (2); mean is still well above 3.5
    stub = _write_stub(tmp_path, _outer(_inner(scores)), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )

    assert code == 1, result
    assert result["pass"] is False
    assert result["mean"] >= 3.5  # the mean rule alone would have passed
    assert min(result["scores"].values()) == 1


def test_fail_on_mean(tmp_path: Path) -> None:
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    # every criterion individually clears the default min_score (2), but the
    # mean (2.0) is below the default pass_mean (3.5).
    stub = _write_stub(tmp_path, _outer(_inner(ALL_TWOS)), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )

    assert code == 1, result
    assert result["pass"] is False
    assert result["mean"] == 2.0
    assert min(result["scores"].values()) >= 2


def test_unparseable_judge_response_is_exit_code_2(tmp_path: Path) -> None:
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    stub = _write_stub(tmp_path, _outer("not json, sorry about that"), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )

    assert code == 2, result
    assert "error" in result
    assert "parse" in result["error"].lower()


def test_fenced_json_code_block_is_tolerated(tmp_path: Path) -> None:
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    fenced = "Here is my assessment:\n```json\n" + _inner(PASSING_SCORES) + "\n```\nThanks!"
    stub = _write_stub(tmp_path, _outer(fenced), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )

    assert code == 0, result
    assert result["pass"] is True
    assert result["scores"] == PASSING_SCORES


def test_front_matter_thresholds_are_honoured(tmp_path: Path) -> None:
    """A custom rubric with a stricter pass_mean than the default must change
    the pass/fail outcome for the exact same scores."""
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    scores = dict.fromkeys(judge.CRITERIA, 4)  # mean 4.0
    stub = _write_stub(tmp_path, _outer(_inner(scores)), argv_file)

    custom_rubric = tmp_path / "custom-rubric.md"
    custom_rubric.write_text("---\npass_mean: 4.9\nmin_score: 1\n---\n\nbody text\n")

    # Default thresholds (pass_mean 3.5) would pass with mean 4.0.
    default_result, default_code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )
    assert default_code == 0
    assert default_result["pass"] is True

    # The stricter custom front matter (pass_mean 4.9) must fail the same scores.
    custom_result, custom_code = judge.run_judge(
        artifact_root=str(tmp_path),
        db=DB,
        job=JOB,
        claude_bin=str(stub),
        rubric_path=custom_rubric,
    )
    assert custom_code == 1, custom_result
    assert custom_result["pass"] is False
    assert custom_result["mean"] == 4.0


def test_prompt_includes_all_six_criterion_keys(tmp_path: Path) -> None:
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    stub = _write_stub(tmp_path, _outer(_inner(PASSING_SCORES)), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )
    assert code == 0, result

    recorded_argv = json.loads(argv_file.read_text())
    full_command_line = " ".join(recorded_argv)
    for criterion in judge.CRITERIA:
        assert criterion in full_command_line, f"missing {criterion!r} in prompt argv"

    # Also confirm it was actually passed as -p's argument, not just coincidentally
    # present somewhere else on the command line.
    assert "-p" in recorded_argv
    prompt_arg = recorded_argv[recorded_argv.index("-p") + 1]
    for criterion in judge.CRITERIA:
        assert criterion in prompt_arg


def test_missing_deliverable_is_exit_code_2_with_clear_error(tmp_path: Path) -> None:
    # Build only report.json -- no decision/engineering report files.
    synthesis_dir = tmp_path / DB / JOB / "synthesis" / "v1"
    synthesis_dir.mkdir(parents=True)
    (synthesis_dir / "report.json").write_text(json.dumps({"database_name": DB, "job_id": JOB}))

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(tmp_path / "unused.py")
    )

    assert code == 2, result
    assert "error" in result
    assert "decision report" in result["error"].lower()


def test_missing_job_directory_is_exit_code_2_with_clear_error(tmp_path: Path) -> None:
    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(tmp_path / "unused.py")
    )

    assert code == 2, result
    assert "error" in result
    assert "no synthesis directory" in result["error"].lower()


def test_pdf_missing_is_a_note_not_an_error(tmp_path: Path) -> None:
    """No PDF deliverable at all -- common for the 'analysis-report may fail
    independently' optional-deliverable path -- grades fine, just notes the
    skip in the prompt (verified indirectly: the pipeline still succeeds)."""
    _build_job_dir(tmp_path)
    argv_file = tmp_path / "argv.json"
    stub = _write_stub(tmp_path, _outer(_inner(PASSING_SCORES)), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )

    assert code == 0, result
    recorded_argv = json.loads(argv_file.read_text())
    prompt_arg = recorded_argv[recorded_argv.index("-p") + 1]
    assert "PDF deliverable not found; skipped" in prompt_arg


def test_pdf_present_but_pypdf_unavailable_is_a_note_not_an_error(
    tmp_path: Path, monkeypatch
) -> None:
    synthesis_dir = _build_job_dir(tmp_path)
    (synthesis_dir / "summary-executive-report.pdf").write_bytes(b"%PDF-1.4 not a real pdf")

    # Simulate the 'e2e' extra not being installed: `import pypdf` raises ImportError
    # regardless of whether it actually happens to be on this machine's path.
    monkeypatch.setitem(sys.modules, "pypdf", None)

    argv_file = tmp_path / "argv.json"
    stub = _write_stub(tmp_path, _outer(_inner(PASSING_SCORES)), argv_file)

    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )

    assert code == 0, result
    recorded_argv = json.loads(argv_file.read_text())
    prompt_arg = recorded_argv[recorded_argv.index("-p") + 1]
    assert "pypdf not installed" in prompt_arg


def test_html_to_text_strips_tags_and_script_style() -> None:
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><h1>Title</h1><p>Hello <b>world</b></p>"
        "<script>alert('x')</script></body></html>"
    )
    text = judge.html_to_text(html)
    assert "Title" in text
    assert "Hello" in text
    assert "world" in text
    assert "color:red" not in text
    assert "alert" not in text


def test_truncate_adds_marker_only_when_over_budget() -> None:
    short = "x" * 10
    assert judge.truncate(short, 100) == short
    long_text = "y" * 200
    truncated = judge.truncate(long_text, 50)
    assert truncated.startswith("y" * 50)
    assert "truncated to 50 characters" in truncated
