"""The deterministic pipeline must give the same analysis, assignment and
Reality Check result whatever the Python hash seed (#186, #288, #449).

Finalize runs in a new process and recomputes the deterministic Reality Check,
so a result that depends on PYTHONHASHSEED can differ from the one the LLM
reviewed. This runs collect → reality check on each sample under several seeds,
each in its own subprocesses, and compares the artifacts with only the
top-level ``timestamp`` and the analysis run's timing removed.

``test_removal_tiebreak_destination_ignores_hash_seed`` (#569) extends this
with a second, much cheaper subprocess check that targets the Reality
Check's Phase 1 settling directly (``_settle_committed_engines``), rather
than the full pipeline: the sample workloads above may or may not happen to
produce an engine-removal destination tie, so this pins a specific,
hand-built scenario (6 engines, 3 queries each, several engines tied on
burden-per-unique-query with query destinations that also tie on score)
that is KNOWN to flip the settled engine set across hash seeds before the
``_removal_tiebreak_key`` fix (verified by running it, unfixed, under seeds
0-5: seeds 1, 3, 4 and 5 settled on ``{documentdb, dynamodb}`` while seeds 0
and 2 settled on ``{aurora_mysql, dynamodb}``).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, SAMPLES, _env, unzip_sample

pytestmark = pytest.mark.e2e

SEEDS = ("0", "1", "2", "3", "4", "5")
JOB = "hashseed"


def _step(args: list[str], seed: str) -> dict:
    env = {**_env(), "PYTHONHASHSEED": seed}
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=env, timeout=600
    )
    assert proc.returncode == 0, f"{args[0]} failed (seed {seed}):\n{proc.stderr[-2000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _run_through_reality_check(sample: str, seed: str, root: Path) -> Path:
    db = SAMPLES[sample]
    artifact_root = root / f"seed-{seed}"
    src = unzip_sample(sample, root / f"input-{seed}")
    common = ["--job-id", JOB, "--db", db, "--artifact-root", str(artifact_root)]
    _step(["scripts/run_collect.py", "--file", str(src), *common], seed)
    triage = _step(["scripts/run_triage.py", *common], seed)
    for engine in triage["selected"]:
        _step(["scripts/run_analysis.py", *common, "--engine", engine, "--llm-mode", "none"], seed)
    _step(["scripts/run_assignment.py", *common], seed)
    _step(["scripts/run_reality_check.py", *common, "--llm-mode", "none"], seed)
    return artifact_root / db / JOB


def _canonical(path: Path) -> str:
    data = json.loads(path.read_text())
    data.pop("timestamp", None)
    # Analysis contracts record when and how long the run took.
    for key in ("analysis_timestamp", "analysis_duration_seconds"):
        (data.get("agent_metadata") or {}).pop(key, None)
    return json.dumps(data, ensure_ascii=False, indent=1)


@pytest.mark.parametrize("sample", list(SAMPLES))
def test_reality_check_and_assignment_ignore_hash_seed(sample: str, tmp_path: Path) -> None:
    workers = min(3, os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = list(pool.map(lambda s: _run_through_reality_check(sample, s, tmp_path), SEEDS))

    rels = sorted(
        str(p.relative_to(jobs[0])) for p in (jobs[0] / "assignment").glob("v*/assignment.json")
    )
    rels.append("reality-check/output.json")
    assert len(rels) >= 2, rels  # the resolver's v1 plus Reality Check's revision
    analysis = sorted(
        str(p.relative_to(jobs[0]))
        for name in ("analysis.json", "decision-trace.json")
        for p in jobs[0].glob(f"analysis-*/{name}")
    )
    assert analysis, "no analysis artifacts written"
    rels += analysis
    for rel in rels:
        by_seed = {seed: _canonical(job / rel) for seed, job in zip(SEEDS, jobs, strict=True)}
        distinct: dict[str, list[str]] = {}
        for seed, text in by_seed.items():
            distinct.setdefault(text, []).append(seed)
        assert len(distinct) == 1, f"{sample} {rel} differs by seed: {list(distinct.values())}"


# ---------------------------------------------------------------------------
# #569: a focused, cheap regression for the removal tie-break's destination
# pick, independent of whichever samples above happen to produce a tie.
# ---------------------------------------------------------------------------

_TIEBREAK_ENGINE_QUERIES: dict[str, list[dict]] = {
    engine: [
        {"query_id": f"q{i}", "assigned_engine": engine, "assignment_reason": "test"}
        for i in range(start, start + 3)
    ]
    for engine, start in (
        ("aurora_mysql", 0),
        ("aurora_postgresql", 3),
        ("dynamodb", 6),
        ("documentdb", 9),
        ("opensearch", 12),
        ("elasticache", 15),
    )
}

TIEBREAK_SCENARIO = {
    "engine_queries": _TIEBREAK_ENGINE_QUERIES,
    "current_engine_by_qid": {
        qa["query_id"]: qa["assigned_engine"]
        for qas in _TIEBREAK_ENGINE_QUERIES.values()
        for qa in qas
    },
    "trace_by_query": {
        "q0": {
            "engines": {
                "aurora_mysql": 70,
                "elasticache": 75,
                "aurora_postgresql": 75,
                "opensearch": 40,
                "dynamodb": 30,
                "documentdb": 30,
            }
        },
        "q1": {
            "engines": {
                "aurora_mysql": 60,
                "opensearch": 65,
                "documentdb": 65,
                "dynamodb": 40,
                "elasticache": 30,
                "aurora_postgresql": 30,
            }
        },
        "q2": {
            "engines": {
                "aurora_mysql": 60,
                "opensearch": 65,
                "elasticache": 65,
                "documentdb": 30,
                "aurora_postgresql": 40,
                "dynamodb": 40,
            }
        },
        "q3": {
            "engines": {
                "aurora_postgresql": 60,
                "opensearch": 68,
                "elasticache": 68,
                "documentdb": 40,
                "aurora_mysql": 30,
                "dynamodb": 30,
            }
        },
        "q4": {
            "engines": {
                "aurora_postgresql": 70,
                "dynamodb": 82,
                "opensearch": 82,
                "documentdb": 82,
                "aurora_mysql": 40,
                "elasticache": 40,
            }
        },
        "q5": {
            "engines": {
                "aurora_postgresql": 60,
                "aurora_mysql": 68,
                "documentdb": 68,
                "dynamodb": 30,
                "opensearch": 40,
                "elasticache": 40,
            }
        },
        "q6": {
            "engines": {
                "dynamodb": 70,
                "elasticache": 82,
                "aurora_mysql": 82,
                "aurora_postgresql": 30,
                "opensearch": 40,
                "documentdb": 30,
            }
        },
        "q7": {
            "engines": {
                "dynamodb": 65,
                "aurora_postgresql": 75,
                "documentdb": 75,
                "aurora_mysql": 75,
                "elasticache": 40,
                "opensearch": 40,
            }
        },
        "q8": {
            "engines": {
                "dynamodb": 60,
                "opensearch": 70,
                "documentdb": 70,
                "aurora_postgresql": 40,
                "elasticache": 40,
                "aurora_mysql": 40,
            }
        },
        "q9": {
            "engines": {
                "documentdb": 70,
                "opensearch": 75,
                "elasticache": 75,
                "aurora_postgresql": 40,
                "aurora_mysql": 30,
                "dynamodb": 30,
            }
        },
        "q10": {
            "engines": {
                "documentdb": 65,
                "opensearch": 77,
                "aurora_mysql": 77,
                "elasticache": 77,
                "aurora_postgresql": 30,
                "dynamodb": 30,
            }
        },
        "q11": {
            "engines": {
                "documentdb": 65,
                "aurora_mysql": 75,
                "aurora_postgresql": 75,
                "dynamodb": 40,
                "elasticache": 40,
                "opensearch": 40,
            }
        },
        "q12": {
            "engines": {
                "opensearch": 60,
                "documentdb": 68,
                "dynamodb": 68,
                "aurora_mysql": 30,
                "elasticache": 40,
                "aurora_postgresql": 30,
            }
        },
        "q13": {
            "engines": {
                "opensearch": 70,
                "aurora_postgresql": 80,
                "documentdb": 80,
                "aurora_mysql": 80,
                "elasticache": 40,
                "dynamodb": 30,
            }
        },
        "q14": {
            "engines": {
                "opensearch": 65,
                "documentdb": 77,
                "dynamodb": 77,
                "aurora_postgresql": 77,
                "aurora_mysql": 30,
                "elasticache": 40,
            }
        },
        "q15": {
            "engines": {
                "elasticache": 65,
                "aurora_postgresql": 73,
                "dynamodb": 73,
                "opensearch": 30,
                "aurora_mysql": 40,
                "documentdb": 30,
            }
        },
        "q16": {
            "engines": {
                "elasticache": 65,
                "aurora_postgresql": 70,
                "documentdb": 70,
                "dynamodb": 30,
                "opensearch": 30,
                "aurora_mysql": 40,
            }
        },
        "q17": {
            "engines": {
                "elasticache": 60,
                "aurora_mysql": 65,
                "documentdb": 65,
                "opensearch": 65,
                "dynamodb": 40,
                "aurora_postgresql": 40,
            }
        },
    },
}

_SETTLE_SCRIPT = """
import json, sys
from src.agents.referee.reality_check import _settle_committed_engines

with open(sys.argv[1], encoding="utf-8") as f:
    scenario = json.load(f)

committed, _assessment = _settle_committed_engines(
    engine_queries=scenario["engine_queries"],
    mandatory_query_ids=set(),
    mandatory_committed_engines=set(),
    trace_by_query=scenario["trace_by_query"],
    current_engine_by_qid=scenario["current_engine_by_qid"],
    query_signals={},
    query_map={},
    query_capabilities=None,
    max_rounds=10,
)
print(json.dumps(sorted(committed)))
"""


def test_removal_tiebreak_destination_ignores_hash_seed(tmp_path: Path) -> None:
    """``_settle_committed_engines`` must settle on the same committed engine
    set under every ``PYTHONHASHSEED`` (#569): a query's destination, when
    the Phase 1 removal simulation (``_removal_tiebreak_key``) finds it tied
    between two or more alternates, must go through ``tie_break.break_tie``
    with a stable key, never ``max()`` over a set whose iteration order is
    seed-dependent.
    """
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(TIEBREAK_SCENARIO), encoding="utf-8")

    results: dict[str, list[str]] = {}
    for seed in SEEDS:
        env = {**_env(), "PYTHONHASHSEED": seed}
        proc = subprocess.run(  # nosec B603 -- fixed interpreter, inline script, repo cwd
            [sys.executable, "-I", "-c", _SETTLE_SCRIPT, str(scenario_path)],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0, f"seed {seed} failed:\n{proc.stderr[-2000:]}"
        results[seed] = json.loads(proc.stdout.strip().splitlines()[-1])

    distinct: dict[str, list[str]] = {}
    for seed, committed in results.items():
        distinct.setdefault(json.dumps(committed), []).append(seed)
    assert len(distinct) == 1, f"settled engines differ by seed: {dict(results.items())}"
