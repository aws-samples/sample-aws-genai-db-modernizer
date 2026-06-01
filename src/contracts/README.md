# Agent Contracts (Pydantic Models)

Pydantic model definitions for all agent contracts.

## Contract Files

### Input Contracts

| File | Purpose |
|------|---------|
| `collector_input.py` | Input for collector agents |
| `analysis_input.py` | Input for analysis agents |
| `referee_input.py` | Input for referee agent |
| `schema_design_input.py` | Input for schema design agents (includes `project_schema_design_input` projection) |

### Output Contracts

| File | Purpose |
|------|---------|
| `collector_output.py` | Output from collector agents |
| `analysis_output.py` | Output from analysis agents |
| `referee_output.py` | Output from referee agent |
| `schema_design_output.py` | Base class for schema design output (`SchemaDesignOutputBase`) |
| `dynamodb_model_output.py` | DynamoDB-specific schema design output (`DynamoDBModelOutputContract`) |

### PE Review

| File | Purpose |
|------|---------|
| `dynamodb_pe_review.py` | PE review models: `PEReviewResult`, `ChangeRequest`, `ReviewVerdict` |

## Usage

```python
from src.contracts import (
    CollectorInput,
    AnalysisOutputContract,
    DynamoDBModelOutputContract,
    PEReviewResult,
)
```

The `__init__.py` re-exports all contracts.

## Documentation

- [Contract Specification](../../docs/contracts/agent-contracts-spec.md)
- [Quick Start](../../docs/contracts/QUICK_START.md)
