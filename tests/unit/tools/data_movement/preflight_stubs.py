# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stubbed AWS responses for the step 1 preflight tests (#1799). Nothing is sent."""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import quote

from src.tools.data_movement.dms_preflight import PreflightClients, PreflightRequest
from tests.unit.tools.aws.aws_stubs import REGION, StubbedCredMgr

CLUSTER = "aurora-shop"
WRITER = "aurora-shop-1"
GROUP = "aurora-shop-params"
INSTANCE_GROUP = "aurora-shop-instance-params"
ACCOUNT = "111122223333"
SOURCE_SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:shop-source-AbCdEf"  # pragma: allowlist secret
TARGET_SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:shop-aurora-GhIjKl"  # pragma: allowlist secret
KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/1234abcd-12ab-34cd-56ef-1234567890ab"


SOURCE_CERT = f"arn:aws:dms:{REGION}:{ACCOUNT}:cert:SRCCERT"
TARGET_CERT = f"arn:aws:dms:{REGION}:{ACCOUNT}:cert:TGTCERT"
SUBNETS = ("subnet-aaa", "subnet-bbb")
VPC = "vpc-123"


def request(**overrides) -> PreflightRequest:
    base = {"region": REGION, "aurora_cluster_id": CLUSTER, "source_secret_arn": SOURCE_SECRET,
            "target_secret_arn": TARGET_SECRET, "source_certificate_arn": SOURCE_CERT,
            "target_certificate_arn": TARGET_CERT, "subnet_ids": SUBNETS}  # fmt: skip
    return PreflightRequest(**{**base, **overrides})


def clients(cred: StubbedCredMgr) -> PreflightClients:
    for service in ("rds", "dms", "secretsmanager", "iam", "ec2", "kms"):
        cred.stub(service)
    return PreflightClients.from_manager(cred)


def cluster(engine="aurora-mysql", version="8.0.mysql_aurora.3.08.0", status="in-sync", ident=CLUSTER,
            cluster_status="available"):  # fmt: skip
    """``status`` is the writer's DBClusterParameterGroupStatus (None leaves it out)."""
    member = {"DBInstanceIdentifier": WRITER, "IsClusterWriter": True}
    if status is not None:
        member["DBClusterParameterGroupStatus"] = status
    return {"DBClusters": [{"DBClusterIdentifier": ident, "Engine": engine, "EngineVersion": version,
                            "Status": cluster_status, "StorageEncrypted": True, "DBClusterParameterGroup": GROUP,
                            "DBClusterMembers": [member]}]}  # fmt: skip


def instance(status="in-sync", group=INSTANCE_GROUP, db_status="available"):
    """``status`` is the group's ParameterApplyStatus (None leaves it out)."""
    attached = {"DBParameterGroupName": group}
    if status is not None:
        attached["ParameterApplyStatus"] = status
    return {"DBInstances": [{"DBInstanceIdentifier": WRITER, "DBInstanceStatus": db_status,
                             "DBParameterGroups": [attached]}]}  # fmt: skip


def key(arn=KEY):
    return {"KeyMetadata": {"KeyId": arn.rsplit("/", 1)[1], "Arn": arn}}


def parameters(**values):
    return {"Parameters": [{"ParameterName": k, "ParameterValue": v} for k, v in values.items()]}


def secret(arn, kms=None, deleted=False):
    out = {"ARN": arn, "Name": arn.rsplit(":", 1)[1]}
    if kms:
        out["KmsKeyId"] = kms
    if deleted:
        out["DeletedDate"] = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    return out


def trust(service="dms.amazonaws.com", action="sts:AssumeRole", effect="Allow"):
    return {"Version": "2012-10-17", "Statement": [{"Effect": effect, "Principal": {"Service": service},
                                                    "Action": action}]}  # fmt: skip


def _encoded(doc):
    """IAM returns the trust document URL-encoded; botocore decodes it after the call."""
    return doc if isinstance(doc, str) else quote(json.dumps(doc))


def role(name, trust_doc=None):
    return {"Role": {"Path": "/service-role/", "RoleName": name, "RoleId": "AROAEXAMPLEID123456",
                     "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}",
                     "CreateDate": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                     "AssumeRolePolicyDocument": _encoded(trust_doc or trust())}}  # fmt: skip


ROLE_POLICIES = {"dms-vpc-role": "AmazonDMSVPCManagementRole",
                 "dms-cloudwatch-logs-role": "AmazonDMSCloudWatchLogsRole"}  # fmt: skip


MYSQL_PARAMS = {"binlog_format": "ROW", "binlog_row_image": "FULL"}
MYSQL_INSTANCE_PARAMS = {"local_infile": "1"}
PG_PARAMS = {"rds.logical_replication": "1"}


def queue_aurora(cred, *, engine="aurora-mysql", version="8.0.mysql_aurora.3.08.0", status="in-sync",
                 params=None, instance_status="in-sync", instance_params=None, reads_instance_params=True,
                 cluster_status="available", writer_status="available"):  # fmt: skip
    """Queue checks 1 and 2's reads, in call order. ``instance_params=None`` means the MySQL defaults the
    step needs; ``reads_instance_params=False`` leaves out the DB parameter group read (PostgreSQL has none).
    """
    rds = cred.stub("rds")
    rds.add_response("describe_db_clusters", cluster(engine, version, status, cluster_status=cluster_status),
                     {"DBClusterIdentifier": CLUSTER})  # fmt: skip
    rds.add_response("describe_db_instances", instance(instance_status, db_status=writer_status),
                     {"DBInstanceIdentifier": WRITER})  # fmt: skip
    default = MYSQL_PARAMS if engine == "aurora-mysql" else PG_PARAMS
    rds.add_response("describe_db_cluster_parameters", parameters(**(params or default)),
                     {"DBClusterParameterGroupName": GROUP})  # fmt: skip
    if reads_instance_params:
        rds.add_response("describe_db_parameters", parameters(**(instance_params or MYSQL_INSTANCE_PARAMS)),
                         {"DBParameterGroupName": INSTANCE_GROUP})  # fmt: skip


def queue_secrets_target(cred):
    sm = cred.stub("secretsmanager")
    sm.add_response("describe_secret", secret(TARGET_SECRET), {"SecretId": TARGET_SECRET})
    sm.add_response(
        "get_resource_policy",
        {"ARN": TARGET_SECRET, "Name": "shop-aurora-GhIjKl"},
        {"SecretId": TARGET_SECRET},
    )


def queue_secrets(cred, *, source=None, target=None, policy=None, target_policy=None):
    sm = cred.stub("secretsmanager")
    for arn, desc in ((SOURCE_SECRET, source), (TARGET_SECRET, target)):
        sm.add_response("describe_secret", desc or secret(arn), {"SecretId": arn})
        body = {"ARN": arn, "Name": arn.rsplit(":", 1)[1]}
        chosen = policy if arn == SOURCE_SECRET else target_policy
        if chosen:
            body["ResourcePolicy"] = chosen
        sm.add_response("get_resource_policy", body, {"SecretId": arn})


def queue_instance_class(cred, classes=("dms.t3.medium", "dms.r6i.large")):
    cred.stub("dms").add_response("describe_orderable_replication_instances",
                                  {"OrderableReplicationInstances": [{"ReplicationInstanceClass": c} for c in classes]})  # fmt: skip


def queue_roles(cred, missing=None, trust_docs=None, policies=None):
    """get_role, then list_attached_role_policies, per role. ``trust_docs`` and ``policies`` override a role's
    trust document (as returned) and its attached policy names."""
    iam = cred.stub("iam")
    for name, policy in ROLE_POLICIES.items():
        if name == missing:
            iam.add_client_error("get_role", service_error_code="NoSuchEntity", http_status_code=404,
                                 expected_params={"RoleName": name})  # fmt: skip
            continue
        iam.add_response("get_role", role(name, (trust_docs or {}).get(name)), {"RoleName": name})
        names = (policies or {}).get(name, [policy])
        iam.add_response("list_attached_role_policies", {"AttachedPolicies": [
            {"PolicyName": n, "PolicyArn": f"arn:aws:iam::aws:policy/service-role/{n}"} for n in names]},
            {"RoleName": name})  # fmt: skip


def cert(arn, valid_to=dt.datetime(2030, 1, 1, tzinfo=dt.UTC)):
    return {"CertificateArn": arn, "ValidToDate": valid_to}


def queue_cert(cred, arn, valid_to=dt.datetime(2030, 1, 1, tzinfo=dt.UTC), found=True):
    cred.stub("dms").add_response("describe_certificates",
                                  {"Certificates": [cert(arn, valid_to)] if found else []},
                                  {"Filters": [{"Name": "certificate-arn", "Values": [arn]}]})  # fmt: skip


def queue_certs(cred):
    queue_cert(cred, SOURCE_CERT)
    queue_cert(cred, TARGET_CERT)


def queue_subnets(cred, vpcs=(VPC, VPC)):
    cred.stub("ec2").add_response("describe_subnets", {"Subnets": [
        {"SubnetId": i, "VpcId": v} for i, v in zip(SUBNETS, vpcs, strict=True)]}, {"SubnetIds": list(SUBNETS)})  # fmt: skip


def queue_endpoints(cred, endpoints=()):
    cred.stub("ec2").add_response("describe_vpc_endpoints", {"VpcEndpoints": list(endpoints)}, {"Filters": [
        {"Name": "vpc-id", "Values": [VPC]},
        {"Name": "service-name", "Values": [f"com.amazonaws.{REGION}.secretsmanager"]},
        {"Name": "vpc-endpoint-type", "Values": ["Interface"]}]})  # fmt: skip


def endpoint(state="available", private_dns=True):
    return {"VpcEndpointId": "vpce-1", "State": state, "PrivateDnsEnabled": private_dns}


def queue_routes(cred, tables, *, subnet=None):
    """One describe_route_tables: by subnet association, or the VPC's main table when ``subnet`` is None."""
    flt = ([{"Name": "association.subnet-id", "Values": [subnet]}] if subnet else
           [{"Name": "vpc-id", "Values": [VPC]}, {"Name": "association.main", "Values": ["true"]}])  # fmt: skip
    cred.stub("ec2").add_response(
        "describe_route_tables", {"RouteTables": tables}, {"Filters": flt}
    )


def nat_table(state="active"):
    return {"RouteTableId": "rtb-1", "Routes": [
        {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"},
        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": state}]}  # fmt: skip


def igw_table():
    return {
        "RouteTableId": "rtb-2",
        "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}],
    }


def queue_network(cred):
    queue_subnets(cred)
    queue_endpoints(cred, [endpoint()])


def queue_ready(cred):
    """Every read of a ready cluster with a self-managed MySQL source (check 6 is a checklist)."""
    queue_aurora(cred)
    queue_secrets(cred)
    queue_instance_class(cred)
    queue_roles(cred)
    queue_certs(cred)
    queue_network(cred)
