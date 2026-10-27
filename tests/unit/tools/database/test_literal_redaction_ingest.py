"""No collected literal value reaches an artifact or a result (#1625).

SQL Server, Oracle and Db2 collections keep the text of real executions. Every ingest
path redacts their query text, tags and definitions before anything is stored: the
offline parser, the scripts' copy of the raw file, a collector output handed in as
the collection, a stored collector output read back (an older job's included), and
the contract however it is built (a live collector). ``query_id``s do not change, and
routing reads the redacted text as it read the collected one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.shared.contract_versioning import load_contract
from src.storage.local_store import LocalArtifactStore
from src.tools.database.offline_parser import parse_offline_collection, rederive_query_facts
from src.tools.sql.redact import redact_raw_collection
from src.tools.sql.shadow import TableShadow, write_shadow_artifact
from tests.fixtures.pii_collections import (
    ENGINES,
    keep_literals,
    leaked,
    pii_collection,
    pii_texts,
)

DB = "trailgear"


def _collector_output(engine: str) -> dict[str, Any]:
    parsed = parse_offline_collection(pii_collection(engine))
    return build_offline_collector_output(parsed, job_id="job1", database_name=DB, s3_key="x")


def _old_collector_output(engine: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A collector output written before #1625, literals and all."""
    with monkeypatch.context() as patch:
        keep_literals(patch)
        old = _collector_output(engine)
    assert leaked(json.dumps(old)), "the old output should hold the seeded values"
    return old


@pytest.mark.parametrize("engine", ENGINES)
class TestOfflineIngest:
    def test_the_collector_output_holds_no_seeded_value(self, engine: str) -> None:
        out = _collector_output(engine)
        assert not leaked(json.dumps(out))
        texts = [q["query_text"] for q in out["queries"]["query_patterns"]]
        assert any("?" in t or ":1" in t for t in texts)

    def test_definitions_keep_their_quotes(self, engine: str) -> None:
        schema = _collector_output(engine)["database_schema"]
        views = schema["views"]
        assert "WHERE Email = '?'" in views[0]["definition"]
        assert schema["procedures"][0]["definition"].count("'?'") == 3

    def test_query_ids_are_the_digests_else_the_hash_of_the_collected_text(
        self, engine: str
    ) -> None:
        raw = pii_collection(engine)["queries"]
        ids = [q["query_id"] for q in _collector_output(engine)["queries"]["query_patterns"]]
        expected = list(
            dict.fromkeys(
                r.get("digest") or hashlib.sha256(r["query_text"].encode()).hexdigest()[:16]
                for r in raw
            )
        )
        assert ids == expected

    def test_tags_are_read_from_the_redacted_text(self, engine: str) -> None:
        tags = [
            t
            for q in _collector_output(engine)["queries"]["query_patterns"]
            for t in q["query_tags"]
        ]
        assert "user: ?" in tags

    def test_the_sql_shadow_holds_no_seeded_value(self, engine: str, tmp_path: Path) -> None:
        shadow = TableShadow()
        parse_offline_collection(pii_collection(engine), shadow=shadow)
        store = LocalArtifactStore(str(tmp_path))
        path = write_shadow_artifact(store, DB, "job1", shadow)
        assert path is not None
        assert not leaked((tmp_path / path).read_text())

    def test_the_raw_copy_is_redacted_and_keeps_every_query_id(self, engine: str) -> None:
        data = pii_collection(engine)
        copy = redact_raw_collection(data)
        assert not leaked(json.dumps(copy))
        assert leaked(json.dumps(data)), "the input is not changed"
        # Read again, the copy names every statement as the collection did.
        ids = [q["query_id"] for q in parse_offline_collection(dict(copy))["queries"]]
        assert ids == [q["query_id"] for q in parse_offline_collection(data)["queries"]]


@pytest.mark.parametrize("engine", ENGINES)
class TestOlderOutputs:
    """A collection or collector output saved before #1625 still holds its literals:
    every read redacts them, so no output shows them."""

    def test_a_stored_collector_output_is_read_back_redacted(
        self, engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = _old_collector_output(engine, monkeypatch)
        path = tmp_path / DB / "job1" / "collector" / "output.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(old))
        store = LocalArtifactStore(str(tmp_path))
        assert not leaked(json.dumps(store.read_json(f"{DB}/job1/collector/output.json")))

    def test_an_older_major_is_read_back_redacted(
        self, engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = {**_old_collector_output(engine, monkeypatch), "contract_version": "3.6"}
        path = tmp_path / DB / "job1" / "collector" / "output.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(old))
        store = LocalArtifactStore(str(tmp_path))
        assert not leaked(json.dumps(store.read_json(f"{DB}/job1/collector/output.json")))

    def test_the_contract_redacts_however_it_is_built(
        self, engine: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A live collector builds the contract from the text it read: the model redacts.
        old = _old_collector_output(engine, monkeypatch)
        loaded = load_contract(CollectorOutputContract, old, artifact="old")
        assert not leaked(loaded.model_dump_json())
        built = CollectorOutputContract(
            **{k: getattr(loaded, k) for k in type(loaded).model_fields}
        )
        assert not leaked(built.model_dump_json())

    def test_a_collector_output_handed_in_is_ingested_redacted(
        self, engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.atx_orchestrator.core import ingest_offline_collection

        old = _old_collector_output(engine, monkeypatch)
        store = LocalArtifactStore(str(tmp_path))
        ingest_offline_collection(store, "job1", DB, old)
        written = (tmp_path / DB / "job1" / "collector" / "output.json").read_text()
        assert not leaked(written)
        assert not leaked(json.dumps(rederive_query_facts(old, engine)))

    def test_ids_do_not_change(self, engine: str, monkeypatch: pytest.MonkeyPatch) -> None:
        old = _old_collector_output(engine, monkeypatch)
        new = _collector_output(engine)
        assert [q["query_id"] for q in old["queries"]["query_patterns"]] == [
            q["query_id"] for q in new["queries"]["query_patterns"]
        ]


@pytest.mark.parametrize("engine", ENGINES)
def test_the_assess_result_holds_no_seeded_value(engine: str) -> None:
    from src.assessment import assess

    result = assess(
        {
            "database_name": DB,
            "collection": pii_collection(engine),
            "options": {"include_artifacts": True},
        }
    )
    dumped = result.model_dump_json()
    assert not leaked(dumped)
    texts = {s.query_text for s in result.statements}
    assert all(t is not None for t in texts)


def test_an_older_collector_output_assessed_holds_no_seeded_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.assessment import assess

    old = _old_collector_output("sqlserver", monkeypatch)
    old["contract_version"] = CollectorOutputContract.model_fields["contract_version"].default
    result = assess(
        {"database_name": DB, "collection": old, "options": {"include_artifacts": True}}
    )
    assert not leaked(result.model_dump_json())


def _routing(collection: dict[str, Any]) -> list[dict[str, Any]]:
    from src.assessment import assess

    result = assess({"database_name": DB, "collection": collection})
    return [
        s.model_dump(mode="json", exclude={"query_text", "query_tags"}) for s in result.statements
    ]


@pytest.mark.parametrize("engine", ["sqlserver", "oracle"])
def test_routing_reads_the_redacted_text_as_it_read_the_collected_one(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every statement is placed, scored and explained as it was before #1625."""
    redacted = _routing(pii_collection(engine))
    with monkeypatch.context() as patch:
        keep_literals(patch)
        collected = _routing(pii_collection(engine))
    assert len(redacted) == len(collected) >= len(pii_texts(engine)) - 1
    assert redacted == collected
