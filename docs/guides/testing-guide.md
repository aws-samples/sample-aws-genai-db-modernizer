# Testing Guide for Database Modernizer Assessment

## Document Information

**Version:** 2.0.0
**Date:** February 18, 2026
**Status:** Draft
**Owner:** Database Modernizer Assessment Engineering Team
**Audience:** All Engineers

---

## Overview

Testing strategies for the Database Modernizer Assessment system, covering all 6 testing layers defined in ADR-009.

Key principles:

- Mock RDS connections (no local databases in CI/CD)
- 6-layer testing pyramid (unit → E2E)
- Contract validation at every layer
- E2E with real RDS (manual only, not automated)

See [ADR-009: Testing Infrastructure](../architecture/decisions/ADR-009-testing-infrastructure.md)

---

## Table of Contents

1. Testing Pyramid (6 Layers)
2. Layer 1: Unit Tests
3. Layer 2: Agent Tests
4. Layer 3: Contract Tests
5. Layer 4: Integration Tests
6. Layer 5: Performance Tests
7. Layer 6: End-to-End Tests
8. CI/CD Integration
9. Testing Tools and Frameworks

---

## 1. Testing Pyramid (6 Layers)

```
                    ▲
                   ╱ ╲
                  ╱ E2E╲              Layer 6: Real RDS (manual)
                 ╱───────╲
                ╱  Perf   ╲            Layer 5: Load testing
               ╱───────────╲
              ╱ Integration ╲          Layer 4: Multi-agent
             ╱───────────────╲
            ╱    Contract     ╲        Layer 3: Pydantic validation
           ╱───────────────────╲
          ╱       Agent         ╲      Layer 2: Mock LLM
         ╱─────────────────────── ╲
        ╱          Unit            ╲   Layer 1: Mock RDS
       ╱─────────────────────────────╲
```

**Test Distribution:**

- Layer 1 (Unit): 60% of tests
- Layer 2 (Agent): 20% of tests
- Layer 3 (Contract): 10% of tests
- Layer 4 (Integration): 5% of tests
- Layer 5 (Performance): 3% of tests
- Layer 6 (E2E): 2% of tests (manual)

---

## 2. Layer 1: Unit Tests

### 2.1 Mock RDS Connections

Never use real databases in unit tests.

```python
import pytest
from unittest.mock import Mock, patch
import mysql.connector

@patch("mysql.connector.connect")
def test_connect_mysql(mock_connect):
    """Test MySQL connection with mocked RDS."""
    mock_connection = Mock()
    mock_cursor = Mock()
    mock_cursor.fetchone.return_value = ["8.0.32"]
    mock_connection.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_connection

    from tools.database.mysql_tools import connect_mysql

    result = connect_mysql({
        "endpoint": "mydb.abc123.us-east-1.rds.amazonaws.com",
        "port": 3306,
        "database_name": "testdb",
        "username": "admin",
        "password": "<PASSWORD>",
    })

    assert result["status"] == "connected"
    assert result["version"] == "8.0.32"
    mock_connect.assert_called_once()
```

### 2.2 Test Database Tools

```python
@patch("mysql.connector.connect")
def test_collect_schema(mock_connect):
    """Test schema collection with mocked data."""
    mock_cursor = Mock()
    mock_cursor.fetchall.return_value = [
        ("users", "id", "int", "PRI"),
        ("users", "name", "varchar(255)", ""),
        ("users", "email", "varchar(255)", "UNI"),
    ]

    mock_connection = Mock()
    mock_connection.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_connection

    from tools.database.mysql_tools import collect_schema

    schema = collect_schema({"database_name": "testdb"})

    assert "users" in schema
    assert len(schema["users"]["columns"]) == 3
    assert schema["users"]["primary_key"] == ["id"]
```

### 2.3 Test Analysis Logic

```python
def test_analyze_access_patterns():
    """Test access pattern analysis (no database needed)."""
    from agents.analysis.dynamodb_agent import analyze_access_patterns

    queries = [
        {"sql": "SELECT * FROM users WHERE id = 123"},
        {"sql": "SELECT * FROM users WHERE created_at BETWEEN '2024-01-01' AND '2024-12-31'"},
        {"sql": "SELECT * FROM users JOIN orders ON users.id = orders.user_id"},
    ]

    result = analyze_access_patterns("users", queries)

    assert result["patterns"]["key_value_lookups"] == 1
    assert result["patterns"]["range_queries"] == 1
    assert result["patterns"]["joins"] == 1
    assert 0.0 <= result["confidence_score"] <= 1.0
```

---

## 3. Layer 2: Agent Tests

### 3.1 Mock LLM Responses

```python
from unittest.mock import Mock, patch
from src.contracts.collector_output import CollectorOutput

@patch("strands.Agent")
def test_mysql_collector_agent(mock_agent_class):
    """Test collector with mocked LLM response."""
    mock_agent = Mock()
    mock_agent.return_value = CollectorOutput(
        collector_version="2.3.1",
        contract_version="1.2",
        job_id="test-job",
        database_metadata={
            "engine": "mysql",
            "version": "8.0.32",
            "size_gb": 100,
            "table_count": 250,
        },
        database_schema={
            "users": {
                "columns": ["id", "name", "email"],
                "primary_key": ["id"],
            }
        },
    )
    mock_agent_class.return_value = mock_agent

    from agents.collector.mysql_collector import MySQLCollectorAgent

    collector = MySQLCollectorAgent(input_contract={})
    output = collector.collect()

    assert isinstance(output, CollectorOutput)
    assert output.contract_version == "1.2"
    assert output.database_metadata["engine"] == "mysql"
```

### 3.2 Test Agent Error Handling

```python
@patch("strands.Agent")
def test_agent_error_handling(mock_agent_class):
    """Test agent handles LLM errors gracefully."""
    mock_agent = Mock()
    mock_agent.side_effect = Exception("LLM timeout")
    mock_agent_class.return_value = mock_agent

    from agents.collector.mysql_collector import MySQLCollectorAgent

    collector = MySQLCollectorAgent(input_contract={})

    with pytest.raises(Exception) as exc_info:
        collector.collect()

    assert "LLM timeout" in str(exc_info.value)
```

---

## 4. Layer 3: Contract Tests

### 4.1 Validate Pydantic Models

```python
import pytest
from pydantic import ValidationError
from src.contracts.collector_output import CollectorOutput

def test_collector_output_valid():
    output = CollectorOutput(
        collector_version="2.3.1",
        contract_version="1.2",
        job_id="test-job",
        database_metadata={"engine": "mysql"},
        database_schema={"users": {}},
    )
    assert output.contract_version == "1.2"

def test_collector_output_missing_required_field():
    with pytest.raises(ValidationError) as exc_info:
        CollectorOutput(
            collector_version="2.3.1",
            contract_version="1.2",
            database_metadata={"engine": "mysql"},
            database_schema={"users": {}},
        )
    assert "job_id" in str(exc_info.value)

def test_collector_output_forward_compatibility():
    output = CollectorOutput(
        collector_version="2.3.1",
        contract_version="1.2",
        job_id="test-job",
        database_metadata={"engine": "mysql"},
        database_schema={"users": {}},
        unknown_field="ignored",
    )
    assert output.job_id == "test-job"
```

### 4.2 Test Version Compatibility

```python
def test_backward_compatibility():
    """Test v1.0 output works with v1.2 contract."""
    output = CollectorOutput(
        collector_version="2.0.0",
        contract_version="1.0",
        job_id="test-job",
        database_metadata={"engine": "mysql"},
        database_schema={"users": {}},
    )
    assert output.query_patterns is None
    assert output.aws_metadata is None
```

---

## 5. Layer 4: Integration Tests

### 5.1 Multi-Agent Integration

```python
import pytest
from unittest.mock import Mock, patch

@pytest.mark.asyncio
@patch("agents.collector.mysql_collector.MySQLCollectorAgent")
@patch("agents.analysis.dynamodb_agent.DynamoDBAnalysisAgent")
async def test_collector_to_analysis_integration(mock_dynamodb, mock_collector):
    """Test collector → analysis integration."""
    collector_output = CollectorOutput(
        collector_version="2.3.1",
        contract_version="1.2",
        job_id="test-job",
        database_metadata={"engine": "mysql"},
        database_schema={"users": {}},
    )
    mock_collector.return_value.collect.return_value = collector_output

    analysis_output = AnalysisOutput(
        contract_version="1.0",
        job_id="test-job",
        analyses={"dynamodb": {"confidence": 0.85}},
        total_analyses=1,
        analysis_types=["dynamodb"],
    )
    mock_dynamodb.return_value.analyze.return_value = analysis_output

    collector = mock_collector()
    collector_result = collector.collect()

    analyzer = mock_dynamodb(collector_result)
    analysis_result = analyzer.analyze()

    assert analysis_result.job_id == collector_result.job_id
```

### 5.2 Test Step Functions Orchestration

```python
@pytest.mark.asyncio
@patch("boto3.client")
async def test_step_functions_start_execution(mock_boto):
    """Test that API correctly starts Step Functions execution."""
    mock_sfn = Mock()
    mock_sfn.start_execution.return_value = {
        "executionArn": "arn:aws:states:us-east-1:123:execution:modernizer:test-job",
        "startDate": "2026-02-18T00:00:00Z",
    }
    mock_boto.return_value = mock_sfn

    from app.main import create_analysis, AnalysisRequest

    request = AnalysisRequest(
        source_database_type="mysql",
        database_name="test-db",
        connection={"host": "localhost", "port": 3306},
    )

    # Verify start_execution is called with correct input
    response = await create_analysis(request)
    mock_sfn.start_execution.assert_called_once()
    call_args = mock_sfn.start_execution.call_args
    assert "database_name" in call_args.kwargs.get("input", call_args[1].get("input", ""))
```

### 5.3 Test Triage → Synthesis Flow

```python
def test_triage_to_synthesis_integration():
    """Test triage output feeds correctly into synthesis."""
    from agents.referee.triage import classify_workload, select_agents
    from agents.referee.synthesis import calculate_tco

    # Triage phase
    collector_output = {
        "query_patterns": {"key_value_lookups": 500, "joins": 2},
        "database_schema": {"users": {}, "sessions": {}},
    }
    classification = classify_workload(collector_output)
    selection = select_agents(classification)

    assert "dynamodb" in selection["selected"]

    # Synthesis phase (only selected agents have outputs)
    analysis_outputs = {
        agent: {"confidence": 0.8, "estimated_monthly_cost": 200}
        for agent in selection["selected"]
    }

    tco = calculate_tco(analysis_outputs)
    assert tco["target_monthly_cost"] > 0
```

---

## 6. Layer 5: Performance Tests

### 6.1 Load Testing

```python
import time

def test_collector_performance():
    """Test collector handles 100 tables in <5 minutes."""
    from agents.collector.mysql_collector import MySQLCollectorAgent

    collector_output = {
        "table_count": 100,
        "database_schema": {f"table_{i}": {} for i in range(100)},
    }

    start = time.time()
    collector = MySQLCollectorAgent(collector_output)
    output = collector.collect()
    duration = time.time() - start

    assert duration < 300
    assert len(output.database_schema) == 100
```

### 6.2 Parallel Execution Performance

```python
@pytest.mark.asyncio
async def test_parallel_analysis_performance():
    """Test analysis agents complete in <10 minutes (mocked)."""
    import asyncio

    start = time.time()

    # Simulate parallel agent execution (as Step Functions Map would do)
    async def mock_agent(agent_type):
        await asyncio.sleep(0.1)  # Simulate work
        return {"agent_type": agent_type, "confidence": 0.8}

    selected_agents = ["dynamodb", "documentdb", "elasticache"]
    results = await asyncio.gather(*[mock_agent(a) for a in selected_agents])

    duration = time.time() - start

    assert duration < 1  # Mocked, should be fast
    assert len(results) == 3
```

---

## 7. Layer 6: End-to-End Tests

### 7.1 Manual E2E Testing

E2E tests use real RDS instances and are manual only (not in CI/CD).

```python
import pytest

@pytest.mark.manual
@pytest.mark.e2e
def test_full_workflow_mysql_to_dynamodb():
    """
    Manual E2E test: MySQL → DynamoDB

    Prerequisites:
    1. Real RDS MySQL instance running
    2. AWS credentials configured
    3. Step Functions state machine deployed
    4. S3 bucket for data
    """
    from api.client import ModernizerClient

    client = ModernizerClient()

    job = client.start_job({
        "source_database_type": "mysql",
        "database_name": "e2e-test-mysql",
        "connection": {
            "host": "mydb.abc123.us-east-1.rds.amazonaws.com",
            "port": 3306,
            "database": "testdb",
            "username": "admin",
            "password_secret_arn": "arn:aws:secretsmanager:...",  # pragma: allowlist secret
        },
    })

    job_id = job["job_id"]

    # Wait for Step Functions execution to complete (up to 1 hour)
    report = client.wait_for_completion(job_id, timeout=3600)

    assert report["status"] == "COMPLETED"
    assert "results" in report
```

### 7.2 Running E2E Tests

```bash
# Skip E2E tests in CI/CD (default)
uv run pytest tests/

# Run E2E tests manually
uv run pytest tests/e2e/ -m e2e --manual
```

---

## 8. CI/CD Integration

### GitHub Actions Pipeline

The CI/CD pipeline runs unit, agent, contract, and integration tests on every push and pull request. E2E tests are manual only.

```yaml
# .github/workflows/test.yml
name: Test
on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install uv
      - run: uv sync
      - run: uv run pytest tests/unit/ -v --cov=src --cov-fail-under=80

  agent-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install uv
      - run: uv sync
      - run: uv run pytest tests/agent/ -v

  contract-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install uv
      - run: uv sync
      - run: uv run pytest tests/contract/ -v

  integration-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install uv
      - run: uv sync
      - run: uv run pytest tests/integration/ -v

# E2E tests are NOT in CI/CD (manual only)
```

### Test Coverage

```bash
# Run with coverage
uv run pytest --cov=src --cov-report=html

# Minimum coverage: 80%
uv run pytest --cov=src --cov-fail-under=80
```

---

## 9. Testing Tools and Frameworks

### 9.1 Required Tools

```bash
# Install all dev dependencies (includes test frameworks)
uv sync
```

Dependencies in `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-mock",
]
```

### 9.2 Test Directory Structure

```
tests/
├── unit/                  # Layer 1: Unit tests
│   ├── test_mysql_tools.py
│   ├── test_postgresql_tools.py
│   └── test_analysis_logic.py
├── agent/                 # Layer 2: Agent tests
│   ├── test_collector_agent.py
│   ├── test_analysis_agent.py
│   ├── test_triage_agent.py
│   └── test_synthesis_agent.py
├── contract/              # Layer 3: Contract tests
│   ├── test_collector_output.py
│   ├── test_analysis_output.py
│   └── test_modernization_report.py
├── integration/           # Layer 4: Integration tests
│   ├── test_collector_to_analysis.py
│   ├── test_triage_to_synthesis.py
│   └── test_step_functions.py
├── performance/           # Layer 5: Performance tests
│   ├── test_collector_performance.py
│   └── test_parallel_execution.py
└── e2e/                   # Layer 6: E2E tests (manual)
    └── test_full_workflow.py
```

---

## Related Documentation

- [ADR-009: Testing Infrastructure](../architecture/decisions/ADR-009-testing-infrastructure.md)
- [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)

---

**Last Updated:** February 18, 2026
**Maintained By:** Database Modernizer Assessment Engineering Team
