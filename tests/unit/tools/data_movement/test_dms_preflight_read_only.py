# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 preflight only reads (#1799)."""

from __future__ import annotations

from pathlib import Path

from tests.unit.tools.data_movement.read_only_scan import READ_ONLY_PREFIXES, aws_operations

MODULE = (
    Path(__file__).resolve().parents[4] / "src" / "tools" / "data_movement" / "dms_preflight.py"
)
SERVICES = ("rds", "dms", "secretsmanager", "iam", "ec2", "kms")


def test_the_preflight_only_reads():
    ops = {op for _, op in aws_operations(MODULE, SERVICES)}
    assert ops == {
        "describe_db_clusters", "describe_db_cluster_parameters", "describe_db_instances", "describe_db_parameters",
        "describe_secret", "get_resource_policy", "describe_key", "describe_orderable_replication_instances",
        "get_role", "list_attached_role_policies", "describe_certificates", "describe_subnets",
        "describe_vpc_endpoints", "describe_route_tables",
    }  # fmt: skip
    assert all(op.startswith(READ_ONLY_PREFIXES) for op in ops) and "get_secret_value" not in ops


def test_clients_come_only_from_the_credential_manager():
    text = MODULE.read_text()
    assert "import boto3" not in text and "boto3." not in text
    assert text.count(".client(") == 6  # PreflightClients.from_manager, one per service
