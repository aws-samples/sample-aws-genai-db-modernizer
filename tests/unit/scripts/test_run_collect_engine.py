"""run_collect records the detected source engine (regression: hardcoded mysql)."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own run_collect.py script with fixed argv
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _unzip(name: str, dest: Path) -> Path:
    zip_path = REPO / "docs" / "examples" / name / f"{name}.zip"
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    return dest / f"{name}-collection.json"


def test_discourse_is_collected_as_postgresql(tmp_path: Path) -> None:
    src = _unzip("discourse", tmp_path / "in")
    root = tmp_path / "artifacts"
    out = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(src),
            "--db",
            "discourse",
            "--job-id",
            "eng00001",
            "--artifact-root",
            str(root),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1])["status"] == "complete"
    collector = json.loads(
        (root / "discourse" / "eng00001" / "collector" / "output.json").read_text()
    )
    assert collector["metadata"]["source_database"]["engine"] == "postgresql"
