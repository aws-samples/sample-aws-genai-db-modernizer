# ADR-009: Testing Infrastructure and Strategies

**Status:** Accepted
**Date:** 2026-02-02
**Deciders:** Architecture Team
**Related ADRs:** ADR-002 (Pydantic Output), ADR-006 (Analysis Agents), ADR-008 (Contract Versioning)

---

## Context

Database Modernizer Assessment uses AI agents (Strands SDK) with LLM interactions. Testing AI agents presents unique challenges:

- **Non-deterministic**: LLM responses vary between runs
- **Expensive**: LLM API calls cost money and time
- **Complex workflows**: Multi-agent orchestration with checkpoints
- **Contract evolution**: Multiple contract versions must coexist
- **Large databases**: Performance testing with 1000+ tables

### Requirements

- **Unit testing**: Test individual agents and tools
- **Integration testing**: Test agent workflows end-to-end
- **Contract testing**: Verify contract compatibility across versions
- **Mock strategies**: Avoid LLM calls in tests (deterministic, fast, free)
- **Performance testing**: Validate scalability (mini-collectors, large databases)
- **Regression testing**: Ensure changes don't break existing functionality

---

## Decision

We will implement **Multi-Layer Testing Strategy**:

1. **Unit Tests** - Test tools and utilities (no LLM)
2. **Agent Tests** - Test agents with mocked LLM responses
3. **Contract Tests** - Test contract compatibility and versioning
4. **Integration Tests** - Test workflows with mocked agents
5. **Performance Tests** - Test scalability with synthetic data
6. **E2E Tests** - Test full workflow with real LLM (limited, expensive)

---

## Testing Layers

### Layer 1: Unit Tests (Tools & Utilities)

**What:** Test individual tools without LLM interaction

**Examples:**

- Database connection tools
- Schema collection tools
- AWS API tools (CloudWatch, Performance Insights)
- Contract validation utilities
- Version adapters

**Pattern:**

```python
# tests/unit/tools/test_mysql_tools.py

import pytest
from unittest.mock import Mock, patch
from tools.database.mysql_tools import connect_mysql, collect_schema

@patch('mysql.connector.connect')
def test_connect_mysql_success(mock_connect):
    """Test MySQL connection with valid credentials (mocked RDS connection)"""
    # Mock RDS connection
    mock_connection = Mock()
    mock_cursor = Mock()
    mock_cursor.fetchone.return_value = ['8.0.32']
    mock_connection.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_connection

    config = {
        'endpoint': 'mydb.abc123.us-east-1.rds.amazonaws.com',
        'port': 3306,
        'database_name': 'testdb',
        'username': 'admin',
        'password': '<PASSWORD>'
    }

    result = connect_mysql(config)

    assert result['status'] == 'connected'
    assert result['database_type'] == 'mysql'
    assert 'version' in result

def test_connect_mysql_failure():
    """Test MySQL connection with invalid credentials"""
    config = {
        'endpoint': 'invalid.rds.amazonaws.com',
        'port': 3306,
        'database_name': 'testdb',
        'username': 'user',
        'password': '<PASSWORD>'
    }

    result = connect_mysql(config)

    assert result['status'] == 'error'
    assert 'error' in result
```

**Mocking:**

- Mock RDS connections (not local databases)
- Mock AWS API calls with `moto` or `boto3` stubs
- Mock CloudWatch, Performance Insights, RDS API calls
- No local database setup required

---

### Layer 2: Agent Tests (Mocked LLM)

**What:** Test agents with mocked LLM responses

**Key Challenge:** Strands agents call LLMs - need to mock responses

**Pattern:**

```python
# tests/agents/test_collector_agent.py

import pytest
from unittest.mock import Mock, patch
from agents.collector.mysql_collector import MySQLCollectorAgent
from contracts.models.collector_output import CollectorOutput

@pytest.fixture
def mock_llm_response():
    """Mock LLM response for collector agent"""
    return CollectorOutput(
        collector_version="2.3.1",
        contract_version="1.2",
        job_id="test-job-123",
        database_metadata={
            "engine": "mysql",
            "version": "8.0.32",
            "size_gb": 100
        },
        schema={
            "users": {
                "columns": ["id", "name", "email"],
                "primary_key": ["id"]
            }
        }
    )

@patch('strands.Agent')
def test_mysql_collector_success(mock_agent_class, mock_llm_response):
    """Test MySQL collector with mocked LLM response"""
    # Setup mock
    mock_agent = Mock()
    mock_agent.return_value = mock_llm_response
    mock_agent_class.return_value = mock_agent

    # Execute
    input_contract = {
        'job_id': 'test-job-123',
        'connection': {...}
    }

    collector = MySQLCollectorAgent(input_contract)
    output = collector.collect()

    # Verify
    assert isinstance(output, CollectorOutput)
    assert output.job_id == "test-job-123"
    assert output.contract_version == "1.2"
    assert "users" in output.database_schema
```

**Mocking Strategies:**

1. **Mock Strands Agent class** - Return pre-defined Pydantic models
2. **Mock LLM API calls** - Intercept HTTP requests to LLM endpoints
3. **Fixture-based responses** - Load responses from JSON files

---

### Layer 3: Contract Tests (Version Compatibility)

**What:** Test contract compatibility across versions

**Examples:**

- Load v1.0 checkpoint with v1.2 code (backward compatibility)
- Load v1.2 checkpoint with v1.0 code (forward compatibility)
- Test version adapters (v1.x → v2.0 migration)

**Pattern:**

```python
# tests/contracts/test_contract_versioning.py

import pytest
from contracts.models.collector_output import CollectorOutput
from contracts.adapters.collector_output_adapter import CollectorOutputAdapter

def test_backward_compatibility_v1_0_to_v1_2():
    """Test loading v1.0 checkpoint with v1.2 code"""
    # v1.0 checkpoint (no query_patterns field)
    v1_0_data = {
        "contract_version": "1.0",
        "collector_version": "2.0.0",
        "job_id": "job-123",
        "database_metadata": {...},
        "schema": {...}
        # No query_patterns field
    }

    # Load with v1.2 code
    output = CollectorOutput(**v1_0_data)

    # Verify
    assert output.contract_version == "1.0"
    assert output.query_patterns is None  # Defaults to None

def test_forward_compatibility_v1_2_to_v1_0():
    """Test loading v1.2 checkpoint with v1.0 code"""
    # v1.2 checkpoint (has query_patterns field)
    v1_2_data = {
        "contract_version": "1.2",
        "collector_version": "2.3.1",
        "job_id": "job-123",
        "database_metadata": {...},
        "schema": {...},
        "query_patterns": {...}  # New field
    }

    # Load with v1.0 code (ignores unknown fields)
    output = CollectorOutput(**v1_2_data)

    # Verify (graceful degradation)
    assert output.contract_version == "1.2"
    # query_patterns ignored by v1.0 code

def test_version_adapter_v1_to_v2():
    """Test version adapter for MAJOR version migration"""
    # v1.x data (has 'schema' field)
    v1_data = {
        "contract_version": "1.2",
        "schema": {...}
    }

    # Adapt to v2.0 (renames 'schema' → 'database_schema')
    v2_data = CollectorOutputAdapter.adapt(v1_data)

    # Verify
    assert v2_data["contract_version"] == "2.0"
    assert "database_schema" in v2_data
    assert "schema" not in v2_data
```

---

### Layer 4: Integration Tests (Workflow)

**What:** Test multi-agent workflows with mocked agents

**Examples:**

- Collector → Analysis → Referee workflow
- Checkpoint and resume workflow
- Error handling and recovery

**Pattern:**

```python
# tests/integration/test_workflow.py

import pytest
from unittest.mock import Mock, patch
from orchestrator.workflow import ModernizerWorkflow

@pytest.fixture
def mock_collector_output():
    """Mock collector output"""
    return CollectorOutput(...)

@pytest.fixture
def mock_analysis_output():
    """Mock analysis output"""
    return AnalysisOutput(...)

@patch('agents.collector.mysql_collector.MySQLCollectorAgent')
@patch('agents.analysis.analysis_orchestrator.AnalysisOrchestrator')
@patch('agents.referee.referee_orchestrator.RefereeOrchestrator')
def test_full_workflow(
    mock_referee,
    mock_analysis,
    mock_collector,
    mock_collector_output,
    mock_analysis_output
):
    """Test full workflow with mocked agents"""
    # Setup mocks
    mock_collector.return_value.collect.return_value = mock_collector_output
    mock_analysis.return_value.analyze.return_value = mock_analysis_output
    mock_referee.return_value.generate_report.return_value = ModernizationReport(...)

    # Execute workflow
    workflow = ModernizerWorkflow(
        job_id="test-job",
        input_contract={...}
    )
    result = workflow.run()

    # Verify
    assert isinstance(result, ModernizationReport)
    assert result.job_id == "test-job"

    # Verify agent calls
    mock_collector.return_value.collect.assert_called_once()
    mock_analysis.return_value.analyze.assert_called_once()
    mock_referee.return_value.generate_report.assert_called_once()
```

---

### Layer 5: Performance Tests (Scalability)

**What:** Test performance with synthetic data

**Examples:**

- Mini-collectors with 1000+ tables
- Parallel analysis agents
- Large checkpoint serialization

**Pattern:**

```python
# tests/performance/test_mini_collectors.py

import pytest
import time
from agents.collector.mysql_collector import MySQLCollectorAgent

def test_mini_collectors_performance():
    """Test mini-collectors with 1000 tables"""
    # Generate synthetic database with 1000 tables
    tables = [f"table_{i}" for i in range(1000)]

    input_contract = {
        'job_id': 'perf-test',
        'connection': {...},
        'tables': tables
    }

    # Execute with mini-collectors
    start_time = time.time()
    collector = MySQLCollectorAgent(input_contract)
    output = collector.collect()
    elapsed_time = time.time() - start_time

    # Verify performance
    assert len(output.database_schema) == 1000
    assert elapsed_time < 120  # Should complete in <2 minutes

    # Verify mini-collectors were used
    assert collector.used_mini_collectors is True
    assert collector.mini_collector_count == 10  # 1000 tables / 100 per mini-collector

@pytest.mark.benchmark
def test_checkpoint_serialization_performance():
    """Test checkpoint serialization with large output"""
    # Generate large collector output
    output = CollectorOutput(
        job_id="perf-test",
        schema={f"table_{i}": {...} for i in range(1000)}
    )

    # Benchmark serialization
    start_time = time.time()
    serialized = output.model_dump_json()
    elapsed_time = time.time() - start_time

    # Verify performance
    assert elapsed_time < 1.0  # Should serialize in <1 second
```

---

### Layer 6: E2E Tests (Real LLM + Real RDS - Limited)

**What:** Test with real LLM and real RDS instance (expensive, slow, non-deterministic)

**When:** Only for critical paths, not in CI/CD

**Setup Required:**

- Test RDS instance (small, db.t3.micro)
- Test database with sample schema (10-20 tables)
- AWS credentials for RDS access
- LLM API access

**Pattern:**

```python
# tests/e2e/test_real_llm_rds.py

import pytest
import os
from agents.collector.mysql_collector import MySQLCollectorAgent

@pytest.mark.e2e
@pytest.mark.expensive
@pytest.mark.requires_rds
def test_mysql_collector_real_llm_rds():
    """Test MySQL collector with real LLM and real RDS (expensive!)"""
    # Requires environment variables:
    # - RDS_ENDPOINT
    # - RDS_USERNAME
    # - RDS_PASSWORD
    # - AWS_REGION

    if not os.getenv('RDS_ENDPOINT'):
        pytest.skip("RDS_ENDPOINT not set (E2E test requires real RDS)")

    input_contract = {
        'job_id': 'e2e-test',
        'connection': {
            'endpoint': os.getenv('RDS_ENDPOINT'),
            'port': 3306,
            'database_name': 'test_db',
            'username': os.getenv('RDS_USERNAME'),
            'password': os.getenv('RDS_PASSWORD')
        },
        'aws_config': {
            'region': os.getenv('AWS_REGION', 'us-east-1'),
            'account_id': os.getenv('AWS_ACCOUNT_ID')
        }
    }

    collector = MySQLCollectorAgent(input_contract)
    output = collector.collect()

    # Verify output structure (not exact content - LLM varies)
    assert isinstance(output, CollectorOutput)
    assert output.job_id == "e2e-test"
    assert len(output.database_schema) > 0
    assert output.contract_version == "1.2"

    # Verify AWS metadata collected
    assert output.aws_metadata is not None
    assert 'cloudwatch_metrics' in output.aws_metadata
```

**E2E Test Guidelines:**

- Mark with `@pytest.mark.e2e`, `@pytest.mark.expensive`, `@pytest.mark.requires_rds`
- Run manually, not in CI/CD
- Use small test RDS instance (db.t3.micro, 10-20 tables)
- Focus on critical paths only
- Accept non-deterministic results (verify structure, not exact content)
- Clean up test data after run

**Test RDS Setup:**

```bash
# Create test RDS instance (one-time setup)
aws rds create-db-instance \
  --db-instance-identifier modernizer-test-db \
  --db-instance-class db.t3.micro \
  --engine mysql \
  --master-username admin \
  --master-user-password <password> \
  --allocated-storage 20 \
  --publicly-accessible \
  --tags Key=Purpose,Value=E2ETests

# Load test schema
mysql -h modernizer-test-db.abc123.us-east-1.rds.amazonaws.com \
  -u admin -p < tests/fixtures/test_schema.sql
```

---

## Test Organization

### Directory Structure

```
tests/
├── unit/
│   ├── tools/
│   │   ├── test_mysql_tools.py
│   │   ├── test_postgresql_tools.py
│   │   └── test_aws_tools.py
│   ├── contracts/
│   │   └── test_contract_validation.py
│   └── utils/
│       └── test_version_adapters.py
├── agents/
│   ├── test_collector_agent.py
│   ├── test_analysis_agent.py
│   └── test_referee_agent.py
├── contracts/
│   ├── test_contract_versioning.py
│   └── test_backward_compatibility.py
├── integration/
│   ├── test_workflow.py
│   ├── test_checkpoint_resume.py
│   └── test_error_handling.py
├── performance/
│   ├── test_mini_collectors.py
│   ├── test_parallel_analysis.py
│   └── test_large_databases.py
├── e2e/
│   ├── test_real_llm_rds.py
│   └── README.md  # E2E test setup instructions
└── fixtures/
    ├── mock_responses/
    │   ├── collector_output_v1_0.json
    │   ├── collector_output_v1_2.json
    │   └── analysis_output_v1_0.json
    ├── test_data/
    │   └── sample_schemas.json
    └── test_schema.sql  # SQL script for test RDS setup
```

**Important Notes:**

- **No local databases**: All tests mock RDS connections or use real RDS (E2E only)
- **AWS mocking**: Use `moto` for AWS API calls (CloudWatch, Performance Insights, RDS API)
- **E2E tests**: Require real RDS instance (manual setup, not in CI/CD)

---

## Mock Strategies

### Strategy 1: Fixture-Based Mocking

**Use for:** Consistent, reusable mock responses

```python
# tests/fixtures/mock_responses/collector_output_v1_2.json
{
  "collector_version": "2.3.1",
  "contract_version": "1.2",
  "job_id": "test-job",
  "database_metadata": {...},
  "schema": {...}
}

# tests/conftest.py
import pytest
import json

@pytest.fixture
def mock_collector_output_v1_2():
    """Load mock collector output from fixture"""
    with open('tests/fixtures/mock_responses/collector_output_v1_2.json') as f:
        data = json.load(f)
    return CollectorOutput(**data)
```

### Strategy 2: Mock LLM API Calls

**Use for:** Intercepting HTTP requests to LLM endpoints

```python
# tests/conftest.py
import pytest
from unittest.mock import patch

@pytest.fixture
def mock_llm_api():
    """Mock LLM API calls"""
    with patch('strands.llm.client.LLMClient.call') as mock:
        mock.return_value = {
            "response": "Mocked LLM response",
            "usage": {"tokens": 100}
        }
        yield mock
```

### Strategy 3: Deterministic Agent Responses

**Use for:** Testing agent logic without LLM variability

```python
# tests/agents/test_collector_agent.py

class DeterministicCollectorAgent(MySQLCollectorAgent):
    """Collector agent with deterministic responses (for testing)"""

    def collect(self) -> CollectorOutput:
        """Return fixed output (no LLM call)"""
        return CollectorOutput(
            collector_version=self.AGENT_VERSION,
            contract_version=self.CONTRACT_VERSION,
            job_id=self.job_id,
            database_metadata={"engine": "mysql"},
            schema={"users": {...}}
        )

def test_collector_deterministic():
    """Test collector with deterministic responses"""
    collector = DeterministicCollectorAgent(input_contract)
    output = collector.collect()

    assert output.database_schema == {"users": {...}}  # Exact match
```

---

## CI/CD Integration

### Test Execution Strategy

```yaml
# .github/workflows/test.yml

name: Test Suite

on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run unit tests
        run: pytest tests/unit/ -v

  agent-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run agent tests (mocked LLM)
        run: pytest tests/agents/ -v

  contract-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run contract tests
        run: pytest tests/contracts/ -v

  integration-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run integration tests
        run: pytest tests/integration/ -v

  performance-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run performance tests
        run: pytest tests/performance/ -v --benchmark-only

  # E2E tests NOT in CI/CD (expensive, manual only, requires real RDS)
```

**Key Points:**

- Unit, agent, contract, integration, performance tests run in CI/CD (fast, mocked)
- E2E tests run manually (expensive, require real RDS instance)
- No local database setup required (all mocked or RDS)

---

## Test Coverage Goals

| Layer | Coverage Target | Rationale |
|-------|----------------|-----------|
| Unit Tests | 90%+ | Core logic, tools, utilities |
| Agent Tests | 80%+ | Agent orchestration, mocked LLM |
| Contract Tests | 100% | All version combinations |
| Integration Tests | 70%+ | Critical workflows |
| Performance Tests | Key scenarios | Scalability validation |
| E2E Tests | Smoke tests only | Expensive, manual |

---

## Consequences

### Positive

✅ **Fast tests**: Mocked LLM = no API calls (fast, free)
✅ **Deterministic**: Fixture-based mocking = consistent results
✅ **Comprehensive**: Multi-layer strategy covers all scenarios
✅ **Contract safety**: Version compatibility testing prevents regressions
✅ **Performance validation**: Scalability testing with synthetic data

### Negative

⚠️ **Mock maintenance**: Need to update mocks when contracts change
⚠️ **Limited E2E**: Real LLM tests expensive (manual only)
⚠️ **Test complexity**: Multi-layer strategy requires discipline

### Neutral

🔶 **E2E tests rare**: Only for critical paths (expensive)
🔶 **Fixture-based**: Reusable mock responses in JSON files
🔶 **CI/CD friendly**: Fast tests (no LLM calls) in pipeline

---

## Related Documents

- [ADR-002: Structured Output with Pydantic](ADR-002-structured-output-and-validation.md)
- [ADR-006: Analysis Agent Architecture](ADR-006-analysis-agent-patterns.md)
- [ADR-008: Contract Versioning](ADR-008-contract-versioning.md)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-02 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**
