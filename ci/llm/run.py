#!/usr/bin/env python3
"""Parse a headless `/modernize` stream-json transcript and merge CI results.

Usage:
    uv run python ci/llm/run.py check-transcript TRANSCRIPT --mode chat|ui|both
    uv run python ci/llm/run.py results --out results.json \
        --transcript-summary summary.json --pytest-junit J1 [J2] \
        --judge judge.json --mode chat|ui|both --fixture wordpress|discourse

Transcript shapes
------------------
``claude -p ... --output-format stream-json --verbose`` writes one JSON
object per line. The field names relied on here (``type``, ``subtype``,
``is_error``, ``num_turns``, ``total_cost_usd``, ``usage``,
``permission_denials``, ``result``, ``duration_ms``, ``model`` on the final
``type == "result"`` line; ``message.content`` blocks of
``{"type": "tool_use", "name": ..., "input": {"command": ...}}`` on
``type == "assistant"`` lines; and ``{"type": "tool_result", "content": ...}``
blocks on ``type == "user"`` lines) come from the public Claude Code docs,
**not** from a recorded transcript against the real CLI (that spike --
Step 1 of this task -- was explicitly skipped to avoid spending tokens).
Every field is read with ``.get()`` and a safe default, and the parsing
below tolerates ``content`` being either a bare string or a list of
``{"type": "text", "text": ...}`` blocks. These shapes should be confirmed
(and this docstring updated) against a real transcript recorded on the
first internal-pipeline run.

check-transcript's fixed failure conditions (anything else is tolerated as
"unknown", not a failure):

* no line with ``type == "result"`` at all -- fail (no final result message).
* that line's ``is_error`` is truthy -- fail.
* that line's ``permission_denials`` is a non-empty list -- fail.
* its ``result`` text contains no ``MODERNIZE_RESULT: complete ...`` line
  (whether because it has none at all, or because it has a
  ``MODERNIZE_RESULT: failed ...`` line instead) -- fail.
* mode mismatch: for ``--mode chat``, a ``Bash`` tool_use ran
  ``scripts/start_local_ui.py`` (to start, not ``--stop``) anywhere in the
  transcript -- fail. For ``--mode ui`` or ``--mode both``, no such tool_use
  ran, or no later tool_result reported ``"status": "ready"`` -- fail.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 -- used only to read `git rev-parse`, no untrusted input
import sys
import xml.etree.ElementTree as ET  # nosec B405 -- CI-produced junit XML, not user input
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

_COMPLETE_RE = re.compile(r"MODERNIZE_RESULT:\s*complete\s+job_id=(\S+)\s+db=(\S+)\s+mode=(\S+)")
_FAILED_RE = re.compile(r"MODERNIZE_RESULT:\s*failed\s+phase=(\S+)\s+reason=(.+)")
_STATUS_READY_RE = re.compile(r'"status"\s*:\s*"ready"')

# Bucketed by substring match against a junit testcase's "classname.name";
# first match wins. Anything left over (notably tests/e2e/test_pipeline.py's
# non-deterministic assertions) falls into "contracts". Mirrors the four
# suites described in ci/README.md / ci/e2e.sh.
_JUNIT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("test_report_html", "html"),
    ("test_report_pdf", "pdf"),
    ("test_ui", "ui"),
)


class TranscriptError(Exception):
    """check-transcript's single failure type; always surfaces as exit 1."""


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse every non-blank line of ``path`` as JSON, skipping lines that
    don't parse (tolerant: a CLI's stream-json output is not guaranteed free
    of stray non-JSON lines, and a truncated/killed run may end mid-line)."""
    records: list[dict[str, Any]] = []
    text = path.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _text_from_content(content: Any) -> str:
    """Flatten a tool_use/tool_result ``content`` field: tolerate a bare
    string or a list of ``{"type": "text", "text": ...}`` blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def iter_tool_events(records: list[dict[str, Any]]) -> list[tuple[str, str | None, str]]:
    """Return ``(kind, tool_name, text)`` tuples in transcript order.

    ``kind`` is ``"tool_use"`` (``text`` is the Bash ``input.command``, or
    ``""`` for non-Bash/argument-less tool uses) or ``"tool_result"``
    (``text`` is the flattened result content)."""
    events: list[tuple[str, str | None, str]] = []
    for rec in records:
        rtype = rec.get("type")
        content = (
            rec.get("message", {}).get("content") if isinstance(rec.get("message"), dict) else None
        )
        if not isinstance(content, list):
            continue
        if rtype == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    input_ = block.get("input")
                    command = ""
                    if isinstance(input_, dict) and isinstance(input_.get("command"), str):
                        command = input_["command"]
                    events.append(("tool_use", name, command))
        elif rtype == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    events.append(("tool_result", None, _text_from_content(block.get("content"))))
    return events


def _contains_status_ready(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return bool(_STATUS_READY_RE.search(text))
    return isinstance(parsed, dict) and parsed.get("status") == "ready"


def check_mode(records: list[dict[str, Any]], mode: str) -> tuple[bool, str | None]:
    """Mode assertion: for ``chat``, no tool_use started the local UI; for
    ``ui``/``both``, one did and a later tool_result reported it ready."""
    events = iter_tool_events(records)
    start_idx: int | None = None
    for i, (kind, _name, text) in enumerate(events):
        if kind == "tool_use" and "scripts/start_local_ui.py" in text and "--stop" not in text:
            start_idx = i
            break
    started_ui = start_idx is not None

    if mode == "chat":
        if started_ui:
            return (
                False,
                "chat mode ran scripts/start_local_ui.py, which only ui/both modes should start",
            )
        return True, None

    if not started_ui:
        return False, f"{mode} mode never ran scripts/start_local_ui.py to start the local API/UI"
    ready = any(
        kind == "tool_result" and _contains_status_ready(text) for kind, _name, text in events[start_idx + 1 :]  # type: ignore[operator]
    )
    if not ready:
        return False, (
            f"{mode} mode ran scripts/start_local_ui.py but no later tool_result reported "
            '{"status": "ready"}'
        )
    return True, None


def _git_sha() -> str | None:
    try:
        proc = subprocess.run(  # nosec B603 B607 -- fixed argv, no untrusted input
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# ---------------------------------------------------------------------------
# check-transcript
# ---------------------------------------------------------------------------


def check_transcript(path: Path, mode: str, state_file: Path | None = None) -> dict[str, Any]:
    records = iter_jsonl(path)

    result_rec: dict[str, Any] | None = None
    for rec in records:
        if rec.get("type") == "result":
            result_rec = rec  # last one wins

    if result_rec is None:
        raise TranscriptError('no line with type == "result" found in the transcript')

    if result_rec.get("is_error"):
        raise TranscriptError(
            f"result reported is_error=true (result={result_rec.get('result')!r})"
        )

    denials = result_rec.get("permission_denials") or []
    if isinstance(denials, list) and denials:
        raise TranscriptError(f"result reported {len(denials)} permission denial(s): {denials!r}")

    result_text = result_rec.get("result")
    if not isinstance(result_text, str):
        raise TranscriptError("result message has no string 'result' field")

    failed_match = _FAILED_RE.search(result_text)
    if failed_match:
        phase, reason = failed_match.groups()
        raise TranscriptError(f"MODERNIZE_RESULT: failed phase={phase} reason={reason.strip()}")

    complete_match = _COMPLETE_RE.search(result_text)
    if not complete_match:
        raise TranscriptError("no 'MODERNIZE_RESULT: complete ...' line found in the result text")

    job_id, db, result_mode = complete_match.groups()
    if result_mode != mode:
        raise TranscriptError(
            f"MODERNIZE_RESULT reported mode={result_mode!r}, which does not match --mode {mode!r}"
        )

    mode_ok, mode_reason = check_mode(records, mode)
    if not mode_ok:
        raise TranscriptError(mode_reason or "mode assertion failed")

    if state_file is not None and state_file.is_file():
        try:
            state = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        state_job = state.get("job_id")
        state_db = state.get("database_name")
        if state_job and state_job != job_id:
            print(
                f"warning: .modernizer-state.json job_id={state_job!r} != transcript job_id={job_id!r}",
                file=sys.stderr,
            )
        if state_db and state_db != db:
            print(
                f"warning: .modernizer-state.json database_name={state_db!r} != transcript db={db!r}",
                file=sys.stderr,
            )

    usage = result_rec.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    return {
        "job_id": job_id,
        "db": db,
        "mode": mode,
        "cost_usd": result_rec.get("total_cost_usd"),
        "num_turns": result_rec.get("num_turns"),
        "usage": usage,
        "duration_ms": result_rec.get("duration_ms"),
        "model": result_rec.get("model"),
    }


# ---------------------------------------------------------------------------
# results merging
# ---------------------------------------------------------------------------


def parse_junit_counts(path: Path) -> dict[str, dict[str, int]]:
    """Bucket a junit XML's testcases into contracts/html/pdf/ui, counting
    total and failed (failure or error child element) per bucket."""
    buckets: dict[str, dict[str, int]] = {
        "contracts": {"total": 0, "failed": 0},
        "html": {"total": 0, "failed": 0},
        "pdf": {"total": 0, "failed": 0},
        "ui": {"total": 0, "failed": 0},
    }
    tree = ET.parse(path)  # nosec B314 -- CI-produced junit XML, not user input
    for testcase in tree.getroot().iter("testcase"):
        classname = f"{testcase.get('classname', '')}.{testcase.get('name', '')}"
        category = "contracts"
        for needle, name in _JUNIT_CATEGORIES:
            if needle in classname:
                category = name
                break
        failed = testcase.find("failure") is not None or testcase.find("error") is not None
        buckets[category]["total"] += 1
        if failed:
            buckets[category]["failed"] += 1
    return buckets


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"error": f"{path} is not valid JSON"}
    return (
        parsed if isinstance(parsed, dict) else {"error": f"{path} did not contain a JSON object"}
    )


def build_results_row(
    *,
    mode: str,
    fixture: str,
    transcript_summary_path: Path,
    pytest_junit_paths: list[Path],
    judge_path: Path,
    git_sha: str | None = None,
) -> dict[str, Any]:
    checks: dict[str, bool | None] = {
        "transcript": None,
        "contracts": None,
        "html": None,
        "pdf": None,
        "ui": None,
        "judge": None,
    }
    missing_required = False

    job_id = db = model = cost_usd = num_turns = tokens_in = tokens_out = duration_s = None

    summary = _load_json(transcript_summary_path)
    if summary is None:
        missing_required = True
    elif "error" in summary:
        checks["transcript"] = False
    else:
        checks["transcript"] = True
        job_id = summary.get("job_id")
        db = summary.get("db")
        model = summary.get("model")
        cost_usd = summary.get("cost_usd")
        num_turns = summary.get("num_turns")
        usage_raw = summary.get("usage")
        usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        duration_ms = summary.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            duration_s = duration_ms / 1000

    report_junit = pytest_junit_paths[0] if pytest_junit_paths else None
    ui_junit = pytest_junit_paths[1] if len(pytest_junit_paths) > 1 else None

    if report_junit is None or not report_junit.is_file():
        missing_required = True
    else:
        buckets = parse_junit_counts(report_junit)
        for category in ("contracts", "html", "pdf"):
            total = buckets[category]["total"]
            if total == 0:
                checks[category] = None
                missing_required = True
            else:
                checks[category] = buckets[category]["failed"] == 0

    if mode == "chat":
        checks["ui"] = None  # expected: chat mode never runs the UI suite
    elif ui_junit is None or not ui_junit.is_file():
        missing_required = True
    else:
        buckets = parse_junit_counts(ui_junit)
        total = buckets["ui"]["total"]
        if total == 0:
            checks["ui"] = None
            missing_required = True
        else:
            checks["ui"] = buckets["ui"]["failed"] == 0

    judge_section: dict[str, Any] | None = None
    judge_payload = _load_json(judge_path)
    if judge_payload is None:
        missing_required = True
    elif "error" in judge_payload:
        checks["judge"] = False
    else:
        checks["judge"] = bool(judge_payload.get("pass"))
        judge_section = {"mean": judge_payload.get("mean"), "scores": judge_payload.get("scores")}

    required = ["transcript", "contracts", "html", "pdf", "judge"]
    if mode != "chat":
        required.append("ui")
    overall_pass = (not missing_required) and all(checks.get(k) is True for k in required)

    return {
        "schema_version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "git_sha": git_sha if git_sha is not None else _git_sha(),
        "mode": mode,
        "fixture": fixture,
        "model": model,
        "job_id": job_id,
        "db": db,
        "cost_usd": cost_usd,
        "num_turns": num_turns,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "duration_s": duration_s,
        "checks": checks,
        "judge": judge_section,
        "pass": overall_pass,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_check_transcript(args: argparse.Namespace) -> None:
    state_file = Path(args.state_file) if args.state_file else REPO_ROOT / ".modernizer-state.json"
    try:
        summary = check_transcript(Path(args.transcript), args.mode, state_file=state_file)
    except TranscriptError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    print(json.dumps(summary))
    sys.exit(0)


def cmd_results(args: argparse.Namespace) -> None:
    row = build_results_row(
        mode=args.mode,
        fixture=args.fixture,
        transcript_summary_path=Path(args.transcript_summary),
        pytest_junit_paths=[Path(p) for p in args.pytest_junit],
        judge_path=Path(args.judge),
        git_sha=args.git_sha,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(row, indent=2))
    print(json.dumps(row, indent=2))
    sys.exit(0 if row["pass"] else 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("check-transcript", help="Validate one stream-json transcript file.")
    p1.add_argument("transcript")
    p1.add_argument("--mode", required=True, choices=["chat", "ui", "both"])
    p1.add_argument(
        "--state-file",
        default=None,
        help="Path to .modernizer-state.json for a soft cross-check (default: repo root)",
    )
    p1.set_defaults(func=cmd_check_transcript)

    p2 = sub.add_parser(
        "results", help="Merge the transcript summary, junit, and judge files into one row."
    )
    p2.add_argument("--out", required=True)
    p2.add_argument("--transcript-summary", required=True)
    p2.add_argument("--pytest-junit", nargs="+", required=True)
    p2.add_argument("--judge", required=True)
    p2.add_argument("--mode", required=True, choices=["chat", "ui", "both"])
    p2.add_argument("--fixture", required=True, choices=["wordpress", "discourse"])
    p2.add_argument("--git-sha", default=None)
    p2.set_defaults(func=cmd_results)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
