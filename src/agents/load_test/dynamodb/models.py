"""DynamoDB-specific models for load test seeding and script generation."""
from pydantic import BaseModel


class DynamoDBTableSeedInfo(BaseModel):
    """Seed info for one DynamoDB table - consumed by DynamoDBScriptGenerator."""

    table_name: str  # Actual provisioned name (e.g., "LoadTest_WpPostMeta")
    pk_attr: str  # Partition key attribute name
    pk_type: str  # "S", "N", or "B"
    pk_count: int  # Number of unique PK values seeded
    pk_pad_width: int | None  # Zero-pad width for S/B type keys (None for N)
    sk_attr: str | None  # Sort key attribute name (None if no SK)
    sk_type: str | None  # "S", "N", or "B"
    sk_count: int | None  # Unique SK values per PK
    sk_pad_width: int | None  # Zero-pad width for S/B SK keys
    items_seeded: int  # Total items written to this table
