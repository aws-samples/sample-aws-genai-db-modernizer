# ADR-002: Structured Output with Pydantic Models

**Status:** Accepted
**Date:** 2026-02-02 (Updated)
**Deciders:** Architecture Team
**Related Issues:** Architecture Review Point #2
**Related ADRs:** ADR-001 (State Management and Checkpoints)

---

## Context

Database Modernizer Assessment agents must produce structured output conforming to strict contract schemas. We need to ensure:

1. **Contract compliance**: All agent outputs match defined schemas
2. **Type safety**: Catch errors at development time, not runtime
3. **LLM clarity**: LLM understands output format upfront (no retry for format)
4. **Developer experience**: Easy to write and maintain
5. **Portability**: Convert to JSON when needed

### Current Problem

Initial implementation guide showed unstructured output:

```python
# Agent returns unstructured text
response = self.agent(agent_input)
output = json.loads(str(response))  # ❌ Can fail, no schema validation
```

**Issues:**

- No guarantee of valid JSON
- No schema validation
- LLM doesn't know format upfront
- Manual parsing can fail
- Wastes tokens on format retries

---

## Decision

We will use **Pydantic Models for Structured Output**:

1. **Pydantic models** define output schemas (type-safe, clear)
2. **LLM structured output** enforces format upfront (no retry needed)
3. **Post-execution validation** catches edge cases (Python, 0 tokens)
4. **Automatic serialization** to JSON when needed

### Why Pydantic Over JSON Schema

**Pydantic Advantages:**

- ✅ **Type hints**: Clearer than JSON Schema for LLM
- ✅ **No format retries**: LLM knows format upfront
- ✅ **Developer friendly**: Easier to write and maintain
- ✅ **IDE support**: Autocomplete, type checking
- ✅ **Portable**: `.model_dump_json()` for JSON
- ✅ **Built-in validation**: Automatic type checking

**JSON Schema Disadvantages:**

- ❌ Verbose and complex
- ❌ LLM may still return wrong format
- ❌ Requires separate validation step
- ❌ No IDE support

---

## Architecture

### Pydantic Model Definition

```python
# src/contracts/models/collector_output.py

from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from datetime import datetime

class DatabaseMetadata(BaseModel):
    """Database instance metadata"""
    engine: str = Field(description="Database engine (mysql, postgresql, etc)")
    version: str = Field(description="Database version")
    size_gb: float = Field(description="Total database size in GB")
    table_count: int = Field(description="Total number of tables")

class TableSchema(BaseModel):
    """Individual table schema"""
    name: str = Field(description="Table name")
    row_count: int = Field(description="Approximate row count")
    size_mb: float = Field(description="Table size in MB")
    columns: List[Dict] = Field(description="Column definitions")
    indexes: List[Dict] = Field(description="Index definitions")

class CollectorOutput(BaseModel):
    """
    Collector agent output contract.

    This is the standardized output format for all collector agents.
    """
    job_id: str = Field(description="Unique job identifier")
    collector_version: str = Field(description="Collector version (e.g., 2.0.0-strands)")
    collection_timestamp: datetime = Field(description="When collection completed")

    database_metadata: DatabaseMetadata = Field(description="Database instance metadata")
    schema: Dict[str, TableSchema] = Field(description="Complete database schema")
    query_patterns: Optional[List[Dict]] = Field(
        default=None,
        description="Query performance patterns (optional)"
    )

    aws_metadata: Optional[Dict] = Field(
        default=None,
        description="AWS RDS metadata (optional, from ADR-004)"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "job_id": "job-123",
                "collector_version": "2.0.0-strands",
                "collection_timestamp": "2026-02-02T10:00:00Z",
                "database_metadata": {
                    "engine": "mysql",
                    "version": "8.0.35",
                    "size_gb": 125.5,
                    "table_count": 450
                },
                "schema": {
                    "users": {
                        "name": "users",
                        "row_count": 1000000,
                        "size_mb": 250.5,
                        "columns": [],
                        "indexes": []
                    }
                }
            }
        }
```

---

## Implementation

### Agent with Pydantic Output

```python
# src/agents/collector/mysql_collector.py

from strands import Agent
from contracts.models.collector_output import CollectorOutput

class MySQLCollectorAgent:
    """
    MySQL Collector with Pydantic structured output.

    Output is guaranteed to match CollectorOutput model.
    """

    def __init__(self, input_contract: dict):
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']

        # Create Strands Agent with Pydantic model
        self.agent = Agent(
            system_prompt=self._create_system_prompt(),
            tools=[
                connect_mysql,
                collect_schema,
                collect_query_patterns
            ],
            response_format=CollectorOutput  # ← Pydantic model
        )

        # Optional: Add validation hook for edge cases
        self.agent.post_execution_hook = self._create_validation_hook()

    def _create_system_prompt(self) -> str:
        return f"""You are a MySQL Database Collector Agent.

Your mission: Collect comprehensive metadata from MySQL.

Your Tools:
1. connect_mysql - Establish connection
2. collect_schema - Gather schema
3. collect_query_patterns - Analyze queries

Output Format: You MUST return a CollectorOutput object with these fields:
- job_id: "{self.job_id}"
- collector_version: "2.0.0-strands"
- collection_timestamp: Current timestamp in ISO 8601 format
- database_metadata: DatabaseMetadata object
- schema: Dictionary of TableSchema objects
- query_patterns: Optional list of query patterns
- aws_metadata: Optional AWS RDS metadata

The output format is enforced by Pydantic, so follow the type hints exactly.
"""

    def _create_validation_hook(self):
        """
        Optional validation hook for edge cases.

        Pydantic already validates types, but this catches business logic issues.
        """
        def hook(result: CollectorOutput, context):
            # Pydantic already validated types
            # Just check business logic

            if result.database_metadata.table_count == 0:
                raise ValidationError("Database has no tables")

            if len(result.database_schema) == 0:
                raise ValidationError("Schema collection returned no tables")

            return result

        return hook

    def collect(self) -> CollectorOutput:
        """
        Execute collection with Pydantic validation.

        Returns:
            CollectorOutput (Pydantic model, guaranteed valid)

        Raises:
            ValidationError: If output fails validation
        """
        result = self.agent(self._format_input())

        # result is already a CollectorOutput instance
        # Pydantic validated it automatically

        return result

    def collect_as_json(self) -> str:
        """
        Execute collection and return as JSON string.

        Useful for serialization to S3 or API responses.
        """
        result = self.collect()
        return result.model_dump_json(indent=2)

    def collect_as_dict(self) -> dict:
        """
        Execute collection and return as dictionary.

        Useful for passing to next agent in workflow.
        """
        result = self.collect()
        return result.model_dump()
```

---

## How It Works

### Execution Flow

```
Agent executes with tools
    ↓
Returns Pydantic model (LLM structured output)
    ↓
Pydantic validates types automatically
    ↓
    ├─ Valid? → Return CollectorOutput instance ✅
    │
    └─ Invalid? → Pydantic raises ValidationError ❌
                  (LLM returned wrong type)
```

**Key Point:** No retry loop needed - LLM knows format upfront from Pydantic type hints.

---

## Integration with ADR-001 (Checkpoints)

Pydantic models work seamlessly with checkpoint strategy:

```python
# Save checkpoint (Pydantic → JSON)
def save_checkpoint(job_id: str, stage: str, output: BaseModel):
    """Save Pydantic model to S3 as JSON"""
    json_data = output.model_dump_json(indent=2)
    s3.put_object(
        Bucket=bucket,
        Key=f"checkpoints/{job_id}/{stage}.json",
        Body=json_data
    )

# Load checkpoint (JSON → Pydantic)
def load_checkpoint(job_id: str, stage: str, model_class: type[BaseModel]):
    """Load JSON from S3 and parse as Pydantic model"""
    response = s3.get_object(
        Bucket=bucket,
        Key=f"checkpoints/{job_id}/{stage}.json"
    )
    json_data = response['Body'].read()
    return model_class.model_validate_json(json_data)
```

---

## Consequences

### Positive

✅ **Type safe**: Catch errors at development time
✅ **LLM clarity**: Type hints clearer than JSON Schema
✅ **No format retries**: LLM knows format upfront
✅ **Developer friendly**: Easy to write and maintain
✅ **IDE support**: Autocomplete, type checking
✅ **Portable**: Easy conversion to JSON
✅ **Built-in validation**: Automatic type checking
✅ **Token efficient**: No wasted tokens on format retries

### Negative

⚠️ **Python-specific**: Pydantic is Python-only (not an issue for us)
⚠️ **Learning curve**: Team needs to learn Pydantic (minimal)

---

## Alternatives Considered

### Alternative 1: JSON Schema with Retry Loop (Rejected)

**Rejected because:**

- ❌ Wastes tokens on format retries
- ❌ JSON Schema verbose and complex
- ❌ LLM may still return wrong format
- ❌ Slower (multiple LLM calls)

### Alternative 2: Unstructured Output (Rejected)

**Rejected because:**

- ❌ No type safety
- ❌ Manual parsing error-prone
- ❌ No validation until runtime

---

## Related Documents

- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)
- [ADR-003: Progress Reporting Architecture](ADR-003-progress-reporting-architecture.md)
- [ADR-004: RDS Tools and AWS Integration](ADR-004-rds-tools-and-aws-integration.md)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-01 | Architecture Team | Initial decision (JSON Schema) |
| 2.0 | 2026-02-02 | Architecture Team | Changed to Pydantic, removed retry logic |

---

**Status: Accepted and Ready for Implementation**
