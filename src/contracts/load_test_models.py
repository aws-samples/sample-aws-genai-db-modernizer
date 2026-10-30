# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pydantic models for the load testing stage.

Version History:
- 1.0 (2026-10-17, #972): First versioned. ``LoadTestOutput``
  (``results/summary.json``), ``PatternResult`` (``results/<query>.json``),
  ``InfrastructureManifest`` (``infrastructure.json``) and ``SeedManifest``
  (``seed-manifest.json``, moved here from ``src.agents.load_test.models``)
  carry ``contract_version``; a file written before this has none.
"""

from typing import Any

from pydantic import BaseModel, Field


class TestConfig(BaseModel):
    """Configuration for a load test run."""

    # Not a pytest test class despite the ``Test`` prefix — this is a domain
    # model. Tells pytest's collector to skip it (silences PytestCollectionWarning).
    __test__ = False

    duration_minutes: int = Field(default=15, ge=1, le=30)
    min_iterations_per_pattern: int = Field(default=10000, ge=10000)
    scale_factor: float = Field(default=1.0, ge=0.1, le=10.0)
    zipfian_alpha: float = Field(default=1.07, ge=1.0, le=2.0)
    warmup_seconds: int = Field(default=30, ge=10, le=60)


class LoadTestInput(BaseModel):
    """Input contract for the load test coordinator."""

    database_name: str
    job_id: str
    schema_version: int
    target_engine: str
    test_config: TestConfig = Field(default_factory=TestConfig)


class LatencyPercentiles(BaseModel):
    """Latency measurements at standard percentiles."""

    p50: float = Field(ge=0)
    p90: float = Field(ge=0)
    p95: float = Field(ge=0)
    p99: float = Field(ge=0)
    p999: float = Field(ge=0)
    min: float = Field(ge=0)
    max: float = Field(ge=0)


class PatternResult(BaseModel):
    """Load test results for a single access pattern."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )

    query_id: str
    access_pattern_description: str
    original_query_text: str
    operation_type: str
    steps: list[str]
    source_latency_ms: LatencyPercentiles
    target_latency_ms: LatencyPercentiles
    improvement_factor: float
    throughput_rps: float
    total_requests: int
    error_count: int
    error_rate_pct: float
    throttle_count: int
    cost_per_operation_usd: float
    consumed_capacity_avg: float
    code_artifact_path: str


class DeployedResource(BaseModel):
    """A single deployed AWS resource."""

    resource_type: str
    resource_arn: str
    configuration: dict


class InfrastructureManifest(BaseModel):
    """Record of all infrastructure deployed for the test."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )

    resources: list[DeployedResource]
    tags: dict[str, str]


class SeedManifest(BaseModel):
    """What was seeded. Engine-specific details live in resources dict."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )

    resources: dict[str, Any]
    total_items: int
    duration_seconds: float


class SeedSummary(BaseModel):
    """Summary of data seeding operation."""

    total_records: int
    entities: dict[str, int]
    relationships: dict[str, dict]
    seed_duration_seconds: float
    key_registry_path: str


class LoadTestOutput(BaseModel):
    """Output contract for the load test stage."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )

    run_id: str
    version: int
    target_engine: str
    test_duration_minutes: float
    total_patterns_tested: int
    patterns_passed: int
    patterns_failed: int
    total_cost_usd: float
    cost_explorer_verified: bool = False
    infrastructure_deployed: InfrastructureManifest
    seed_summary: SeedSummary
    pattern_results: list[PatternResult]
    assumptions: list[str]
