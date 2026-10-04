"""The deterministic pipeline must give the same assignment and Reality Check
result whatever the Python hash seed (#186, #288).

Finalize runs in a new process and recomputes the deterministic Reality Check,
so a result that depends on PYTHONHASHSEED can differ from the one the LLM
reviewed. This runs collect → reality check on each sample under several seeds,
each in its own subprocesses, and compares the artifacts with only the
top-level ``timestamp`` removed.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    proc = subprocess.run(
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
    for rel in rels:
        by_seed = {seed: _canonical(job / rel) for seed, job in zip(SEEDS, jobs, strict=True)}
        distinct: dict[str, list[str]] = {}
        for seed, text in by_seed.items():
            distinct.setdefault(text, []).append(seed)
        assert len(distinct) == 1, f"{sample} {rel} differs by seed: {list(distinct.values())}"
