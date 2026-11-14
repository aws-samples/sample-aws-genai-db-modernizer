#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Compare e2e-llm runs' cost, tokens, time and judge score (#763).

Usage:
    uv run python ci/llm/compare_costs.py RESULTS [RESULTS ...] \
        [--baseline RESULTS ...] [--by phase|agent|both] [--allow-mixed] [--json]

Each ``RESULTS`` is a ``results.json`` written by ``ci/e2e-llm.sh`` or the run
folder holding one (``test-results/llm-<mode>-<fixture>/``), optionally named
``LABEL=PATH``; without a name a run is labelled by its folder (and its
``git_sha`` when two folders share a name). The output is a Markdown table with
one column per run and one row per metric: the CLI's total cost
(``total_cost_usd``, what the budgets compare) and the list-price cost, run and
``/explore`` cost, wall time, judge mean, turns, the token totals, the largest
single context, unattributed calls, then a block per phase (``--by phase``, the
default), per agent group (``--by agent``) or both: input-side cost at list
price, cache writes, cache reads and calls (from ``token_usage``, see
``ci/llm/token_usage.py``). An agent group is the orchestrator, the ``/explore``
session, or the subagents sharing a dispatch description once its "part N of M"
is dropped (the DynamoDB group drafts are one group), with the number of agents.

With ``--baseline`` (repeatable) the baseline runs come first, then a
``baseline mean`` column, the other runs, a ``candidate mean`` column and the
``change`` of the candidate mean against the baseline mean in percent: the shape
a cost lever's measurement needs (two baseline runs, two runs with the lever). A
mean taken over fewer runs than its group has says so (``$3.70 (1/2)``), and a
``results.json`` from before a field existed shows ``-`` there.

Runs of different fixtures or modes are not comparable: the tool refuses them
(exit 2) unless ``--allow-mixed`` is given, and then prints a warning line above
the table. Read-only; no model calls.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

# How a row's values are formatted.
_MONEY = "money"
_COUNT = "count"
_SCORE = "score"
_SECONDS = "seconds"
_PART_RE = re.compile(r",?\s*part \d+ of \d+.*$", re.IGNORECASE)
BY_CHOICES = ("phase", "agent", "both")


class MixedRunsError(ValueError):
    """The runs compared differ in fixture or mode."""


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dict(value: Any) -> dict[str, Any]:
    """``value`` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _load(spec: str | Path) -> tuple[str | None, Path, dict[str, Any]]:
    text = str(spec)
    label: str | None = None
    if "=" in text and not Path(text).exists():
        label, text = text.split("=", 1)
    path = Path(text)
    if path.is_dir():
        path = path / "results.json"
    try:
        row = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(row, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return label, path, row


def _token_usage(row: dict[str, Any]) -> dict[str, Any]:
    section = row.get("token_usage")
    return section if isinstance(section, dict) else {}


def _total_cost(row: dict[str, Any]) -> float | None:
    total = _num(row.get("total_cost_usd"))
    return total if total is not None else _num(row.get("cost_usd"))


def _agents(row: dict[str, Any]) -> list[dict[str, Any]]:
    agents = _token_usage(row).get("agents")
    return [a for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []


def _largest_context(row: dict[str, Any]) -> float | None:
    known = [s for a in _agents(row) if (s := _num(a.get("largest_context"))) is not None]
    return max(known) if known else None


def agent_group(agent: dict[str, Any]) -> str:
    """The comparison group of one agent row: its kind for the orchestrator and the
    /explore session, else its label without "part N of M"."""
    kind = str(agent.get("kind") or "")
    if kind in ("orchestrator", "explore"):
        return kind
    label = str(agent.get("label") or agent.get("agent") or "?")
    return _PART_RE.sub("", label).strip() or label


def _agent_groups(row: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    groups: dict[str, dict[str, float | None]] = {}
    for agent in _agents(row):
        group = groups.setdefault(
            agent_group(agent),
            {"agents": 0, "input_cost_usd": 0.0, "writes": 0, "reads": 0, "calls": 0},
        )
        group["agents"] = (group["agents"] or 0) + 1
        cost = _num(agent.get("input_cost_usd"))
        group["input_cost_usd"] = (
            None if cost is None or group["input_cost_usd"] is None
            else group["input_cost_usd"] + cost
        )  # fmt: skip
        for key, src in (
            ("writes", "cacheWriteInputTokens"),
            ("reads", "cacheReadInputTokens"),
            ("calls", "calls"),
        ):
            group[key] = (group[key] or 0) + (_num(agent.get(src)) or 0)
    return groups


def _metrics(row: dict[str, Any]) -> list[tuple[str, str, float | None]]:
    usage = _token_usage(row)
    total = _dict(usage.get("total"))
    cost = _dict(usage.get("cost"))
    explore = _dict(row.get("explore"))
    judge = _dict(row.get("judge"))
    list_price = _num(row.get("list_price_cost_usd"))
    if list_price is None:
        list_price = _num(cost.get("list_price_cost_usd"))
    return [
        ("total cost (CLI)", _MONEY, _total_cost(row)),
        ("list-price cost", _MONEY, list_price),
        ("run cost (CLI)", _MONEY, _num(row.get("cost_usd"))),
        ("explore cost (CLI)", _MONEY, _num(explore.get("cost_usd"))),
        ("output cost", _MONEY, _num(cost.get("output_cost_usd"))),
        ("unmatched cost", _MONEY, _num(cost.get("unmatched_cost_usd"))),
        ("wall time (s)", _SECONDS, _num(row.get("wall_duration_s"))),
        ("judge mean", _SCORE, _num(judge.get("mean"))),
        ("turns", _COUNT, _num(row.get("num_turns"))),
        ("calls", _COUNT, _num(total.get("calls"))),
        ("cache writes", _COUNT, _num(total.get("cacheWriteInputTokens"))),
        ("cache reads", _COUNT, _num(total.get("cacheReadInputTokens"))),
        ("input tokens", _COUNT, _num(total.get("inputTokens"))),
        ("output tokens", _COUNT, _num(total.get("outputTokens"))),
        ("largest context", _COUNT, _largest_context(row)),
        ("unattributed calls", _COUNT, _num(usage.get("unattributed_calls"))),
    ]


def _phase_metrics(row: dict[str, Any], phase: str) -> list[tuple[str, str, float | None]]:
    phases = _token_usage(row).get("phases")
    counters = (phases or {}).get(phase) if isinstance(phases, dict) else None
    counters = counters if isinstance(counters, dict) else {}
    return [
        (f"{phase} cost", _MONEY, _num(counters.get("input_cost_usd"))),
        (f"{phase} cache writes", _COUNT, _num(counters.get("cacheWriteInputTokens"))),
        (f"{phase} cache reads", _COUNT, _num(counters.get("cacheReadInputTokens"))),
        (f"{phase} calls", _COUNT, _num(counters.get("calls"))),
    ]


def _group_metrics(row: dict[str, Any], group: str) -> list[tuple[str, str, float | None]]:
    found = _agent_groups(row).get(group, {})
    name = f"agent {group}"
    return [
        (f"{name} cost", _MONEY, found.get("input_cost_usd")),
        (f"{name} cache writes", _COUNT, found.get("writes")),
        (f"{name} cache reads", _COUNT, found.get("reads")),
        (f"{name} calls", _COUNT, found.get("calls")),
        (f"{name} agents", _COUNT, found.get("agents")),
    ]


def _ranked(weights: dict[str, float]) -> list[str]:
    return sorted(weights, key=lambda name: (-weights[name], name))


def _phases(rows: list[dict[str, Any]]) -> list[str]:
    """Every phase any run shows, the costliest (summed over runs) first."""
    weight: dict[str, float] = {}
    for row in rows:
        phases = _token_usage(row).get("phases")
        for name, counters in (phases or {}).items() if isinstance(phases, dict) else []:
            counters = counters if isinstance(counters, dict) else {}
            cost = _num(counters.get("input_cost_usd")) or 0.0
            writes = _num(counters.get("cacheWriteInputTokens")) or 0.0
            weight[name] = weight.get(name, 0.0) + cost * 1e9 + writes
    return _ranked(weight)


def _groups(rows: list[dict[str, Any]]) -> list[str]:
    weight: dict[str, float] = {}
    for row in rows:
        for name, group in _agent_groups(row).items():
            cost = group.get("input_cost_usd") or 0.0
            weight[name] = weight.get(name, 0.0) + cost * 1e9 + (group.get("writes") or 0.0)
    return _ranked(weight)


def _labels(loaded: list[tuple[str | None, Path, dict[str, Any]]]) -> list[str]:
    names = [label or path.parent.name or str(path) for label, path, _ in loaded]
    labels = []
    for (label, _path, row), name in zip(loaded, names, strict=True):
        if label is None and names.count(name) > 1:
            name = f"{name}@{row.get('git_sha') or '?'}"
        labels.append(name)
    # Still clashing (same folder name and same commit): number them.
    return [
        f"{name}#{labels[: i + 1].count(name)}" if labels.count(name) > 1 else name
        for i, name in enumerate(labels)
    ]


def _mean(values: list[float | None]) -> tuple[float | None, int]:
    known = [v for v in values if v is not None]
    return (statistics.fmean(known) if known else None), len(known)


def _mixed(rows: list[dict[str, Any]]) -> str | None:
    """Why the runs are not comparable, or None."""
    for field in ("fixture", "mode"):
        seen = sorted({str(row.get(field)) for row in rows if row.get(field) is not None})
        if len(seen) > 1:
            return f"the runs differ in {field} ({', '.join(seen)})"
    return None


def compare(
    runs: list[str | Path],
    baselines: list[str | Path] | None = None,
    by: str = "phase",
    allow_mixed: bool = False,
) -> dict[str, Any]:
    """``{"runs": [...], "rows": [{metric, kind, values, baseline_mean?, baseline_n?,
    candidate_mean?, candidate_n?, change_pct?}], "baselines": n, "warnings": [...]}``.
    Raises MixedRunsError when the runs differ in fixture or mode, unless
    ``allow_mixed``."""
    if by not in BY_CHOICES:
        raise ValueError(f"--by must be one of {', '.join(BY_CHOICES)}")
    base = [_load(spec) for spec in baselines or []]
    cand = [_load(spec) for spec in runs]
    loaded = base + cand
    rows = [row for _, _, row in loaded]
    warnings: list[str] = []
    mixed = _mixed(rows)
    if mixed:
        if not allow_mixed:
            raise MixedRunsError(f"{mixed}: the change would compare different work")
        warnings.append(f"WARNING: {mixed}; the comparison is not like for like")
    labels = _labels(loaded)
    phases = _phases(rows) if by in ("phase", "both") else []
    groups = _groups(rows) if by in ("agent", "both") else []
    per_run = [
        _metrics(row)
        + [m for phase in phases for m in _phase_metrics(row, phase)]
        + [m for group in groups for m in _group_metrics(row, group)]
        for row in rows
    ]
    table = []
    for i, (metric, kind, _) in enumerate(per_run[0] if per_run else []):
        values = [metrics[i][2] for metrics in per_run]
        entry: dict[str, Any] = {"metric": metric, "kind": kind, "values": values}
        if base:
            baseline_mean, baseline_n = _mean(values[: len(base)])
            candidate_mean, candidate_n = _mean(values[len(base) :])
            entry.update(
                baseline_mean=baseline_mean,
                baseline_n=baseline_n,
                candidate_mean=candidate_mean,
                candidate_n=candidate_n,
                change_pct=(
                    (candidate_mean - baseline_mean) / baseline_mean * 100
                    if baseline_mean and candidate_mean is not None
                    else None
                ),
            )
        table.append(entry)
    return {
        "runs": [
            {
                "label": label,
                "path": str(path),
                "fixture": row.get("fixture"),
                "mode": row.get("mode"),
                "git_sha": row.get("git_sha"),
                "baseline": i < len(base),
            }
            for i, (label, (_, path, row)) in enumerate(zip(labels, loaded, strict=True))
        ],
        "rows": table,
        "baselines": len(base),
        "warnings": warnings,
    }


def _fmt(value: float | None, kind: str) -> str:
    if value is None:
        return "-"
    if kind == _MONEY:
        return f"${value:,.2f}"
    if kind == _SCORE:
        return f"{value:.2f}"
    return f"{value:,.0f}"


def _fmt_mean(value: float | None, kind: str, n: int, of: int) -> str:
    text = _fmt(value, kind)
    return text if n == of or value is None else f"{text} ({n}/{of})"


def render(data: dict[str, Any]) -> str:
    """The comparison as a Markdown table, under any warning lines."""
    runs = data["runs"]
    n_base = data["baselines"]
    n_cand = len(runs) - n_base
    header = ["metric"]
    if n_base:
        header += [r["label"] for r in runs[:n_base]] + ["baseline mean"]
        header += [r["label"] for r in runs[n_base:]] + ["candidate mean", "change"]
    else:
        header += [r["label"] for r in runs]
    lines = [*data.get("warnings", [])]
    if lines:
        lines.append("")
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in data["rows"]:
        kind, values = row["kind"], row["values"]
        cells = [row["metric"]]
        if n_base:
            cells += [_fmt(v, kind) for v in values[:n_base]]
            cells.append(_fmt_mean(row["baseline_mean"], kind, row["baseline_n"], n_base))
            cells += [_fmt(v, kind) for v in values[n_base:]]
            cells.append(_fmt_mean(row["candidate_mean"], kind, row["candidate_n"], n_cand))
            change = row["change_pct"]
            cells.append("-" if change is None else f"{change:+.1f}%")
        else:
            cells += [_fmt(v, kind) for v in values]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("runs", nargs="+", help="results.json files or run folders (LABEL=PATH)")
    parser.add_argument(
        "--baseline",
        action="append",
        default=[],
        help="a baseline results.json or run folder (repeatable)",
    )
    parser.add_argument(
        "--by", choices=BY_CHOICES, default="phase", help="break costs down by phase or agent"
    )
    parser.add_argument(
        "--allow-mixed",
        action="store_true",
        help="compare runs of different fixtures or modes anyway (prints a warning)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    try:
        data = compare(args.runs, baselines=args.baseline, by=args.by, allow_mixed=args.allow_mixed)
    except MixedRunsError as exc:
        print(f"error: {exc}; pass --allow-mixed to compare them anyway", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in data["warnings"]:
        print(warning, file=sys.stderr)
    print(json.dumps(data, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
