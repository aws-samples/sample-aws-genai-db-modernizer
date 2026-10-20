"""ADR-004 Decision 5: either secret reference form reaches live collection (#991).

The Assessments API accepts the database credentials by reference only, as
``connection.secret_arn`` or ``connection.secret_name`` (#723). The live collector reads
one secret id (the execution input's ``secret_arn``, the collector's ``SECRET_ARN``) and
hands it to Secrets Manager as ``SecretId``, which takes an ARN or a name. So whichever
form the request uses must reach that id, and the collector must authenticate with it.
No AWS call is made: Secrets Manager is a botocore stub.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.agents.collector.handler import run_collector
from src.api.main import app
from src.api.models.requests import ConnectionDetails
from src.api.routes import assessments
from src.api.services.local_execution import LocalExecutionService
from src.contracts.collector_input import CollectorInput
from src.storage.artifact_store import ArtifactStore
from src.storage.local_store import LocalArtifactStore
from src.tools.aws.rds_tools import get_database_credentials
from src.tools.aws.ssm_executor import _build_sql_command
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

client = TestClient(app)

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db/creds-AbCdEf"  # pragma: allowlist secret
SECRET_NAME = "prod/db/credentials"  # pragma: allowlist secret
RDS_SECRET_NAME = "rds!cluster-0c4b8f0e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"  # pragma: allowlist secret
FROM_THE_SECRET = "from-" + "the-secret"  # pragma: allowlist secret


@pytest.fixture
def real_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    monkeypatch.setattr(assessments, "sfn_service", LocalExecutionService(store))
    monkeypatch.setattr(assessments, "s3_service", MagicMock())
    return tmp_path / "artifacts"


def _live_request(**connection: object) -> dict:
    return {
        "source_database_type": "mysql",
        "database_name": "test_db",
        "collection_mode": "live",
        "connection": {
            "host": "db.example.internal",
            "port": 3306,
            "database": "app",
            **connection,
        },
    }


def _execution_input(root: Path, **connection: object) -> dict:
    resp = client.post("/api/v1/assessments", json=_live_request(**connection))
    assert resp.status_code == 202, resp.text
    meta = json.loads((root / "test_db" / resp.json()["job_id"] / "_meta.json").read_text())
    execution_input: dict = meta["input"]
    return execution_input


def _collector_input_from(execution_input: dict) -> CollectorInput:
    """Run the collector handler with the environment the execution input maps to."""
    env = {
        "ENGINE": execution_input["engine"],
        "CLUSTER_ENDPOINT": execution_input["cluster_endpoint"],
        "PORT": execution_input["port"],
        "SECRET_ARN": execution_input["secret_arn"],
        "AUTOMATION_INSTANCE_ID": "i-0abc123",
        "AWS_REGION": "us-east-1",
        "COLLECTION_MODE": execution_input["collection_mode"],
    }
    with (
        patch.dict("os.environ", env, clear=True),
        patch("src.agents.collector.handler._dispatch_collect") as dispatch,
    ):
        dispatch.return_value.model_dump_json.return_value = "{}"
        run_collector("job-1", "test_db", MagicMock(spec=ArtifactStore))
    collector_input: CollectorInput = dispatch.call_args[0][1]
    return collector_input


def _authenticate(secret_id: str) -> dict:
    """Fetch the credentials as the collector does, against a stub that expects ``secret_id``."""
    cred_mgr = StubbedCredMgr()
    cred_mgr.stub("secretsmanager").add_response(
        "get_secret_value",
        {"SecretString": json.dumps({"username": "app", "password": FROM_THE_SECRET})},
        {"SecretId": secret_id},
    )
    creds = get_database_credentials(cred_mgr, secret_id)
    cred_mgr.stubbers["secretsmanager"].assert_no_pending_responses()
    return creds


REFERENCE_FORMS = [
    pytest.param(
        {"credentials_type": "secrets-arn", "secret_arn": SECRET_ARN}, SECRET_ARN, id="arn"
    ),
    pytest.param(
        {"credentials_type": "secrets-name", "secret_name": SECRET_NAME}, SECRET_NAME, id="name"
    ),
    pytest.param(
        {"credentials_type": "secrets-name", "secret_name": RDS_SECRET_NAME},
        RDS_SECRET_NAME,
        id="rds-managed-name",
    ),
]


class TestEitherSecretReferenceReachesLiveCollection:
    @pytest.mark.parametrize(("connection", "secret_id"), REFERENCE_FORMS)
    def test_the_execution_input_carries_the_reference(
        self, connection, secret_id, real_store
    ) -> None:
        assert _execution_input(real_store, **connection)["secret_arn"] == secret_id

    @pytest.mark.parametrize(("connection", "secret_id"), REFERENCE_FORMS)
    def test_the_collector_authenticates_with_that_secret(
        self, connection, secret_id, real_store
    ) -> None:
        collector_input = _collector_input_from(_execution_input(real_store, **connection))

        assert collector_input.live_config is not None
        assert collector_input.live_config.secret_arn == secret_id
        creds = _authenticate(collector_input.live_config.secret_arn)
        assert (creds["username"], creds["password"]) == ("app", FROM_THE_SECRET)

    @pytest.mark.parametrize(("connection", "secret_id"), REFERENCE_FORMS)
    def test_the_automation_command_fetches_that_secret(
        self, connection, secret_id, real_store
    ) -> None:
        collector_input = _collector_input_from(_execution_input(real_store, **connection))
        assert collector_input.live_config is not None

        command = _build_sql_command(
            "mysql",
            collector_input.cluster_endpoint,
            collector_input.port,
            collector_input.database_name,
            collector_input.live_config.secret_arn,
            "SELECT 1",
            "us-east-1",
        )

        assert f"get-secret-value --secret-id '{secret_id}' " in command


class TestWhichReferenceIsForwarded:
    def test_the_credentials_type_picks_the_reference_when_both_are_given(self) -> None:
        both = {"secret_arn": SECRET_ARN, "secret_name": SECRET_NAME}
        by_arn = ConnectionDetails(
            host="db", port=3306, database="app", credentials_type="secrets-arn", **both
        )
        by_name = ConnectionDetails(
            host="db", port=3306, database="app", credentials_type="secrets-name", **both
        )

        assert by_arn.secret_reference() == SECRET_ARN
        assert by_name.secret_reference() == SECRET_NAME

    @pytest.mark.parametrize(
        ("credentials_type", "given", "expected"),
        [
            ("secrets-arn", {"secret_name": SECRET_NAME}, SECRET_NAME),
            ("secrets-name", {"secret_arn": SECRET_ARN}, SECRET_ARN),
            ("secrets-name", {"secret_name": "", "secret_arn": SECRET_ARN}, SECRET_ARN),
        ],
        ids=["arn-type-name-only", "name-type-arn-only", "empty-name-falls-back"],
    )
    def test_the_one_reference_given_is_used_whatever_the_type(
        self, credentials_type, given, expected
    ) -> None:
        connection = ConnectionDetails(
            host="db", port=3306, database="app", credentials_type=credentials_type, **given
        )
        assert connection.secret_reference() == expected

    @pytest.mark.parametrize(
        "given", [{}, {"secret_arn": "", "secret_name": ""}], ids=["none", "empty"]
    )
    def test_no_reference_forwards_an_empty_id(self, given) -> None:
        connection = ConnectionDetails(host="db", port=3306, database="app", **given)
        assert connection.secret_reference() == ""

    def test_the_name_is_forwarded_without_surrounding_spaces(self, real_store) -> None:
        execution_input = _execution_input(
            real_store, credentials_type="secrets-name", secret_name=f"  {SECRET_NAME} "
        )
        assert execution_input["secret_arn"] == SECRET_NAME

    def test_an_offline_request_without_a_connection_forwards_an_empty_id(self, real_store) -> None:
        body = {
            "source_database_type": "mysql",
            "database_name": "test_db",
            "collection_mode": "offline",
            "offline_s3_key": "test_db/job/uploads/collector-output.json",
        }
        resp = client.post("/api/v1/assessments", json=body)
        assert resp.status_code == 202, resp.text
        meta = json.loads(
            (real_store / "test_db" / resp.json()["job_id"] / "_meta.json").read_text()
        )
        assert meta["input"]["secret_arn"] == ""


class TestALiveRequestNeedsASecretReference:
    """#1024: a live request naming no secret is refused up front, not in the collector."""

    @pytest.mark.parametrize(
        "given",
        [{}, {"secret_arn": "", "secret_name": ""}, {"secret_arn": None, "secret_name": None}],
        ids=["absent", "empty", "null"],
    )
    def test_no_reference_is_a_422_naming_both_fields(self, given, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=_live_request(**given))

        assert resp.status_code == 422, resp.text
        assert "connection.secret_arn" in resp.text
        assert "connection.secret_name" in resp.text
        assert not (real_store / "test_db").exists()

    @pytest.mark.passes_on_main  # what #1024 must keep accepting
    @pytest.mark.parametrize(
        "given",
        [{"secret_arn": SECRET_ARN}, {"secret_name": SECRET_NAME}],
        ids=["arn", "name"],
    )
    def test_either_reference_alone_is_enough(self, given, real_store) -> None:
        resp = client.post("/api/v1/assessments", json=_live_request(**given))
        assert resp.status_code == 202, resp.text

    @pytest.mark.passes_on_main  # what #1024 must keep accepting
    def test_a_ddl_request_still_needs_no_reference(self, real_store) -> None:
        body = {**_live_request(), "collection_mode": "ddl"}
        resp = client.post("/api/v1/assessments", json=body)
        assert resp.status_code == 202, resp.text
