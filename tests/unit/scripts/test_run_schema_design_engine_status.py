"""``--status`` for a single-pass engine reports the last ``--finalize`` verdict (#332).

Schema-design subagents reported their phases complete without writing
``phase_status.schema_design_<engine>`` into ``.modernizer-state.json``. The
orchestrator now sets that entry itself from a deterministic, read-only check:
``run_schema_design.py --engine <engine> --status``, which reads the verdict the
last ``--finalize`` printed (``finalize_record.json``) instead of the subagent's
self-report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design
from src.storage.local_store import LocalArtifactStore

DB, JOB, ENGINE = "wordpress", "job-001", "documentdb"
BASE = f"{DB}/{JOB}/schema-{ENGINE}/v1"
RESPONSE = f"{DB}/{JOB}/llm_responses/schema_design_{ENGINE}.json"


def _run(monkeypatch, capsys, root: Path, *extra: str, engine: str = ENGINE):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            engine,
            "--artifact-root",
            str(root),
            *extra,
        ],
    )
    code = 0
    try:
        run_schema_design.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, json.loads(lines[-1])


def _fake_finalize(monkeypatch, result: dict) -> None:
    def fake(**kwargs):
        store = kwargs["store"]
        if result.get("status") == "complete" or result.get("output_path"):
            store.write_json(f"{BASE}/schema_output.json", {"validation_passed": True})
        return dict(result)

    monkeypatch.setattr("src.agents.schema_design.handler.finalize_schema_design", fake)


def _response(store: LocalArtifactStore, body: dict | None = None) -> None:
    store.write_json(RESPONSE, body or {"collections": []})


def test_before_any_finalize_the_engine_is_not_finalized(monkeypatch, capsys, tmp_path):
    code, status = _run(monkeypatch, capsys, tmp_path, "--status")
    assert code == 0
    assert status["status"] == "not_finalized"
    assert status["assignment_version"] == 1


def test_a_complete_finalize_reads_complete(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _response(store)
    _fake_finalize(monkeypatch, {"status": "complete", "output_path": f"{BASE}/schema_output.json"})
    assert _run(monkeypatch, capsys, tmp_path, "--finalize")[1]["status"] == "complete"

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")
    assert code == 0
    assert status["status"] == "complete"
    assert status["output_path"].endswith(f"{BASE}/schema_output.json")


def test_a_failed_finalize_reads_validation_failed_with_its_errors(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _response(store)
    _fake_finalize(monkeypatch, {"status": "validation_failed", "errors": ["collections: missing"]})
    _run(monkeypatch, capsys, tmp_path, "--finalize")

    _, status = _run(monkeypatch, capsys, tmp_path, "--status")
    assert status["status"] == "validation_failed"
    assert status["errors"] == ["collections: missing"]


def test_a_response_edited_after_finalize_reads_finalize_pending(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _response(store)
    _fake_finalize(monkeypatch, {"status": "complete", "output_path": f"{BASE}/schema_output.json"})
    _run(monkeypatch, capsys, tmp_path, "--finalize")
    _response(store, {"collections": [{"name": "changed"}]})

    _, status = _run(monkeypatch, capsys, tmp_path, "--status")
    assert status["status"] == "finalize_pending"


def test_a_complete_record_without_its_output_is_not_complete(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _response(store)
    _fake_finalize(monkeypatch, {"status": "complete", "output_path": f"{BASE}/schema_output.json"})
    _run(monkeypatch, capsys, tmp_path, "--finalize")
    (tmp_path / BASE / "schema_output.json").unlink()

    _, status = _run(monkeypatch, capsys, tmp_path, "--status")
    assert status["status"] == "not_finalized"


@pytest.mark.passes_on_main  # --status stays read-only
def test_status_writes_nothing(monkeypatch, capsys, tmp_path):
    _run(monkeypatch, capsys, tmp_path, "--status")
    assert not (tmp_path / DB / JOB / f"schema-{ENGINE}").exists()


@pytest.mark.parametrize(
    "engine", ["opensearch", "elasticache", "aurora_mysql", "aurora_postgresql"]
)
def test_every_single_pass_engine_has_a_status(monkeypatch, capsys, tmp_path, engine):
    code, status = _run(monkeypatch, capsys, tmp_path, "--status", engine=engine)
    assert code == 0 and status["status"] == "not_finalized"


def test_a_hand_edit_of_the_output_after_finalize_reads_finalize_pending(
    monkeypatch, capsys, tmp_path
):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _response(store)
    _fake_finalize(monkeypatch, {"status": "complete", "output_path": f"{BASE}/schema_output.json"})
    _run(monkeypatch, capsys, tmp_path, "--finalize")
    store.write_json(f"{BASE}/schema_output.json", {"validation_passed": True, "edited": 1})

    _, status = _run(monkeypatch, capsys, tmp_path, "--status")
    assert status["status"] == "finalize_pending"


# --- The real --finalize, unmocked, on a deterministic wordpress run (#1294 review) ---


def _wordpress_run(directory: Path) -> None:
    from tests.contract.test_declared_artifacts import _deterministic_run

    _deterministic_run("wordpress", directory)


def _cli(cwd: Path, root: Path, job: str, *args: str) -> tuple[int, dict]:
    import os
    import subprocess  # nosec B404 -- runs this repo's own script with fixed argv

    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.pop("MODERNIZER_CI_SANDBOX", None)
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    script = Path(run_schema_design.__file__)
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script
        [
            sys.executable,
            str(script),
            "--job-id",
            job,
            "--db",
            DB,
            "--engine",
            "aurora_mysql",
            "--artifact-root",
            str(root),
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    lines = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")]
    return proc.returncode, (lines[-1] if lines else {})


@pytest.fixture
def wordpress_job(tmp_path_factory, tmp_path):
    from tests.shared_runs import cached_run, copy_run

    cached = cached_run(tmp_path_factory, "declared-artifacts-none-wordpress", _wordpress_run)
    run = copy_run(cached, tmp_path / "run")
    root = run / "artifacts"
    [job] = [p.name for p in (root / DB).iterdir() if p.is_dir() and not p.name.startswith(".")]
    code, request = _cli(run, root, job)
    assert code == 0 and request["status"] == "awaiting_llm", request
    return run, root, job


def _answer(root: Path, job: str, body: dict) -> None:
    path = root / DB / job / "llm_responses" / "schema_design_aurora_mysql.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


def test_a_real_valid_finalize_then_status_reads_complete(wordpress_job):
    run, root, job = wordpress_job
    _answer(root, job, {"delta_version": "1.0"})  # an empty design delta
    assert _cli(run, root, job, "--finalize")[1]["status"] == "complete"

    code, status = _cli(run, root, job, "--status")
    assert code == 0 and status["status"] == "complete"


def test_a_real_invalid_finalize_then_status_reads_its_errors(wordpress_job):
    run, root, job = wordpress_job
    _answer(root, job, {})
    _, final = _cli(run, root, job, "--finalize")
    assert final["status"] == "validation_failed"

    _, status = _cli(run, root, job, "--status")
    assert status["status"] == "validation_failed"
    assert status["errors"] == final["errors"]


def test_a_finalize_with_no_response_crashes_and_status_reads_not_finalized(wordpress_job):
    run, root, job = wordpress_job
    code, _ = _cli(run, root, job, "--finalize")
    assert code != 0  # the response file the subagent should have written is missing

    _, status = _cli(run, root, job, "--status")
    assert status["status"] == "not_finalized"
