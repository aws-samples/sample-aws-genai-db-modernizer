# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 gate only reads (#1800)."""

from __future__ import annotations

from pathlib import Path

from tests.unit.tools.data_movement.read_only_scan import READ_ONLY_PREFIXES, aws_operations

MODULE = Path(__file__).resolve().parents[4] / "src" / "tools" / "data_movement" / "dms_gate.py"
SERVICES = ("dms", "cloudwatch", "cloudformation")


def test_the_gate_only_reads():
    ops = {op for _, op in aws_operations(MODULE, SERVICES)}
    assert ops == {"describe_stacks", "describe_replication_tasks", "list_tags_for_resource",
                   "describe_table_statistics", "describe_replication_instances", "get_metric_data",
                   "list_metrics"}  # fmt: skip
    assert all(op.startswith(READ_ONLY_PREFIXES) for op in ops)


def test_clients_come_only_from_the_credential_manager():
    text = MODULE.read_text()
    assert "import boto3" not in text and "boto3." not in text
    assert text.count(".client(") == 3  # GateClients.from_manager, one per service
