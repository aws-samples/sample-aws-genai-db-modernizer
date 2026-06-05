"""DynamoDB multi-table provisioner for load testing."""

import concurrent.futures

import boto3
import structlog

from src.agents.load_test.base import BaseProvisioner
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

logger = structlog.get_logger()

BATCH_SIZE = 10


class DynamoDBProvisioner(BaseProvisioner):
    """Provisions and tears down DynamoDB tables from schema design contract."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.client = boto3.client("dynamodb", region_name=region)

    def provision(self, schema_output: dict, tags: dict[str, str]) -> InfrastructureManifest:
        """Create all DynamoDB tables with in-scope traffic, in batches of 10."""
        table_defs = self._filter_tables_with_traffic(schema_output)
        resources: list[DeployedResource] = []

        for batch_start in range(0, len(table_defs), BATCH_SIZE):
            batch = table_defs[batch_start : batch_start + BATCH_SIZE]
            batch_resources = self._provision_batch(batch, tags)
            resources.extend(batch_resources)

        return InfrastructureManifest(resources=resources, tags=tags)

    def teardown(self, manifest: InfrastructureManifest) -> None:
        """Delete all DynamoDB tables in the manifest, in batches of 10."""
        tables = [r for r in manifest.resources if r.resource_type == "AWS::DynamoDB::Table"]
        for batch_start in range(0, len(tables), BATCH_SIZE):
            batch = tables[batch_start : batch_start + BATCH_SIZE]
            for resource in batch:
                table_name = resource.configuration["table_name"]
                logger.info("deleting_dynamodb_table", table_name=table_name)
                try:
                    self.client.delete_table(TableName=table_name)
                except self.client.exceptions.ResourceNotFoundException:
                    logger.warning("table_not_found_during_teardown", table_name=table_name)

    def _filter_tables_with_traffic(self, schema_output: dict) -> list[dict]:
        """Return table definitions that have in-scope access patterns with design_rps > 0."""
        access_patterns = schema_output.get("access_patterns", [])
        tables_with_traffic: set[str] = set()
        for ap in access_patterns:
            if ap.get("in_scope", True) and ap.get("design_rps", 0) > 0:
                tables_with_traffic.add(ap["table_name"])
        return [
            td
            for td in schema_output.get("table_definitions", [])
            if td["table_name"] in tables_with_traffic
        ]

    def _provision_batch(
        self, table_defs: list[dict], tags: dict[str, str]
    ) -> list[DeployedResource]:
        """Create a batch of tables in parallel."""
        resources: list[DeployedResource] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
            futures = {
                executor.submit(self._create_single_table, td, tags): td for td in table_defs
            }
            for future in concurrent.futures.as_completed(futures):
                resources.append(future.result())
        return resources

    def _create_single_table(self, table_def: dict, tags: dict[str, str]) -> DeployedResource:
        """Create one DynamoDB table and wait for ACTIVE."""
        params = self._build_create_table_params(table_def)
        params["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
        table_name = params["TableName"]

        logger.info("creating_dynamodb_table", table_name=table_name)
        try:
            response = self.client.create_table(**params)
            table_arn = response["TableDescription"]["TableArn"]
        except self.client.exceptions.ResourceInUseException:
            logger.warning("table_already_exists", table_name=table_name)
            desc = self.client.describe_table(TableName=table_name)
            table_arn = desc["Table"]["TableArn"]

        waiter = self.client.get_waiter("table_exists")
        waiter.wait(TableName=table_name)

        return DeployedResource(
            resource_type="AWS::DynamoDB::Table",
            resource_arn=table_arn,
            configuration={"table_name": table_name},
        )

    def _build_create_table_params(self, table_def: dict) -> dict:
        """Build boto3 create_table params from contract TableDefinition."""
        pk = table_def["partition_key"]
        sk = table_def.get("sort_key")
        table_name = f"LoadTest_{table_def['table_name']}"

        key_schema = [{"AttributeName": pk["attribute_name"], "KeyType": "HASH"}]
        attr_defs = [{"AttributeName": pk["attribute_name"], "AttributeType": pk["attribute_type"]}]

        if sk:
            key_schema.append({"AttributeName": sk["attribute_name"], "KeyType": "RANGE"})
            attr_defs.append(
                {"AttributeName": sk["attribute_name"], "AttributeType": sk["attribute_type"]}
            )

        params: dict = {
            "TableName": table_name,
            "KeySchema": key_schema,
            "AttributeDefinitions": attr_defs,
            "BillingMode": "PAY_PER_REQUEST",
        }

        gsis = table_def.get("gsis", [])
        if gsis:
            params["GlobalSecondaryIndexes"] = []
            for gsi in gsis:
                gsi_key_schema = []
                for gsi_pk in gsi.get("partition_key", []):
                    gsi_key_schema.append(
                        {"AttributeName": gsi_pk["attribute_name"], "KeyType": "HASH"}
                    )
                    if not any(ad["AttributeName"] == gsi_pk["attribute_name"] for ad in attr_defs):
                        attr_defs.append(
                            {
                                "AttributeName": gsi_pk["attribute_name"],
                                "AttributeType": gsi_pk["attribute_type"],
                            }
                        )

                for gsi_sk in gsi.get("sort_key") or []:
                    gsi_key_schema.append(
                        {"AttributeName": gsi_sk["attribute_name"], "KeyType": "RANGE"}
                    )
                    if not any(ad["AttributeName"] == gsi_sk["attribute_name"] for ad in attr_defs):
                        attr_defs.append(
                            {
                                "AttributeName": gsi_sk["attribute_name"],
                                "AttributeType": gsi_sk["attribute_type"],
                            }
                        )

                projection_type = gsi.get("projection", "ALL")
                projection_spec: dict = {"ProjectionType": projection_type}
                if projection_type == "INCLUDE":
                    non_key_attrs = gsi.get("non_key_attributes") or gsi.get(
                        "projection_attributes"
                    )
                    if non_key_attrs:
                        projection_spec["NonKeyAttributes"] = non_key_attrs
                    else:
                        # Schema doesn't specify attributes; use ALL for load testing
                        projection_spec["ProjectionType"] = "ALL"

                gsi_params = {
                    "IndexName": gsi.get("gsi_name"),
                    "KeySchema": gsi_key_schema,
                    "Projection": projection_spec,
                }
                params["GlobalSecondaryIndexes"].append(gsi_params)

        return params
