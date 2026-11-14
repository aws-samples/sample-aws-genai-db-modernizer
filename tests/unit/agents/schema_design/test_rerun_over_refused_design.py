# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A rerun of the schema design replaces a saved design its read refuses (#2199 review).

The refusal tells the user to rerun the engine's schema design. Each path that starts a
design with a model (the external request, Bedrock, the split) sets the refused file
aside first: renamed ``schema_output.json.refused`` in the same folder, never deleted,
with only its path and the refused field's position logged. The rerun then runs as if
there were no design.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess  # nosec B404 -- runs the repo's own script
import sys
from pathlib import Path

import pytest

from src.agents.schema_design import handler
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[4]
RUN = REPO / "tests/fixtures/real_runs/wordpress-both/job"
DB, JOB, ENGINE, VERSION = "wordpress", "099e6873", "aurora_mysql", 2
KEY = f"{DB}/{JOB}/schema-{ENGINE}/v{VERSION}/schema_output.json"
INJECTED = "CREATE INDEX `args` ON `wp_actionscheduler_actions` (`args`); DROP TABLE `wp_users`;"


def _job(tmp_path: Path) -> tuple[LocalArtifactStore, bytes, dict]:
    """The wordpress run's job with one injected index in its Aurora MySQL design; the
    refused file's bytes and the clean design."""
    shutil.copytree(RUN, tmp_path / DB / JOB)
    path = tmp_path / KEY
    clean = json.loads(path.read_text(encoding="utf-8"))
    design = json.loads(json.dumps(clean))
    old = design["table_definitions"][0]["indexes"][0]
    design["table_definitions"][0]["indexes"][0] = INJECTED
    design["generated_ddl"] = design["generated_ddl"].replace(old, INJECTED, 1)
    path.write_text(json.dumps(design), encoding="utf-8")
    return LocalArtifactStore(str(tmp_path)), path.read_bytes(), clean


def _kept(tmp_path: Path, refused: bytes) -> None:
    assert (tmp_path / f"{KEY}.refused").read_bytes() == refused


def test_the_external_rerun_sets_the_refused_design_aside_and_asks_for_a_design(
    tmp_path: Path,
) -> None:
    _, refused, _ = _job(tmp_path)
    proc = subprocess.run(  # nosec B603 -- fixed argv, the repo's own script
        [sys.executable, "scripts/run_schema_design.py", "--job-id", JOB, "--db", DB,
         "--artifact-root", str(tmp_path), "--engine", ENGINE, "--llm-mode", "external",
         "--assignment-version", str(VERSION)],
        cwd=REPO, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "awaiting_llm" in proc.stdout
    assert "Traceback" not in proc.stdout + proc.stderr
    _kept(tmp_path, refused)
    assert not (tmp_path / KEY).exists()
    assert "DROP TABLE" not in proc.stdout + proc.stderr


def test_the_bedrock_rerun_replaces_the_refused_design(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, refused, clean = _job(tmp_path)
    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(
        handler, "_dispatch_schema_agent", lambda *a, **k: (json.dumps(clean), None)
    )
    with caplog.at_level(logging.WARNING, logger=handler.__name__):
        handler.run_schema_design(JOB, DB, ENGINE, store, VERSION, llm_mode="bedrock")
    _kept(tmp_path, refused)
    assert store.read_json(KEY)["table_definitions"][0]["indexes"][0] != INJECTED
    [line] = [r.getMessage() for r in caplog.records if ".refused" in r.getMessage()]
    assert "table_definitions[0].indexes[0]" in line and "DROP" not in line


def test_the_split_rerun_sets_the_refused_design_aside(tmp_path: Path) -> None:
    store, refused, _ = _job(tmp_path)
    handler.run_schema_split(JOB, DB, ENGINE, store, VERSION)
    _kept(tmp_path, refused)
    assert not store.exists(KEY)


def test_a_second_refusal_keeps_the_first_refused_copy(tmp_path: Path) -> None:
    store, refused, _ = _job(tmp_path)
    assert handler.set_aside_refused_design(store, DB, JOB, ENGINE, VERSION) == f"{KEY}.refused"
    (tmp_path / KEY).write_bytes(refused + b" ")
    assert handler.set_aside_refused_design(store, DB, JOB, ENGINE, VERSION) == (f"{KEY}.refused.2")
    _kept(tmp_path, refused)
    assert (tmp_path / f"{KEY}.refused.2").read_bytes() == refused + b" "


@pytest.mark.passes_on_main
def test_a_design_that_reads_is_left_where_it_is(tmp_path: Path) -> None:
    shutil.copytree(RUN, tmp_path / DB / JOB)
    store = LocalArtifactStore(str(tmp_path))
    before = (tmp_path / KEY).read_bytes()
    assert handler.clear_design_skip(store, DB, JOB, ENGINE, VERSION) is False
    assert (tmp_path / KEY).read_bytes() == before
    assert not (tmp_path / f"{KEY}.refused").exists()
