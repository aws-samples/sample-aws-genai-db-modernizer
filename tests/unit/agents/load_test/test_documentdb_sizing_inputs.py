"""DocumentDB sizing reads every input from where the collector writes it (#1540).

The fixture is a real collector output: the trailgear sample collection run through
the offline collector, so the paths are the contract's (``metadata.source_database``,
``metrics.source_host``), not the older ``rds_metadata`` / ``source_database`` shape.
An RDS-sourced run adds the contract's ``rds_instance_metadata`` and
``rds_cloudwatch_metrics`` to it.
"""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.load_test.documentdb.sizing import (
    VCPU_HEADROOM,
    compute_conn_constraint,
    compute_replicas,
    compute_vcpu_constraint,
    derive_cluster_config,
    estimate_target_data_size_gb,
    estimate_working_set_gb,
    has_complete_metrics,
    missing_source_inputs,
    resolve_source_input,
)
from src.contracts.collector_output import CollectorOutputContract
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection

REPO = Path(__file__).resolve().parents[4]

RDS_METADATA = {
    "db_instance_identifier": "trailgear",
    "instance_class": "db.r6i.2xlarge",
    "vcpu_count": 8,
    "memory_gb": 64.0,
    "multi_az": True,
    "read_replica_count": 2,
}
CLOUDWATCH = {
    "cpu_utilization": {"avg": 40.0, "max": 70.0},
    "database_connections": {"avg": 120.0, "max": 300.0},
}


class _Config:
    scale_factor = 1.0
    max_concurrent_vus = 50


@pytest.fixture(scope="module")
def sample_output() -> dict[str, Any]:
    with zipfile.ZipFile(REPO / "docs/examples/trailgear/trailgear.zip") as z:
        text = z.read("trailgear-collection.json").decode("utf-8")
    parsed = parse_offline_collection(json.loads(extract_json_object(text, source="trailgear")))
    out = build_offline_collector_output(parsed, job_id="j", database_name="trailgear", s3_key="k")
    CollectorOutputContract.model_validate(out)
    return out


@pytest.fixture
def collector_output(sample_output: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(sample_output)


@pytest.fixture
def rds_output(collector_output: dict[str, Any]) -> dict[str, Any]:
    collector_output["metadata"]["source_database"]["rds_instance_metadata"] = dict(RDS_METADATA)
    collector_output["metrics"]["rds_cloudwatch_metrics"] = copy.deepcopy(CLOUDWATCH)
    collector_output["metrics"]["performance_metrics"]["active_connections_max"] = 300.0
    CollectorOutputContract.model_validate(collector_output)
    return collector_output


def test_a_sample_collection_reads_size_memory_and_vcpus_and_names_what_it_lacks(
    collector_output: dict[str, Any],
) -> None:
    assert resolve_source_input(collector_output, "database_size_gb") == (
        27.0,
        "metadata.source_database.database_size_gb",
    )
    assert resolve_source_input(collector_output, "memory_gb")[0] == 64.0
    assert resolve_source_input(collector_output, "vcpus") == (8, "metrics.source_host.cpu_count")
    assert not has_complete_metrics(collector_output)
    assert missing_source_inputs(collector_output) == [
        "cpu_utilization_avg_pct",
        "max_connections",
        "read_replica_count",
        "multi_az",
        "instance_class",
    ]


def test_a_missing_input_is_stated_in_the_rationale_never_defaulted_silently(
    collector_output: dict[str, Any], minimal_schema: dict[str, Any]
) -> None:
    config = derive_cluster_config(minimal_schema, collector_output, _Config())
    rationale = config.sizing_rationale
    assert rationale.strategy == "fallback"
    assert "cpu_utilization_avg_pct" in rationale.missing_inputs
    assert "cpu_utilization_avg_pct" in rationale.chosen_class_reason
    assert rationale.input_sources["database_size_gb"] == (
        "metadata.source_database.database_size_gb"
    )
    assert "not reported" in rationale.replicas_reason


def test_an_rds_collection_sizes_from_what_the_collector_writes(
    rds_output: dict[str, Any], minimal_schema: dict[str, Any]
) -> None:
    assert has_complete_metrics(rds_output)
    assert missing_source_inputs(rds_output) == []
    rationale = derive_cluster_config(minimal_schema, rds_output, _Config()).sizing_rationale
    assert rationale.strategy == "derived"
    assert rationale.source_instance_class == "db.r6i.2xlarge"
    assert rationale.source_vcpu == 8
    assert rationale.source_ram_gb == 64.0
    assert rationale.missing_inputs == []
    assert rationale.input_sources["cpu_utilization_avg_pct"] == (
        "metrics.rds_cloudwatch_metrics.cpu_utilization.avg"
    )


def test_cpu_vcpus_connections_replicas_and_size_each_reach_the_constraints(
    rds_output: dict[str, Any],
) -> None:
    _, vcpu = compute_vcpu_constraint({}, rds_output, _Config())
    assert vcpu["source_observed_vcpu"] == pytest.approx(8 * 0.40 * VCPU_HEADROOM)
    _, conn = compute_conn_constraint(rds_output, _Config())
    assert conn["source_max_connections"] == 300
    assert compute_replicas(rds_output)[0] == 2
    assert estimate_target_data_size_gb({}, rds_output, 1.0) == pytest.approx(27.0)
    assert estimate_working_set_gb(rds_output)[0] > 0


def test_the_contract_path_wins_over_an_older_shape_value_beside_it(
    rds_output: dict[str, Any],
) -> None:
    rds_output["metrics"]["cpu_utilization_avg"] = 1.0
    rds_output["metrics"]["max_connections"] = 5
    rds_output["rds_metadata"] = {
        "instance_class": "db.t3.small",
        "read_replica_count": 9,
        "multi_az": False,
        "instance_specs": {"vcpus": 2, "memory_gb": 2.0},
    }
    rds_output["source_database"] = {"database_size_gb": 1.0}
    assert resolve_source_input(rds_output, "cpu_utilization_avg_pct")[0] == 40.0
    assert resolve_source_input(rds_output, "max_connections")[0] == 300.0
    assert resolve_source_input(rds_output, "vcpus")[0] == 8
    assert resolve_source_input(rds_output, "read_replica_count")[0] == 2
    assert resolve_source_input(rds_output, "multi_az")[0] is True
    assert resolve_source_input(rds_output, "instance_class")[0] == "db.r6i.2xlarge"
    assert resolve_source_input(rds_output, "memory_gb")[0] == 64.0
    assert resolve_source_input(rds_output, "database_size_gb")[0] == 27.0


def test_the_cloudwatch_connection_maximum_backs_up_the_performance_metric(
    rds_output: dict[str, Any],
) -> None:
    rds_output["metrics"]["performance_metrics"]["active_connections_max"] = None
    assert resolve_source_input(rds_output, "max_connections") == (
        300.0,
        "metrics.rds_cloudwatch_metrics.database_connections.max",
    )


def test_the_older_shape_still_works() -> None:
    old = {
        "metrics": {
            "cache_hit_ratio_pct": 99.5,
            "cpu_utilization_avg": 30.0,
            "max_connections": 100,
        },
        "source_database": {"database_size_gb": 50.0},
        "rds_metadata": {
            "instance_class": "db.r6i.2xlarge",
            "instance_specs": {"memory_gb": 64.0, "vcpus": 8},
            "read_replica_count": 1,
            "multi_az": False,
        },
    }
    assert has_complete_metrics(old)
    assert missing_source_inputs(old) == []
    assert resolve_source_input(old, "vcpus") == (8, "rds_metadata.instance_specs.vcpus")
    assert compute_replicas(old)[0] == 1


def test_zero_is_a_reported_value_for_replicas_and_cpu_but_not_for_vcpus() -> None:
    out = {
        "metadata": {"source_database": {"rds_instance_metadata": {"read_replica_count": 0}}},
        "metrics": {"rds_cloudwatch_metrics": {"cpu_utilization": {"avg": 0.0}}},
    }
    assert resolve_source_input(out, "read_replica_count")[0] == 0
    assert resolve_source_input(out, "cpu_utilization_avg_pct")[0] == 0.0
    assert resolve_source_input({"metrics": {"source_host": {"cpu_count": 0}}}, "vcpus")[1] is None
    assert compute_replicas(out) == (0, "source is single-instance")


@pytest.fixture
def minimal_schema() -> dict[str, Any]:
    return {
        "collections": [
            {"source_tables": ["t"], "embedded_entities": [], "indexes": [{"keys": {"_id": 1}}]}
        ],
        "access_patterns": [{"operation": "find", "design_rps": 10}],
        "target_engine_version_min": "5.0.0",
    }
