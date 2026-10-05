"""Session fixtures: one deterministic pipeline run per sample input."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess  # nosec B404 -- launches this repo's own API/UI servers with fixed argv
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.pipeline import PipelineResult, _env, from_existing_job, run_pipeline

SAMPLES = ["wordpress", "discourse"]

REPO = Path(__file__).resolve().parents[2]
UI_BUILD = REPO / "src" / "ui" / "build"

# Every PipelineResult produced this session, so the end-of-session finalizer
# below can copy each run's deliverables out, regardless of which fixture
# (`run` or `all_runs`) created it.
_RESULTS: list[PipelineResult] = []

# `run` is BOTH session-scoped AND parametrized (params=SAMPLES below). pytest's
# fixture caching keys a parametrized higher-scope fixture by the full chain of
# parameter ids active for the test requesting it -- not just the fixture's own
# param. pytest-playwright's `--browser chromium --browser webkit` makes
# `browser_name` effectively parametrized too, and as test collection
# interleaves `run`'s two sample ids with `browser_name`'s two browser ids
# across tests.py/test_report_html.py/test_report_pdf.py, pytest tears down and
# re-creates `run` far more than twice a session (13 times, measured with
# `--setup-plan` on this suite) even though it only has 2 possible values. Each
# teardown+setup re-executes the full deterministic pipeline (~13s), so that
# alone roughly matched the old "~13s once per invocation" estimate below for
# the wrong reason -- it was 13 reruns of one sample's pipeline, not one run of
# the whole suite.
#
# A plain module-level cache sidesteps pytest's cache-key logic entirely: no
# matter how many times pytest invalidates and re-requests the `run` fixture,
# the pipeline subprocess chain for a given sample is only ever executed once
# per test session. (`all_runs`, below, is NOT parametrized itself, so it does
# not hit this bug -- verified with --setup-plan: it is set up exactly once
# regardless of how many browsers are passed -- and is left uncached.)
_PIPELINE_CACHE: dict[str, PipelineResult] = {}


def _run_pipeline_tracked(sample: str, artifact_root: Path, job_id: str) -> PipelineResult:
    result = run_pipeline(sample, artifact_root, job_id=job_id)
    _RESULTS.append(result)
    return result


def _run_pipeline_cached(sample: str, artifact_root: Path, job_id: str) -> PipelineResult:
    cached = _PIPELINE_CACHE.get(sample)
    if cached is not None:
        return cached
    result = _run_pipeline_tracked(sample, artifact_root, job_id=job_id)
    _PIPELINE_CACHE[sample] = result
    return result


# External-job mode: point the suite at a job produced elsewhere (e.g. a headless
# Claude run) instead of running the deterministic pipeline. All three of
# E2E_ARTIFACT_ROOT/E2E_DB/E2E_JOB must be set together, or none at all.
_EXTERNAL_JOB_CACHE: PipelineResult | None = None


def _external_env() -> tuple[str, str, str] | None:
    root = os.environ.get("E2E_ARTIFACT_ROOT")
    db = os.environ.get("E2E_DB")
    job = os.environ.get("E2E_JOB")
    if not any((root, db, job)):
        return None
    if not all((root, db, job)):
        pytest.fail(
            "E2E_ARTIFACT_ROOT, E2E_DB, and E2E_JOB must all be set together (or all "
            f"unset) -- got E2E_ARTIFACT_ROOT={root!r}, E2E_DB={db!r}, E2E_JOB={job!r}."
        )
    return root, db, job  # type: ignore[return-value]


def _external_job_result() -> PipelineResult:
    global _EXTERNAL_JOB_CACHE
    if _EXTERNAL_JOB_CACHE is None:
        root, db, job = _external_env()  # type: ignore[misc]
        result = from_existing_job(db, job, Path(root))
        _RESULTS.append(result)
        _EXTERNAL_JOB_CACHE = result
    return _EXTERNAL_JOB_CACHE


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize the session `run` fixture: the two deterministic samples
    normally, or a single "external" param when E2E_ARTIFACT_ROOT/E2E_DB/E2E_JOB
    point the suite at an existing job instead."""
    if "run" not in metafunc.fixturenames:
        return
    if _external_env() is not None:
        metafunc.parametrize("run", ["external"], indirect=True)
    else:
        metafunc.parametrize("run", SAMPLES, indirect=True)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    """Count ACTUAL pipeline executions this session (appends to _RESULTS happen
    only on a cache miss -- see _run_pipeline_tracked), as opposed to how many
    times a fixture that *returns* a run was merely set up. Grep this line to
    verify the fixture-caching fix above: `./ci/e2e.sh` greps for it.
    """
    terminalreporter.write_line(f"[e2e] pipeline executed {len(_RESULTS)} time(s) this session")


@pytest.fixture(scope="session")
def e2e_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return Path(tmp_path_factory.mktemp("e2e"))


def _version_sort_key(path: Path) -> tuple[int, str]:
    """Sort key for synthesis "v<N>" directories: numeric on the digits after
    "v" so "v10" sorts after "v2" (plain `sorted()` would put "v10" first,
    lexically). Falls back to a string sort for anything that doesn't match."""
    digits = path.name[1:]
    if digits.isdigit():
        return (int(digits), "")
    return (-1, path.name)


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
        # Numeric sort, not lexical: "v10" must sort after "v2", not before.
        for version_dir in sorted(synthesis_dir.glob("v*"), key=_version_sort_key):
            if not version_dir.is_dir():
                continue
            for f in version_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)


@pytest.fixture(scope="session")
def run(request: pytest.FixtureRequest, e2e_root: Path) -> PipelineResult:
    sample = request.param
    if sample == "external":
        return _external_job_result()
    return _run_pipeline_cached(sample, e2e_root / "artifacts", job_id=f"e2e-{sample[:4]}")


def _ensure_port_free(port: int) -> None:
    """Fail fast, with a clear message, instead of silently talking to whatever
    (possibly stale) process is already listening on ``port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            pytest.fail(
                f"port {port} is already in use ({exc}). Something -- maybe a "
                "leftover `uvicorn` or `serve` from a previous e2e run -- is "
                "already listening on it; stop it and re-run."
            )


def _log_path(name: str) -> Path:
    """A file to redirect a subprocess's stdout/stderr to, instead of an
    unread ``subprocess.PIPE`` (which deadlocks once its OS buffer fills,
    since nothing ever drains it). Under $E2E_OUTPUT when set (so CI can
    publish it as an artifact), else a throwaway tmp file."""
    output = os.environ.get("E2E_OUTPUT")
    if output:
        path = Path(output) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    fd, tmp = tempfile.mkstemp(prefix=f"{name}-")
    os.close(fd)
    return Path(tmp)


def _wait_for(port: int, proc: subprocess.Popen, log_path: Path, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text()[-4000:] if log_path.exists() else "(no log)"
            raise RuntimeError(
                f"process exited (code {proc.returncode}) before anything "
                f"started listening on port {port}. {log_path} tail:\n{tail}"
            )
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise TimeoutError(f"nothing listening on {port} after {timeout}s")


def _terminate(proc: subprocess.Popen, log_file) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    log_file.close()


@pytest.fixture(scope="session")
def all_runs(e2e_root: Path) -> list[PipelineResult]:
    """Both samples in ONE artifact root, so the UI lists both jobs. In external-job
    mode (E2E_ARTIFACT_ROOT/E2E_DB/E2E_JOB set) there's only the one job to list."""
    if _external_env() is not None:
        return [_external_job_result()]
    root = e2e_root / "ui-artifacts"
    return [_run_pipeline_tracked(s, root, job_id=f"ui-{s[:4]}") for s in SAMPLES]


@pytest.fixture(scope="session")
def api(all_runs: list[PipelineResult]) -> Iterator[str]:
    _ensure_port_free(8000)
    # Reuse pipeline._env() so the API subprocess is stripped of AWS_PROFILE /
    # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN the same way
    # the deterministic pipeline steps are -- same proof: this suite needs no
    # AWS credentials. src.api.main always uses the local filesystem services
    # (hosted Step Functions/S3 services removed, #175).
    env = _env()
    env["ARTIFACT_DIR"] = str(all_runs[0].artifact_root)
    log_path = _log_path("api.log")
    log_file = log_path.open("w")
    proc = subprocess.Popen(  # nosec B603 -- fixed argv, this repo's own API server
        [sys.executable, "-m", "uvicorn", "src.api.main:app", "--port", "8000"],
        cwd=REPO,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(8000, proc, log_path)
        yield "http://localhost:8000/api/v1/"
    finally:
        _terminate(proc, log_file)


@pytest.fixture(scope="session")
def ui(api: str) -> Iterator[str]:
    if not (UI_BUILD / "index.html").exists():
        pytest.fail(
            "UI not built. Run: cd src/ui && npm ci && REACT_APP_API_URL=http://localhost:8000/api/v1/ npx react-scripts build"
        )
    serve_bin = REPO / "src" / "ui" / "node_modules" / ".bin" / "serve"
    if not serve_bin.exists():
        pytest.fail(f"{serve_bin} not found. Run: cd src/ui && npm ci")
    _ensure_port_free(3000)
    log_path = _log_path("serve.log")
    log_file = log_path.open("w")
    proc = subprocess.Popen(  # nosec B603 -- fixed argv, the repo's own pinned `serve` binary
        # Launched directly (not via `npx serve`): npx re-resolves and may
        # re-install the package on every invocation, which is slower and,
        # offline, can fail outright. The pinned dependency's own binary
        # (installed by `npm ci`) is what we actually want to run.
        [str(serve_bin), "-s", "build", "-l", "3000", "--no-clipboard"],
        cwd=REPO / "src" / "ui",
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(3000, proc, log_path)
        yield "http://localhost:3000"
    finally:
        _terminate(proc, log_file)
