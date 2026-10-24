"""``assess(run_through="analysis")`` gives the same result in every process (#1259).

The public contract promises that, with no model, the same collection, library version
and engine scope give an identical ``AssessmentResult``. This runs ``assess()`` on each
bundled sample in separate processes under different ``PYTHONHASHSEED`` values and
compares the results serialized with sorted keys. The result carries no run timestamp
(only the opt-in ``artifacts`` block, which has no compatibility promise, records run
times), so the whole payload is compared.

The pipeline's own artifacts are checked the same way in
``test_hashseed_determinism.py``; this test checks the public surface built from them.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs an inline script with this repo's interpreter
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, SAMPLES, _env
from tests.unit.assessment.conftest import sample_collection

pytestmark = pytest.mark.e2e

SEEDS = ("0", "1", "2")

#: Each sample unscoped, plus one engine scope on the heterogeneous source.
CASES: tuple[tuple[str, list[str] | None], ...] = (
    *((name, None) for name in SAMPLES),
    ("trailgear", ["aurora_postgresql", "dynamodb"]),
)

_ASSESS_SCRIPT = """
import json, sys
from src.assessment import assess

with open(sys.argv[1], encoding="utf-8") as f:
    request = json.load(f)
result = assess(request)
print(json.dumps(result.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
"""


def _assess_in_subprocess(request_path: Path, seed: str) -> str:
    env = {**_env(), "PYTHONHASHSEED": seed}
    proc = subprocess.run(  # nosec B603 -- fixed interpreter, inline script, repo cwd
        [sys.executable, "-I", "-c", _ASSESS_SCRIPT, str(request_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    assert proc.returncode == 0, f"seed {seed} failed:\n{proc.stderr[-2000:]}"
    return proc.stdout.strip().splitlines()[-1]


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("sample", "engines"),
    CASES,
    ids=[f"{name}-{'-'.join(engines) if engines else 'all'}" for name, engines in CASES],
)
def test_assess_analysis_ignores_process_and_hash_seed(
    sample: str, engines: list[str] | None, tmp_path: Path
) -> None:
    options: dict[str, object] = {"run_through": "analysis"}
    if engines is not None:
        options["target_engines"] = engines
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {"database_name": sample, "collection": sample_collection(sample), "options": options}
        ),
        encoding="utf-8",
    )

    workers = min(len(SEEDS), os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outputs = list(pool.map(lambda seed: _assess_in_subprocess(request_path, seed), SEEDS))

    first = json.loads(outputs[0])
    assert first["summary"]["run_through_reached"] == "analysis"
    assert first["label"] == "analysis_only"
    assert first["statements"], "no statements assessed"
    assert first["artifacts"] is None
    distinct: dict[str, list[str]] = {}
    for seed, text in zip(SEEDS, outputs, strict=True):
        distinct.setdefault(text, []).append(seed)
    assert len(distinct) == 1, f"{sample} result differs by seed: {list(distinct.values())}"
