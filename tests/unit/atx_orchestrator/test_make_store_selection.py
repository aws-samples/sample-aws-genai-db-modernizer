"""make_store() picks the ATX backend when STORAGE_BACKEND=atx, else S3/local."""

from __future__ import annotations

import pytest

from src.atx_orchestrator import core
from src.atx_orchestrator.runtime.atx_store import TransformAtxStore


def test_atx_backend_selected_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "atx")
    monkeypatch.setattr(
        "src.atx_orchestrator.runtime.atx_store.TransformAtxStore.__init__",
        lambda self, *a, **k: None,
    )

    def _must_not_be_called(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("factory must not be consulted on the ATX path")

    monkeypatch.setattr("src.storage.create_artifact_store", _must_not_be_called)
    store = core.make_store()
    assert isinstance(store, TransformAtxStore)


def test_local_backend_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    from src.atx_orchestrator.runtime.store import TransformLocalStore

    assert isinstance(core.make_store(), TransformLocalStore)
