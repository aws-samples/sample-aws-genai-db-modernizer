"""The live collectors never read table rows (#387).

Before #387 every live collector ran ``SELECT * ... LIMIT 10`` per table by default
and stored the rows as ``sample_data``; ``anonymize_pii`` claimed an anonymization
nothing performed. No consumer reads sample rows, so the read, the options and the
claim are gone. A request written before #387 that still sets them is accepted and
the old options are ignored.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.collector import (
    mysql_collector,
    oracle_collector,
    postgres_collector,
    sqlserver_collector,
)
from src.api.models.requests import CollectionOptions as ApiCollectionOptions
from src.contracts.collector_input import CollectionOptions
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

COLLECTORS = [mysql_collector, postgres_collector, sqlserver_collector, oracle_collector]


@pytest.mark.parametrize(
    "cls",
    [
        MySQLRemoteCollector,
        MariaDBRemoteCollector,
        PostgreSQLRemoteCollector,
        SQLServerRemoteCollector,
        OracleRemoteCollector,
    ],
)
def test_no_remote_collector_can_read_rows(cls: type) -> None:
    assert not hasattr(cls, "collect_sample_data")


def _db() -> MagicMock:
    db = MagicMock()
    db.collect_tables.return_value = [{"table_name": "orders", "schema_name": "shop"}]
    db.collect_columns.return_value = []
    db.collect_column_cardinality.return_value = {}
    return db


@pytest.mark.parametrize("module", COLLECTORS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_schema_collection_reads_no_rows(module) -> None:
    db = _db()
    raw = module._collect_schema_raw(db)
    assert [t["table_name"] for t in raw["tables"]] == ["orders"]
    assert "sample_data" not in raw["tables"][0]
    assert not db.collect_sample_data.called


@pytest.mark.parametrize("module", COLLECTORS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_build_tables_drops_rows_from_an_old_checkpoint(module) -> None:
    """A schema checkpoint written before #387 may hold rows; none reach the output."""
    table = {
        "table_name": "orders",
        "schema_name": "shop",
        "row_count": 1,
        "columns": [
            {
                "column_name": "id",
                "data_type": "int",
                "is_nullable": "NO",
                "ordinal_position": 1,
            }
        ],
        "indexes": [],
        "foreign_keys": [],
        "primary_key": ["id"],
        "sample_data": [{"id": 1, "email": "a@example.com"}],
    }
    built = (
        module._build_tables([table], "shop")
        if module is mysql_collector
        else module._build_tables({"tables": [table]})
    )
    assert len(built) == 1
    assert "sample_data" not in built[0].model_dump()


def test_collection_options_have_no_row_or_anonymization_settings() -> None:
    fields = set(CollectionOptions.model_fields)
    assert not fields & {"collect_sample_data", "sample_row_count", "anonymize_pii"}
    api_fields = set(ApiCollectionOptions.model_fields)
    assert not api_fields & {"include_sample_data", "sample_size", "anonymize_pii"}


def test_a_request_from_before_387_is_accepted_and_its_row_options_ignored() -> None:
    old = {
        "anonymize_pii": True,
        "collect_sample_data": True,
        "sample_row_count": 10,
        "query_log_days": 3,
    }
    opts = CollectionOptions.model_validate(old)
    assert opts.query_log_days == 3
    assert "collect_sample_data" not in opts.model_dump()

    api = ApiCollectionOptions.model_validate(
        {"anonymize_pii": True, "include_sample_data": True, "sample_size": 1000}
    )
    assert not {"anonymize_pii", "include_sample_data", "sample_size"} & set(api.model_dump())


def test_the_api_accepts_an_old_request_with_the_removed_options(tmp_path, monkeypatch) -> None:
    """Over HTTP: an old client's removed fields give no 422, even a ``sample_size``
    the old model refused (50 < its minimum of 100), and none reaches the execution."""
    import json

    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.api.routes import assessments
    from src.api.services.local_execution import LocalExecutionService
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    monkeypatch.setattr(assessments, "sfn_service", LocalExecutionService(store))
    monkeypatch.setattr(assessments, "s3_service", MagicMock())
    request = {
        "source_database_type": "mysql",
        "database_name": "test_db",
        "collection_mode": "live",
        "connection": {
            "host": "db.example.internal",
            "port": 3306,
            "database": "app",
            "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:app-AbCdEf",  # pragma: allowlist secret
        },
        "options": {
            "anonymize_pii": True,
            "include_sample_data": True,
            "sample_size": 50,
            "query_log_period_days": 3,
        },
    }
    resp = TestClient(app).post("/api/v1/assessments", json=request)
    assert resp.status_code == 202, resp.text
    meta = json.loads(
        (tmp_path / "artifacts" / "test_db" / resp.json()["job_id"] / "_meta.json").read_text()
    )
    options = meta["input"]["options"]
    assert options["query_log_period_days"] == 3
    assert not {"anonymize_pii", "include_sample_data", "sample_size"} & set(options)
