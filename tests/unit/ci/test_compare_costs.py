# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ci/llm/compare_costs.py: compare e2e-llm runs' cost and quality (#763)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ci.llm import compare_costs


def _phase(cost: float | None, cw: int, cr: int, calls: int = 3) -> dict[str, Any]:
    row: dict[str, Any] = {
        "calls": calls,
        "inputTokens": 10,
        "outputTokens": None,
        "cacheReadInputTokens": cr,
        "cacheWriteInputTokens": cw,
    }
    if cost is not None:
        row["input_cost_usd"] = cost
    return row


def _results(
    path: Path,
    *,
    total: float,
    judge: float | None,
    wall: float,
    schema_cost: float | None = 1.0,
    git_sha: str = "abc1234",
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    row = {
        "schema_version": 2,
        "git_sha": git_sha,
        "fixture": "wordpress",
        "mode": "both",
        "model": "global.anthropic.claude-sonnet-5-5",
        "cost_usd": total,
        "total_cost_usd": total,
        "num_turns": 40,
        "wall_duration_s": wall,
        "judge": None if judge is None else {"mean": judge, "scores": {}},
        "pass": True,
        "token_usage": {
            "total": {
                "calls": 6,
                "inputTokens": 20,
                "outputTokens": 1000,
                "cacheReadInputTokens": 3000,
                "cacheWriteInputTokens": 500,
            },
            "phases": {
                "schema_design": _phase(schema_cost, 400, 2000),
                "orchestrator": _phase(0.5 if schema_cost is not None else None, 100, 1000),
            },
            "agents": [
                {"agent": "orchestrator", "largest_context": 50_000},
                {"agent": "toolu_a", "largest_context": 150_000},
            ],
            "cost": {"output_cost_usd": 0.01, "unmatched_cost_usd": 0.0},
            "unattributed_calls": 0,
        },
    }
    out = path / "results.json"
    out.write_text(json.dumps(row))
    return out


def test_the_table_has_one_column_per_run_and_the_phase_rows(tmp_path: Path) -> None:
    a = _results(tmp_path / "a", total=3.6, judge=3.857, wall=560)
    b = _results(tmp_path / "b", total=3.2, judge=4.0, wall=500, schema_cost=0.6, git_sha="def5678")
    table = compare_costs.render(compare_costs.compare([a, b]))
    lines = table.splitlines()
    assert lines[0].startswith("| metric |") and "a" in lines[0] and "b" in lines[0]
    rows = {line.split("|")[1].strip(): line for line in lines[2:]}
    assert "$3.60" in rows["total cost (CLI)"] and "$3.20" in rows["total cost (CLI)"]
    assert "3.86" in rows["judge mean"] and "4.00" in rows["judge mean"]
    assert "560" in rows["wall time (s)"]
    assert "$1.00" in rows["schema_design cost"] and "$0.60" in rows["schema_design cost"]
    assert "400" in rows["schema_design cache writes"]
    assert "2,000" in rows["schema_design cache reads"]
    assert "150,000" in rows["largest context"]


def test_baseline_runs_are_averaged_and_the_change_is_against_their_mean(tmp_path: Path) -> None:
    base = [
        _results(tmp_path / "b1", total=3.6, judge=3.8, wall=560),
        _results(tmp_path / "b2", total=3.8, judge=4.0, wall=580),
    ]
    lever = [
        _results(tmp_path / "l1", total=3.0, judge=3.9, wall=500, schema_cost=0.5),
        _results(tmp_path / "l2", total=3.2, judge=3.9, wall=520, schema_cost=0.5),
    ]
    data = compare_costs.compare(lever, baselines=base)  # type: ignore[arg-type]
    by_metric = {row["metric"]: row for row in data["rows"]}
    total = by_metric["total cost (CLI)"]
    assert total["baseline_mean"] == pytest.approx(3.7)
    assert total["candidate_mean"] == pytest.approx(3.1)
    assert total["change_pct"] == pytest.approx((3.1 - 3.7) / 3.7 * 100)
    assert by_metric["schema_design cost"]["change_pct"] == pytest.approx(-50.0)
    table = compare_costs.render(data)
    header = table.splitlines()[0]
    assert "baseline mean" in header and "candidate mean" in header and "change" in header
    assert "-16.2%" in table


def test_a_directory_and_older_results_without_costs_are_accepted(tmp_path: Path) -> None:
    """A run folder works as well as its results.json, and a results.json from
    before the cost fields (or with no token usage at all) shows '-' there."""
    old = _results(tmp_path / "old", total=3.7, judge=None, wall=550, schema_cost=None)
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "results.json").write_text(json.dumps({"total_cost_usd": None, "cost_usd": 4.0}))
    data = compare_costs.compare([old.parent, bare])
    by_metric = {row["metric"]: row for row in data["rows"]}
    assert by_metric["total cost (CLI)"]["values"] == [3.7, 4.0]  # falls back to cost_usd
    assert by_metric["judge mean"]["values"] == [None, None]
    assert by_metric["schema_design cost"]["values"] == [None, None]
    assert "| judge mean | - | - |" in compare_costs.render(data)


def test_main_prints_the_table_or_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = _results(tmp_path / "a", total=3.6, judge=3.857, wall=560)
    assert compare_costs.main([str(a)]) == 0
    assert "| total cost (CLI) |" in capsys.readouterr().out
    assert compare_costs.main(["--json", str(a)]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["label"] == "a"
    assert compare_costs.main([str(tmp_path / "missing.json")]) == 1
    assert "missing.json" in capsys.readouterr().err


def test_labels_can_be_named(tmp_path: Path) -> None:
    a = _results(tmp_path / "x" / "llm-both-wordpress", total=3.6, judge=3.857, wall=560)
    b = _results(tmp_path / "y" / "llm-both-wordpress", total=3.6, judge=3.857, wall=560)
    c = _results(
        tmp_path / "z" / "llm-both-wordpress", total=3.6, judge=3.857, wall=560, git_sha="f00"
    )
    data = compare_costs.compare([f"ttl-1h={a}", b])
    assert [run["label"] for run in data["runs"]] == ["ttl-1h", "llm-both-wordpress"]
    # Unnamed runs from same-named folders are told apart by commit, then by number.
    data = compare_costs.compare([a, b, c])
    assert [run["label"] for run in data["runs"]] == [
        "llm-both-wordpress@abc1234#1",
        "llm-both-wordpress@abc1234#2",
        "llm-both-wordpress@f00",
    ]


def test_runs_of_another_fixture_or_mode_are_refused_unless_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _results(tmp_path / "a", total=3.6, judge=3.9, wall=560)
    b = _results(tmp_path / "b", total=41.0, judge=3.5, wall=1460)
    row = json.loads(b.read_text())
    row["fixture"] = "discourse"
    b.write_text(json.dumps(row))
    with pytest.raises(compare_costs.MixedRunsError, match="fixture"):
        compare_costs.compare([b], baselines=[a])
    assert compare_costs.main(["--baseline", str(a), str(b)]) == 2
    assert "--allow-mixed" in capsys.readouterr().err
    data = compare_costs.compare([b], baselines=[a], allow_mixed=True)
    assert data["warnings"] and data["warnings"][0].startswith("WARNING:")
    assert compare_costs.render(data).startswith("WARNING:")
    row["fixture"], row["mode"] = "wordpress", "chat"
    b.write_text(json.dumps(row))
    with pytest.raises(compare_costs.MixedRunsError, match="mode"):
        compare_costs.compare([b], baselines=[a])


def test_a_mean_over_fewer_runs_than_its_group_says_how_many(tmp_path: Path) -> None:
    base = [
        _results(tmp_path / "b1", total=3.6, judge=3.8, wall=560),
        _results(tmp_path / "b2", total=3.8, judge=None, wall=580),
    ]
    lever = [_results(tmp_path / "l1", total=3.0, judge=3.9, wall=500)]
    data = compare_costs.compare(lever, baselines=base)  # type: ignore[arg-type]
    judge = {row["metric"]: row for row in data["rows"]}["judge mean"]
    assert (judge["baseline_mean"], judge["baseline_n"]) == (3.8, 1)
    lines = {line.split("|")[1].strip(): line for line in compare_costs.render(data).splitlines()}
    assert "3.80 (1/2)" in lines["judge mean"]
    assert "/2)" not in lines["total cost (CLI)"]  # every run had it


def test_agents_can_be_compared_by_group(tmp_path: Path) -> None:
    """--by agent: the orchestrator, and the subagents that share a description once
    "part N of M" is dropped, as one group each."""
    path = _results(tmp_path / "a", total=3.6, judge=3.9, wall=560)
    row = json.loads(path.read_text())
    row["token_usage"]["agents"] = [
        {"agent": "orchestrator", "kind": "orchestrator", "label": "orchestrator",
         "calls": 30, "cacheWriteInputTokens": 80_000, "cacheReadInputTokens": 1_900_000,
         "input_cost_usd": 0.4, "largest_context": 80_000},
        {"agent": "t1", "kind": "subagent", "label": "DynamoDB design, part 1 of 3 (queries 1-23 of 40)",
         "calls": 5, "cacheWriteInputTokens": 100_000, "cacheReadInputTokens": 280_000,
         "input_cost_usd": 0.3, "largest_context": 110_000},
        {"agent": "t2", "kind": "subagent", "label": "DynamoDB design, part 2 of 3 (queries 24-29 of 40)",
         "calls": 7, "cacheWriteInputTokens": 70_000, "cacheReadInputTokens": 350_000,
         "input_cost_usd": 0.2, "largest_context": 78_000},
    ]  # fmt: skip
    path.write_text(json.dumps(row))
    data = compare_costs.compare([path], by="agent")
    by_metric = {r["metric"]: r for r in data["rows"]}
    assert by_metric["agent DynamoDB design cost"]["values"] == [pytest.approx(0.5)]
    assert by_metric["agent DynamoDB design agents"]["values"] == [2]
    assert by_metric["agent DynamoDB design cache writes"]["values"] == [170_000]
    assert by_metric["agent orchestrator calls"]["values"] == [30]
    assert not any(m.startswith("schema_design") for m in by_metric)  # phases left out
    both = {r["metric"] for r in compare_costs.compare([path], by="both")["rows"]}
    assert "schema_design cost" in both and "agent orchestrator cost" in both
