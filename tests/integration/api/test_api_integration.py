"""Integration tests for the API using moto-mocked AWS services.

Tests the full path: API route → service → mocked AWS (S3, Step Functions).
No real AWS credentials needed.
"""

import json

import boto3
import pytest
from moto import mock_aws

from tests.integration.api.fixtures import ALL_ARTIFACTS, BUCKET, DATABASE_NAME, JOB_ID


@pytest.fixture()
def aws_env(monkeypatch):
    """Set environment variables for AWS mocking."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # noqa: S105
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")  # noqa: S105
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")  # noqa: S105
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture()
def s3_with_artifacts(aws_env):
    """Create mocked S3 bucket with all agent artifacts."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        for key, data in ALL_ARTIFACTS.items():
            s3.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=json.dumps(data),
                ContentType="application/json",
            )

        yield s3


class TestS3ArtifactsIntegration:
    """Test S3ArtifactsService against moto-mocked S3."""

    def test_read_collector(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        data = svc.read_collector(DATABASE_NAME, JOB_ID)
        assert data is not None
        assert data["agent_type"] == "collector"
        assert data["metadata"]["tables_collected"] == 1247

    def test_read_triage(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        data = svc.read_triage(DATABASE_NAME, JOB_ID)
        assert data is not None
        assert len(data["selected_agents"]) == 3
        assert data["confidence"] == 0.87

    def test_read_analysis(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        data = svc.read_analysis(DATABASE_NAME, JOB_ID, "dynamodb")
        assert data is not None
        assert data["confidence"] == 0.92
        assert data["table_count"] == 850

    def test_read_analysis_not_found(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        data = svc.read_analysis(DATABASE_NAME, JOB_ID, "neptune")
        assert data is None

    def test_read_synthesis(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        data = svc.read_synthesis(DATABASE_NAME, JOB_ID)
        assert data is not None
        assert len(data["ranking"]) == 3
        assert data["needs_deeper_analysis"] is False

    def test_read_schema_design(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        data = svc.read_schema_design(DATABASE_NAME, JOB_ID, "dynamodb")
        assert data is not None
        assert data["target_type"] == "dynamodb"
        assert len(data["tables"]) == 1

    def test_read_all_schema_designs(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        designs = svc.read_all_schema_designs(DATABASE_NAME, JOB_ID)
        assert len(designs) == 1
        assert designs[0]["target_type"] == "dynamodb"

    def test_artifact_exists(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        assert svc.artifact_exists(DATABASE_NAME, JOB_ID, "collector", "output.json") is True
        assert svc.artifact_exists(DATABASE_NAME, JOB_ID, "nonexistent", "x.json") is False

    def test_artifact_size(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        size = svc.artifact_size(DATABASE_NAME, JOB_ID, "collector", "output.json")
        assert size is not None
        assert size > 0

    def test_list_agent_artifacts(self, s3_with_artifacts):
        from src.api.services.s3_artifacts import S3ArtifactsService

        svc = S3ArtifactsService(BUCKET)
        agents = svc.list_agent_artifacts(DATABASE_NAME, JOB_ID)
        assert "collector" in agents
        assert "referee-triage" in agents
        assert "analysis-dynamodb" in agents
        assert "referee-synthesis" in agents
        assert "schema-dynamodb" in agents


# ============================================================
# Step Functions Service Integration Tests
# ============================================================

STATE_MACHINE_DEF = json.dumps(
    {
        "Comment": "Test state machine",
        "StartAt": "Pass",
        "States": {
            "Pass": {"Type": "Pass", "End": True},
        },
    }
)


@pytest.fixture()
def sfn_with_execution(aws_env):
    """Create mocked Step Functions with a state machine and execution."""
    with mock_aws():
        iam = boto3.client("iam", region_name="us-east-1")
        role = iam.create_role(
            RoleName="test-sfn-role",
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "states.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )
        role_arn = role["Role"]["Arn"]

        sfn = boto3.client("stepfunctions", region_name="us-east-1")
        sm = sfn.create_state_machine(
            name="modernizer-test-orchestrator",
            definition=STATE_MACHINE_DEF,
            roleArn=role_arn,
        )
        sm_arn = sm["stateMachineArn"]

        # Start an execution
        sfn.start_execution(
            stateMachineArn=sm_arn,
            name=JOB_ID,
            input=json.dumps(
                {
                    "job_id": JOB_ID,
                    "database_name": DATABASE_NAME,
                    "source_database_type": "mysql",
                }
            ),
        )

        yield {"sfn": sfn, "sm_arn": sm_arn, "role_arn": role_arn}


class TestStepFunctionsIntegration:
    """Test StepFunctionsService against moto-mocked Step Functions."""

    def test_describe_execution(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        result = svc.describe_execution(JOB_ID)
        assert result is not None
        assert result["status"] in ("RUNNING", "SUCCEEDED")
        assert result["input"]["database_name"] == DATABASE_NAME

    def test_describe_execution_not_found(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        result = svc.describe_execution("nonexistent-job-id")
        assert result is None

    def test_list_executions(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        results = svc.list_executions()
        assert len(results) >= 1
        assert results[0]["job_id"] == JOB_ID

    def test_start_execution(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        new_job_id = "new-test-job-12345"
        result = svc.start_execution(
            new_job_id,
            {
                "job_id": new_job_id,
                "database_name": "test_db",
            },
        )
        assert "execution_arn" in result
        assert new_job_id in result["execution_arn"]

    def test_stop_execution(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        result = svc.stop_execution(JOB_ID)
        assert result is True

    def test_stop_nonexistent_execution(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        result = svc.stop_execution("nonexistent-job")
        assert result is False

    def test_get_execution_history(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        history = svc.get_execution_history(JOB_ID)
        # moto's simple Pass state machine produces at least some events
        assert isinstance(history, list)

    def test_execution_arn_format(self, sfn_with_execution):
        from src.api.services.step_functions import StepFunctionsService

        svc = StepFunctionsService(sfn_with_execution["sm_arn"])
        arn = svc._execution_arn(JOB_ID)
        assert ":execution:" in arn
        assert JOB_ID in arn


# ============================================================
# CloudWatch Logs Service Integration Tests
# ============================================================
# NOTE: moto does not persist put_log_events for retrieval via
# filter_log_events. CloudWatch Logs service is tested via unit
# tests with mocked responses instead. The service is a thin
# wrapper around filter_log_events — no complex logic to
# integration-test beyond what moto supports.
# ============================================================


LOG_GROUP = "/ecs/modernizer-test"


@pytest.fixture()
def cw_with_log_group(aws_env):
    """Create mocked CloudWatch log group (without events — moto limitation)."""
    with mock_aws():
        cw = boto3.client("logs", region_name="us-east-1")
        cw.create_log_group(logGroupName=LOG_GROUP)
        cw.create_log_stream(logGroupName=LOG_GROUP, logStreamName="collector/task-abc123")
        yield cw


class TestCloudWatchLogsIntegration:
    """Test CloudWatchLogsService against moto-mocked CloudWatch."""

    def test_nonexistent_log_group_returns_empty(self, aws_env):
        with mock_aws():
            from src.api.services.cloudwatch import CloudWatchLogsService

            svc = CloudWatchLogsService("/ecs/nonexistent")
            result = svc.get_logs()
            assert result["logs"] == []
            assert result["next_token"] is None

    def test_empty_log_group_returns_empty(self, cw_with_log_group):
        from src.api.services.cloudwatch import CloudWatchLogsService

        svc = CloudWatchLogsService(LOG_GROUP)
        result = svc.get_logs()
        # moto doesn't persist log events, so empty is expected
        assert isinstance(result["logs"], list)

    def test_service_initializes_with_log_group(self, cw_with_log_group):
        from src.api.services.cloudwatch import CloudWatchLogsService

        svc = CloudWatchLogsService(LOG_GROUP)
        assert svc.log_group == LOG_GROUP
