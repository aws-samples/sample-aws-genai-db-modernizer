#!/usr/bin/env python3
"""Parse a headless `/modernize` stream-json transcript and merge CI results.

Usage:
    uv run python ci/llm/run.py check-transcript TRANSCRIPT --mode chat|ui|both
    uv run python ci/llm/run.py results --out results.json \
        --transcript-summary summary.json --pytest-junit J1 [J2] \
        --judge judge.json --mode chat|ui|both --fixture wordpress|discourse
    uv run python ci/llm/run.py collect-logs --artifact-root artifacts --out DIR \
        [--db DB --job JOB] [--extra-log .local-ui/api.log ...]

Transcript shapes
------------------
``claude -p ... --output-format stream-json --verbose`` writes one JSON
object per line. These field names are now **confirmed against a real
transcript**: the first internal-pipeline run (chat/wordpress, 2026-10-03,
493 lines, 11 ``result`` messages -- one per background subagent plus the
orchestrator -- 14 permission denials, ``total_cost_usd`` cumulative at
4.06). A trimmed copy lives at
``tests/unit/ci/fixtures/real-chat-wordpress-failed.jsonl`` (message
structure and all 11 ``result`` lines kept verbatim, most intermediate
tool_use/tool_result bodies dropped). Confirmed shapes/behaviors that differ
from the original (public-docs-only) assumptions baked into this module:

* **Every background subagent (``Task``/``Agent`` tool call) emits its own
  ``type == "result"`` line**, not just the top-level run. A real run's
  ``num_turns``, ``duration_ms`` and ``permission_denials`` must be
  aggregated across *all* of them (summed, summed, and concatenated,
  respectively) -- taking only the last line (the original approach) silently
  dropped most of a run's turns, time and denials. ``total_cost_usd`` is
  cumulative on every line, so the aggregate is its ``max()``, not a sum.
* ``permission_denials`` entries are dicts shaped
  ``{"tool_name": "Bash", "tool_use_id": ..., "tool_input": {"command": ...}}``
  -- the command lives under ``tool_input``, not ``input``. (Both keys are
  checked, in case an older/different CLI build uses ``input``.)
* A denial also shows up as a ``tool_result`` block in the *next* ``user``
  message, with ``is_error: true`` and ``content`` starting
  ``"Permission to use Bash has been denied because Claude Code is running
  in don't ask mode. ..."`` -- but that text never repeats the denied
  command, so it is only used as a fallback denial source when a CLI build
  doesn't populate ``permission_denials`` on the result line at all.
* ``usage`` has several numeric sub-fields beyond ``input_tokens``/
  ``output_tokens`` (``cache_creation_input_tokens``, ``cache_read_input_tokens``,
  nested dicts like ``cache_creation``/``server_tool_use``/
  ``output_tokens_details``) -- the aggregate sums every numeric leaf,
  recursively, across all ``result`` lines.
* The confirmed real run's ``result`` lines have **no ``model`` field at
  all** (it lives on the ``system``/``init`` line instead); every field is
  still read with ``.get()`` and a safe default.
* The ``MODERNIZE_RESULT: complete|failed ...`` sentinel can appear in more
  than one place -- an assistant ``text`` block and the ``result`` line
  that follows it typically repeat the same sentence. The *last* occurrence
  anywhere in the transcript (assistant text or result text, in document
  order) wins, which in practice means the final subagent-notification
  ``result`` line decides the outcome.

Every field is still read with ``.get()`` and a safe default, and the
parsing below tolerates ``content`` being either a bare string or a list of
``{"type": "text", "text": ...}`` blocks.

check-transcript's fixed failure conditions (anything else is tolerated as
"unknown", not a failure):

* no line with ``type == "result"`` at all -- fail (no final result message).
* any ``result`` line's ``is_error`` is truthy -- fail.
* a denial (from any ``result`` line's aggregated ``permission_denials``,
  or -- if none of those were populated -- a ``tool_result`` denial text)
  whose command starts with one of the pipeline's own allowlisted ``Bash``
  prefixes (read from ``.claude/settings.ci.json``'s ``permissions.allow``
  rules) -- fail, because that means the allowlist meant to let the pipeline
  run its own documented commands is broken. Any *other* denial (the
  model's own exploratory ``cat``/``jq``/``sed``/``python3 -c`` etc.) is
  **not** a failure: it is recorded in ``results.json`` as
  ``denials: {count, commands}`` (each command's first 120 characters, at
  most 20 commands) and a warning is printed to stderr.
* the ``MODERNIZE_RESULT`` line's ``job_id``/``db`` aren't a single
  ``[A-Za-z0-9_.-]+`` path component -- fail (they become paths and argv
  downstream).
* no ``MODERNIZE_RESULT: complete ...`` line anywhere (see above for "last
  one wins") -- fail (whether because there is no sentinel at all, or
  because the last one found is a ``MODERNIZE_RESULT: failed ...`` line --
  in which case its ``phase``/``reason`` are reported in the error message
  and, downstream, in ``results.json``'s ``transcript_error``).
* mode mismatch: for ``--mode chat``, a ``Bash`` tool_use ran
  ``scripts/start_local_ui.py`` (to start, not ``--stop``) anywhere in the
  transcript -- fail. For ``--mode ui`` or ``--mode both``, no such tool_use
  ran, or no later tool_result reported ``"status": "ready"`` -- fail.

On failure, check-transcript still prints the aggregated cost/turn/usage
fields next to ``error``, and ``results`` copies them (plus
``transcript_error``) into results.json, so a failed run still reports what
it spent. An unparseable junit file marks the buckets it covers False.

``duration_s`` in ``results.json`` is the aggregated ``duration_ms`` (summed
across all ``result`` lines) converted to seconds -- time the model API
reported spending, not wall-clock. The ``results`` command also accepts an
optional ``--claude-exit`` pointing at ``claude-exit.txt`` (written by
``ci/e2e-llm.sh`` as ``"claude exit=<rc> duration=<N>s"``); when given, its
``duration=<N>s`` is parsed into a separate ``wall_duration_s`` field, which
includes CLI startup/queueing/non-API time that ``duration_s`` does not.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 -- used only to read `git rev-parse`, no untrusted input
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[2]

_COMPLETE_RE = re.compile(r"MODERNIZE_RESULT:\s*complete\s+job_id=(\S+)\s+db=(\S+)\s+mode=(\S+)")
_FAILED_RE = re.compile(r"MODERNIZE_RESULT:\s*failed\s+phase=(\S+)\s+reason=(.+)")
_STATUS_READY_RE = re.compile(r'"status"\s*:\s*"ready"')
# job_id/db from MODERNIZE_RESULT become path components and shell arguments
# downstream (pytest env, judge argv, artifact paths) -- same rule as
# scripts/_sandbox.py.
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")

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
    """check-transcript's single failure type; always surfaces as exit 1.

    ``partial`` carries whatever usage/cost fields were readable off the
    result line, so a failed run still reports what it spent."""

    def __init__(self, message: str, partial: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.partial: dict[str, Any] = partial or {}


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


def tool_result_permission_errors(records: list[dict[str, Any]]) -> list[str]:
    """Texts of ``tool_result`` blocks flagged ``is_error`` that mention
    "permission" -- a denied tool call as the model saw it, which may not
    (depending on CLI version) also land in the result line's
    ``permission_denials``."""
    found: list[str] = []
    for rec in records:
        if rec.get("type") != "user" or not isinstance(rec.get("message"), dict):
            continue
        content = rec["message"].get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("is_error")
            ):
                text = _text_from_content(block.get("content"))
                if "permission" in text.lower():
                    found.append(text[:300])
    return found


def _is_safe_name(value: str) -> bool:
    return bool(_SAFE_NAME_RE.fullmatch(value)) and set(value) != {"."}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _merge_usage(usages: list[Any]) -> dict[str, Any]:
    """Merge ``usage`` dicts from every ``result`` line: numeric leaves
    (including inside nested dicts like ``cache_creation``) are summed, and
    anything else keeps the last value seen."""
    merged: dict[str, Any] = {}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if _is_number(value):
                prior = merged.get(key, 0)
                merged[key] = (prior if _is_number(prior) else 0) + value
            elif isinstance(value, dict):
                prior = merged.get(key)
                merged[key] = _merge_usage([prior if isinstance(prior, dict) else {}, value])
            else:
                merged[key] = value
    return merged


def _aggregate_result_fields(result_recs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the fields of interest across *every* ``result`` line, not
    just the last: a real run emits one per background subagent plus the
    orchestrator (see module docstring). ``total_cost_usd`` is cumulative on
    every line, so its aggregate is a max; everything else sums (or, for
    ``usage``, sums every numeric leaf)."""
    costs: list[float] = [
        r["total_cost_usd"] for r in result_recs if _is_number(r.get("total_cost_usd"))
    ]
    turns: list[float] = [r["num_turns"] for r in result_recs if _is_number(r.get("num_turns"))]
    durations: list[float] = [
        r["duration_ms"] for r in result_recs if _is_number(r.get("duration_ms"))
    ]
    model = next((r.get("model") for r in result_recs if isinstance(r.get("model"), str)), None)
    return {
        "cost_usd": max(costs) if costs else None,
        "num_turns": sum(turns) if turns else None,
        "usage": _merge_usage([r.get("usage") for r in result_recs]),
        "duration_ms": sum(durations) if durations else None,
        "model": model,
    }


def _denial_command(denial: dict[str, Any]) -> str:
    """Extract the Bash command text from a ``permission_denials`` entry.
    The confirmed real shape nests it under ``tool_input``; ``input`` is
    also checked in case a different CLI build uses that key instead."""
    tool_input = denial.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = denial.get("input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str):
            return command
    return ""


def _allowlisted_bash_prefixes(settings_path: Path) -> list[str]:
    """Read ``Bash(<prefix> *)``/``Bash(<prefix>)`` rules out of a Claude
    Code settings file's ``permissions.allow`` list, returning the bare
    command prefixes. Used to tell a denial of one of the pipeline's own
    documented commands (allowlist is broken -- a real failure) apart from
    an exploratory one (the model's own ``cat``/``jq``/``sed``, which is
    not)."""
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    allow = settings.get("permissions", {}).get("allow", [])
    prefixes: list[str] = []
    if not isinstance(allow, list):
        return prefixes
    for rule in allow:
        if not isinstance(rule, str) or not rule.startswith("Bash(") or not rule.endswith(")"):
            continue
        inner = rule[len("Bash(") : -1]
        if inner.endswith(" *"):
            inner = inner[: -len(" *")]
        if inner:
            prefixes.append(inner)
    return prefixes


def _truncate(text: str, limit: int = 120) -> str:
    return text[:limit] + "…" if len(text) > limit else text


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


def _iter_modernize_result_matches(
    records: list[dict[str, Any]],
) -> list[tuple[str, re.Match[str]]]:
    """Every ``MODERNIZE_RESULT: complete|failed ...`` match found, in
    document order, across both assistant ``text`` blocks and ``result``
    lines' ``result`` text. The real run repeats the same sentinel in an
    assistant text block and the ``result`` line that immediately follows
    it; the caller wants the *last* one found overall ("last one wins").

    Only top-level messages (``parent_tool_use_id is None`` -- the real
    fixture confirms this is ``null`` for the orchestrator and set for
    anything running inside a dispatched subagent) are considered: a
    subagent quoting or illustrating a ``MODERNIZE_RESULT`` line (e.g. when
    reporting what the orchestrator previously said) must never be mistaken
    for the authoritative one."""
    matches: list[tuple[str, re.Match[str]]] = []
    for rec in records:
        if rec.get("parent_tool_use_id") is not None:
            continue
        rtype = rec.get("type")
        texts: list[str] = []
        if rtype == "assistant" and isinstance(rec.get("message"), dict):
            content = rec["message"].get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ):
                        texts.append(block["text"])
        elif rtype == "result":
            result_text = rec.get("result")
            if isinstance(result_text, str):
                texts.append(result_text)
        for text in texts:
            complete = _COMPLETE_RE.search(text)
            failed = _FAILED_RE.search(text)
            if complete and failed:
                # Both sentinels in one block (shouldn't happen in practice):
                # whichever appears later in the text wins.
                matches.append(
                    ("failed", failed)
                    if failed.start() > complete.start()
                    else ("complete", complete)
                )
            elif complete:
                matches.append(("complete", complete))
            elif failed:
                matches.append(("failed", failed))
    return matches


def check_transcript(
    path: Path,
    mode: str,
    state_file: Path | None = None,
    settings_file: Path | None = None,
) -> dict[str, Any]:
    records = iter_jsonl(path)

    result_recs = [rec for rec in records if rec.get("type") == "result"]
    if not result_recs:
        raise TranscriptError('no line with type == "result" found in the transcript')

    fields = _aggregate_result_fields(result_recs)

    def fail(message: str) -> TranscriptError:
        return TranscriptError(message, partial=fields)

    error_recs = [r for r in result_recs if r.get("is_error")]
    if error_recs:
        raise fail(f"result reported is_error=true (result={error_recs[-1].get('result')!r})")

    # Denials: the authoritative source is each result line's own
    # permission_denials list (it carries the actual command). Only fall
    # back to tool_result-text denials (no command text available) when no
    # result line populated permission_denials at all -- see module
    # docstring.
    all_denials: list[dict[str, Any]] = []
    for rec in result_recs:
        denials = rec.get("permission_denials")
        if isinstance(denials, list):
            all_denials.extend(d for d in denials if isinstance(d, dict))

    denial_commands: list[str] = [_denial_command(d) for d in all_denials]
    if not all_denials:
        denial_commands = list(tool_result_permission_errors(records))

    allow_prefixes = _allowlisted_bash_prefixes(
        settings_file or REPO_ROOT / ".claude" / "settings.ci.json"
    )
    pipeline_denials = [
        cmd for cmd in denial_commands if cmd and any(cmd.startswith(p) for p in allow_prefixes)
    ]
    other_denials = [cmd for cmd in denial_commands if cmd not in pipeline_denials]

    denials_summary: dict[str, Any] | None = None
    if other_denials:
        denials_summary = {
            "count": len(other_denials),
            "commands": [_truncate(cmd) for cmd in other_denials[:20]],
        }
        print(
            f"warning: {len(other_denials)} permission denial(s) that are not pipeline "
            f"commands (the model's own exploratory tool use): {denials_summary['commands']!r}",
            file=sys.stderr,
        )
        fields = {**fields, "denials": denials_summary}

    if pipeline_denials:
        raise fail(
            f"{len(pipeline_denials)} denial(s) of the pipeline's own allowlisted command(s) -- "
            f"the allowlist in .claude/settings.ci.json is broken: "
            f"{[_truncate(cmd) for cmd in pipeline_denials[:5]]!r}"
        )

    matches = _iter_modernize_result_matches(records)
    if not matches:
        raise fail("no 'MODERNIZE_RESULT: complete ...' line found anywhere in the transcript")

    kind, match = matches[-1]  # last one wins
    if kind == "failed":
        phase, reason = match.groups()
        raise fail(f"MODERNIZE_RESULT: failed phase={phase} reason={reason.strip()}")

    job_id, db, result_mode = match.groups()
    for label, value in (("job_id", job_id), ("db", db)):
        if not _is_safe_name(value):
            raise fail(
                f"MODERNIZE_RESULT reported an unsafe {label}={value!r} "
                "(must match [A-Za-z0-9_.-]+ and not be '.' or '..')"
            )
    if result_mode != mode:
        raise fail(
            f"MODERNIZE_RESULT reported mode={result_mode!r}, which does not match --mode {mode!r}"
        )

    mode_ok, mode_reason = check_mode(records, mode)
    if not mode_ok:
        raise fail(mode_reason or "mode assertion failed")

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

    return {"job_id": job_id, "db": db, "mode": mode, **fields}


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
    tree = ET.parse(path)
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


_WALL_DURATION_RE = re.compile(r"duration=(\d+)s")


def _wall_duration_s(claude_exit_path: Path | None) -> float | None:
    """Parse the wall-clock ``duration=<N>s`` out of ``claude-exit.txt``
    (``ci/e2e-llm.sh`` writes ``"claude exit=<rc> duration=<N>s"``). This is
    real elapsed time for the whole claude process -- CLI startup, queueing,
    etc. included -- unlike ``duration_s``, which is the sum of the
    transcript's own ``duration_ms`` (model API time only). See module
    docstring."""
    if claude_exit_path is None or not claude_exit_path.is_file():
        return None
    match = _WALL_DURATION_RE.search(claude_exit_path.read_text())
    return float(match.group(1)) if match else None


def build_results_row(
    *,
    mode: str,
    fixture: str,
    transcript_summary_path: Path,
    pytest_junit_paths: list[Path],
    judge_path: Path,
    git_sha: str | None = None,
    claude_exit_path: Path | None = None,
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
    transcript_error: str | None = None
    denials: dict[str, Any] | None = None

    summary = _load_json(transcript_summary_path)
    if summary is None:
        missing_required = True
    else:
        if "error" in summary:
            checks["transcript"] = False
            transcript_error = str(summary["error"])
        else:
            checks["transcript"] = True
            job_id = summary.get("job_id")
            db = summary.get("db")
        # Spend is reported whether or not the transcript passed: a failed
        # run still cost money and turns.
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
        denials_raw = summary.get("denials")
        denials = denials_raw if isinstance(denials_raw, dict) else None

    report_junit = pytest_junit_paths[0] if pytest_junit_paths else None
    ui_junit = pytest_junit_paths[1] if len(pytest_junit_paths) > 1 else None

    def apply_junit(path: Path | None, categories: tuple[str, ...]) -> None:
        """Set ``checks`` for ``categories`` from one junit file: missing file
        or an empty bucket is null (and fails the run), an unparseable file
        (e.g. pytest killed mid-write) is False for every bucket it covers."""
        nonlocal missing_required
        if path is None or not path.is_file():
            missing_required = True
            return
        try:
            buckets = parse_junit_counts(path)
        except ET.ParseError:
            for category in categories:
                checks[category] = False
            return
        for category in categories:
            if buckets[category]["total"] == 0:
                checks[category] = None
                missing_required = True
            else:
                checks[category] = buckets[category]["failed"] == 0

    apply_junit(report_junit, ("contracts", "html", "pdf"))
    if mode == "chat":
        checks["ui"] = None  # expected: chat mode never runs the UI suite
    else:
        apply_junit(ui_junit, ("ui",))

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
        "wall_duration_s": _wall_duration_s(claude_exit_path),
        "checks": checks,
        "transcript_error": transcript_error,
        "denials": denials,
        "judge": judge_section,
        "pass": overall_pass,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_check_transcript(args: argparse.Namespace) -> None:
    state_file = Path(args.state_file) if args.state_file else REPO_ROOT / ".modernizer-state.json"
    settings_file = Path(args.settings_file) if args.settings_file else None
    try:
        summary = check_transcript(
            Path(args.transcript), args.mode, state_file=state_file, settings_file=settings_file
        )
    except TranscriptError as exc:
        print(json.dumps({"error": str(exc), **exc.partial}))
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
        claude_exit_path=Path(args.claude_exit) if args.claude_exit else None,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(row, indent=2))
    print(json.dumps(row, indent=2))
    sys.exit(0 if row["pass"] else 1)


# The CI job uploads only test-results/, so the run's own diagnostic logs (the
# compact-mode run_assessment.py log under artifacts/<db>/<job>/_logs/, #278,
# and the local UI's server logs) are copied next to the transcript. Each file
# keeps at most its last LOG_COPY_MAX_BYTES so the upload stays bounded.
LOG_COPY_MAX_BYTES = 1_000_000


def _copy_tail(src: Path, dest: Path, max_bytes: int) -> dict:
    size = src.stat().st_size
    with src.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if size > max_bytes:
        note = f"[truncated: last {max_bytes} of {size} bytes of {src}]\n".encode()
        data = note + data
    dest.write_bytes(data)
    return {"src": str(src), "dest": str(dest), "bytes": size, "truncated": size > max_bytes}


def collect_logs(
    artifact_root: Path,
    out_dir: Path,
    db: str = "",
    job: str = "",
    state_file: Path | None = None,
    extra_logs: list[Path] | None = None,
    max_bytes: int = LOG_COPY_MAX_BYTES,
) -> list[dict]:
    """Copy the job's ``_logs/*`` and ``extra_logs`` into ``out_dir/job-logs/``.

    ``db``/``job`` default to ``.modernizer-state.json`` (a run that failed
    before check-transcript found them). Missing files are skipped.
    """
    if (not db or not job) and state_file is not None and state_file.is_file():
        try:
            state = json.loads(state_file.read_text())
            db = db or str(state.get("database_name") or "")
            job = job or str(state.get("job_id") or "")
        except (OSError, ValueError):
            pass
    sources: list[tuple[Path, str]] = []
    if db and job:
        log_dir = artifact_root / db / job / "_logs"
        if log_dir.is_dir():
            sources += [(p, p.name) for p in sorted(log_dir.iterdir()) if p.is_file()]
    for extra in extra_logs or []:
        if extra.is_file():
            sources.append((extra, f"{extra.parent.name.lstrip('.')}-{extra.name}"))
    return [_copy_tail(src, out_dir / "job-logs" / name, max_bytes) for src, name in sources]


def cmd_collect_logs(args: argparse.Namespace) -> None:
    copied = collect_logs(
        Path(args.artifact_root),
        Path(args.out),
        db=args.db or "",
        job=args.job or "",
        state_file=(
            Path(args.state_file) if args.state_file else REPO_ROOT / ".modernizer-state.json"
        ),
        extra_logs=[Path(p) for p in args.extra_log],
    )
    print(json.dumps({"copied": copied}))


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
    p1.add_argument(
        "--settings-file",
        default=None,
        help=(
            "Path to the Claude Code settings file whose permissions.allow rules name the "
            "pipeline's own Bash command prefixes (default: .claude/settings.ci.json)"
        ),
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
    p2.add_argument(
        "--claude-exit",
        default=None,
        help="Path to claude-exit.txt for a wall-clock wall_duration_s (optional)",
    )
    p2.set_defaults(func=cmd_results)

    p3 = sub.add_parser(
        "collect-logs", help="Copy the job's _logs/ and other small logs into the output dir."
    )
    p3.add_argument("--artifact-root", required=True)
    p3.add_argument("--out", required=True)
    p3.add_argument("--db", default="")
    p3.add_argument("--job", default="")
    p3.add_argument("--state-file", default=None)
    p3.add_argument("--extra-log", action="append", default=[])
    p3.set_defaults(func=cmd_collect_logs)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
