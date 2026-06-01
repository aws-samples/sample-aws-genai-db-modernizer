# Agent Contracts (Pydantic Models)

Pydantic model definitions for all agent contracts in the Database Modernizer pipeline.

## Contract Files

### Input Contracts

| File | Purpose |
|------|---------|
| `collector_input.py` | Input for collector agents (connection details, collection mode) |
| `analysis_input.py` | Input for analysis agents (collector output subset per engine) |
| `referee_input.py` | Input for referee agent |
| `schema_design_input.py` | Input for schema design agents (includes `project_schema_design_input` projection) |

### Output Contracts

| File | Purpose |
|------|---------|
| `collector_output.py` | Output from collector agents (schema, queries, metrics) |
| `analysis_output.py` | Output from analysis agents (suitability scores, access patterns) |
| `triage_output.py` | Output from referee-triage (selected engines with reasons) |
| `assignment_models.py` | Query-to-engine assignment output |
| `reality_check_output.py` | Engine consolidation and validation output |
| `schema_design_output.py` | Base class for schema design output (`SchemaDesignOutputBase`) |
| `synthesis_output.py` | Final synthesis report with weighted rankings |
| `referee_output.py` | Legacy referee output (pre-split into triage/synthesis) |

### Engine-Specific Schema Outputs

| File | Purpose |
|------|---------|
| `dynamodb_model_output.py` | DynamoDB table/GSI schema design |
| `documentdb_model_output.py` | DocumentDB collection schema design |
| `elasticache_model_output.py` | ElastiCache/Redis key pattern design |
| `opensearch_model_output.py` | OpenSearch index mapping design |

### PE Review

| File | Purpose |
|------|---------|
| `dynamodb_pe_review.py` | PE review models: `PEReviewResult`, `ChangeRequest`, `ReviewVerdict` |

### Pipeline & Interaction Models

| File | Purpose |
|------|---------|
| `phase_models.py` | Phase progression tracking (`PhaseProgression`) |
| `load_test_models.py` | Load test configuration and results |
| `schema_revision_models.py` | Schema revision request/response |
| `agent_interaction_models.py` | Agent question/answer flow models |
| `post_schema_router_output.py` | Post-schema routing decisions |

## Usage

```python
from src.contracts import (
    CollectorInput,
    CollectorOutputContract,
    AnalysisOutputContract,
    TriageOutputContract,
    RealityCheckOutputContract,
    SchemaDesignOutputBase,
    DynamoDBModelOutputContract,
    SynthesisOutputContract,
    PEReviewResult,
)
```

The `__init__.py` re-exports the most commonly used contracts.

## Documentation

- [Contract Specification](../../docs/contracts/agent-contracts-spec.md)
- [Quick Start](../../docs/contracts/QUICK_START.md)
