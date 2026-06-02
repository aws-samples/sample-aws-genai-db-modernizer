"""Tests for DynamoDB multi-table provisioner."""
from unittest.mock import MagicMock, patch

import pytest

from src.agents.load_test.dynamodb.provisioner import DynamoDBProvisioner
from src.contracts.load_test_models import InfrastructureManifest


@pytest.fixture
def table_def_numeric_pk():
    """WordPress WpPosts table - numeric PK, no SK."""
    return {
        "table_name": "WpPosts",
        "partition_key": {"attribute_name": "ID", "attribute_type": "N"},
        "sort_key": None,
        "gsis": [],
        "item_count": 5000,
    }


@pytest.fixture
def table_def_with_sk_and_gsi():
    """WordPress WpPostMeta - numeric PK, string SK, one GSI."""
    return {
        "table_name": "WpPostMeta",
        "partition_key": {"attribute_name": "post_id", "attribute_type": "N"},
        "sort_key": {"attribute_name": "meta_key_id", "attribute_type": "S"},
        "gsis": [
            {
                "gsi_name": "MetaKeyIndex",
                "partition_key": [{"attribute_name": "meta_key", "attribute_type": "S"}],
                "sort_key": [{"attribute_name": "post_id", "attribute_type": "N"}],
                "projection": "ALL",
            }
        ],
        "item_count": 50000,
    }


class TestDynamoDBProvisioner:
    @patch("boto3.client")
    def test_build_create_table_params_numeric_pk(self, mock_boto, table_def_numeric_pk):
        provisioner = DynamoDBProvisioner(region="us-east-1")
        params = provisioner._build_create_table_params(table_def_numeric_pk)

        assert params["TableName"] == "LoadTest_WpPosts"
        assert params["BillingMode"] == "PAY_PER_REQUEST"
        assert params["KeySchema"] == [{"AttributeName": "ID", "KeyType": "HASH"}]
        assert {"AttributeName": "ID", "AttributeType": "N"} in params["AttributeDefinitions"]
        assert "GlobalSecondaryIndexes" not in params

    @patch("boto3.client")
    def test_build_create_table_params_with_sk_and_gsi(self, mock_boto, table_def_with_sk_and_gsi):
        provisioner = DynamoDBProvisioner(region="us-east-1")
        params = provisioner._build_create_table_params(table_def_with_sk_and_gsi)

        assert params["TableName"] == "LoadTest_WpPostMeta"
        assert {"AttributeName": "post_id", "KeyType": "HASH"} in params["KeySchema"]
        assert {"AttributeName": "meta_key_id", "KeyType": "RANGE"} in params["KeySchema"]
        assert len(params["GlobalSecondaryIndexes"]) == 1
        gsi = params["GlobalSecondaryIndexes"][0]
        assert gsi["IndexName"] == "MetaKeyIndex"
        assert {"AttributeName": "meta_key", "KeyType": "HASH"} in gsi["KeySchema"]
        assert {"AttributeName": "post_id", "KeyType": "RANGE"} in gsi["KeySchema"]

    @patch("boto3.client")
    def test_filter_tables_with_traffic(
        self, mock_boto, table_def_numeric_pk, table_def_with_sk_and_gsi
    ):
        provisioner = DynamoDBProvisioner(region="us-east-1")
        schema_output = {
            "table_definitions": [table_def_numeric_pk, table_def_with_sk_and_gsi],
            "access_patterns": [
                {"table_name": "WpPosts", "in_scope": True, "design_rps": 100},
                {"table_name": "WpPostMeta", "in_scope": True, "design_rps": 200},
                {"table_name": "WpUnused", "in_scope": True, "design_rps": 0},
            ],
        }
        result = provisioner._filter_tables_with_traffic(schema_output)
        assert len(result) == 2
        names = [t["table_name"] for t in result]
        assert "WpPosts" in names
        assert "WpPostMeta" in names

    @patch("boto3.client")
    def test_provision_creates_tables_and_returns_manifest(self, mock_boto, table_def_numeric_pk):
        mock_ddb = MagicMock()
        mock_boto.return_value = mock_ddb
        mock_ddb.create_table.return_value = {
            "TableDescription": {
                "TableArn": "arn:aws:dynamodb:us-east-1:123:table/LoadTest_WpPosts"
            }
        }
        mock_waiter = MagicMock()
        mock_ddb.get_waiter.return_value = mock_waiter

        provisioner = DynamoDBProvisioner(region="us-east-1")
        schema_output = {
            "table_definitions": [table_def_numeric_pk],
            "access_patterns": [{"table_name": "WpPosts", "in_scope": True, "design_rps": 100}],
        }
        manifest = provisioner.provision(schema_output, {"job_id": "test"})

        mock_ddb.create_table.assert_called_once()
        mock_waiter.wait.assert_called_once()
        assert len(manifest.resources) == 1
        assert manifest.resources[0].resource_type == "AWS::DynamoDB::Table"
        assert manifest.resources[0].configuration["table_name"] == "LoadTest_WpPosts"

    @patch("boto3.client")
    def test_teardown_deletes_all_tables(self, mock_boto):
        mock_ddb = MagicMock()
        mock_boto.return_value = mock_ddb

        provisioner = DynamoDBProvisioner(region="us-east-1")
        from src.contracts.load_test_models import DeployedResource

        manifest = InfrastructureManifest(
            resources=[
                DeployedResource(
                    resource_type="AWS::DynamoDB::Table",
                    resource_arn="arn1",
                    configuration={"table_name": "LoadTest_WpPosts"},
                ),
                DeployedResource(
                    resource_type="AWS::DynamoDB::Table",
                    resource_arn="arn2",
                    configuration={"table_name": "LoadTest_WpPostMeta"},
                ),
            ],
            tags={"job_id": "test"},
        )
        provisioner.teardown(manifest)
        assert mock_ddb.delete_table.call_count == 2
