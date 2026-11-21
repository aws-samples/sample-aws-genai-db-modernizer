# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""make_store() gives the core a text-capable local store when no S3 bucket is set."""

from __future__ import annotations

import pytest

from src.orchestrator import core


def test_local_backend_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    from src.storage.text_store import TextLocalStore

    store = core.make_store()
    assert isinstance(store, TextLocalStore)
    store.write_text("db/job/analysis-dynamodb/er-diagram.mmd", "erDiagram")
    assert (tmp_path / "db/job/analysis-dynamodb/er-diagram.mmd").read_text() == "erDiagram"
