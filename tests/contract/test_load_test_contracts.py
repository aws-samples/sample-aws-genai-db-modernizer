"""Contract tests for load test Pydantic models."""
import pytest
from pydantic import ValidationError

from src.contracts.load_test_models import (
    DeployedResource,
    InfrastructureManifest,
    LatencyPercentiles,
    LoadTestInput,
    LoadTestOutput,
    PatternResult,
    SeedSummary,
    TestConfig,
)


class TestTestConfig:
    def test_defaults(self):
        config = TestConfig()
        assert config.duration_minutes == 15
        assert config.min_iterations_per_pattern == 10000
        assert config.scale_factor == 1.0
        assert config.zipfian_alpha == 1.07
        assert config.warmup_seconds == 30

    def test_duration_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            TestConfig(duration_minutes=0)

    def test_duration_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            TestConfig(duration_minutes=60)

    def test_scale_factor_bounds(self):
        TestConfig(scale_factor=0.1)  # min ok
        TestConfig(scale_factor=10.0)  # max ok
        with pytest.raises(ValidationError):
            TestConfig(scale_factor=0.0)


class TestLoadTestInput:
    def test_valid_input(self):
        inp = LoadTestInput(
            database_name="wordpress",
            job_id="job-123",
            schema_version=1,
            target_engine="dynamodb",
            test_config=TestConfig(),
        )
        assert inp.target_engine == "dynamodb"

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            LoadTestInput(database_name="wordpress")


class TestLatencyPercentiles:
    def test_valid_percentiles(self):
        lp = LatencyPercentiles(p50=3.2, p90=5.1, p95=7.8, p99=12.4, p999=45.2, min=1.0, max=100.0)
        assert lp.p50 == 3.2

    def test_negative_latency_rejected(self):
        with pytest.raises(ValidationError):
            LatencyPercentiles(p50=-1.0, p90=5.1, p95=7.8, p99=12.4, p999=45.2, min=1.0, max=100.0)


class TestPatternResult:
    def test_round_trip_serialization(self):
        result = PatternResult(
            query_id="abc123",
            access_pattern_description="Get user by ID",
            original_query_text="SELECT * FROM users WHERE id = ?",
            operation_type="single",
            steps=["GetItem"],
            source_latency_ms=LatencyPercentiles(
                p50=14.0, p90=20.0, p95=25.0, p99=50.0, p999=100.0, min=5.0, max=200.0
            ),
            target_latency_ms=LatencyPercentiles(
                p50=3.0, p90=5.0, p95=7.0, p99=12.0, p999=45.0, min=1.0, max=80.0
            ),
            improvement_factor=4.67,
            throughput_rps=8.3,
            total_requests=10000,
            error_count=0,
            error_rate_pct=0.0,
            throttle_count=0,
            cost_per_operation_usd=0.0000025,
            consumed_capacity_avg=0.5,
            code_artifact_path="load-test/v1/scripts/abc123.js",
        )
        data = result.model_dump()
        restored = PatternResult.model_validate(data)
        assert restored.query_id == "abc123"
        assert restored.improvement_factor == 4.67


class TestLoadTestOutput:
    def test_valid_output(self):
        output = LoadTestOutput(
            run_id="test123abc456",
            version=1,
            target_engine="dynamodb",
            test_duration_minutes=15.0,
            total_patterns_tested=3,
            patterns_passed=3,
            patterns_failed=0,
            total_cost_usd=0.05,
            cost_explorer_verified=False,
            infrastructure_deployed=InfrastructureManifest(
                resources=[
                    DeployedResource(
                        resource_type="AWS::DynamoDB::Table",
                        resource_arn="arn:aws:dynamodb:us-east-1:123:table/test",
                        configuration={"capacity_mode": "on-demand"},
                    )
                ],
                tags={"job_id": "job-123", "run_id": "run-456"},
            ),
            seed_summary=SeedSummary(
                total_records=100000,
                entities={"USER": 100000},
                relationships={},
                seed_duration_seconds=45.0,
                key_registry_path="wordpress/job-123/load-test/v1/key-registry.json",
            ),
            pattern_results=[],
            assumptions=["on-demand capacity mode", "uniform write distribution"],
        )
        assert output.version == 1
        assert output.infrastructure_deployed.tags["run_id"] == "run-456"
