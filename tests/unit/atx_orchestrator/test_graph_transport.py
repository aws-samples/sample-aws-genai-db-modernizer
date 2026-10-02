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

    def test_pointer_records_sha256_of_published_bytes(self, tmp_path) -> None:
        # R2: the pointer must carry a content digest so the download side can
        # verify integrity before the native parser opens the file.
        import hashlib

        store, _sdk = _store()
        data = b"\x00GRAPH-BYTES\x01"
        lbug = tmp_path / "context.lbug"
        lbug.write_bytes(data)

        with patch(
            "src.atx_orchestrator.runtime.artifacts.publish",
            return_value={graph_transport._GRAPH_LABEL: "graph-art-1"},
        ):
            graph_transport.publish_graph(store, "mydb", "job-1", str(lbug))

        pointer = store.read_json(graph_transport.pointer_key("mydb", "job-1"))
        assert pointer["sha256"] == hashlib.sha256(data).hexdigest()

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

    def test_download_verifies_sha256_on_round_trip(self, tmp_path) -> None:
        # The full publish->download path records and re-verifies the digest, so
        # a clean round trip passes the integrity gate.
        store, sdk = _store()
        src = tmp_path / "context.lbug"
        src.write_bytes(b"\x00VERIFIED\x01")

        def _fake_publish(items):
            content = items[0][0]
            sdk._by_id["graph-art-1"] = ("Assessment Context Graph", "CUSTOMER_OUTPUT", content)
            return {graph_transport._GRAPH_LABEL: "graph-art-1"}

        with patch("src.atx_orchestrator.runtime.artifacts.publish", side_effect=_fake_publish):
            graph_transport.publish_graph(store, "mydb", "job-1", str(src))

        dest = tmp_path / "downloaded" / "context.lbug"
        assert graph_transport.download_graph(store, "mydb", "job-1", str(dest)) is True
        assert dest.read_bytes() == b"\x00VERIFIED\x01"

    def test_corrupt_download_fails_integrity_and_routes_to_rebuild(self, tmp_path) -> None:
        # R2 core case: the published bytes are tampered between publish and
        # download. The recorded sha256 no longer matches, so download_graph must
        # return False (caller rebuilds from contracts) and must NOT leave the bad
        # bytes on disk for the native GraphStore parser to open.
        store, sdk = _store()
        src = tmp_path / "context.lbug"
        src.write_bytes(b"\x00GOOD-GRAPH\x01")

        # publish_graph records sha256(GOOD) in the pointer...
        with patch(
            "src.atx_orchestrator.runtime.artifacts.publish",
            return_value={graph_transport._GRAPH_LABEL: "graph-art-1"},
        ):
            graph_transport.publish_graph(store, "mydb", "job-1", str(src))

        # ...but the stored artifact bytes are hostile/corrupt (digest won't match).
        sdk._by_id["graph-art-1"] = (
            "Assessment Context Graph",
            "CUSTOMER_OUTPUT",
            b"TAMPERED-NOT-A-REAL-LBUG",
        )

        dest = tmp_path / "downloaded" / "context.lbug"
        assert graph_transport.download_graph(store, "mydb", "job-1", str(dest)) is False
        assert not dest.exists()

    def test_download_allows_legacy_pointer_without_sha256(self, tmp_path) -> None:
        # Pointers written before the digest field existed carry no "sha256".
        # Those are allowed through unverified so older jobs keep working — the
        # graph is best-effort and non-authoritative.
        store, sdk = _store()
        sdk._by_id["legacy-1"] = ("Assessment Context Graph", "CUSTOMER_OUTPUT", b"\x00LEGACY\x01")
        store.write_json(
            graph_transport.pointer_key("mydb", "job-1"),
            {"artifact_id": "legacy-1", "label": graph_transport._GRAPH_LABEL, "bytes": 8},
        )

        dest = tmp_path / "downloaded" / "context.lbug"
        assert graph_transport.download_graph(store, "mydb", "job-1", str(dest)) is True
        assert dest.read_bytes() == b"\x00LEGACY\x01"

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


class TestBuildAndPublishGraph:
    """build_and_publish_graph rebuilds the graph from the job's contract JSON
    (via the real GraphStore + rebuild_graph) and publishes the .lbug + pointer.
    Uses a real LadybugDB graph over the fake store, so it proves the whole
    boundary path end to end."""

    def _seed_contracts(self, store) -> None:
        # Minimal collector + assignment contracts, written as STATE JSON the way
        # a real phase would. rebuild_graph reads these by key.
        store.write_json(
            "mydb/job-1/collector/output.json",
            {
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT * FROM orders WHERE id = ?",
                            "query_type": "SELECT",
                            "tables_accessed": ["orders"],
                            "calls_per_second": 10.0,
                        }
                    ]
                }
            },
        )
        store.write_json(
            "mydb/job-1/assignment/v1/assignment.json",
            {
                "query_assignments": [
                    {
                        "query_id": "q1",
                        "assigned_engine": "dynamodb",
                        "confidence": 0.9,
                        "source_tables": ["orders"],
                        "assignment_reason": "kv",
                        "in_scope": True,
                    }
                ],
                "table_assignments": [],
                "co_dependency_groups": [],
            },
        )

    def test_builds_from_contracts_publishes_and_pointer(self) -> None:
        store, sdk = _store()
        self._seed_contracts(store)

        captured: dict[str, bytes] = {}

        def _fake_publish(items):
            content = items[0][0]
            captured["bytes"] = content
            sdk._by_id["graph-art-1"] = ("Assessment Context Graph", "CUSTOMER_OUTPUT", content)
            return {graph_transport._GRAPH_LABEL: "graph-art-1"}

        with patch("src.atx_orchestrator.runtime.artifacts.publish", side_effect=_fake_publish):
            aid = graph_transport.build_and_publish_graph(store, "mydb", "job-1")

        assert aid == "graph-art-1"
        # A non-empty .lbug was built and handed to publish().
        assert captured["bytes"] and len(captured["bytes"]) > 0
        # Pointer recorded.
        pointer = store.read_json(graph_transport.pointer_key("mydb", "job-1"))
        assert pointer["artifact_id"] == "graph-art-1"

    def test_build_failure_is_swallowed(self) -> None:
        # rebuild_graph blowing up must not raise out of build_and_publish_graph.
        store, _sdk = _store()
        with patch("src.graph.populators.rebuild_graph", side_effect=RuntimeError("boom")):
            assert graph_transport.build_and_publish_graph(store, "mydb", "job-1") is None
