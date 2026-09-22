"""Tests for the ATX .lbug publish + STATE-pointer discovery round-trip.

graph_transport bridges the gap that a binary .lbug cannot go through the
JSON-only ATX STATE store and that publish()'s artifact-id map is per-process:
publish_graph uploads the .lbug EXTERNAL and records a STATE JSON pointer;
download_graph reads that pointer and fetches the bytes by id. These prove the
pointer is written/discovered and the bytes round-trip, and that both sides
degrade gracefully when publishing is unavailable.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

from src.atx_orchestrator.runtime import graph_transport
from src.atx_orchestrator.runtime.atx_store import TransformAtxStore
from src.storage.artifact_store import ArtifactStore

# Reuse the fake SDK store the atx_store tests already model the backend with.
from tests.unit.atx_orchestrator.test_atx_store import _FakeSdkStore


def _store() -> tuple[TransformAtxStore, _FakeSdkStore]:
    sdk = _FakeSdkStore()
    return TransformAtxStore(sdk_store=sdk, agent_instance_id="inst1"), sdk


class TestPointerKey:
    def test_pointer_key_layout(self) -> None:
        assert (
            graph_transport.pointer_key("mydb", "job-1")
            == "mydb/job-1/graph/context.lbug.pointer.json"
        )


class TestPublishGraph:
    def test_publishes_and_writes_pointer(self, tmp_path) -> None:
        store, _sdk = _store()
        lbug = tmp_path / "context.lbug"
        lbug.write_bytes(b"\x00GRAPH-BYTES\x01")

        # Simulate a successful ATX publish returning an artifact id for the label.
        with patch(
            "src.atx_orchestrator.runtime.artifacts.publish",
            return_value={graph_transport._GRAPH_LABEL: "graph-art-1"},
        ) as mock_pub:
            aid = graph_transport.publish_graph(store, "mydb", "job-1", str(lbug))

        assert aid == "graph-art-1"
        # publish() got bytes, OTHER, EXTERNAL CUSTOMER_OUTPUT, with a .lbug name.
        items = mock_pub.call_args.args[0]
        content, file_type, label, category, path = items[0]
        assert content == b"\x00GRAPH-BYTES\x01"
        assert file_type == "OTHER"
        assert category == "CUSTOMER_OUTPUT"
        assert path.endswith(".lbug")
        # The STATE pointer was written and records the id.
        pointer = store.read_json(graph_transport.pointer_key("mydb", "job-1"))
        assert pointer["artifact_id"] == "graph-art-1"

    def test_publish_unavailable_returns_none_no_pointer(self, tmp_path) -> None:
        store, _sdk = _store()
        lbug = tmp_path / "context.lbug"
        lbug.write_bytes(b"bytes")

        # publish() returns {} outside the ATX runtime / on failure — never raises.
        with patch("src.atx_orchestrator.runtime.artifacts.publish", return_value={}):
            aid = graph_transport.publish_graph(store, "mydb", "job-1", str(lbug))

        assert aid is None
        assert not store.exists(graph_transport.pointer_key("mydb", "job-1"))


class TestDownloadGraph:
    def test_round_trip_publish_then_download(self, tmp_path) -> None:
        store, sdk = _store()
        src = tmp_path / "context.lbug"
        src.write_bytes(b"\x00ROUND-TRIP\x01")

        # publish_graph normally calls the real publish(); here fake it, but land
        # the bytes in the SDK by the returned id so download can fetch them.
        def _fake_publish(items):
            content = items[0][0]
            sdk._by_id["graph-art-1"] = ("Assessment Context Graph", "CUSTOMER_OUTPUT", content)
            return {graph_transport._GRAPH_LABEL: "graph-art-1"}

        with patch("src.atx_orchestrator.runtime.artifacts.publish", side_effect=_fake_publish):
            graph_transport.publish_graph(store, "mydb", "job-1", str(src))

        dest = tmp_path / "downloaded" / "context.lbug"
        ok = graph_transport.download_graph(store, "mydb", "job-1", str(dest))

        assert ok is True
        assert dest.read_bytes() == b"\x00ROUND-TRIP\x01"

    def test_download_returns_false_without_pointer(self, tmp_path) -> None:
        store, _sdk = _store()
        dest = tmp_path / "context.lbug"
        assert graph_transport.download_graph(store, "mydb", "job-1", str(dest)) is False
        assert not dest.exists()

    def test_download_returns_false_when_store_cannot_download_by_id(self, tmp_path) -> None:
        # A store without download_artifact_to (e.g. a plain non-ATX store) is not
        # this module's responsibility — return False so the caller rebuilds.
        class _NoById:
            def __init__(self) -> None:
                self._p: dict = {}

            def exists(self, key: str) -> bool:
                return key in self._p

            def read_json(self, key: str) -> dict:
                result: dict = self._p[key]
                return result

            def write_json(self, key: str, data: dict) -> None:
                self._p[key] = data

        store = _NoById()
        store.write_json(graph_transport.pointer_key("mydb", "job-1"), {"artifact_id": "x"})
        dest = tmp_path / "context.lbug"
        assert (
            graph_transport.download_graph(cast(ArtifactStore, store), "mydb", "job-1", str(dest))
            is False
        )
