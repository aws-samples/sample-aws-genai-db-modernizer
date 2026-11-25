# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each wait loop gives up once its monotonic deadline passes (#2599).

The fake ``time`` reads a monotonic clock that is inside the deadline on the
first check and past it on the second, and its ``sleep`` is a no-op, so each
loop polls once, sleeps once and takes its timeout path. A deadline built on
the wall clock reads a ``time`` the fake lacks, and fails.
"""

from __future__ import annotations

import logging
import types
from unittest.mock import MagicMock

import pytest

from src.agents.load_test.documentdb import provisioner as documentdb_provisioner
from src.agents.load_test.elasticache import provisioner as elasticache_provisioner
from src.agents.load_test.opensearch import provisioner as opensearch_provisioner
from src.tools.aws import automation


def _clock_past_the_deadline(slept: list[float]) -> types.SimpleNamespace:
    readings = iter([0.0, 1.0, 10_000_000.0])
    return types.SimpleNamespace(monotonic=lambda: next(readings), sleep=slept.append)


def test_the_documentdb_cluster_wait_times_out(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(documentdb_provisioner, "time", _clock_past_the_deadline(slept))
    provisioner = object.__new__(documentdb_provisioner.DocumentDBProvisioner)
    provisioner.docdb = MagicMock()
    provisioner.docdb.describe_db_clusters.return_value = {"DBClusters": [{"Status": "creating"}]}

    with pytest.raises(RuntimeError, match="did not become available"):
        provisioner._wait_cluster_available("c-1")

    assert provisioner.docdb.describe_db_clusters.call_count == 1
    assert len(slept) == 1


def test_the_elasticache_replication_group_wait_times_out(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(elasticache_provisioner, "time", _clock_past_the_deadline(slept))
    provisioner = object.__new__(elasticache_provisioner.ElastiCacheProvisioner)
    provisioner.client = MagicMock()
    provisioner.client.describe_replication_groups.return_value = {
        "ReplicationGroups": [{"Status": "creating"}]
    }

    with pytest.raises(TimeoutError, match="not available after 15 minutes"):
        provisioner._wait_for_available("rg-1")

    assert provisioner.client.describe_replication_groups.call_count == 1
    assert len(slept) == 1


def test_the_opensearch_domain_wait_times_out(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(opensearch_provisioner, "time", _clock_past_the_deadline(slept))
    provisioner = object.__new__(opensearch_provisioner.OpenSearchProvisioner)
    provisioner.client = MagicMock()
    provisioner.client.describe_domain.return_value = {"DomainStatus": {"Processing": True}}

    with pytest.raises(TimeoutError, match="not active after 40 minutes"):
        provisioner._wait_for_active("d-1")

    assert provisioner.client.describe_domain.call_count == 1
    assert len(slept) == 1


def test_the_ssm_wait_warns_and_returns_at_its_deadline(monkeypatch, caplog):
    slept: list[float] = []
    monkeypatch.setattr(automation, "time", _clock_past_the_deadline(slept))
    ssm = MagicMock()
    ssm.describe_instance_information.return_value = {"InstanceInformationList": []}
    monkeypatch.setattr(automation, "boto3", MagicMock(client=MagicMock(return_value=ssm)))

    with caplog.at_level(logging.WARNING, logger=automation.__name__):
        automation._wait_for_ssm("i-1", "us-east-1")

    assert "SSM agent not online after 180s: i-1" in caplog.text
    assert ssm.describe_instance_information.call_count == 1
    assert len(slept) == 1
