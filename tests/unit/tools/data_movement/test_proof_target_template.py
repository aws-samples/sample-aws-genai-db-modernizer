# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine-proof test cluster's template (#1946): a throwaway Aurora Serverless v2 cluster
that is never reachable from a network, encrypted, tagged with the job id, and deleted with
no final snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import run_dms_step as script

REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = (
    REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement" / "routine-proof-target.yaml"
)
OPEN = ("0.0.0.0/0", "::/0")
#: The resource types the stack may create: no VPC, subnet, route, NAT or internet gateway.
ALLOWED_TYPES = {
    "AWS::RDS::DBSubnetGroup",
    "AWS::EC2::SecurityGroup",
    "AWS::RDS::DBCluster",
    "AWS::RDS::DBInstance",
}


class _AnyText:
    """Equal to any text that is not blank."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and bool(other.strip())


ANY_TEXT = _AnyText()


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    assert TEMPLATE.is_file(), f"no routine-proof test cluster template at {TEMPLATE}"
    loaded: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text())
    return loaded


def _of_type(template: dict[str, Any], kind: str) -> dict[str, Any]:
    found = [r for r in template["Resources"].values() if r["Type"] == kind]
    assert len(found) == 1, f"expected one {kind}, found {len(found)}"
    resource: dict[str, Any] = found[0]
    return resource


def test_cfn_lint_finds_nothing() -> None:
    from cfnlint.api import lint

    assert TEMPLATE.is_file(), f"no routine-proof test cluster template at {TEMPLATE}"
    matches = lint(TEMPLATE.read_text(), regions=["us-east-1"])
    assert [str(m) for m in matches] == []


def test_the_cluster_is_never_publicly_accessible_and_takes_no_inbound_traffic(template) -> None:
    writer = _of_type(template, "AWS::RDS::DBInstance")["Properties"]
    assert writer["PubliclyAccessible"] is False
    group = _of_type(template, "AWS::EC2::SecurityGroup")["Properties"]
    assert "SecurityGroupIngress" not in group
    assert not [r for r in template["Resources"].values()
                if r["Type"] in ("AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress")]  # fmt: skip
    # The default allow-all egress rule is replaced by one that reaches nothing.
    assert group["SecurityGroupEgress"] == [{"IpProtocol": "-1", "CidrIp": "127.0.0.1/32",
                                             "Description": ANY_TEXT}]  # fmt: skip
    cluster = _of_type(template, "AWS::RDS::DBCluster")["Properties"]
    assert cluster["VpcSecurityGroupIds"] == [{"Ref": "SecurityGroup"}]


def test_no_open_cidr_appears_anywhere(template) -> None:
    text = json.dumps(template)
    for cidr in OPEN:
        assert cidr not in text


def test_the_stack_creates_no_network_of_its_own(template) -> None:
    kinds = {r["Type"] for r in template["Resources"].values()}
    assert kinds == ALLOWED_TYPES
    assert template["Parameters"]["VpcId"]["Type"] == "AWS::EC2::VPC::Id"
    assert template["Parameters"]["SubnetIds"]["Type"] == "List<AWS::EC2::Subnet::Id>"


def test_the_cluster_is_encrypted_with_a_managed_secret_and_the_data_api(template) -> None:
    cluster = _of_type(template, "AWS::RDS::DBCluster")["Properties"]
    assert cluster["StorageEncrypted"] is True
    assert cluster["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in cluster
    assert cluster["EnableHttpEndpoint"] is True
    # The Data API signs in with the managed secret; IAM database authentication is not used.
    assert "EnableIAMDatabaseAuthentication" not in cluster


def test_no_log_group_is_created_outside_the_stack(template) -> None:
    """Log exports make RDS create /aws/rds/cluster/<id>/... log groups outside the stack, which
    teardown would leave behind, holding failed statements (routine text). The proof reads
    errors through the Data API, so the cluster exports none."""
    cluster = _of_type(template, "AWS::RDS::DBCluster")["Properties"]
    assert "EnableCloudwatchLogsExports" not in cluster
    assert "CloudwatchLogs" not in json.dumps(template)


def test_it_is_throwaway_with_no_final_snapshot(template) -> None:
    resources = template["Resources"]
    cluster = _of_type(template, "AWS::RDS::DBCluster")
    assert cluster["Properties"]["DeletionProtection"] is False
    # DBCluster's default DeletionPolicy is Snapshot: Delete takes no final snapshot.
    for r in resources.values():
        if r["Type"] in ("AWS::RDS::DBCluster", "AWS::RDS::DBInstance"):
            assert r["DeletionPolicy"] == "Delete" and r["UpdateReplacePolicy"] == "Delete"
    assert "snapshot" in template["Description"].lower()


def test_serverless_v2_scales_to_zero_or_half_an_acu(template) -> None:
    params = template["Parameters"]
    assert params["MinCapacity"]["AllowedValues"] == [0, 0.5]
    assert params["Engine"]["AllowedValues"] == ["aurora-postgresql", "aurora-mysql"]
    cluster = _of_type(template, "AWS::RDS::DBCluster")["Properties"]
    scaling = cluster["ServerlessV2ScalingConfiguration"]
    assert scaling["MinCapacity"] == {"Ref": "MinCapacity"}
    assert scaling["MaxCapacity"] == {"Ref": "MaxCapacity"}
    assert _of_type(template, "AWS::RDS::DBInstance")["Properties"]["DBInstanceClass"] == (
        "db.serverless"
    )


def test_every_resource_carries_the_job_id_and_purpose_tags(template) -> None:
    for logical, r in template["Resources"].items():
        tags = {t["Key"]: t["Value"] for t in r["Properties"]["Tags"]}
        assert tags.get("modernizer:job-id") == {"Ref": "JobId"}, logical
        assert tags.get("modernizer:purpose") == "routine-proof", logical


def test_the_ownership_check_expects_exactly_the_templates_resources(template) -> None:
    expected = getattr(script, "PROOF_TARGET_RESOURCES", {})
    assert {k: r["Type"] for k, r in template["Resources"].items()} == expected
