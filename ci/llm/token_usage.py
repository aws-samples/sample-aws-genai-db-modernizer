#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sum a headless session's token usage per pipeline phase and per agent (#676, #763).

Usage:
    uv run python ci/llm/token_usage.py TRANSCRIPT --artifact-root DIR --db DB --job JOB \
        [--explore EXPLORE_TRANSCRIPT]

Reads a stream-json session transcript (``transcript.jsonl`` of an
``ci/e2e-llm.sh`` run, plus the ``/explore`` session's when given), adds up the
``usage`` of every assistant turn (input, output, cache-read and cache-creation
tokens) and attributes each turn to the pipeline phase the session was working
on and to the agent that made it, then writes
``<artifact-root>/<db>/<job>/_logs/token_usage.json``. Token counters use the
Bedrock path's ``token_usage`` names (``calls``, ``inputTokens``,
``outputTokens``, ``cacheReadInputTokens``, ``cacheWriteInputTokens``)::

    {"source": "transcript", "output_tokens_note": "...", "total": {...},
     "session_model_usage": {...}, "phases": {"schema_design": {...}, ...},
     "agents": [{"agent": "orchestrator", ...}, ...], "cost": {...},
     "unattributed_calls": 0, "skipped_lines": 0}

Output tokens are NOT available per phase or per agent: stream-json
``assistant`` records are written before a message finishes, so their
``usage.output_tokens`` is the first-chunk count (about 100x too low) and no
accurate per-message output count exists in the transcript (the Agent tool's
own result and ``task_notification`` carry only the last message's usage).
Per-row ``outputTokens`` is therefore ``null``, and ``total.outputTokens`` is
the session total from the final ``result`` records' ``modelUsage``
(cumulative; the largest seen), falling back to the sum of the result
records' ``usage.output_tokens``. Per-row input, cache-read, cache-write and
call counts are accurate to about 1%; ``session_model_usage`` carries the
session figures from ``modelUsage`` so the gap can be read off the file.

Attribution is deterministic and uses only what the transcript shows:

* A turn that runs ``scripts/run_<phase>.py`` (Bash tool call) belongs to
  ``<phase>`` and every later turn of the same agent stays on it until it runs
  another phase script.
* A turn that calls the ``Skill`` tool for ``design-schema*``,
  ``review-schema``, ``reality-check`` or ``synthesize`` moves that agent to
  ``schema_design``, ``pe_review``, ``reality_check`` or ``synthesis``.
* Each subagent (records with ``parent_tool_use_id``) is tracked separately
  from the orchestrator, so interleaved background subagents do not mix. A
  subagent starts on the phase its dispatch names (the Agent tool call's
  prompt: a slash command such as ``/review-schema``, else the first
  ``scripts/run_<phase>.py`` it mentions), else on the first phase it shows
  itself, so the turns in which it reads its command, skills and input count
  for that phase.
* The top-level session's own turns before it shows any phase are
  ``orchestrator``.
* ``unattributed`` is left only for subagent turns with no dispatch record
  that names a phase and no phase of their own; ``unattributed_calls`` counts
  them.
* Every turn of the ``/explore`` session is ``explore``.

``agents`` is one row per agent (the orchestrator, each subagent in the order
it first appears, then the ``/explore`` session): its label (the dispatch's
description), phases, the counters above, ``largest_context`` (the largest
single turn's input + cache read + cache write) and ``input_cost_usd``.

Cost has two figures, kept apart on purpose:

* **List price** (``list_price_cost_usd``, every row's ``input_cost_usd``): the
  repo's one price table, ``src/catalog/model_prices.toml`` (public Bedrock list
  prices, dated there), read with ``scripts/usage_cost.py``'s loader, by Bedrock
  model id including its inference profile (``global.`` or ``us.``). A turn's
  record names the bare model (``claude-sonnet-5-5``); it is priced as the one
  profile id of that model the session's ``modelUsage`` (or ``init`` record)
  names. A 5-minute cache write is the table's ``cache_write``; a 1-hour write
  (the table has no row for it) is priced at 2x ``input``, the Anthropic rate
  for that TTL. Each row's input side (input, cache read, both writes) is
  priced; output is per session only (``output_cost_usd``, from
  ``modelUsage``). ``rows_input_cost_usd + output_cost_usd + unmatched_cost_usd
  == list_price_cost_usd``, where ``list_price_cost_usd`` prices each model's
  ``modelUsage`` totals (1-hour writes split out as in the rows) and
  ``unmatched_cost_usd`` is what the turns do not show (the per-turn sums run
  0.2-3.3% short on cache reads).
* **The CLI's figure** (``cli_cost_usd``): the sessions' own ``total_cost_usd``,
  the run's plus the ``/explore`` session's. This is the figure ``results.json``
  reports as ``total_cost_usd`` and the soft budgets compare, because every
  saved run and the budgets were measured with it. The CLI estimates it with its
  own prices, so it can differ from the list price (on the Sonnet 5.5 runs it is
  about 12% higher: it charges cache reads at $0.20/M where the Bedrock list has
  $0.10/M). ``cli_minus_list_price_usd`` reports the difference as information,
  not as an error; ``cost.models`` gives both figures per model.

A model with no catalog price makes its rows' cost ``null``, counts in
``cost.unpriced_calls`` and is named in ``cost.unpriced_models``: a guessed price
would be a wrong figure (``scripts/usage_cost.py`` refuses one the same way).

A transcript that cannot be read, or that has no assistant turn, is an error:
``token_usage.json`` then holds ``{"source": "transcript", "error": ...}`` and
the command exits 1, so ``results.json`` can report ``token_usage_error``.
Lines that are not JSON (a run killed mid-write) are skipped and counted in
``skipped_lines``.

One assistant message can be streamed as several records that share
``message.id`` and repeat its usage; it is counted once, with the last usage
seen. Stdout is one JSON status line. No model calls.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # run as a script: ci.llm.judge is importable too
    sys.path.insert(0, str(REPO_ROOT))

from ci.llm.judge import canonical_model  # noqa: E402
from scripts.usage_cost import PRICES_FILE  # noqa: E402
from scripts.usage_cost import load_prices as load_catalog_prices  # noqa: E402

# Same keys as RoleBedrockModel.usage (src/shared/role_bedrock_model.py).
FIELDS = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "cache_creation_input_tokens": "cacheWriteInputTokens",
}
UNATTRIBUTED = "unattributed"
ORCHESTRATOR = "orchestrator"
EXPLORE = "explore"
_SCRIPT_RE = re.compile(r"scripts/run_([a-z0-9_]+)\.py")
# A slash command at the start of the prompt or after white space (never a path
# segment such as artifacts/wordpress).
_COMMAND_RE = re.compile(r"(?:^|\s)/([a-z][a-z0-9-]*)")
_SKILL_PHASES = (
    ("design-schema", "schema_design"),
    ("review-schema", "pe_review"),
    ("reality-check", "reality_check"),
    ("synthesize", "synthesis"),
)
_COMMAND_PHASES = (
    *_SKILL_PHASES,
    ("collect", "collect"),
    ("triage", "triage"),
    ("analyze", "analysis"),
    ("assign", "assignment"),
    ("explore", EXPLORE),
)
OUTPUT_NOTE = (
    "per-phase and per-agent outputTokens is null: transcript assistant records carry only "
    "the first-chunk output count; total.outputTokens is the session total from the result "
    "records, and cost.output_cost_usd its price"
)
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_PER_MILLION = 1_000_000
#: A 1-hour cache write costs twice the input price (the catalog lists the 5-minute rate).
ONE_HOUR_WRITE_FACTOR = 2.0


class TokenUsageError(Exception):
    """The transcript cannot be summarized (unreadable, or no assistant turn)."""


def empty_usage() -> dict[str, int]:
    return {"calls": 0, **dict.fromkeys(FIELDS.values(), 0)}


def add_usage(total: dict[str, int], other: dict[str, Any]) -> None:
    """Add ``other``'s numeric counters into ``total`` (missing keys count as 0)."""
    for key in ("calls", *FIELDS.values()):
        value = other.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + int(value)


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _turn_usage(raw: Any) -> dict[str, int]:
    usage = empty_usage()
    usage["calls"] = 1
    if isinstance(raw, dict):
        for src, dst in FIELDS.items():
            value = raw.get(src)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[dst] = int(value)
    return usage


def price_source() -> str:
    """Where the list prices come from, with the date the catalog was read."""
    with PRICES_FILE.open("rb") as handle:
        retrieved = tomllib.load(handle).get("retrieved_on")
    return f"src/catalog/{PRICES_FILE.name} (retrieved {retrieved})"


class _Pricing:
    """List prices by Bedrock model id (profile included), and the profile ids a
    run's sessions name, so a turn's bare model resolves to one priced id."""

    def __init__(self, prices: dict[str, dict[str, float]], profile_ids: set[str]) -> None:
        self.prices = prices
        by_model: dict[str, set[str]] = defaultdict(set)
        for model_id in profile_ids:
            if model_id in prices:
                by_model[canonical_model(model_id) or ""].add(model_id)
        self._by_model = by_model
        self.unpriced: set[str] = set()

    def model_id(self, model: Any) -> str | None:
        """The priced id ``model`` names: itself, else the session's one profile id
        of that model; None when there is none (or more than one)."""
        if not isinstance(model, str) or not model:
            return None
        if model in self.prices:
            return model
        ids = self._by_model.get(canonical_model(model) or "", set())
        return next(iter(ids)) if len(ids) == 1 else None

    def rates(self, model: Any) -> dict[str, float] | None:
        model_id = self.model_id(model)
        if model_id is None:
            if isinstance(model, str) and model:
                self.unpriced.add(model)
            return None
        return self.prices[model_id]


def _profile_ids(sessions: list[list[dict[str, Any]]]) -> set[str]:
    """Every model id the sessions' ``modelUsage`` and ``init`` records name."""
    ids: set[str] = set()
    for records in sessions:
        for rec in records:
            if rec.get("type") == "system" and rec.get("subtype") == "init":
                if isinstance(rec.get("model"), str):
                    ids.add(rec["model"])
            if rec.get("type") == "result" and isinstance(rec.get("modelUsage"), dict):
                ids.update(str(m) for m in rec["modelUsage"])
    return ids


def input_side_cost(
    usage: dict[str, Any], rates: dict[str, float], one_hour_writes: int = 0
) -> float:
    """USD for one turn's input side at list price: input, cache reads and cache
    writes (5-minute at the catalog's ``cache_write``, the ``one_hour_writes`` part
    at ``ONE_HOUR_WRITE_FACTOR`` x ``input``)."""
    writes = _number(usage.get("cacheWriteInputTokens"))
    one_hour = min(max(one_hour_writes, 0), writes)
    return (
        _number(usage.get("inputTokens")) * rates["input"]
        + _number(usage.get("cacheReadInputTokens")) * rates["cache_read"]
        + (writes - one_hour) * rates["cache_write"]
        + one_hour * rates["input"] * ONE_HOUR_WRITE_FACTOR
    ) / _PER_MILLION


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


def _phase_of_prompt(prompt: str) -> str | None:
    """The phase a subagent's task names: its slash command, else the first phase
    script it mentions."""
    for command in _COMMAND_RE.findall(prompt):
        for prefix, phase in _COMMAND_PHASES:
            if command.startswith(prefix):
                return phase
    match = _SCRIPT_RE.search(prompt)
    return match.group(1) if match else None


def _read_records(transcript: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        text = transcript.read_text(errors="replace")
    except OSError as exc:
        raise TokenUsageError(f"cannot read {transcript}: {exc.strerror or exc}") from exc
    records: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records, skipped


def _dispatches(records: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """``{tool_use_id: {description, prompt}}`` for every subagent start the
    transcript shows (Agent/Task tool calls, and ``task_started`` records)."""
    found: dict[str, dict[str, str]] = {}
    for rec in records:
        if rec.get("type") == "system" and rec.get("subtype") == "task_started":
            tool_id = rec.get("tool_use_id")
            if isinstance(tool_id, str) and rec.get("task_type") == "local_agent":
                found.setdefault(
                    tool_id,
                    {
                        "description": str(rec.get("description") or ""),
                        "prompt": str(rec.get("prompt") or ""),
                    },
                )
        message = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(message, dict):
            continue
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in ("Agent", "Task")
                and isinstance(block.get("id"), str)
                and isinstance(block.get("input"), dict)
            ):
                found[block["id"]] = {
                    "description": str(block["input"].get("description") or ""),
                    "prompt": str(block["input"].get("prompt") or ""),
                }
    return found


def _assistant_turns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per assistant message (records sharing ``message.id`` merged),
    in order of first appearance: ``{agent, phases, usage, model}``."""
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
        turn["model"] = message.get("model")
    return turns


def _one_hour_writes(raw: Any) -> int:
    creation = raw.get("cache_creation") if isinstance(raw, dict) else None
    return _number(creation.get("ephemeral_1h_input_tokens")) if isinstance(creation, dict) else 0


class _Table:
    """Accumulates one row per phase and per agent."""

    def __init__(self, pricing: _Pricing) -> None:
        self.pricing = pricing
        # 1-hour cache writes per (session, model), to price modelUsage the same way.
        self.one_hour: dict[tuple[int, str], int] = defaultdict(int)
        self.session = 0
        self.phases: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.total: dict[str, Any] = {**empty_usage(), "input_cost_usd": 0.0}
        self.unpriced_calls = 0

    def agent_row(self, key: str, label: str, kind: str) -> dict[str, Any]:
        row = self.agents.get(key)
        if row is None:
            row = {
                "agent": key,
                "label": label,
                "kind": kind,
                "phases": [],
                **empty_usage(),
                "largest_context": 0,
                "input_cost_usd": 0.0,
            }
            self.agents[key] = row
        return row

    def add(self, row: dict[str, Any], phase: str, turn: dict[str, Any]) -> None:
        usage = _turn_usage(turn["usage"])
        usage.pop("outputTokens")  # first-chunk count only: see the module docstring
        context = (
            usage["inputTokens"] + usage["cacheReadInputTokens"] + usage["cacheWriteInputTokens"]
        )
        one_hour = _one_hour_writes(turn["usage"])
        self.one_hour[(self.session, canonical_model(turn.get("model")) or "")] += one_hour
        cost: float | None
        if context == 0:
            cost = 0.0  # nothing billed (an error or synthetic message)
        else:
            rates = self.pricing.rates(turn.get("model"))
            if rates is None:
                cost = None
                self.unpriced_calls += 1
            else:
                cost = input_side_cost(usage, rates, one_hour)
        phase_row = self.phases.setdefault(phase, {**empty_usage(), "input_cost_usd": 0.0})
        for target in (phase_row, row, self.total):
            add_usage(target, usage)
            prior = target["input_cost_usd"]
            target["input_cost_usd"] = None if prior is None or cost is None else prior + cost
        if phase not in row["phases"]:
            row["phases"].append(phase)
        row["largest_context"] = max(row["largest_context"], context)


def _attribute(table: _Table, records: list[dict[str, Any]]) -> None:
    """Attribute the main session's turns to phases and agents."""
    dispatches = _dispatches(records)
    turns = _assistant_turns(records)
    first_phase: dict[str, str] = {}
    for turn in turns:
        if turn["agent"] is not None and turn["phases"]:
            first_phase.setdefault(turn["agent"], turn["phases"][0])
    current: dict[str | None, str] = {None: ORCHESTRATOR}
    table.agent_row(ORCHESTRATOR, ORCHESTRATOR, ORCHESTRATOR)
    for turn in turns:
        agent = turn["agent"]
        if agent is None:
            row = table.agents[ORCHESTRATOR]
        else:
            dispatch = dispatches.get(agent, {})
            row = table.agent_row(agent, dispatch.get("description") or agent, "subagent")
            if agent not in current:
                current[agent] = (
                    _phase_of_prompt(dispatch.get("prompt", ""))
                    or first_phase.get(agent)
                    or UNATTRIBUTED
                )
        if turn["phases"]:
            # A turn that runs a phase script is that phase's; the agent stays
            # on the last phase the turn ran until it runs another.
            phase = turn["phases"][0]
            current[agent] = turn["phases"][-1]
        else:
            phase = current[agent]
        table.add(row, phase, turn)


def _session_cost(records: list[dict[str, Any]]) -> float | None:
    """The session's own cost: ``total_cost_usd`` is cumulative on every result
    record, so the largest is the session's (as ``ci/llm/run.py`` reads it)."""
    costs = [
        float(r["total_cost_usd"])
        for r in records
        if r.get("type") == "result"
        and isinstance(r.get("total_cost_usd"), (int, float))
        and not isinstance(r.get("total_cost_usd"), bool)
    ]
    return max(costs) if costs else None


def _model_usage(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The cumulative ``modelUsage`` of the result record with the largest output."""
    best: dict[str, dict[str, Any]] = {}
    best_output = -1
    for rec in records:
        model_usage = rec.get("modelUsage") if rec.get("type") == "result" else None
        if not isinstance(model_usage, dict):
            continue
        output = sum(
            _number(u.get("outputTokens")) for u in model_usage.values() if isinstance(u, dict)
        )
        if output >= best_output:
            best_output = output
            best = {str(m): u for m, u in model_usage.items() if isinstance(u, dict)}
    return best


def _session_usage(records: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    """(``modelUsage`` summed over models, session output tokens).

    Without ``modelUsage``, the output total is the sum of the result records'
    own ``usage.output_tokens``."""
    fallback = sum(
        _number(rec["usage"].get("output_tokens"))
        for rec in records
        if rec.get("type") == "result" and isinstance(rec.get("usage"), dict)
    )
    model_usage = _model_usage(records)
    if not model_usage:
        return {}, fallback
    summed = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 0,
    }
    for per_model in model_usage.values():
        summed["inputTokens"] += _number(per_model.get("inputTokens"))
        summed["outputTokens"] += _number(per_model.get("outputTokens"))
        summed["cacheReadInputTokens"] += _number(per_model.get("cacheReadInputTokens"))
        summed["cacheWriteInputTokens"] += _number(per_model.get("cacheCreationInputTokens"))
    return summed, summed["outputTokens"]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _cost_section(
    table: _Table, sessions: list[list[dict[str, Any]]], explore_cli_cost: float | None
) -> dict[str, Any]:
    output_cost: float | None = 0.0
    list_price: float | None = 0.0
    models: dict[str, dict[str, Any]] = {}
    for index, records in enumerate(sessions):
        for model, usage in _model_usage(records).items():
            entry = models.setdefault(model, {"cli": 0.0, "list": 0.0, "priced": True})
            cli = usage.get("costUSD")
            if isinstance(cli, (int, float)) and not isinstance(cli, bool):
                entry["cli"] += float(cli)
            rates = table.pricing.rates(model)
            if rates is None:
                entry["priced"] = False
                output_cost = list_price = None
                continue
            output = _number(usage.get("outputTokens")) * rates["output"] / _PER_MILLION
            one_hour = table.one_hour.get((index, canonical_model(model) or ""), 0)
            priced = output + input_side_cost(
                {
                    "inputTokens": usage.get("inputTokens"),
                    "cacheReadInputTokens": usage.get("cacheReadInputTokens"),
                    "cacheWriteInputTokens": usage.get("cacheCreationInputTokens"),
                },
                rates,
                one_hour,
            )
            entry["list"] += priced
            if output_cost is not None and list_price is not None:
                output_cost += output
                list_price += priced
    if not models:
        output_cost = list_price = None  # no modelUsage: nothing to price the output from
    run_cli = _session_cost(sessions[0])
    known = [c for c in (run_cli, explore_cli_cost) if c is not None]
    cli_total = sum(known) if known else None
    rows_cost = table.total["input_cost_usd"]
    unmatched = (
        None
        if list_price is None or rows_cost is None or output_cost is None
        else list_price - rows_cost - output_cost
    )
    return {
        "price_source": price_source(),
        "list_price_cost_usd": _round(list_price),
        "rows_input_cost_usd": _round(rows_cost),
        "output_cost_usd": _round(output_cost),
        "unmatched_cost_usd": _round(unmatched),
        "cli_cost_usd": _round(cli_total),
        "run_cli_cost_usd": _round(run_cli),
        "explore_cli_cost_usd": _round(explore_cli_cost),
        "cli_minus_list_price_usd": (
            None if cli_total is None or list_price is None else _round(cli_total - list_price)
        ),
        "unpriced_calls": table.unpriced_calls,
        "unpriced_models": sorted(table.pricing.unpriced),
        "models": {
            model: {
                "cli_cost_usd": _round(entry["cli"]),
                "list_price_cost_usd": _round(entry["list"]) if entry["priced"] else None,
            }
            for model, entry in models.items()
        },
    }


def summarize(
    transcript: Path,
    explore: Path | None = None,
    prices: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """The ``token_usage.json`` payload for one run's transcript (and its
    ``/explore`` session's). Raises TokenUsageError when the run's transcript
    cannot be read or has no assistant turn."""
    records, skipped = _read_records(transcript)
    if not any(r.get("type") == "assistant" for r in records):
        raise TokenUsageError(f"no assistant turns in {transcript.name}")
    sessions = [records]
    if explore is not None:
        explore_records, explore_skipped = _read_records(explore)
        skipped += explore_skipped
        sessions.append(explore_records)
    pricing = _Pricing(load_catalog_prices() if prices is None else prices, _profile_ids(sessions))
    table = _Table(pricing)
    _attribute(table, records)
    explore_cost: float | None = None
    if explore is not None:
        explore_cost = _session_cost(explore_records)
        table.session = 1
        row = table.agent_row(EXPLORE, "/explore session", EXPLORE)
        for turn in _assistant_turns(explore_records):
            table.add(row, EXPLORE, turn)

    session: dict[str, int] = {}
    output_total = 0
    for recs in sessions:
        summed, output = _session_usage(recs)
        output_total += output
        for key, value in summed.items():
            session[key] = session.get(key, 0) + value
    total = dict(table.total)
    total["outputTokens"] = output_total
    total["input_cost_usd"] = _round(total["input_cost_usd"])
    phases = table.phases
    agents = list(table.agents.values())
    for row in (*phases.values(), *agents):
        row["outputTokens"] = None
        row["input_cost_usd"] = _round(row["input_cost_usd"])
    return {
        "source": "transcript",
        "output_tokens_note": OUTPUT_NOTE,
        "total": total,
        "session_model_usage": session,
        "phases": phases,
        "agents": agents,
        "cost": _cost_section(table, sessions, explore_cost),
        "unattributed_calls": phases.get(UNATTRIBUTED, {}).get("calls", 0),
        "skipped_lines": skipped,
    }


def _log_dir(artifact_root: Path, db: str, job: str) -> Path:
    if not (_SAFE_NAME.fullmatch(db) and _SAFE_NAME.fullmatch(job)) or {db, job} & {".", ".."}:
        raise ValueError("db and job must each be a single path component")
    root = artifact_root.resolve()
    log_dir = (artifact_root / db / job / "_logs").resolve()
    if not log_dir.is_relative_to(root):
        raise ValueError("log directory escapes the artifact root")
    return log_dir


def write_token_usage(
    transcript: Path, artifact_root: Path, db: str, job: str, explore: Path | None = None
) -> tuple[Path, dict[str, Any]]:
    """Write ``token_usage.json`` and return its path and payload. When the
    transcript cannot be summarized the file holds ``{"source", "error"}`` and
    TokenUsageError is raised after it is written."""
    log_dir = _log_dir(artifact_root, db, job)
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / "token_usage.json"
    try:
        payload = summarize(transcript, explore=explore)
    except TokenUsageError as exc:
        out.write_text(json.dumps({"source": "transcript", "error": str(exc)}, indent=2) + "\n")
        raise
    out.write_text(json.dumps(payload, indent=2) + "\n")
    return out, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("transcript")
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--explore", help="the /explore session's transcript (optional)")
    args = parser.parse_args(argv)
    try:
        out, payload = write_token_usage(
            Path(args.transcript),
            Path(args.artifact_root),
            args.db,
            args.job,
            explore=Path(args.explore) if args.explore else None,
        )
    except (OSError, ValueError, TokenUsageError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "path": str(out),
                "calls": payload["total"]["calls"],
                "unattributed_calls": payload["unattributed_calls"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
