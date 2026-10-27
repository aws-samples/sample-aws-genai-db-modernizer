# Load Testing: Adding a New Engine

This guide explains how to add load testing support for a new target engine. DynamoDB, DocumentDB, ElastiCache and OpenSearch are implemented (`src/agents/load_test/{documentdb,dynamodb,elasticache,opensearch}/`, registered in `create_engine_components()` in `handler.py`); the DynamoDB implementation is the reference. The engines without a load test are the two Aurora engines. Neptune and Keyspaces are catalog placeholders that are never routed (ADR-031 Decision 9), so a load test for them has nothing to test.

## Architecture Overview

The load testing stage uses an engine-agnostic coordinator (`handler.py`) that delegates to four engine-specific components via abstract base classes:

```
src/agents/load_test/
├── base.py                    # Abstract base classes (do not change)
├── handler.py                 # Engine-agnostic coordinator (a new engine only edits the factory and SUPPORTED_ENGINES)
├── models.py                  # Shared models: SeedManifest, RunResult
├── dynamodb/                  # Reference implementation
│   ├── __init__.py
│   ├── provisioner.py         # BaseProvisioner → DynamoDBProvisioner
│   ├── seeder.py              # BaseSeeder → DynamoDBSeeder
│   ├── script_generator.py    # BaseScriptGenerator → DynamoDBScriptGenerator
│   ├── runner.py              # BaseRunner → K6Runner
│   ├── models.py              # Engine-specific models
│   ├── key_condition_parser.py
│   └── templates/             # Jinja2 templates for k6 scripts
│       ├── main.js.j2
│       ├── helpers/
│       │   ├── aws-client.js
│       │   ├── key-generator.js
│       │   └── metrics-collector.js
│       └── operations/
│           ├── GetItem.js.j2
│           ├── Query.js.j2
│           ├── PutItem.js.j2
│           ├── UpdateItem.js.j2
│           ├── DeleteItem.js.j2
│           ├── BatchGetItem.js.j2
│           └── BatchWriteItem.js.j2
└── {your_engine}/             # YOUR NEW ENGINE HERE
    ├── __init__.py
    ├── provisioner.py
    ├── seeder.py
    ├── script_generator.py
    ├── runner.py              # Can reuse K6Runner if using k6
    └── templates/
```

## Step-by-Step Implementation

### Step 1: Create the Engine Directory

```bash
mkdir -p src/agents/load_test/{engine}/templates/operations
touch src/agents/load_test/{engine}/__init__.py
```

### Step 2: Implement BaseProvisioner

Create `provisioner.py`. Your provisioner must:

1. **Create target infrastructure** from the schema design output
2. **Tag all resources** with the provided tags dict (contains `job_id`, `run_id`, `database_name`)
3. **Return an `InfrastructureManifest`** listing all deployed resources
4. **Implement teardown** that deletes everything in the manifest

```python
from src.agents.load_test.base import BaseProvisioner
from src.contracts.load_test_models import DeployedResource, InfrastructureManifest

class OpenSearchProvisioner(BaseProvisioner):
    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def provision(self, schema_output: dict, tags: dict[str, str]) -> InfrastructureManifest:
        # 1. Parse schema_output for index definitions
        # 2. Create OpenSearch domain/indexes
        # 3. Apply tags for cost attribution
        # 4. Wait for resources to be active
        resources = [
            DeployedResource(
                resource_type="AWS::OpenSearchService::Domain",
                resource_id="LoadTest_my-domain",
                arn="arn:aws:es:...",
                configuration={"domain_name": "...", "endpoint": "..."},
            )
        ]
        return InfrastructureManifest(resources=resources, tags=tags)

    def teardown(self, manifest: InfrastructureManifest) -> None:
        # Delete all resources in manifest
        # Handle "not found" gracefully (log warning, don't raise)
        pass
```

**Key conventions:**

- Prefix resource names with `LoadTest_` for easy identification and IAM scoping
- Use on-demand/serverless capacity where available (minimize cost)
- Wait for resources to be ACTIVE before returning
- Teardown must be idempotent (handle already-deleted resources)

### Step 3: Implement BaseSeeder

Create `seeder.py`. Your seeder must:

1. **Generate synthetic data** based on the schema design output (entities, relationships, cardinalities)
2. **Write data to the provisioned infrastructure**
3. **Return a `SeedManifest`** with resource details and key ranges that the script generator uses

```python
from src.agents.load_test.base import BaseSeeder
from src.agents.load_test.models import SeedManifest

class OpenSearchSeeder(BaseSeeder):
    def seed(self, schema_output: dict, max_items_per_table: int = 10_000) -> SeedManifest:
        # 1. Parse schema output for document structure
        # 2. Generate synthetic documents
        # 3. Bulk-index into OpenSearch
        # 4. Return manifest with index names and document counts
        return SeedManifest(
            resources={
                "posts_index": {
                    "index_name": "LoadTest_posts",
                    "document_count": 10000,
                    "fields": ["title", "body", "author_id", "created_at"],
                }
            },
            total_items=10000,
            duration_seconds=12.5,
        )
```

**Key conventions:**

- `max_items_per_table` is the upper bound per collection/index/table
- The `resources` dict keys should match the logical entity names from the schema output
- Include enough info in each resource entry for the script generator to produce realistic queries (field names, key ranges, cardinality)

### Step 4: Implement BaseScriptGenerator

Create `script_generator.py`. Your generator must:

1. **Generate one k6 scenario per access pattern** (the test script IS the customer deliverable)
2. **Generate a `main.js` entry point** that imports all scenarios with `constant-arrival-rate` executors
3. **Return the path to a temporary directory** containing all generated scripts

```python
from src.agents.load_test.base import BaseScriptGenerator
from src.agents.load_test.models import SeedManifest
from src.contracts.load_test_models import TestConfig

class OpenSearchScriptGenerator(BaseScriptGenerator):
    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def generate_scenario(self, access_pattern: dict, table_definition: dict, seed_info: dict) -> str:
        # Generate k6 JavaScript for one access pattern
        # For OpenSearch: use k6 HTTP module (OpenSearch is REST-native)
        # Include: endpoint, auth (SigV4), request body, response validation
        return "// k6 scenario code..."

    def generate_main(self, scenarios: list, duration_minutes: int, warmup_seconds: int) -> str:
        # Generate main.js with constant-arrival-rate executors
        # Each scenario rate = access_pattern["design_rps"]
        return "// main.js code..."

    def generate_all(self, access_patterns, schema_output, seed_manifest, test_config):
        # Orchestrate generation, write to tempdir, return path
        # Copy helper files (metrics-collector, etc.)
        pass
```

**Key conventions:**

- Use Jinja2 templates in `templates/operations/` for each operation type
- Each scenario must track custom k6 metrics for cost measurement (e.g., `consumed_rcu_{query_id}`)
- The `main.js` must include a `handleSummary()` function that writes JSON summary
- Generated code must be clean, readable, copy-paste ready for customers

### Step 5: Implement BaseRunner (or Reuse K6Runner)

If your engine uses k6 (recommended), you can **reuse `K6Runner`** from the DynamoDB implementation:

```python
from src.agents.load_test.dynamodb.runner import K6Runner
```

The `K6Runner` already handles:

- Dry-run validation via `k6 inspect`
- Full execution with `--summary-export` fallback
- Per-scenario latency extraction from k6 summary JSON
- Per-scenario iteration counting via `requests_X` counters

If your engine needs a different load generator, implement `BaseRunner`:

```python
from src.agents.load_test.base import BaseRunner
from src.agents.load_test.models import RunResult

class CustomRunner(BaseRunner):
    def dry_run(self, scripts_dir: str, env_vars: dict) -> bool:
        # Validate scripts without full execution
        pass

    def run(self, scripts_dir: str, duration_minutes: int, env_vars: dict) -> RunResult:
        # Execute the load test, return results
        pass
```

### Step 6: Register in the Factory

Update `handler.py` to register your engine in the `create_engine_components()` factory:

```python
def create_engine_components(target_engine: str, region: str):
    match target_engine:
        case "dynamodb":
            from src.agents.load_test.dynamodb import (...)
            return (...)
        case "opensearch":
            from src.agents.load_test.opensearch import (
                OpenSearchProvisioner,
                OpenSearchScriptGenerator,
                OpenSearchSeeder,
            )
            from src.agents.load_test.dynamodb.runner import K6Runner
            return (
                OpenSearchProvisioner(region=region),
                OpenSearchSeeder(region=region),
                OpenSearchScriptGenerator(region=region),
                K6Runner(),
            )
        case _:
            raise ValueError(f"Unsupported engine: {target_engine}")
```

Also add the engine to the `SUPPORTED_ENGINES` set in `run_load_test()`. Any engine outside the set is skipped with a `skipped` `result.json`:

```python
SUPPORTED_ENGINES = {"dynamodb", "documentdb", "elasticache", "opensearch"}
if target_engine not in SUPPORTED_ENGINES:
    log.info("load_test_skipped", reason=f"not implemented for {target_engine}")
    ...
```

### Step 7: AWS Permissions

Load tests run locally (`scripts/run_load_test.py`) or inside the
`agent-load-test` container, using whichever AWS credentials are active —
there is no hosted IAM role to update (the IAM role template this step used
to point at was retired with the hosted deployment, [#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175)).
Make sure the active credentials can create and delete your engine's
`LoadTest_*`-prefixed resources, e.g. for OpenSearch:

```json
{
  "Effect": "Allow",
  "Action": ["es:CreateDomain", "es:DeleteDomain", "es:ESHttpPost", "es:ESHttpGet", "es:ESHttpPut"],
  "Resource": "arn:aws:es:*:*:domain/loadtest-*"
}
```

### Step 8: Write Tests

Tests are flat files under `tests/unit/agents/load_test/`, named `test_<engine>_<part>.py`:

```
tests/unit/agents/load_test/
├── test_<engine>_provisioner.py       # Mock boto3, verify resource creation/deletion
├── test_<engine>_seeder.py            # Mock boto3, verify data generation
├── test_<engine>_script_generator.py  # Verify generated JS is syntactically valid
└── test_<engine>_runner.py            # If the engine has its own runner
```

Use the existing engines' tests as reference, for example `test_opensearch_provisioner.py` and `test_elasticache_runner.py`. The DynamoDB tests have no engine prefix (`test_provisioner.py`, `test_seeder.py`, `test_script_generator.py`, `test_runner.py`).

## k6 Script Conventions

### Operation Metrics

Every scenario must emit custom metrics for cost tracking. Pattern:

```javascript
import { Counter, Trend } from 'k6/metrics';

const rcu = new Trend('consumed_rcu_QUERY_ID');
const wcu = new Trend('consumed_wcu_QUERY_ID');
const errors = new Counter('errors_QUERY_ID');
const requests = new Counter('requests_QUERY_ID');
```

### handleSummary

The `main.js` must include a `handleSummary()` function:

```javascript
export function handleSummary(data) {
  return {
    '/tmp/k6_summary.json': JSON.stringify(data),
  };
}
```

This is how the runner extracts per-scenario results.

### constant-arrival-rate

Each scenario uses `constant-arrival-rate` executor with `rate` matching `design_rps` from schema output:

```javascript
scenarios: {
  pattern_abc123: {
    executor: 'constant-arrival-rate',
    rate: 8,              // from access_pattern.design_rps
    timeUnit: '1s',
    duration: '15m',
    preAllocatedVUs: 10,
    maxVUs: 50,
  }
}
```

## Engine-Specific Notes

The notes that used to sit here described engines that are now implemented. Read their code instead:

- DocumentDB: `src/agents/load_test/documentdb/` (cluster sizing in `sizing.py`, [ADR-022](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-022-documentdb-cluster-sizing.md))
- ElastiCache: `src/agents/load_test/elasticache/` (its own `ValkeyRunner`)
- OpenSearch: `src/agents/load_test/opensearch/` (sizing in `sizing.py`, its own `OpenSearchRunner`)

Pick the provisioning, auth and cost metric of the engine you add from the same pattern: a provisioner that tags every resource, a seeder that matches the schema output's cardinality, and a runner that reports the per-pattern latency percentiles and the engine's cost metric.

## Checklist

Before submitting your PR:

- [ ] Provisioner creates and tears down all resources cleanly
- [ ] Seeder generates realistic data matching schema output cardinality
- [ ] Script generator produces valid k6 scripts (or equivalent)
- [ ] Dry-run passes (scripts parse without errors)
- [ ] Full run produces per-pattern latency percentiles
- [ ] Cost metrics are captured (engine-specific)
- [ ] Factory and `SUPPORTED_ENGINES` in `handler.py` register the new engine
- [ ] The active AWS credentials can create and delete the engine's `LoadTest_*` resources (Step 7)
- [ ] Tests written and passing
- [ ] Resource teardown is idempotent (re-run teardown doesn't fail)

## Reference

- [ADR-020: Load Testing Stage Architecture](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-020-load-testing-stage.md)
- [ADR-023: Context graph layer](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-023-context-graph-layer.md) (query journeys are served from the graph; ADR-023 superseded ADR-019)
- DynamoDB reference implementation: `src/agents/load_test/dynamodb/`
- Contracts: `src/contracts/load_test_models.py`
