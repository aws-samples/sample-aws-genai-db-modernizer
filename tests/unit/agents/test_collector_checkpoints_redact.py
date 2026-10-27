"""A live collector stores no literal value in its stage checkpoints (#1625).

Each live collector saves its ``schema``, ``queries`` and ``aws_metrics`` stages to S3
(``CheckpointStore``) before the collector output contract is built, so the contract's
own redaction comes too late for them. The store redacts every stage, in the source
engine's dialect, before it saves it, returns the redacted stage, and redacts a stage
an older run saved when it reads it back.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agents.collector import (
    mysql_collector,
    oracle_collector,
    postgres_collector,
    sqlserver_collector,
)
from src.agents.collector.mysql_collector import CheckpointStore
from src.contracts.collector_input import CollectionMode, CollectorInput
from tests.fixtures.pii_collections import CARD, EMAIL, LAST_NAME, leaked

# (collector module, engine): every live collector, MariaDB through the MySQL one's store.
COLLECTORS = [
    (mysql_collector, "mysql"),
    (mysql_collector, "mariadb"),
    (postgres_collector, "postgresql"),
    (oracle_collector, "oracle"),
    (sqlserver_collector, "sqlserver"),
]


def _input(engine: str) -> CollectorInput:
    return CollectorInput.model_validate(
        {
            "job_id": "job-001",
            "engine": engine,
            "cluster_endpoint": "db.abc123.us-east-1.rds.amazonaws.com",
            "port": 1433,
            "database_name": "testdb",
            "mode": CollectionMode.live,
            "live_config": {
                "secret_arn": "arn:aws:secretsmanager:us-east-1:123:secret:test-AAAAAA",  # pragma: allowlist secret  # nosec B105 -- an ARN, not a secret value; fake test fixture
                "automation_instance_id": "i-0abc123",
            },
        }
    )


# A stage as the live tools return it: rows with the text the source reported.
STAGES: dict[str, Any] = {
    "schema": {
        "tables": [{"table_name": "Customer"}],
        "views": [{"view_name": "v", "definition": f"SELECT * FROM c WHERE e = '{EMAIL}'"}],
        "procedures": [{"routine_name": "p", "definition": f"UPDATE c SET n = '{LAST_NAME}'"}],
    },
    "queries": [
        {"query_id": "q1", "query_text": f"SELECT * FROM c WHERE e = '{EMAIL}'"},
        {"query_id": "q2", "query_text": f"SELECT * FROM c WHERE card = {CARD}"},
    ],
    "aws_metrics": {
        "query_patterns": [
            {
                "query_id": "q1",
                "query_text": f"/* user {EMAIL} */ SELECT * FROM c WHERE n = '{LAST_NAME}'",
                "query_tags": [f"user {EMAIL}"],
            }
        ]
    },
}


@pytest.mark.parametrize(("module", "engine"), COLLECTORS)
def test_every_live_collector_store_knows_its_source_engine(
    module: Any, engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = module
    monkeypatch.setattr(collector, "_build_cred_mgr", lambda inp: object())
    monkeypatch.setattr(
        collector,
        "init_storage",
        lambda *args: {"bucket": "b", "collector_prefix": "db/collector/"},
    )
    store = collector._init_checkpoint_store(_input(engine))
    assert isinstance(store, CheckpointStore)
    assert store._engine == engine


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    monkeypatch.setattr(
        mysql_collector, "save_json", lambda _c, _b, key, data: saved.__setitem__(key, data)
    )
    monkeypatch.setattr(mysql_collector, "load_json", lambda _c, _b, key: saved[key])
    return saved


@pytest.mark.parametrize("engine", ["sqlserver", "oracle", "mysql", "postgresql"])
@pytest.mark.parametrize("stage", sorted(STAGES))
def test_a_stage_is_redacted_before_it_is_saved(
    engine: str, stage: str, s3: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CheckpointStore(object(), "b", "db/collector/", engine)  # type: ignore[arg-type]
    monkeypatch.setattr(store, "exists", lambda _stage: False)
    returned = store.load_or_run(stage, lambda: json.loads(json.dumps(STAGES[stage])))
    assert leaked(json.dumps(STAGES[stage]))
    assert not leaked(json.dumps(s3[f"db/collector/{stage}.json"]))
    assert not leaked(json.dumps(returned))


@pytest.mark.parametrize("stage", sorted(STAGES))
def test_a_stage_an_older_run_saved_is_read_back_redacted(
    stage: str, s3: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    s3[f"db/collector/{stage}.json"] = STAGES[stage]
    store = CheckpointStore(object(), "b", "db/collector/", "sqlserver")  # type: ignore[arg-type]
    monkeypatch.setattr(store, "exists", lambda _stage: True)
    assert not leaked(json.dumps(store.load_or_run(stage, lambda: pytest.fail("ran"))))
