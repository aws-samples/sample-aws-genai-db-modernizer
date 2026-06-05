"""Contract-aware DynamoDB seeder with correct key types."""

import time

import boto3
import structlog

from src.agents.load_test.base import BaseSeeder
from src.agents.load_test.models import SeedManifest

logger = structlog.get_logger()

DEFAULT_SK_PER_PK = 10
PAD_WIDTH = 4


class DynamoDBSeeder(BaseSeeder):
    """Seeds DynamoDB tables using partition_key/sort_key from the contract."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def seed(self, schema_output: dict, max_items_per_table: int = 10_000) -> SeedManifest:
        """Seed all tables that have in-scope access patterns with traffic."""
        start = time.time()
        ddb = boto3.resource("dynamodb", region_name=self.region)

        table_defs = self._filter_tables_with_traffic(schema_output)
        resources: dict = {}
        total_items = 0

        for table_def in table_defs:
            table_name = f"LoadTest_{table_def['table_name']}"
            item_count = min(max_items_per_table, table_def.get("item_count", max_items_per_table))

            items = self._generate_items(table_def, max_items=item_count)
            self._write_items(ddb, table_name, items)

            seed_info = self._build_seed_info(table_def, table_name, len(items))
            resources[table_def["table_name"]] = seed_info
            total_items += len(items)

            logger.info("seeded_table", table_name=table_name, items=len(items))

        duration = time.time() - start
        return SeedManifest(
            resources=resources, total_items=total_items, duration_seconds=round(duration, 2)
        )

    def _filter_tables_with_traffic(self, schema_output: dict) -> list[dict]:
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

    def _generate_items(self, table_def: dict, max_items: int) -> list[dict]:
        pk = table_def["partition_key"]
        sk = table_def.get("sort_key")
        pk_attr = pk["attribute_name"]
        pk_type = pk["attribute_type"]

        if sk:
            sk_attr = sk["attribute_name"]
            sk_type = sk["attribute_type"]
            sk_per_pk = DEFAULT_SK_PER_PK
            pk_count = max(1, max_items // sk_per_pk)
            items: list[dict] = []
            for pk_i in range(1, pk_count + 1):
                pk_val = self._generate_key_value(pk_type, pk_i, pk_count)
                for sk_i in range(1, sk_per_pk + 1):
                    if len(items) >= max_items:
                        break
                    sk_val = self._generate_key_value(sk_type, sk_i, sk_per_pk)
                    items.append({pk_attr: pk_val, sk_attr: sk_val})
            return items
        else:
            items = []
            for i in range(1, max_items + 1):
                pk_val = self._generate_key_value(pk_type, i, max_items)
                items.append({pk_attr: pk_val})
            return items

    def _generate_key_value(self, key_type: str, index: int, total: int) -> int | str:
        match key_type:
            case "N":
                return index
            case "S" | "B":
                width = max(PAD_WIDTH, len(str(total)))
                return str(index).zfill(width)
            case _:
                return str(index).zfill(PAD_WIDTH)

    def _write_items(self, ddb, table_name: str, items: list[dict]) -> None:
        table = ddb.Table(table_name)
        with table.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=item)

    def _build_seed_info(self, table_def: dict, table_name: str, items_seeded: int) -> dict:
        pk = table_def["partition_key"]
        sk = table_def.get("sort_key")
        pk_type = pk["attribute_type"]
        pk_pad_width = PAD_WIDTH if pk_type in ("S", "B") else None

        if sk:
            sk_per_pk = DEFAULT_SK_PER_PK
            pk_count = max(1, items_seeded // sk_per_pk)
            sk_type = sk["attribute_type"]
            sk_pad_width = PAD_WIDTH if sk_type in ("S", "B") else None
        else:
            pk_count = items_seeded
            sk_per_pk = None
            sk_type = None
            sk_pad_width = None

        return {
            "table_name": table_name,
            "pk_attr": pk["attribute_name"],
            "pk_type": pk_type,
            "pk_count": pk_count,
            "pk_pad_width": pk_pad_width,
            "sk_attr": sk["attribute_name"] if sk else None,
            "sk_type": sk_type,
            "sk_count": sk_per_pk,
            "sk_pad_width": sk_pad_width,
            "items_seeded": items_seeded,
        }
