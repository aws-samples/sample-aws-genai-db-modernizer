"""Migration complexity remains a mode, with higher complexity breaking ties (#903)."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs the current interpreter on a fixed test module
import sys
from itertools import permutations
from pathlib import Path

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_ranking

REPO = Path(__file__).resolve().parents[4]


def _ranking(complexities: tuple[str | None, ...]) -> list[dict]:
    analysis = {
        "table_recommendations": [
            {"table_id": f"t{i}", "confidence_score": 80, "migration_complexity": level}
            for i, level in enumerate(complexities)
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        "cost_estimate": {"monthly_cost_usd": 12.5},
    }
    return build_ranking(
        SynthesisData(
            job_id="complexity-tie",
            database_name="test",
            engines={"dynamodb": EngineArtifacts(engine="dynamodb", analysis=analysis)},
        )
    )


@pytest.mark.parametrize(
    ("complexities", "expected"),
    [
        (("MEDIUM", "HIGH"), "HIGH"),
        (("LOW", "MEDIUM"), "MEDIUM"),
        (("LOW", "HIGH"), "HIGH"),
        (("LOW", "MEDIUM", "HIGH"), "HIGH"),
        (("LOW", "MEDIUM", "HIGH", "MEDIUM", "HIGH"), "HIGH"),
        (("LOW", "LOW", "HIGH"), "LOW"),
        (("MEDIUM", "MEDIUM", "HIGH"), "MEDIUM"),
        (("HIGH", "HIGH", "LOW"), "HIGH"),
        (("LOW",), "LOW"),
        ((), "MEDIUM"),
        ((None,), None),
        ((None, None, "HIGH"), None),
        ((None, "HIGH"), "HIGH"),
    ],
)
def test_complexity_mode_and_other_ranking_fields(
    complexities: tuple[str | None, ...], expected: str | None
) -> None:
    # Hold every other input constant: only the modal category may change.
    reference = _ranking(("MEDIUM",) * len(complexities))
    reference[0]["migration_complexity_avg"] = expected
    for ordered in dict.fromkeys(permutations(complexities)):
        assert _ranking(ordered) == reference


@pytest.mark.passes_on_main
def test_missing_complexity_defaults_to_medium() -> None:
    data = SynthesisData(
        job_id="missing-complexity",
        database_name="test",
        engines={
            "dynamodb": EngineArtifacts(
                engine="dynamodb",
                analysis={"table_recommendations": [{"table_id": "t", "confidence_score": 80}]},
            )
        },
    )
    assert build_ranking(data)[0]["migration_complexity_avg"] == "MEDIUM"


def test_medium_high_tie_ignores_hash_seed() -> None:
    script = """
import json
from tests.unit.agents.referee.test_synthesis_migration_complexity import _ranking
print(json.dumps(_ranking(("MEDIUM", "HIGH")), sort_keys=True))
"""
    results = {}
    for seed in ("0", "1", "2", "3", "4", "5"):
        proc = subprocess.run(  # nosec B603 -- fixed script and interpreter, no shell
            [sys.executable, "-c", script],
            cwd=REPO,
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        results[seed] = json.loads(proc.stdout.strip().splitlines()[-1])
    assert {seed: rows[0]["migration_complexity_avg"] for seed, rows in results.items()} == {
        seed: "HIGH" for seed in results
    }
    assert all(rows == results["0"] for rows in results.values())
