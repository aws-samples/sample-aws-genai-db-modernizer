# Analysis Agent Implementation Guide

## Document Information

**Version:** 2.0.0
**Date:** February 18, 2026
**Status:** Draft
**Owner:** Database Modernizer Assessment Engineering Team
**Audience:** Backend Engineers implementing Analysis Agents

---

## Overview

Analysis agents evaluate database workloads and provide migration recommendations for specific AWS database services. Each agent specializes in one target database.

Key points (ADR-016):

- Analysis agents are a **category**, not a single agent (ADR-006)
- **Not all 7 agents run** — Referee-Triage selects relevant agents based on workload patterns
- Step Functions Map state runs selected agents in parallel (each as a separate ECS Fargate task)
- Each agent reads collector output from S3 using env vars, writes analysis output to S3
- Agents use Strands SDK; output validated with Pydantic models

---

## Table of Contents

1. Architecture Overview
2. Analysis Agent Types
3. Building an Analysis Agent
   - 3.4 Shared Scoring Layer
4. Contract Validation
5. Testing Strategy
6. Parallel Execution via Step Functions

---

## 1. Architecture Overview

### Triage-Driven Execution

Not all 7 agents run for every job. Referee-Triage reads the collector output and selects which agents are relevant. Step Functions then runs only those agents via a Map state:

```
Referee-Triage
    ↓ triage.json (selected_agents[])
Step Functions Map State (MaxConcurrency: 7)
    ├── analysis-dynamodb    (ECS task)
    ├── analysis-elasticache (ECS task)
    └── analysis-documentdb  (ECS task)
    ↓
Referee-Synthesis
```

Each analysis agent runs in its own ECS Fargate task. The agent reads collector output from S3, performs analysis, and writes results back to S3.

See [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)

---

## 2. Analysis Agent Types

### 2.1 Current Agents

| Agent | Target Database | Specialization |
|-------|----------------|----------------|
| DynamoDB Agent | Amazon DynamoDB | NoSQL key-value, document patterns |
| DocumentDB Agent | Amazon DocumentDB | MongoDB-compatible document store |
| Aurora Agent | Amazon Aurora | MySQL/PostgreSQL-compatible RDBMS |
| ElastiCache Agent | Amazon ElastiCache | Redis/Memcached caching patterns |
| OpenSearch Agent | Amazon OpenSearch | Full-text search, analytics |
| Neptune Agent | Amazon Neptune | Graph database patterns |
| Keyspaces Agent | Amazon Keyspaces | Cassandra-compatible wide-column |

### 2.2 Extensibility

New agents can be added without modifying existing agents. Register the new agent type in the Step Functions Map state input and the triage agent's available agents list:

```python
ANALYSIS_AGENTS = {
    "dynamodb": DynamoDBAnalysisAgent,
    "documentdb": DocumentDBAnalysisAgent,
    "aurora": AuroraAnalysisAgent,
    "elasticache": ElastiCacheAnalysisAgent,
    "opensearch": OpenSearchAnalysisAgent,
    "neptune": NeptuneAnalysisAgent,
    "keyspaces": KeyspacesAnalysisAgent,
    "timestream": TimestreamAnalysisAgent,  # NEW
}
```

---

## 3. Building an Analysis Agent

### 3.1 Agent Entrypoint

Each analysis agent uses the env var + S3 data plane pattern (ADR-016):

```python
import os
import json
import boto3
from strands import Agent

def main():
    agent_type = os.environ["AGENT_TYPE"]      # e.g. "analysis-dynamodb"
    job_id = os.environ["JOB_ID"]
    database_name = os.environ["DATABASE_NAME"]
    bucket = os.environ["S3_BUCKET"]

    s3 = boto3.client("s3")

    # Read collector output from S3
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    response = s3.get_object(Bucket=bucket, Key=collector_key)
    collector_output = json.loads(response["Body"].read())

    # Dispatch to the right agent
    agent = create_analysis_agent(agent_type)
    result = agent(json.dumps(collector_output))

    # Write analysis output to S3
    output_key = f"{database_name}/{job_id}/{agent_type}/analysis.json"
    s3.put_object(
        Bucket=bucket,
        Key=output_key,
        Body=json.dumps(result, indent=2),
        ContentType="application/json",
    )


if __name__ == "__main__":
    main()
```

### 3.2 Agent Definition (DynamoDB Example)

```python
from strands import Agent
from src.contracts.analysis_output import AnalysisOutput

def create_dynamodb_agent() -> Agent:
    return Agent(
        system_prompt=DYNAMODB_SYSTEM_PROMPT,
        tools=[analyze_access_patterns, estimate_capacity, calculate_costs],
        response_format=AnalysisOutput,
    )

DYNAMODB_SYSTEM_PROMPT = """
You are a DynamoDB migration analysis expert.

Your task:
1. Analyze the source database schema and query patterns
2. Identify tables suitable for DynamoDB migration
3. Recommend partition keys and sort keys
4. Estimate DynamoDB capacity units and costs
5. Flag anti-patterns (hot partitions, large items, complex joins)

Output format: AnalysisOutput Pydantic model

Guidelines:
- Confidence score: 0.0-1.0 (1.0 = perfect fit)
- Flag tables with complex joins (low confidence)
- Recommend GSIs for alternate access patterns
- Consider DynamoDB Streams for change data capture
"""
```

### 3.3 Analysis Tools

```python
def analyze_access_patterns(table_name: str, queries: list[dict]) -> dict:
    """Analyze query patterns for DynamoDB suitability."""
    patterns = {
        "key_value_lookups": 0,
        "range_queries": 0,
        "full_scans": 0,
        "joins": 0,
    }

    for query in queries:
        if "WHERE id =" in query["sql"]:
            patterns["key_value_lookups"] += 1
        elif "WHERE" in query["sql"] and "BETWEEN" in query["sql"]:
            patterns["range_queries"] += 1
        elif "JOIN" in query["sql"]:
            patterns["joins"] += 1
        else:
            patterns["full_scans"] += 1

    total = sum(patterns.values()) or 1
    confidence = (patterns["key_value_lookups"] + patterns["range_queries"]) / total

    return {
        "patterns": patterns,
        "confidence_score": confidence,
        "recommendation": "high" if confidence > 0.7 else "medium" if confidence > 0.4 else "low",
    }


def estimate_capacity(table_metadata: dict) -> dict:
    """Estimate DynamoDB capacity units."""
    row_count = table_metadata.get("row_count", 0)
    avg_row_size = table_metadata.get("avg_row_size_bytes", 1024)

    read_capacity = max(1, row_count // 3600)
    write_capacity = max(1, row_count // 36000)

    return {
        "read_capacity_units": read_capacity,
        "write_capacity_units": write_capacity,
        "storage_gb": (row_count * avg_row_size) / (1024**3),
    }


def calculate_costs(capacity: dict) -> dict:
    """Calculate DynamoDB costs (us-east-1 pricing)."""
    RCU_PRICE = 0.00013
    WCU_PRICE = 0.00065
    STORAGE_PRICE = 0.25

    monthly_cost = (
        capacity["read_capacity_units"] * RCU_PRICE * 730
        + capacity["write_capacity_units"] * WCU_PRICE * 730
        + capacity["storage_gb"] * STORAGE_PRICE
    )

    return {
        "monthly_cost_usd": round(monthly_cost, 2),
        "annual_cost_usd": round(monthly_cost * 12, 2),
    }
```

---

## 3.4 Shared Scoring Layer

All analysis agents share a generic, evidence-based scoring framework at `src/tools/analysis/scoring.py`. This avoids each agent reinventing scoring logic and ensures consistent confidence/recommendation semantics across targets.

The scoring module also provides engine-agnostic aggregate identification:

- `detect_co_access()` — finds pairs of tables co-accessed by the same query
- `identify_aggregates()` — combines FK adjacency graphs with co-access patterns to group related tables into migration units

These are reusable across all analysis agents.

### Core Principle

All four score dimensions start at **0** and earn points through evidence. A table with no queries and no detected patterns scores near zero — not a false-positive "suitable". Structural signals (primary key, few columns, small size) earn modest points even without query data, but query-driven signals (frequency, latency, load) are required for high scores.

### Pipeline

Every analysis agent follows the same pipeline:

```
build_table_profiles()          # Generic: aggregate per-table stats
        ↓
compute_base_scores(profile)    # Generic: evidence-based 0→100 scoring
        ↓
_apply_<target>_adjustments()   # Target-specific: bonuses/penalties
        ↓
compute_confidence(scores)      # Generic: weighted confidence (0–100)
```

### TableProfile

`build_table_profiles(collector_output, workload_analysis)` produces a `TableProfile` per table by aggregating:

- **Schema signals**: row_count, size_mb, column_count, has_primary_key, foreign_key_count
- **Query aggregates**: total/read/write query counts, read_ratio, total calls_per_second, total db_load_percent, frequency-weighted avg_execution_time_ms, max_rows_returned_avg, has_joins, max_join_count
- **Pattern aggregates**: pattern_count, anti_pattern_count, pattern_types

This is computed once per table and passed to both generic and target-specific scoring.

### Base Scores

`compute_base_scores(profile)` returns a `ScoreBreakdown` with four dimensions:

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| `pattern_match_score` | 40% | Detected patterns, query frequency, read ratio, anti-pattern penalties |
| `complexity_score` | 30% | Structural simplicity — PK, few FKs, no joins, small result sets, few columns |
| `performance_score` | 20% | Latency, throughput, DB load contribution, query count |
| `cost_score` | 10% | Table size, row count, call frequency, read ratio |

Each dimension scores 0–100. Points are awarded for specific evidence thresholds (e.g., calls_per_second >= 10 → +15 to pattern_match). See `scoring.py` for the full point tables.

### Target-Specific Adjustments

Each agent adds a thin layer of target-specific bonuses and penalties **on top of** the base scores. The function signature:

```python
def _apply_<target>_adjustments(scores: ScoreBreakdown, profile: TableProfile) -> ScoreBreakdown:
```

For example, the Redis agent (`redis_analysis_tools.py`) adds:

- **Pattern bonuses**: +10 for caching/session-store/leaderboard patterns, +5 for time-series
- **Complexity bonus**: +10 (Redis migrations are structurally simple)
- **Performance bonus**: +10 when avg_execution_time >= 5ms (sub-ms Redis latency helps)
- **Cost bonus**: +10 for small + hot tables (size <= 100MB and cps >= 1)
- **Cost penalty**: -15 for large datasets (> 1GB in-memory is expensive)
- **Write-heavy penalty**: -10 to pattern_match and performance when read_ratio < 0.3

All results are clamped to [0, 100].

### Building a New Agent

When implementing a new analysis agent (e.g., DynamoDB):

1. **Reuse `build_table_profiles()` and `compute_base_scores()`** — these are target-agnostic
2. **Write `_apply_dynamodb_adjustments()`** with target-specific bonuses/penalties (e.g., +15 for key-value lookup patterns, -20 for heavy join workloads)
3. **Reuse `compute_confidence()`** — same formula across all agents, accepts per-target weights
4. **Reuse `identify_aggregates()` and `detect_co_access()`** — engine-agnostic aggregate identification from FK graphs and query co-access patterns
5. **Write target-specific detection functions** — PK classification, GSI candidates, denormalization sub-types, secondary index dominance (these are DynamoDB-specific examples; other targets will have their own)
6. **Write target-specific `_build_rationale()` and `_build_concerns()`** for human-readable output

The Redis agent implementation in `redis_analysis_tools.py` serves as the reference pattern.

---

## 4. Contract Validation

### 4.1 Output Contract

```python
from src.contracts.analysis_output import AnalysisOutput

# Agent automatically validates output via response_format
output = agent(collector_data)
assert isinstance(output, AnalysisOutput)
assert output.contract_version == "2.1"
```

### 4.2 Handling Validation Errors

```python
from pydantic import ValidationError

try:
    output = agent(collector_data)
except ValidationError as e:
    logger.error(f"Contract validation failed: {e}")
    return AnalysisOutput(
        contract_version="1.0",
        job_id=job_id,
        analyses={"error": str(e)},
        total_analyses=0,
        analysis_types=[],
    )
```

---

## 5. Testing Strategy (ADR-009)

### 5.1 Unit Tests

```python
import pytest

def test_analyze_access_patterns():
    """Test access pattern analysis."""
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

### 5.2 Agent Tests (Mock LLM)

```python
from unittest.mock import Mock, patch

@patch("strands.Agent")
def test_dynamodb_agent(mock_agent_class):
    """Test DynamoDB agent with mocked LLM."""
    mock_agent = Mock()
    mock_agent.return_value = AnalysisOutput(
        contract_version="1.0",
        job_id="test-job",
        analyses={"dynamodb": {"confidence": 0.85}},
        total_analyses=1,
        analysis_types=["dynamodb"],
    )
    mock_agent_class.return_value = mock_agent

    output = mock_agent({"collector_output": {}})

    assert isinstance(output, AnalysisOutput)
    assert output.total_analyses == 1
```

See [ADR-009: Testing Infrastructure](../architecture/decisions/ADR-009-testing-infrastructure.md)

---

## 6. Parallel Execution via Step Functions

Analysis agents run in parallel via a Step Functions Map state. The Map state iterates over the `selected_agents` array from triage output, launching one ECS Fargate task per agent.

```json
{
  "AnalysisMap": {
    "Type": "Map",
    "ItemsPath": "$.triage.selected_agents",
    "MaxConcurrency": 7,
    "Iterator": {
      "StartAt": "RunAnalysisAgent",
      "States": {
        "RunAnalysisAgent": {
          "Type": "Task",
          "Resource": "arn:aws:states:::ecs:runTask.sync",
          "Parameters": {
            "LaunchType": "FARGATE",
            "Cluster": "${EcsClusterArn}",
            "TaskDefinition": "${AnalysisTaskDef}",
            "Overrides": {
              "ContainerOverrides": [{
                "Name": "agent",
                "Environment": [
                  {"Name": "AGENT_TYPE", "Value.$": "States.Format('analysis-{}', $.agent_type)"},
                  {"Name": "JOB_ID", "Value.$": "$$.Execution.Input.job_id"},
                  {"Name": "DATABASE_NAME", "Value.$": "$$.Execution.Input.database_name"}
                ]
              }]
            }
          },
          "End": true
        }
      }
    }
  }
}
```

Each agent is independent — no shared state between parallel tasks. All coordination happens through S3 artifacts.

---

## Related Documentation

- [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- [ADR-006: Analysis Agent Patterns](../architecture/decisions/ADR-006-analysis-agent-patterns.md)
- [Referee Agent Guide](referee-agent-guide.md)
- [Storage Architecture Guide](storage-architecture-guide.md)

---

**Last Updated:** March 17, 2026
**Maintained By:** Database Modernizer Assessment Engineering Team
