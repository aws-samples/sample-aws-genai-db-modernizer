#!/usr/bin/env python3
"""Sum a headless session's token usage per pipeline phase (#676).

Usage:
    uv run python ci/llm/token_usage.py TRANSCRIPT --artifact-root DIR --db DB --job JOB

Reads a stream-json session transcript (``transcript.jsonl`` of an
``ci/e2e-llm.sh`` run), adds up the ``usage`` of every assistant turn
(input, output, cache-read and cache-creation tokens) and attributes each turn
to the pipeline phase the session was working on, then writes
``<artifact-root>/<db>/<job>/_logs/token_usage.json`` in the same shape as the
Bedrock path's ``token_usage`` (``calls``, ``inputTokens``, ``outputTokens``,
``cacheReadInputTokens``, ``cacheWriteInputTokens``)::

    {"source": "transcript", "output_tokens_note": "...", "total": {...},
     "session_model_usage": {...}, "phases": {"schema_design": {...}, ...}}

Output tokens are NOT available per phase: stream-json ``assistant`` records
are written before a message finishes, so their ``usage.output_tokens`` is the
first-chunk count (about 100x too low) and no accurate per-message output count
exists in the transcript. Per-phase ``outputTokens`` is therefore ``null``, and
``total.outputTokens`` is the session total from the final ``result`` records'
``modelUsage`` (cumulative; the largest seen), falling back to the sum of the
result records' ``usage.output_tokens``. Per-phase input, cache-read, cache-write
and call counts are accurate to about 1%; ``session_model_usage`` carries the
session figures from ``modelUsage`` so the gap can be read off the file.

Attribution is deterministic and uses only what the transcript shows:

* A turn that runs ``scripts/run_<phase>.py`` (Bash tool call) belongs to
  ``<phase>`` and every later turn of the same agent stays on it until it runs
  another phase script.
* A turn that calls the ``Skill`` tool for ``design-schema*``, ``reality-check``
  or ``synthesize`` moves that agent to ``schema_design``, ``reality_check`` or
  ``synthesis``.
* Each subagent (records with ``parent_tool_use_id``) is tracked separately
  from the orchestrator, so interleaved background subagents do not mix.
* Turns before an agent has shown any phase are ``unattributed``.

One assistant message can be streamed as several records that share
``message.id`` and repeat its usage; it is counted once, with the last usage
seen. Stdout is one JSON status line. No model calls.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Same keys as RoleBedrockModel.usage (src/shared/role_bedrock_model.py).
FIELDS = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "cache_creation_input_tokens": "cacheWriteInputTokens",
}
UNATTRIBUTED = "unattributed"
_SCRIPT_RE = re.compile(r"scripts/run_([a-z0-9_]+)\.py")
_SKILL_PHASES = (
    ("design-schema", "schema_design"),
    ("reality-check", "reality_check"),
    ("synthesize", "synthesis"),
)
OUTPUT_NOTE = (
    "per-phase outputTokens is null: transcript assistant records carry only the first-chunk "
    "output count; total.outputTokens is the session total from the result records"
)
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def empty_usage() -> dict[str, int]:
    return {"calls": 0, **dict.fromkeys(FIELDS.values(), 0)}


def add_usage(total: dict[str, int], other: dict[str, Any]) -> None:
    """Add ``other``'s numeric counters into ``total`` (missing keys count as 0)."""
    for key in ("calls", *FIELDS.values()):
        value = other.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + int(value)


def _turn_usage(raw: Any) -> dict[str, int]:
    usage = empty_usage()
    usage["calls"] = 1
    if isinstance(raw, dict):
        for src, dst in FIELDS.items():
            value = raw.get(src)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[dst] = int(value)
    return usage


def _phase_of_tool_use(block: dict[str, Any]) -> str | None:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    if block.get("name") == "Bash":
        match = _SCRIPT_RE.search(str(tool_input.get("command") or ""))
        return match.group(1) if match else None
    if block.get("name") == "Skill":
        skill = str(tool_input.get("skill") or "")
        for prefix, phase in _SKILL_PHASES:
            if skill.startswith(prefix):
                return phase
    return None


def _assistant_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per assistant message (records sharing ``message.id`` merged),
    in order of first appearance: ``{agent, phases, usage}``."""
    turns: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for rec in records:
        message = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(message, dict):
            continue
        content = message.get("content")
        phases = [
            p
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
            for p in [_phase_of_tool_use(block)]
            if p
        ]
        agent = rec.get("parent_tool_use_id")
        msg_id = message.get("id")
        turn = by_id.get(msg_id) if isinstance(msg_id, str) else None
        if turn is None:
            turn = {"agent": agent if isinstance(agent, str) else None, "phases": []}
            turns.append(turn)
            if isinstance(msg_id, str):
                by_id[msg_id] = turn
        turn["phases"].extend(phases)
        turn["usage"] = message.get("usage")
    return turns


def summarize(transcript: Path) -> dict[str, Any]:
    """The ``token_usage.json`` payload for one transcript file."""
    records: list[dict[str, Any]] = []
    for line in transcript.read_text(errors="replace").splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)

    current: dict[str | None, str] = {}
    phases: dict[str, dict[str, int | None]] = {}
    total = empty_usage()
    for turn in _assistant_turns(records):
        if turn["phases"]:
            # A turn that runs a phase script is that phase's; the agent stays
            # on the last phase the turn ran until it runs another.
            phase = turn["phases"][0]
            current[turn["agent"]] = turn["phases"][-1]
        else:
            phase = current.get(turn["agent"], UNATTRIBUTED)
        usage = _turn_usage(turn["usage"])
        usage.pop("outputTokens")  # first-chunk count only: see the module docstring
        add_usage(phases.setdefault(phase, empty_usage()), usage)  # type: ignore[arg-type]
        add_usage(total, usage)
    for counters in phases.values():
        counters["outputTokens"] = None
    session, output_total = _session_usage(records)
    total["outputTokens"] = output_total
    return {
        "source": "transcript",
        "output_tokens_note": OUTPUT_NOTE,
        "total": total,
        "session_model_usage": session,
        "phases": phases,
    }


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _session_usage(records: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    """(``modelUsage`` summed over models, session output tokens).

    ``modelUsage`` is cumulative on every ``result`` record, so the record with
    the largest output total is the session's. Without it, the output total is
    the sum of the result records' own ``usage.output_tokens``."""
    best: dict[str, int] = {}
    fallback = 0
    for rec in records:
        if rec.get("type") != "result":
            continue
        usage = rec.get("usage")
        if isinstance(usage, dict):
            fallback += _number(usage.get("output_tokens"))
        model_usage = rec.get("modelUsage")
        if not isinstance(model_usage, dict):
            continue
        summed = {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }
        for per_model in model_usage.values():
            if isinstance(per_model, dict):
                summed["inputTokens"] += _number(per_model.get("inputTokens"))
                summed["outputTokens"] += _number(per_model.get("outputTokens"))
                summed["cacheReadInputTokens"] += _number(per_model.get("cacheReadInputTokens"))
                summed["cacheWriteInputTokens"] += _number(
                    per_model.get("cacheCreationInputTokens")
                )
        if summed["outputTokens"] >= best.get("outputTokens", -1):
            best = summed
    return best, (best["outputTokens"] if best else fallback)


def write_token_usage(
    transcript: Path, artifact_root: Path, db: str, job: str
) -> tuple[Path, dict[str, Any]]:
    if not (_SAFE_NAME.fullmatch(db) and _SAFE_NAME.fullmatch(job)) or {db, job} & {".", ".."}:
        raise ValueError("db and job must each be a single path component")
    root = artifact_root.resolve()
    log_dir = (artifact_root / db / job / "_logs").resolve()
    if not log_dir.is_relative_to(root):
        raise ValueError("log directory escapes the artifact root")
    payload = summarize(transcript)
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / "token_usage.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    return out, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("transcript")
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        out, payload = write_token_usage(
            Path(args.transcript), Path(args.artifact_root), args.db, args.job
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    print(json.dumps({"status": "ok", "path": str(out), "calls": payload["total"]["calls"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
