"""Every local entry point that takes a database name must reject a
malformed or path-traversing one before writing any artifact, not only
under MODERNIZER_CI_SANDBOX=1 (#420). scripts/_sandbox.py already covers the
headless-CI case (tests/unit/scripts/test_sandbox.py); this file exercises
the general-purpose src.shared.database_name.validate_database_name check
each scripts/run_*.py script now runs on a plain, unsandboxed invocation.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
import zipfile
from pathlib import Path

import pytest

from src.tools.database.collection_text import extract_json_object

REPO_ROOT = Path(__file__).resolve().parents[3]

# One safe argv per script, with "--db", "<value>" always last so a test can
# swap the value in.
_SCRIPT_ARGS: dict[str, list[str]] = {
    "run_triage.py": ["--job-id", "j1"],
    "run_assignment.py": ["--job-id", "j1"],
    "run_reality_check.py": ["--job-id", "j1"],
    "run_synthesis.py": ["--job-id", "j1"],
    "run_report.py": ["--job-id", "j1"],
    "run_analysis.py": ["--job-id", "j1", "--engine", "dynamodb"],
    "run_schema_design.py": ["--job-id", "j1", "--engine", "dynamodb"],
    "run_assessment.py": ["--job-id", "j1"],
}

_INVALID_NAMES = ["../escape", "..", "a/b", "a b", "", "café"]


def _status_line(stdout: str) -> dict:
    """Return the first JSON stdout line that carries a ``status`` key.

    Most scripts print exactly one JSON line. run_assessment.py's compact
    output (stdout isn't a terminal under subprocess) prints the phase status
    line and then a trailing ``{"log": ..., "output_tail": ...}`` pointer, so
    this skips straight to the status line."""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "status" in data:
            return data
    raise AssertionError(f"no JSON line with a 'status' key in stdout:\n{stdout}")


def _run(script: str, args: list[str], artifact_root: Path):
    """Run ``script`` with ``args`` plus an explicit ``--artifact-root`` under
    ``artifact_root`` (every script accepts the flag; not all of them also
    read $ARTIFACT_DIR, so the flag is what keeps this test off the real repo
    tree)."""
    env = {k: v for k, v in os.environ.items() if k != "MODERNIZER_CI_SANDBOX"}
    return subprocess.run(  # nosec B603 -- fixed argv, repo-owned script
        [sys.executable, f"scripts/{script}", *args, "--artifact-root", str(artifact_root)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("script,base_args", list(_SCRIPT_ARGS.items()))
@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_rejects_invalid_db_outside_sandbox(
    script: str, base_args: list[str], bad_name: str, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run(script, [*base_args, "--db", bad_name], artifact_root)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "database_name" in out["message"]
    # Nothing should have been written under the artifact root.
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_rejects_path_traversal_before_writing_anything(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_triage.py", ["--job-id", "j1", "--db", "../../outside"], artifact_root)
    assert proc.returncode != 0
    assert not any(tmp_path.rglob("outside"))


def test_run_collect_rejects_bad_explicit_db(tmp_path: Path) -> None:
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(json.dumps({"collection_version": "1.0", "queries": []}))
    artifact_root = tmp_path / "artifacts"
    proc = _run(
        "run_collect.py",
        ["--file", str(collector_file), "--db", "../escape"],
        artifact_root,
    )
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "database_name" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def _wordpress_as(tmp_path: Path, file_name: str, db_in_file: str | None = None) -> Path:
    """The wordpress sample collection written under ``file_name``. The sample's
    own metadata names ``wordpress``; ``db_in_file`` replaces that name, and ``""``
    removes it."""
    with zipfile.ZipFile(REPO_ROOT / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        text = z.read("wordpress-collection.json").decode()
    if db_in_file is not None:
        data = json.loads(extract_json_object(text))
        meta = dict(data.get("metadata") or {})
        meta.pop("database_name", None)
        if db_in_file:
            meta["database_name"] = db_in_file
        data["metadata"] = meta
        text = "collection_output\n" + json.dumps(data)
    path = tmp_path / file_name
    path.write_text(text)
    return path


def _collect_line(stdout: str) -> dict:
    for line in stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("phase", "collect") == "collect":
            if "database_name" in data:
                return data
    raise AssertionError(f"no collect status line with a database_name:\n{stdout}")


@pytest.mark.parametrize("script", ["run_collect.py", "run_assessment.py"])
@pytest.mark.parametrize(
    ("file_name", "db_in_file", "expected", "source"),
    [
        # a valid metadata name wins over the file name
        ("my.app-collection.json", None, "wordpress", "metadata"),
        ("sales data.json", "Sales_DB", "Sales_DB", "metadata"),
        # hostile, empty-after-strip or invalid metadata names fall back to the file name
        ("my.app-collection.json", "../evil", "my_app", "file_name"),
        ("sales data.json", "has space", "sales_data", "file_name"),
        ("caf\u00e9-collection.json", "caf\u00e9", "caf_", "file_name"),
        ("wordpress-collection.json", "a" * 129, "wordpress", "file_name"),
        # no metadata name at all
        ("sales data.json", "", "sales_data", "file_name"),
    ],
)
def test_no_db_uses_valid_metadata_name_else_the_file_name(
    tmp_path: Path, script: str, file_name: str, db_in_file: str | None, expected: str, source: str
) -> None:
    """#1096: with no --db a valid metadata name is kept (backward compatible);
    anything else falls back to the name derived from the file name, so a
    hostile name never reaches an artifact path and never fails the run. The
    collect status line says which source was used."""
    collector_file = _wordpress_as(tmp_path, file_name, db_in_file=db_in_file)
    artifact_root = tmp_path / "artifacts"
    args = ["--file", str(collector_file)]
    if script == "run_assessment.py":
        args += ["--llm-mode", "none"]
    proc = _run(script, args, artifact_root)
    line = _collect_line(proc.stdout)
    assert line["status"] == "complete"
    assert line["database_name"] == expected
    assert line["database_name_source"] == source
    assert (artifact_root / expected / line["job_id"] / "collector" / "output.json").is_file()
    assert [p.name for p in artifact_root.iterdir()] == [expected]


@pytest.mark.parametrize("script", ["run_collect.py", "run_assessment.py"])
def test_explicit_db_wins_over_the_file_name(tmp_path: Path, script: str) -> None:
    collector_file = _wordpress_as(tmp_path, "my.app-collection.json")
    artifact_root = tmp_path / "artifacts"
    args = ["--file", str(collector_file), "--db", "chosen"]
    if script == "run_assessment.py":
        args += ["--llm-mode", "none"]
    proc = _run(script, args, artifact_root)
    line = _collect_line(proc.stdout)
    assert line["database_name"] == "chosen"
    assert line["database_name_source"] == "explicit"
    assert [p.name for p in artifact_root.iterdir()] == ["chosen"]


def test_accepts_valid_db(tmp_path: Path) -> None:
    """A valid --db is not rejected by the new check (it still fails later,
    for the unrelated reason that there's no such job -- proving the
    validator itself let it through)."""
    proc = _run("run_triage.py", ["--job-id", "j1", "--db", "wordpress"], tmp_path)
    out = _status_line(proc.stdout)
    assert "database_name must contain" not in out.get("message", "")


def test_strips_trailing_newline_and_uses_the_stripped_value(tmp_path: Path) -> None:
    """A --db with a trailing newline is accepted (stripped), and the
    stripped value -- not the original with the newline -- is what ends up
    in the artifact path. Observed through run_triage.py's "not found"
    message, which embeds the (job-less) collector path it looked for."""
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_triage.py", ["--job-id", "j1", "--db", "abc\n"], artifact_root)
    out = _status_line(proc.stdout)
    assert "database_name must contain" not in out.get("message", "")
    assert "abc/j1/collector/output.json" in out["message"]
    assert "abc\n" not in out["message"]


def _acme(tmp_path: Path) -> Path:
    """The reviewer's case: acme-prod-collection.json whose metadata says acme."""
    return _wordpress_as(tmp_path, "acme-prod-collection.json", db_in_file="acme")


@pytest.mark.parametrize("script", ["run_collect.py", "run_assessment.py"])
@pytest.mark.parametrize(
    ("flag", "expected", "source"),
    [
        # a direct call keeps the metadata-first behaviour
        ([], "acme", "metadata"),
        # /modernize and /collect pass the flag and name the folder from the file
        (["--db-from-file-name"], "acme-prod", "file_name"),
    ],
)
def test_acme_prod_direct_call_vs_file_name_flag(
    tmp_path: Path, script: str, flag: list[str], expected: str, source: str
) -> None:
    artifact_root = tmp_path / "artifacts"
    args = ["--file", str(_acme(tmp_path)), *flag]
    if script == "run_assessment.py":
        args += ["--llm-mode", "none"]
    proc = _run(script, args, artifact_root)
    line = _collect_line(proc.stdout)
    assert line["database_name"] == expected
    assert line["database_name_source"] == source
    assert [p.name for p in artifact_root.iterdir()] == [expected]


@pytest.mark.parametrize("script", ["run_collect.py", "run_assessment.py"])
def test_db_and_db_from_file_name_conflict(tmp_path: Path, script: str) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run(
        script,
        ["--file", str(_acme(tmp_path)), "--db", "x", "--db-from-file-name"],
        artifact_root,
    )
    assert proc.returncode != 0
    assert _status_line(proc.stdout)["status"] == "error"
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


@pytest.mark.parametrize("script", ["run_collect.py", "run_assessment.py"])
def test_schema_name_is_reported_as_its_own_source(tmp_path: Path, script: str) -> None:
    path = _wordpress_as(tmp_path, "shop-collection.json", db_in_file="")
    text = path.read_text()
    data = json.loads(text[text.index("{") :])
    data["metadata"]["schema_name"] = "shopdb"
    path.write_text("collection_output\n" + json.dumps(data))
    args = ["--file", str(path)]
    if script == "run_assessment.py":
        args += ["--llm-mode", "none"]
    line = _collect_line(_run(script, args, tmp_path / "artifacts").stdout)
    assert (line["database_name"], line["database_name_source"]) == ("shopdb", "schema_name")


@pytest.mark.parametrize("script", ["run_collect.py", "run_assessment.py"])
def test_leading_hyphen_db_is_rejected(tmp_path: Path, script: str) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run(script, ["--file", str(_acme(tmp_path)), "--db=-rf"], artifact_root)
    assert proc.returncode != 0
    assert "database_name" in _status_line(proc.stdout)["message"]
