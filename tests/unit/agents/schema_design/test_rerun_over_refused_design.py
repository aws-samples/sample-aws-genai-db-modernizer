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
from src.shared.contract_versioning import check_artifact
from src.storage.artifact_store import ArtifactStore
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


# --- Stores that cannot copy bytes or delete (S3, AWS Transform) -----------------------


class _MemoryStore(ArtifactStore):
    """A case-sensitive in-memory store with the local store's methods (it deletes)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def read_json(self, path: str) -> dict:
        if path not in self.files:
            raise FileNotFoundError(path)
        return check_artifact(path, json.loads(self.files[path]))  # type: ignore[no-any-return]

    def write_json(self, path: str, data: dict) -> None:
        self.files[path] = json.dumps(data).encode("utf-8")

    def read_bytes(self, path: str) -> bytes:
        return self.files[path]

    def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def exists(self, path: str) -> bool:
        return path in self.files

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self.files if k.startswith(prefix))

    def delete(self, path: str) -> bool:
        return self.files.pop(path, None) is not None


class _S3Like(_MemoryStore):
    """S3's method set: raw bytes, and the base class's delete, which deletes nothing."""

    delete = ArtifactStore.delete  # type: ignore[assignment]


class _AtxLike(_S3Like):
    """AWS Transform's method set: JSON only, no raw bytes, no delete."""

    def read_bytes(self, path: str) -> bytes:
        raise NotImplementedError("JSON only")

    def write_bytes(self, path: str, data: bytes) -> None:
        raise NotImplementedError("JSON only")


class _DeleteDoesNothing(_MemoryStore):
    """A delete that reports success and leaves the file."""

    def delete(self, path: str) -> bool:
        return True


def _loaded(store: _MemoryStore, tmp_path: Path) -> bytes:
    _job(tmp_path)
    for path in (tmp_path / DB / JOB).rglob("*.json"):
        store.files[str(path.relative_to(tmp_path))] = path.read_bytes()
    return store.files[KEY]


class _DeleteRaises(_MemoryStore):
    """A delete that is refused (S3's AccessDenied)."""

    def delete(self, path: str) -> bool:
        raise PermissionError("AccessDenied")


_KEPT = {
    _S3Like: ("copied the refused saved design", True),
    _DeleteDoesNothing: ("copied the refused saved design", True),
    _DeleteRaises: (f"copied it to {KEY}.refused, and the original stays", True),
    _AtxLike: ("this store cannot copy it", False),
}


@pytest.mark.parametrize("store_type", list(_KEPT), ids=lambda t: t.__name__)
def test_a_store_that_cannot_move_the_file_keeps_one_copy_at_most_and_claims_no_move(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, store_type: type[_MemoryStore]
) -> None:
    """Round 4: where the store can write a copy (S3) it keeps one, reused on every rerun;
    the original stays for the rerun to overwrite, and no log line says it was set aside."""
    store = store_type()
    refused = _loaded(store, tmp_path)
    wording, copied = _KEPT[store_type]
    with caplog.at_level(logging.WARNING, logger=handler.__name__):
        for _ in range(3):
            assert handler.set_aside_refused_design(store, DB, JOB, ENGINE, VERSION) is None
    assert store.files[KEY] == refused
    copies = [k for k in store.files if ".refused" in k]
    assert copies == ([f"{KEY}.refused"] if copied else [])
    assert all(store.files[k] == refused for k in copies)
    lines = [r.getMessage() for r in caplog.records if KEY in r.getMessage()]
    assert len(lines) == 3 and all(wording in line for line in lines)
    assert all(not line.startswith("Schema design: set aside") for line in lines)
    assert all("DROP" not in line for line in lines)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("store_type", [_S3Like, _AtxLike])
def test_a_rerun_on_a_store_that_cannot_set_aside_replaces_the_refused_design(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_type: type[_MemoryStore]
) -> None:
    store = store_type()
    _loaded(store, tmp_path)
    clean = json.loads((RUN / f"schema-{ENGINE}/v{VERSION}/schema_output.json").read_text())
    handler.run_schema_design(JOB, DB, ENGINE, store, VERSION, llm_mode="external")
    assert store.exists(f"{DB}/{JOB}/schema-{ENGINE}/v{VERSION}/llm_input.json")
    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(
        handler, "_dispatch_schema_agent", lambda *a, **k: (json.dumps(clean), None)
    )
    handler.run_schema_design(JOB, DB, ENGINE, store, VERSION, llm_mode="bedrock")
    assert store.read_json(KEY)["table_definitions"][0]["indexes"][0] != INJECTED


def test_a_run_without_a_model_sets_the_refused_design_aside_before_its_skip_record(
    tmp_path: Path,
) -> None:
    store, refused, _ = _job(tmp_path)
    handler.run_schema_design(JOB, DB, ENGINE, store, VERSION, llm_mode="none")
    _kept(tmp_path, refused)
    assert store.read_json(KEY)["status"] == "skipped"


class _TextOnly(_MemoryStore):
    """A store that deletes and reads and writes text, but has no raw bytes."""

    def read_bytes(self, path: str) -> bytes:
        raise NotImplementedError("text only")

    def write_bytes(self, path: str, data: bytes) -> None:
        raise NotImplementedError("text only")

    def read_text(self, path: str) -> str:
        return self.files[path].decode("utf-8")

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content.encode("utf-8")


class _NoRawRead(_TextOnly):
    read_text = None  # type: ignore[assignment]


def test_a_store_with_only_text_sets_aside_through_text(tmp_path: Path) -> None:
    store = _TextOnly()
    refused = _loaded(store, tmp_path)
    assert handler.set_aside_refused_design(store, DB, JOB, ENGINE, VERSION) == f"{KEY}.refused"
    assert store.files[f"{KEY}.refused"] == refused and KEY not in store.files


def test_a_store_with_no_raw_read_keeps_the_file_and_moves_nothing(tmp_path: Path) -> None:
    store = _NoRawRead()
    refused = _loaded(store, tmp_path)
    assert handler.set_aside_refused_design(store, DB, JOB, ENGINE, VERSION) is None
    assert store.files[KEY] == refused
    assert not [k for k in store.files if ".refused" in k]


class _Broken(_MemoryStore):
    def exists(self, path: str) -> bool:
        raise RuntimeError("store unavailable")


def test_setting_aside_never_fails_the_run(tmp_path: Path) -> None:
    store = _Broken()
    _loaded(store, tmp_path)
    assert handler.set_aside_refused_design(store, DB, JOB, ENGINE, VERSION) is None
