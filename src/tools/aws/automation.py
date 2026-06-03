"""
Automation machine provisioning — discovers RDS cluster metadata,
provisions per-VPC automation machines, and manages SG ingress rules.

Used by the API layer before starting live-mode collector jobs.
DDL and offline modes do not use this module.
"""

import logging
import time

import boto3

logger = logging.getLogger(__name__)

STACK_PREFIX = "modernizer-automation"
TEMPLATE_PATH = "infrastructure/cloudformation/automation.yaml"


def discover_cluster(cluster_id: str, region: str = "us-east-1") -> dict:
    """Discover VPC, subnets, SG, port, engine, endpoint from an RDS cluster/instance ID.

    Returns dict with: vpc_id, subnet_id, subnet_id_2, vpc_cidr, route_table_id,
    rds_security_group_id, port, engine, endpoint, db_instance_identifier.
    """
    rds = boto3.client("rds", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)

    # Try instance first, then cluster
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=cluster_id)
        inst = resp["DBInstances"][0]
        vpc_id = inst["DBSubnetGroup"]["VpcId"]
        subnets = inst["DBSubnetGroup"]["Subnets"]
        port = inst["Endpoint"]["Port"]
        engine = inst["Engine"]
        endpoint = inst["Endpoint"]["Address"]
        rds_sg = inst["VpcSecurityGroups"][0]["VpcSecurityGroupId"]
        db_instance_id = inst["DBInstanceIdentifier"]
    except Exception:
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cl = resp["DBClusters"][0]
        port = cl["Port"]
        engine = cl["Engine"]
        endpoint = cl["Endpoint"]
        rds_sg = cl["VpcSecurityGroups"][0]["VpcSecurityGroupId"]
        db_instance_id = cl["DBClusterIdentifier"]
        sg_resp = rds.describe_db_subnet_groups(DBSubnetGroupName=cl["DBSubnetGroup"])
        subnets = sg_resp["DBSubnetGroups"][0]["Subnets"]
        vpc_id = sg_resp["DBSubnetGroups"][0]["VpcId"]

    subnet_id = subnets[0]["SubnetIdentifier"]
    subnet_id_2 = subnets[1]["SubnetIdentifier"] if len(subnets) > 1 else ""

    # Get VPC CIDR
    vpc_resp = ec2.describe_vpcs(VpcIds=[vpc_id])
    vpc_cidr = vpc_resp["Vpcs"][0]["CidrBlock"]

    # Get route table for the subnet
    rt_resp = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
    )
    if rt_resp["RouteTables"]:
        route_table_id = rt_resp["RouteTables"][0]["RouteTableId"]
    else:
        # Fallback: main route table for the VPC
        rt_resp = ec2.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "association.main", "Values": ["true"]},
            ]
        )
        route_table_id = rt_resp["RouteTables"][0]["RouteTableId"]

    return {
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "subnet_id_2": subnet_id_2,
        "vpc_cidr": vpc_cidr,
        "route_table_id": route_table_id,
        "rds_security_group_id": rds_sg,
        "port": port,
        "engine": engine,
        "endpoint": endpoint,
        "db_instance_identifier": db_instance_id,
    }


def ensure_automation_machine(
    vpc_id: str,
    subnet_id: str,
    vpc_cidr: str,
    route_table_id: str,
    subnet_id_2: str = "",
    s3_bucket: str = "",
    region: str = "us-east-1",
) -> dict:
    """Ensure an automation machine exists for this VPC. Returns instance_id and sg_id.

    If the CloudFormation stack already exists, returns its outputs.
    Otherwise deploys automation.yaml and waits for completion.
    """
    cfn = boto3.client("cloudformation", region_name=region)
    stack_name = f"{STACK_PREFIX}-{vpc_id[-8:]}"

    # Check if stack already exists
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        stack = resp["Stacks"][0]
        if stack["StackStatus"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
            logger.info(
                "Automation machine already exists in VPC %s: %s",
                vpc_id,
                outputs.get("AutomationInstanceId"),
            )
            return {
                "instance_id": outputs["AutomationInstanceId"],
                "security_group_id": outputs["AutomationSecurityGroupId"],
                "stack_name": stack_name,
            }
    except cfn.exceptions.ClientError:
        pass  # Stack doesn't exist

    # Deploy
    logger.info("Deploying automation machine in VPC %s", vpc_id)

    # Read template from S3 or local — in ECS, use S3; locally, use file
    import os

    template_body = None

    local_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", TEMPLATE_PATH)
    if os.path.exists(local_path):
        with open(local_path, encoding="utf-8") as f:
            template_body = f.read()

    params = [
        {"ParameterKey": "VpcId", "ParameterValue": vpc_id},
        {"ParameterKey": "SubnetId", "ParameterValue": subnet_id},
        {"ParameterKey": "VpcCidr", "ParameterValue": vpc_cidr},
        {"ParameterKey": "RouteTableId", "ParameterValue": route_table_id},
    ]
    if subnet_id_2:
        params.append({"ParameterKey": "SubnetId2", "ParameterValue": subnet_id_2})
    if s3_bucket:
        params.append({"ParameterKey": "S3BucketName", "ParameterValue": s3_bucket})

    create_kwargs: dict = {
        "StackName": stack_name,
        "Parameters": params,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Tags": [
            {"Key": "Project", "Value": "Database Modernizer Assessment"},
            {"Key": "ManagedBy", "Value": "API"},
            {"Key": "VpcId", "Value": vpc_id},
        ],
    }
    if template_body:
        create_kwargs["TemplateBody"] = template_body

    cfn.create_stack(**create_kwargs)

    # Wait for completion (max 5 min)
    waiter = cfn.get_waiter("stack_create_complete")
    waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 20})

    resp = cfn.describe_stacks(StackName=stack_name)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    logger.info("Automation machine deployed: %s", outputs.get("AutomationInstanceId"))

    # Wait for SSM agent to come online
    _wait_for_ssm(outputs["AutomationInstanceId"], region)

    return {
        "instance_id": outputs["AutomationInstanceId"],
        "security_group_id": outputs["AutomationSecurityGroupId"],
        "stack_name": stack_name,
    }


def add_ingress_rule(
    rds_security_group_id: str,
    automation_security_group_id: str,
    port: int,
    region: str = "us-east-1",
) -> None:
    """Add ingress rule to RDS SG allowing automation machine. Idempotent."""
    ec2 = boto3.client("ec2", region_name=region)
    try:
        ec2.authorize_security_group_ingress(
            GroupId=rds_security_group_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "UserIdGroupPairs": [
                        {
                            "GroupId": automation_security_group_id,
                            "Description": "Database Modernizer Assessment automation machine",
                        }
                    ],
                }
            ],
        )
        logger.info(
            "Added ingress rule: %s <- %s on port %d",
            rds_security_group_id,
            automation_security_group_id,
            port,
        )
    except ec2.exceptions.ClientError as e:
        if "Duplicate" in str(e):
            logger.info(
                "Ingress rule already exists: %s <- %s on port %d",
                rds_security_group_id,
                automation_security_group_id,
                port,
            )
        else:
            raise


def _wait_for_ssm(instance_id: str, region: str, timeout: int = 180) -> None:
    """Wait for SSM agent to report Online."""
    ssm = boto3.client("ssm", region_name=region)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
            instances = resp.get("InstanceInformationList", [])
            if instances and instances[0].get("PingStatus") == "Online":
                logger.info("SSM agent online: %s", instance_id)
                return
        except Exception:  # nosec B110
            pass
        time.sleep(10)  # nosemgrep: arbitrary-sleep  # waiting for SSM agent to come online
    logger.warning("SSM agent not online after %ds: %s", timeout, instance_id)
