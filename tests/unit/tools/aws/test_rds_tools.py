"""ADR-004 Decisions 1, 3 and 4: the RDS tools (#702).

Granular tools (one data source each, so the agent collects only what it needs), graceful
degradation (optional tools return ``{"available": False}``, critical ones fail fast), and
pre-aggregated summaries (seven days, hourly, average and maximum). Clients are botocore
stubs, so parameters are validated against the real service models and nothing is sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.agents.collector import mysql_collector
from src.contracts.collector_input import CollectorInput
from src.tools.aws import rds_tools
from src.tools.aws.rds_tools import (
    get_cloudwatch_metrics,
    get_database_credentials,
    get_database_insights,
    get_enhanced_monitoring_cpu,
    get_performance_insights_queries,
    get_pi_counter_metrics,
    get_rds_instance_metadata,
)
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr, capture_params, hourly_datapoints

INSTANCE = "my-prod-db"
RESOURCE_ID = "db-ABCDEFGHIJKLMNOP"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:creds-AbCdEf"  # nosec B105  # pragma: allowlist secret


# --- Decision 1: granular tools -------------------------------------------------------------

# Every optional tool and the one AWS API it uses.
GRANULAR_TOOLS = [
    (get_rds_instance_metadata, (INSTANCE,), "rds"),
    (get_cloudwatch_metrics, (INSTANCE,), "cloudwatch"),
    (get_performance_insights_queries, (RESOURCE_ID,), "pi"),
    (get_pi_counter_metrics, (RESOURCE_ID,), "pi"),
    (get_enhanced_monitoring_cpu, (RESOURCE_ID,), "logs"),
    (get_database_insights, (INSTANCE,), "logs"),
]


class TestGranularTools:
    @pytest.mark.parametrize(("tool", "args", "service"), GRANULAR_TOOLS)
    def test_each_tool_touches_only_its_own_aws_api(self, tool, args, service) -> None:
        cred_mgr = StubbedCredMgr()  # no responses queued: every call fails and degrades

        tool(cred_mgr, *args)

        assert set(cred_mgr.asked_for) == {service}

    def test_secrets_manager_is_its_own_tool(self) -> None:
        cred_mgr = StubbedCredMgr()
        cred_mgr.stub("secretsmanager").add_response(
            "get_secret_value", {"SecretString": '{"username": "u", "password": "p"}'}
        )

        get_database_credentials(cred_mgr, SECRET_ARN)

        assert cred_mgr.asked_for == ["secretsmanager"]

    @pytest.mark.parametrize(
        ("flag", "tool"),
        [
            ("collect_cloudwatch_metrics", "get_cloudwatch_metrics"),
            ("collect_performance_insights", "get_performance_insights_queries"),
            ("collect_database_insights", "get_database_insights"),
        ],
    )
    def test_the_collector_runs_only_the_sources_it_is_asked_for(self, flag, tool) -> None:
        inp = _collector_input(**{flag: False})
        spies = {name: MagicMock(return_value={"available": False}) for name in _AWS_TOOL_NAMES}

        with _patched_tools(spies):
            mysql_collector._collect_aws_raw(inp, MagicMock())

        assert spies[tool].call_count == 0
        assert spies["get_rds_instance_metadata"].call_count == 1

    def test_every_source_is_collected_when_none_is_turned_off(self) -> None:
        spies = {name: MagicMock(return_value={"available": False}) for name in _AWS_TOOL_NAMES}

        with _patched_tools(spies):
            mysql_collector._collect_aws_raw(_collector_input(), MagicMock())

        assert all(spy.call_count == 1 for spy in spies.values())


_AWS_TOOL_NAMES = [
    "get_rds_instance_metadata",
    "get_cloudwatch_metrics",
    "get_performance_insights_queries",
    "get_database_insights",
    "get_pi_counter_metrics",
    "get_enhanced_monitoring_cpu",
]


def _patched_tools(spies: dict):
    from contextlib import ExitStack

    stack = ExitStack()
    for name, spy in spies.items():
        stack.enter_context(patch.object(mysql_collector, name, spy))
    return stack


def _collector_input(**aws_flags: bool) -> CollectorInput:
    return CollectorInput.model_validate(
        {
            "job_id": "j",
            "engine": "mysql",
            "cluster_endpoint": "db.example.internal",
            "port": 3306,
            "database_name": "app",
            "live_config": {"secret_arn": SECRET_ARN, "automation_instance_id": "i-0123"},
            "aws_config": {"region": "us-east-1", "db_instance_identifier": INSTANCE, **aws_flags},
        }
    )


# --- Decision 3: graceful degradation -------------------------------------------------------


def _access_denied() -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Op")


class TestGracefulDegradation:
    @pytest.mark.parametrize(("tool", "args", "service"), GRANULAR_TOOLS)
    def test_an_optional_tool_returns_unavailable_when_its_api_errors(
        self, tool, args, service
    ) -> None:
        cred_mgr = StubbedCredMgr()  # an unexpected call raises StubResponseError

        result = tool(cred_mgr, *args)

        assert result["available"] is False
        assert result["error"]

    @pytest.mark.parametrize(("tool", "args", "service"), GRANULAR_TOOLS)
    def test_an_optional_tool_returns_unavailable_when_no_client_can_be_made(
        self, tool, args, service
    ) -> None:
        cred_mgr = MagicMock()
        cred_mgr.client.side_effect = _access_denied()

        result = tool(cred_mgr, *args)

        assert result["available"] is False
        assert "AccessDenied" in result["error"]

    def test_the_rds_tool_reports_a_permission_error_as_unavailable(self) -> None:
        cred_mgr = StubbedCredMgr()
        cred_mgr.stub("rds").add_client_error(
            "describe_db_instances", service_error_code="AccessDenied"
        )

        result = get_rds_instance_metadata(cred_mgr, INSTANCE)

        assert result["available"] is False
        assert "AccessDenied" in result["error"]

    def test_critical_credentials_fail_fast(self) -> None:
        cred_mgr = StubbedCredMgr()
        cred_mgr.stub("secretsmanager").add_client_error(
            "get_secret_value", service_error_code="AccessDeniedException"
        )

        with pytest.raises(ClientError):
            get_database_credentials(cred_mgr, SECRET_ARN)

    def test_a_failed_database_connection_stops_a_live_collection(self) -> None:
        inp = _collector_input()
        db = MagicMock()
        db.get_version.side_effect = RuntimeError("cannot reach the database")

        with (
            patch.object(mysql_collector, "_build_cred_mgr", return_value=MagicMock()),
            patch.object(mysql_collector, "SSMExecutor"),
            patch.object(mysql_collector, "MySQLRemoteCollector", return_value=db),
            pytest.raises(RuntimeError, match="cannot reach the database"),
        ):
            mysql_collector._collect_live(inp, mysql_collector.NoopCheckpointStore())

    def test_a_failed_schema_collection_stops_a_live_collection(self) -> None:
        inp = _collector_input()
        db = MagicMock()
        db.get_version.return_value = "8.0"
        db.get_database_size_gb.return_value = 1.0

        with (
            patch.object(mysql_collector, "_build_cred_mgr", return_value=MagicMock()),
            patch.object(mysql_collector, "SSMExecutor"),
            patch.object(mysql_collector, "MySQLRemoteCollector", return_value=db),
            patch.object(
                mysql_collector, "_collect_schema_raw", side_effect=RuntimeError("no schema")
            ),
            pytest.raises(RuntimeError, match="no schema"),
        ):
            mysql_collector._collect_live(inp, mysql_collector.NoopCheckpointStore())

    def test_the_collector_carries_on_with_partial_data_when_every_optional_tool_fails(
        self,
    ) -> None:
        inp = _collector_input()
        unavailable = {"available": False, "error": "denied"}
        spies = {name: MagicMock(return_value=dict(unavailable)) for name in _AWS_TOOL_NAMES}

        with _patched_tools(spies):
            result = mysql_collector._collect_aws_raw(inp, MagicMock())

        assert result == {}


# --- Decision 4: pre-aggregated summaries ---------------------------------------------------

CLOUDWATCH_WINDOW = timedelta(days=7)


def _queue_cloudwatch(cred_mgr: StubbedCredMgr, days: int = 7) -> None:
    stubber = cred_mgr.stub("cloudwatch")
    for _ in rds_tools._CW_METRICS:
        stubber.add_response("get_metric_statistics", {"Datapoints": hourly_datapoints(days)})


class TestPreAggregatedSummaries:
    def test_cloudwatch_asks_for_seven_days_at_hourly_resolution(self) -> None:
        cred_mgr = StubbedCredMgr()
        calls = capture_params(cred_mgr, "cloudwatch", "GetMetricStatistics")
        _queue_cloudwatch(cred_mgr)

        get_cloudwatch_metrics(cred_mgr, INSTANCE)

        assert len(calls) == len(rds_tools._CW_METRICS)
        for params in calls:
            assert params["Period"] == 3600
            assert params["EndTime"] - params["StartTime"] == CLOUDWATCH_WINDOW
            assert {"Average", "Maximum"} <= set(params["Statistics"])

    def test_168_hourly_points_come_back_as_one_average_and_maximum_per_metric(self) -> None:
        cred_mgr = StubbedCredMgr()
        _queue_cloudwatch(cred_mgr)

        result = get_cloudwatch_metrics(cred_mgr, INSTANCE)

        assert result["available"] is True
        for key in ("cpu_utilization", "read_iops", "write_iops", "database_connections"):
            assert set(result[key]) >= {"avg", "max"}
            assert isinstance(result[key]["avg"], float)
            assert result[key]["max"] == 30.0  # one summary, not 168 data points
        assert len(result) <= len(rds_tools._CW_METRICS) + 1  # the metrics plus "available"

    def test_performance_insights_asks_for_seven_days_hourly_and_a_bounded_top_list(self) -> None:
        cred_mgr = StubbedCredMgr()
        calls = capture_params(cred_mgr, "pi", "DescribeDimensionKeys")
        pi = cred_mgr.stub("pi")
        now = datetime.now(UTC)
        pi.add_response(
            "describe_dimension_keys",
            {
                "AlignedStartTime": now,
                "AlignedEndTime": now,
                "Keys": [
                    {
                        "Dimensions": {
                            "db.sql.id": "S1",
                            "db.sql.statement": "SELECT * FROM users",
                            "db.sql.tokenized_id": "T1",
                        },
                        "Total": 2.0,
                    }
                ],
            },
        )
        pi.add_response(
            "get_dimension_key_details",
            {"Dimensions": [{"Dimension": "db.sql.statement", "Value": "SELECT * FROM users u"}]},
        )
        pi.add_response("describe_dimension_keys", {"Keys": []})

        result = get_performance_insights_queries(cred_mgr, RESOURCE_ID)

        top_sql = calls[0]
        assert top_sql["PeriodInSeconds"] == 3600
        assert top_sql["EndTime"] - top_sql["StartTime"] == CLOUDWATCH_WINDOW
        assert top_sql["GroupBy"]["Limit"] == 25
        assert top_sql["Metric"] == "db.load.avg"
        assert [q["query_id"] for q in result["query_patterns"]] == ["S1"]
