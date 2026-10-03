"""Session fixtures: one deterministic pipeline run per sample input."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.pipeline import PipelineResult, run_pipeline

SAMPLES = ["wordpress", "discourse"]

REPO = Path(__file__).resolve().parents[2]
UI_BUILD = REPO / "src" / "ui" / "build"

# Every PipelineResult produced this session, so the end-of-session finalizer
# below can copy each run's deliverables out, regardless of which fixture
# (`run` or `all_runs`) created it.
_RESULTS: list[PipelineResult] = []


def _run_pipeline_tracked(sample: str, artifact_root: Path, job_id: str) -> PipelineResult:
    result = run_pipeline(sample, artifact_root, job_id=job_id)
    _RESULTS.append(result)
    return result


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def e2e_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return Path(tmp_path_factory.mktemp("e2e"))


@pytest.fixture(scope="session", autouse=True)
def _copy_deliverables_to_e2e_output() -> Iterator[None]:
    """If E2E_OUTPUT is set, copy each run's synthesis/v*/ deliverables into
    $E2E_OUTPUT/deliverables/<db>/ at session end, so CI can publish the
    rendered HTML/PDF as a downloadable artifact without re-running anything.
    """
    yield
    output = os.environ.get("E2E_OUTPUT")
    if not output:
        return
    dest_root = Path(output) / "deliverables"
    for result in _RESULTS:
        synthesis_dir = result.job_dir() / "synthesis"
        if not synthesis_dir.is_dir():
            continue
        dest = dest_root / result.db
        dest.mkdir(parents=True, exist_ok=True)
        for version_dir in sorted(synthesis_dir.glob("v*")):
            if not version_dir.is_dir():
                continue
            for f in version_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)


@pytest.fixture(scope="session", params=SAMPLES)
def run(request: pytest.FixtureRequest, e2e_root: Path) -> PipelineResult:
    sample = request.param
    return _run_pipeline_tracked(sample, e2e_root / "artifacts", job_id=f"e2e-{sample[:4]}")


def _wait_for(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise TimeoutError(f"nothing listening on {port}")


@pytest.fixture(scope="session")
def all_runs(e2e_root: Path) -> list[PipelineResult]:
    """Both samples in ONE artifact root, so the UI lists both jobs."""
    root = e2e_root / "ui-artifacts"
    return [_run_pipeline_tracked(s, root, job_id=f"ui-{s[:4]}") for s in SAMPLES]


@pytest.fixture(scope="session")
def api(all_runs: list[PipelineResult]) -> Iterator[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("S3_BUCKET", "STATE_MACHINE_ARN", "AWS_PROFILE")
    }
    env.update(ARTIFACT_DIR=str(all_runs[0].artifact_root), AWS_DEFAULT_REGION="us-east-1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.api.main:app", "--port", "8000"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(8000)
        yield "http://localhost:8000/api/v1/"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def ui(api: str) -> Iterator[str]:
    if not (UI_BUILD / "index.html").exists():
        pytest.fail(
            "UI not built. Run: cd src/ui && npm ci && REACT_APP_API_URL=http://localhost:8000/api/v1/ npx react-scripts build"
        )
    proc = subprocess.Popen(
        ["npx", "--no-install", "serve", "-s", "build", "-l", "3000"],
        cwd=REPO / "src" / "ui",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(3000)
        yield "http://localhost:3000"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
