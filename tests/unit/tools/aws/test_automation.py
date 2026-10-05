"""Mocked unit tests for the SSM automation-instance provisioning helpers.

These exercise `discover_cluster`, `ensure_automation_machine` and
`add_ingress_rule` against mocked boto3 clients -- no real AWS calls, no
real resources. The end-to-end wiring (passing `automation_instance_id`
into the collector) is tracked separately, #342.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from src.tools.aws import automation


def _client_error(code: str, message: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class TestDiscoverCluster:
    @patch("src.tools.aws.automation.boto3")
    def test_discovers_from_instance(self, mock_boto3):
        rds = MagicMock()
        ec2 = MagicMock()
        mock_boto3.client.side_effect = lambda service, region_name=None: {
            "rds": rds,
            "ec2": ec2,
        }[service]

        rds.describe_db_instances.return_value = {
            "DBInstances": [
                {
                    "DBSubnetGroup": {
                        "VpcId": "vpc-123",
                        "Subnets": [
                            {"SubnetIdentifier": "subnet-1"},
                            {"SubnetIdentifier": "subnet-2"},
                        ],
                    },
                    "Endpoint": {"Port": 5432, "Address": "db.example.com"},
                    "Engine": "postgres",
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-rds"}],
                    "DBInstanceIdentifier": "my-instance",
                }
            ]
        }
        ec2.describe_vpcs.return_value = {"Vpcs": [{"CidrBlock": "10.0.0.0/16"}]}
        ec2.describe_route_tables.return_value = {"RouteTables": [{"RouteTableId": "rtb-1"}]}

        result = automation.discover_cluster("my-instance")

        assert result == {
            "vpc_id": "vpc-123",
            "subnet_id": "subnet-1",
            "subnet_id_2": "subnet-2",
            "vpc_cidr": "10.0.0.0/16",
            "route_table_id": "rtb-1",
            "rds_security_group_id": "sg-rds",
            "port": 5432,
            "engine": "postgres",
            "endpoint": "db.example.com",
            "db_instance_identifier": "my-instance",
        }
        rds.describe_db_instances.assert_called_once_with(DBInstanceIdentifier="my-instance")
        rds.describe_db_clusters.assert_not_called()

    @patch("src.tools.aws.automation.boto3")
    def test_falls_back_to_cluster_when_not_an_instance(self, mock_boto3):
        rds = MagicMock()
        ec2 = MagicMock()
        mock_boto3.client.side_effect = lambda service, region_name=None: {
            "rds": rds,
            "ec2": ec2,
        }[service]

        rds.describe_db_instances.side_effect = _client_error(
            "DBInstanceNotFound", "not found", "DescribeDBInstances"
        )
        rds.describe_db_clusters.return_value = {
            "DBClusters": [
                {
                    "Port": 3306,
                    "Engine": "aurora-mysql",
                    "Endpoint": "cluster.example.com",
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-cluster"}],
                    "DBClusterIdentifier": "my-cluster",
                    "DBSubnetGroup": "my-subnet-group",
                }
            ]
        }
        rds.describe_db_subnet_groups.return_value = {
            "DBSubnetGroups": [
                {
                    "VpcId": "vpc-456",
                    "Subnets": [{"SubnetIdentifier": "subnet-a"}],
                }
            ]
        }
        ec2.describe_vpcs.return_value = {"Vpcs": [{"CidrBlock": "10.1.0.0/16"}]}
        ec2.describe_route_tables.return_value = {"RouteTables": [{"RouteTableId": "rtb-main"}]}

        result = automation.discover_cluster("my-cluster")

        assert result["vpc_id"] == "vpc-456"
        assert result["subnet_id"] == "subnet-a"
        assert result["subnet_id_2"] == ""
        assert result["engine"] == "aurora-mysql"
        assert result["db_instance_identifier"] == "my-cluster"
        rds.describe_db_clusters.assert_called_once_with(DBClusterIdentifier="my-cluster")


class TestEnsureAutomationMachine:
    @patch("src.tools.aws.automation.boto3")
    def test_returns_existing_stack_without_deploying(self, mock_boto3):
        cfn = MagicMock()
        mock_boto3.client.return_value = cfn
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {"OutputKey": "AutomationInstanceId", "OutputValue": "i-existing"},
                        {"OutputKey": "AutomationSecurityGroupId", "OutputValue": "sg-existing"},
                    ],
                }
            ]
        }
        cfn.exceptions.ClientError = ClientError

        result = automation.ensure_automation_machine(
            vpc_id="vpc-123",
            subnet_id="subnet-1",
            vpc_cidr="10.0.0.0/16",
            route_table_id="rtb-1",
        )

        assert result["instance_id"] == "i-existing"
        assert result["security_group_id"] == "sg-existing"
        cfn.create_stack.assert_not_called()

    @patch("src.tools.aws.automation._wait_for_ssm")
    @patch("src.tools.aws.automation.boto3")
    def test_deploys_new_stack_when_none_exists(self, mock_boto3, mock_wait_for_ssm):
        cfn = MagicMock()
        mock_boto3.client.return_value = cfn
        cfn.exceptions.ClientError = ClientError
        cfn.describe_stacks.side_effect = [
            _client_error("ValidationError", "does not exist", "DescribeStacks"),
            {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": "AutomationInstanceId", "OutputValue": "i-new"},
                            {"OutputKey": "AutomationSecurityGroupId", "OutputValue": "sg-new"},
                        ]
                    }
                ]
            },
        ]
        waiter = MagicMock()
        cfn.get_waiter.return_value = waiter

        result = automation.ensure_automation_machine(
            vpc_id="vpc-123",
            subnet_id="subnet-1",
            vpc_cidr="10.0.0.0/16",
            route_table_id="rtb-1",
            subnet_id_2="subnet-2",
            s3_bucket="my-bucket",
        )

        assert result["instance_id"] == "i-new"
        assert result["security_group_id"] == "sg-new"
        cfn.create_stack.assert_called_once()
        kwargs = cfn.create_stack.call_args.kwargs
        assert kwargs["StackName"] == "modernizer-automation-vpc-123"
        param_keys = {p["ParameterKey"] for p in kwargs["Parameters"]}
        assert {"VpcId", "SubnetId", "VpcCidr", "RouteTableId", "SubnetId2", "S3BucketName"} <= (
            param_keys
        )
        waiter.wait.assert_called_once()
        mock_wait_for_ssm.assert_called_once_with("i-new", "us-east-1")


class TestAddIngressRule:
    @patch("src.tools.aws.automation.boto3")
    def test_adds_ingress_rule(self, mock_boto3):
        ec2 = MagicMock()
        mock_boto3.client.return_value = ec2

        automation.add_ingress_rule(
            rds_security_group_id="sg-rds",
            automation_security_group_id="sg-auto",
            port=5432,
        )

        ec2.authorize_security_group_ingress.assert_called_once()
        kwargs = ec2.authorize_security_group_ingress.call_args.kwargs
        assert kwargs["GroupId"] == "sg-rds"
        perm = kwargs["IpPermissions"][0]
        assert perm["FromPort"] == 5432
        assert perm["ToPort"] == 5432
        assert perm["UserIdGroupPairs"][0]["GroupId"] == "sg-auto"

    @patch("src.tools.aws.automation.boto3")
    def test_ignores_duplicate_rule(self, mock_boto3):
        ec2 = MagicMock()
        mock_boto3.client.return_value = ec2
        ec2.exceptions.ClientError = ClientError
        ec2.authorize_security_group_ingress.side_effect = _client_error(
            "InvalidPermission.Duplicate", "Duplicate rule", "AuthorizeSecurityGroupIngress"
        )

        automation.add_ingress_rule(
            rds_security_group_id="sg-rds",
            automation_security_group_id="sg-auto",
            port=5432,
        )  # must not raise

    @patch("src.tools.aws.automation.boto3")
    def test_reraises_non_duplicate_errors(self, mock_boto3):
        ec2 = MagicMock()
        mock_boto3.client.return_value = ec2
        ec2.exceptions.ClientError = ClientError
        ec2.authorize_security_group_ingress.side_effect = _client_error(
            "UnauthorizedOperation", "not allowed", "AuthorizeSecurityGroupIngress"
        )

        try:
            automation.add_ingress_rule(
                rds_security_group_id="sg-rds",
                automation_security_group_id="sg-auto",
                port=5432,
            )
        except ClientError:
            pass
        else:
            raise AssertionError("expected ClientError to propagate")
