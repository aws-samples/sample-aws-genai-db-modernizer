"""``--engines-in-use`` records the engines a source runs today in the collection (#963)."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own run_collect.py script with fixed argv
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SAMPLES = ("wordpress", "discourse", "trailgear")


def _unzip(name: str, dest: Path) -> Path:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        z.extractall(dest)
    return dest / f"{name}-collection.json"


def _collect(src: Path, root: Path, *extra: str, job: str = "eng00001") -> tuple[int, dict, str]:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(src),
            "--db",
            "sample",
            "--job-id",
            job,
            "--artifact-root",
            str(root),
            *extra,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    out = proc.stdout.strip().splitlines()
    return proc.returncode, (json.loads(out[-1]) if out else {}), proc.stderr


def _metadata(root: Path, job: str = "eng00001") -> dict:
    stored = json.loads((root / "sample" / job / "collector" / "output.json").read_text())
    metadata: dict = stored["metadata"]
    return metadata


@pytest.mark.parametrize("name", SAMPLES)
def test_a_bundled_sample_declares_nothing_so_the_collection_is_unchanged(
    tmp_path: Path, name: str
) -> None:
    src = _unzip(name, tmp_path / "in")
    code, status, _ = _collect(src, tmp_path / "a")
    assert code == 0 and status["status"] == "complete"
    assert _metadata(tmp_path / "a")["engines_in_use"] == []


def test_the_option_records_the_engine_on_a_raw_collection(tmp_path: Path) -> None:
    src = _unzip("wordpress", tmp_path / "in")
    code, _, _ = _collect(src, tmp_path / "a", "--engines-in-use", "opensearch,ElastiCache")
    assert code == 0
    assert _metadata(tmp_path / "a")["engines_in_use"] == ["opensearch", "elasticache"]


def test_the_option_adds_to_what_the_file_already_declares(tmp_path: Path) -> None:
    src = _unzip("wordpress", tmp_path / "in")
    raw = json.loads(src.read_text()[src.read_text().index("{") :])
    raw["metadata"]["engines_in_use"] = ["documentdb"]
    declared = tmp_path / "declared.json"
    declared.write_text(json.dumps(raw))
    code, _, _ = _collect(declared, tmp_path / "a", "--engines-in-use", "opensearch,documentdb")
    assert code == 0
    assert _metadata(tmp_path / "a")["engines_in_use"] == ["documentdb", "opensearch"]


def test_the_option_records_the_engine_on_an_already_processed_collection(tmp_path: Path) -> None:
    src = _unzip("wordpress", tmp_path / "in")
    assert _collect(src, tmp_path / "a")[0] == 0
    processed = tmp_path / "a" / "sample" / "eng00001" / "collector" / "output.json"
    code, _, _ = _collect(
        processed, tmp_path / "b", "--engines-in-use", "opensearch", job="eng00002"
    )
    assert code == 0
    assert _metadata(tmp_path / "b", "eng00002")["engines_in_use"] == ["opensearch"]


def test_an_unknown_engine_fails_before_a_job_is_written(tmp_path: Path) -> None:
    src = _unzip("wordpress", tmp_path / "in")
    code, status, _ = _collect(src, tmp_path / "a", "--engines-in-use", "redis")
    assert code != 0
    assert "redis" in json.dumps(status)
    assert not (tmp_path / "a" / "sample").exists()


def test_run_assessment_records_the_declared_engines(tmp_path: Path) -> None:
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    src = _unzip("wordpress", tmp_path / "in")
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(
        str(src), "sample", store, engines_in_use=["opensearch"]
    )
    stored = json.loads(
        (tmp_path / "artifacts" / db / job_id / "collector" / "output.json").read_text()
    )
    assert stored["metadata"]["engines_in_use"] == ["opensearch"]


def test_run_assessment_rejects_an_unknown_engine(tmp_path: Path) -> None:
    src = _unzip("wordpress", tmp_path / "in")
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_assessment.py",
            "--file",
            str(src),
            "--engines-in-use",
            "redis",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "unknown engine(s) in --engines-in-use: redis" in proc.stderr


def test_the_legacy_aurora_id_is_refused_by_the_script(tmp_path: Path) -> None:
    src = _unzip("wordpress", tmp_path / "in")
    code, status, _ = _collect(src, tmp_path / "a", "--engines-in-use", "aurora")
    assert code != 0
    assert "aurora_mysql" in json.dumps(status)
    assert not (tmp_path / "a" / "sample").exists()
