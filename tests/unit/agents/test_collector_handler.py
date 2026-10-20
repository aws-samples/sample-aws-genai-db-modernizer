"""Unit tests for collector agent handler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.storage.artifact_store import ArtifactStore

ENV_VARS = {
    "S3_BUCKET": "test-bucket",
    "ENGINE": "mysql",
    "CLUSTER_ENDPOINT": "mydb.abc123.us-east-1.rds.amazonaws.com",
    "PORT": "3306",
    "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:mydb",  # nosec B105 -- an ARN, not a secret value; fake test fixture
    "AUTOMATION_INSTANCE_ID": "i-0abc123",
    "AWS_REGION": "us-east-1",
    "DB_INSTANCE_IDENTIFIER": "mydb",
    "COLLECTION_MODE": "live",
}


def _mock_store() -> MagicMock:
    """Create a mock ArtifactStore."""
    store = MagicMock(spec=ArtifactStore)
    return store


@patch("src.agents.collector.handler._dispatch_collect")
def test_collector_writes_output_to_store(mock_dispatch):
    """Handler calls collect() and writes output via ArtifactStore."""
    mock_result = MagicMock()
    mock_result.model_dump_json.return_value = json.dumps(
        {"job_id": "job-001", "status": "ok", "queries": {"query_patterns": []}}
    )
    mock_result.database_schema.tables = [MagicMock(), MagicMock()]
    mock_result.queries.query_patterns = [MagicMock()]
    mock_dispatch.return_value = mock_result

    store = _mock_store()

    with patch.dict("os.environ", ENV_VARS):
        from src.agents.collector.handler import run_collector

        run_collector("job-001", "mydb", store)

    store.write_json.assert_called_once()
    call_args = store.write_json.call_args
    assert call_args[0][0] == "mydb/job-001/collector/output.json"


@patch("src.agents.collector.handler._dispatch_collect")
def test_collector_passes_correct_input(mock_dispatch):
    """Handler builds CollectorInput correctly from env vars."""
    mock_result = MagicMock()
    mock_result.model_dump_json.return_value = json.dumps({"queries": {"query_patterns": []}})
    mock_result.database_schema.tables = []
    mock_result.queries.query_patterns = []
    mock_dispatch.return_value = mock_result

    store = _mock_store()

    with patch.dict("os.environ", ENV_VARS):
        from src.agents.collector.handler import run_collector

        run_collector("job-001", "mydb", store)

    # Verify dispatch was called with correct engine and CollectorInput
    mock_dispatch.assert_called_once()
    engine = mock_dispatch.call_args[0][0]
    input_contract = mock_dispatch.call_args[0][1]
    assert engine == "mysql"
    assert input_contract.job_id == "job-001"
    assert input_contract.database_name == "mydb"
    assert input_contract.engine.value == "mysql"
    assert input_contract.cluster_endpoint == "mydb.abc123.us-east-1.rds.amazonaws.com"
    assert (
        input_contract.live_config.secret_arn
        == "arn:aws:secretsmanager:us-east-1:123:secret:mydb"  # nosec B105 — fake ARN in test fixture
    )
    assert input_contract.live_config.automation_instance_id == "i-0abc123"


def test_collector_exits_without_required_env_vars():
    """Handler exits with error if CLUSTER_ENDPOINT or SECRET_ARN missing."""
    store = _mock_store()

    with patch.dict("os.environ", {"S3_BUCKET": "test-bucket"}, clear=True):
        from src.agents.collector.handler import run_collector

        with pytest.raises(SystemExit) as exc_info:
            run_collector("job-001", "mydb", store)
        assert exc_info.value.code == 1


@patch("src.agents.collector.handler._dispatch_collect")
def test_collector_records_the_declared_engines_in_use(mock_dispatch):
    """ENGINES_IN_USE reaches the CollectorInput and the stored collection (#963)."""
    mock_result = MagicMock()
    mock_result.model_dump_json.return_value = json.dumps(
        {"metadata": {"engines_in_use": ["documentdb"]}}
    )
    mock_dispatch.return_value = mock_result
    store = _mock_store()
    with patch.dict("os.environ", {**ENV_VARS, "ENGINES_IN_USE": "OpenSearch,documentdb"}):
        from src.agents.collector.handler import run_collector

        run_collector("job-001", "mydb", store)

    assert mock_dispatch.call_args[0][1].engines_in_use == ["opensearch", "documentdb"]
    written = store.write_json.call_args[0][1]
    assert written["metadata"]["engines_in_use"] == ["documentdb", "opensearch"]


@patch("src.agents.collector.handler._dispatch_collect")
def test_collector_without_the_variable_declares_nothing(mock_dispatch):
    mock_result = MagicMock()
    mock_result.model_dump_json.return_value = json.dumps({"metadata": {"engines_in_use": []}})
    mock_dispatch.return_value = mock_result
    store = _mock_store()
    with patch.dict("os.environ", ENV_VARS):
        from src.agents.collector.handler import run_collector

        run_collector("job-001", "mydb", store)
    assert mock_dispatch.call_args[0][1].engines_in_use == []
    assert store.write_json.call_args[0][1]["metadata"]["engines_in_use"] == []


@patch("src.agents.collector.handler._dispatch_collect")
def test_an_invalid_engines_in_use_exits_before_collection_starts(mock_dispatch, capsys):
    store = _mock_store()
    with patch.dict("os.environ", {**ENV_VARS, "ENGINES_IN_USE": "opensearch,redis"}):
        from src.agents.collector.handler import run_collector

        with pytest.raises(SystemExit) as exc:
            run_collector("job-001", "mydb", store)

    assert exc.value.code == 1
    assert "redis" in capsys.readouterr().err
    mock_dispatch.assert_not_called()
    store.write_json.assert_not_called()


# ---------------------------------------------------------------------------
# #1177: the source engine comes from ENGINE or the collection, never a guess
# ---------------------------------------------------------------------------

_OFFLINE_ENV = {
    "S3_BUCKET": "test-bucket",
    "COLLECTION_MODE": "offline",
    "OFFLINE_S3_KEY": "uploads/collection.json",
    "AWS_REGION": "us-east-1",
}


def _run_dispatched(env: dict, offline_metadata: dict | None = None):
    """Run the handler with ``env`` only; return the dispatched (engine, CollectorInput)."""
    with (
        patch.dict("os.environ", env, clear=True),
        patch("src.agents.collector.handler._dispatch_collect") as dispatch,
        patch(
            "src.tools.database.offline_parser.fetch_offline_json",
            return_value={"metadata": offline_metadata or {}},
        ) as fetch,
    ):
        dispatch.return_value.model_dump_json.return_value = "{}"
        from src.agents.collector.handler import run_collector

        run_collector("job-1", "trailgear", MagicMock(spec=ArtifactStore))
    return dispatch.call_args[0][0], dispatch.call_args[0][1], fetch


@pytest.mark.parametrize(
    ("metadata", "engine", "port"),
    [
        ({"engine": "sqlserver"}, "sqlserver", 1433),
        ({"version": "Oracle Database 19c Enterprise Edition"}, "oracle", 1521),
        ({"version": "DB2 v11.5.8.0"}, "db2", 50000),
        ({"engine": "mariadb"}, "mariadb", 3306),
        ({"version": "10.11.6-MariaDB"}, "mariadb", 3306),
        ({"version": "PostgreSQL 16.10"}, "postgresql", 5432),
        ({"version": "8.0.36"}, "mysql", 3306),
    ],
)
def test_offline_without_engine_reads_it_from_the_collection(metadata, engine, port):
    dispatched, input_contract, fetch = _run_dispatched(_OFFLINE_ENV, metadata)

    assert dispatched == engine
    assert input_contract.engine.value == engine
    assert input_contract.port == port
    fetch.assert_called_once_with(
        bucket="test-bucket", key="uploads/collection.json", region="us-east-1"
    )


def test_an_explicit_engine_is_kept_when_the_file_names_none():
    env = {**_OFFLINE_ENV, "ENGINE": "oracle"}
    dispatched, input_contract, fetch = _run_dispatched(env, {"version": "19.0.0.0.0"})

    assert dispatched == "oracle"
    assert input_contract.port == 1521  # the engine's own default port, not MySQL's
    fetch.assert_called_once()


def test_the_file_wins_over_a_declared_engine_that_disagrees(capsys):
    """The upload form's engine (the UI used to default it to MySQL) is overruled by a
    file whose metadata names its engine (#1177 review)."""
    env = {**_OFFLINE_ENV, "ENGINE": "mysql", "PORT": "0"}
    dispatched, input_contract, fetch = _run_dispatched(env, {"engine": "sqlserver"})

    assert dispatched == input_contract.engine.value == "sqlserver"
    assert input_contract.port == 1433
    assert "metadata says sqlserver, not the declared mysql" in capsys.readouterr().err
    fetch.assert_called_once()


def test_an_explicit_postgres_alias_is_accepted():
    env = {k: v for k, v in ENV_VARS.items() if k != "PORT"}
    dispatched, input_contract, _ = _run_dispatched({**env, "ENGINE": "postgres"})

    assert dispatched == input_contract.engine.value == "postgresql"
    assert input_contract.port == 5432


@pytest.mark.parametrize("mode", ["live", "ddl"])
def test_live_and_ddl_without_engine_exit_instead_of_assuming_mysql(mode, capsys):
    env = {k: v for k, v in ENV_VARS.items() if k != "ENGINE"}
    env["COLLECTION_MODE"] = mode
    with (
        patch.dict("os.environ", env, clear=True),
        patch("src.agents.collector.handler._dispatch_collect") as dispatch,
    ):
        from src.agents.collector.handler import run_collector

        with pytest.raises(SystemExit):
            run_collector("job-1", "mydb", MagicMock(spec=ArtifactStore))
    dispatch.assert_not_called()
    assert f"ENGINE is required for {mode} mode" in capsys.readouterr().err


def test_live_db2_exits_with_a_clear_message(capsys):
    env = {**ENV_VARS, "ENGINE": "db2"}
    with (
        patch.dict("os.environ", env, clear=True),
        patch("src.agents.collector.handler._dispatch_collect") as dispatch,
    ):
        from src.agents.collector.handler import run_collector

        with pytest.raises(SystemExit):
            run_collector("job-1", "mydb", MagicMock(spec=ArtifactStore))
    dispatch.assert_not_called()
    assert "db2 has no live collector" in capsys.readouterr().err


def _trailgear_raw(**metadata) -> dict:
    import zipfile
    from pathlib import Path

    from src.tools.database.collection_text import extract_json_object

    zip_path = Path(__file__).resolve().parents[3] / "docs/examples/trailgear/trailgear.zip"
    with zipfile.ZipFile(zip_path) as z:
        raw: dict = json.loads(
            extract_json_object(z.read("trailgear-collection.json").decode("utf-8"))
        )
    raw["metadata"] = {"database_name": "trailgear", **metadata}
    return raw


@pytest.mark.parametrize(
    ("metadata", "engine"),
    [
        ({"engine": "db2", "version": "11.5.8.0"}, "db2"),
        ({"version": "DB2 v11.5.8.0"}, "db2"),
        ({"engine": "sqlserver"}, "sqlserver"),
    ],
)
def test_an_offline_collection_runs_through_the_real_collector(metadata, engine):
    """No dispatch patch: the handler's offline mode runs the shared offline collector
    for every engine, DB2 included (it used to raise "Unsupported engine: db2"), and
    reads the file once. Only the S3 download and the S3 checkpoint store are faked."""
    from src.agents.collector.mysql_collector import NoopCheckpointStore

    store = MagicMock(spec=ArtifactStore)
    raw = _trailgear_raw(**metadata)
    with (
        patch.dict("os.environ", _OFFLINE_ENV, clear=True),
        patch("src.tools.database.offline_parser.fetch_offline_json", return_value=raw) as fetch,
        patch(
            "src.agents.collector.mysql_collector._init_checkpoint_store",
            return_value=NoopCheckpointStore(),
        ),
    ):
        from src.agents.collector.handler import run_collector

        run_collector("job-1", "trailgear", store)

    fetch.assert_called_once()
    key, output = store.write_json.call_args[0]
    assert key == "trailgear/job-1/collector/output.json"
    assert output["metadata"]["source_database"]["engine"] == engine
    assert len(output["database_schema"]["tables"]) == len(raw["tables"])
    assert len(output["queries"]["query_patterns"]) == len(raw["queries"])


def test_live_mode_without_port_uses_the_engines_default_port():
    env = {k: v for k, v in ENV_VARS.items() if k != "PORT"}
    _, input_contract, _ = _run_dispatched({**env, "ENGINE": "postgresql"})

    assert input_contract.port == 5432  # it used to be MySQL's 3306 for every engine


@pytest.mark.passes_on_main
def test_an_explicit_port_is_kept():
    env = {**ENV_VARS, "ENGINE": "postgresql", "PORT": "6543"}
    _, input_contract, _ = _run_dispatched(env)

    assert input_contract.port == 6543
