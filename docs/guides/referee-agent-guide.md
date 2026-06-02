# Referee Agent Implementation Guide

## Document Information

**Version:** 2.0.0
**Date:** February 18, 2026
**Status:** Draft
**Owner:** Database Modernizer Assessment Engineering Team
**Audience:** Backend Engineers implementing the Referee Agents

---

## Overview

The referee is split into two agents (ADR-016):

- **Referee-Triage:** Reads collector output from S3, selects which analysis agents are relevant, writes `triage.json` to S3
- **Referee-Synthesis:** Reads analysis outputs from S3, produces weighted ranking with confidence scores, may request deeper analysis (max 2 iterations)

Step Functions orchestrates the workflow between them. EventBridge is for progress notifications only.

---

## Table of Contents

1. Architecture Overview
2. Referee-Triage Agent
3. Referee-Synthesis Agent
4. Shared Tools (TCO, Risk Assessment)
5. Testing Strategy

---

## 1. Architecture Overview

### Step Functions Workflow

```
Collector
    ↓
Referee-Triage  →  writes triage.json (selected_agents, skipped_agents, confidence)
    ↓
Step Functions Map State  →  runs only triage-selected analysis agents in parallel
    ↓
Referee-Synthesis  →  reads all analysis outputs, produces weighted ranking
    ↓ (optional, max 2x)
Deeper Analysis Loop  →  synthesis requests additional analysis if needed
    ↓
Schema Design
```

### S3 Data Plane

Both referee agents use env vars to locate data:

```python
import os

DATABASE_NAME = os.environ["DATABASE_NAME"]
JOB_ID = os.environ["JOB_ID"]
AGENT_TYPE = os.environ["AGENT_TYPE"]  # "referee-triage" or "referee-synthesis"
BUCKET = os.environ["S3_BUCKET"]
```

Path convention: `<database-name>/<job_id>/<agent-name>/artifact.json`

---

## 2. Referee-Triage Agent

### Purpose

Read collector output, classify the workload, and select which analysis agents should run. Not all 7 agents run for every workload — a key-value PostgreSQL database doesn't need Neptune or OpenSearch analysis.

### Entrypoint

```python
import os
import json
import boto3
from strands import Agent

def main():
    database_name = os.environ["DATABASE_NAME"]
    job_id = os.environ["JOB_ID"]
    bucket = os.environ["S3_BUCKET"]

    s3 = boto3.client("s3")

    # Read collector output from S3
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    response = s3.get_object(Bucket=bucket, Key=collector_key)
    collector_output = json.loads(response["Body"].read())

    # Run triage
    agent = create_triage_agent()
    triage_result = agent(json.dumps(collector_output))

    # Write triage.json to S3
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    s3.put_object(
        Bucket=bucket,
        Key=triage_key,
        Body=json.dumps(triage_result, indent=2),
        ContentType="application/json",
    )


if __name__ == "__main__":
    main()
```

### Triage Agent Definition

```python
from strands import Agent, Tool

def create_triage_agent() -> Agent:
    return Agent(
        system_prompt=TRIAGE_SYSTEM_PROMPT,
        tools=[classify_workload, analyze_access_patterns, select_agents],
    )

TRIAGE_SYSTEM_PROMPT = """
You are a database workload triage expert.

Given collector output (schema, query patterns, metrics), determine which
analysis agents are relevant for this workload.

Available agents: dynamodb, documentdb, aurora, elasticache, opensearch, neptune, keyspaces

Rules:
- Select agents whose target database matches the workload's access patterns
- Skip agents that are clearly irrelevant (e.g., no graph traversals → skip neptune)
- Provide a reason for each selection and each skip
- Output a confidence score (0.0-1.0) for the overall triage decision
- If confidence < 0.7, the orchestrator will fall back to running all agents

Output format: JSON with selected_agents, skipped_agents, confidence
"""
```

### Triage Tools

```python
def classify_workload(collector_output: dict) -> dict:
    """Classify the workload based on access patterns."""
    patterns = collector_output.get("query_patterns", {})
    schema = collector_output.get("database_schema", {})

    has_key_value = patterns.get("key_value_lookups", 0) > 0
    has_joins = patterns.get("joins", 0) > 0
    has_full_text = patterns.get("full_text_searches", 0) > 0
    has_graph = patterns.get("graph_traversals", 0) > 0
    has_time_series = patterns.get("time_series_queries", 0) > 0

    return {
        "has_key_value": has_key_value,
        "has_joins": has_joins,
        "has_full_text": has_full_text,
        "has_graph": has_graph,
        "has_time_series": has_time_series,
        "table_count": len(schema),
    }


def select_agents(classification: dict) -> dict:
    """Select analysis agents based on workload classification."""
    selected = []
    skipped = []

    agent_criteria = {
        "dynamodb": lambda c: c["has_key_value"],
        "documentdb": lambda c: not c["has_joins"] or c.get("has_nested_json", False),
        "aurora": lambda c: c["has_joins"],
        "elasticache": lambda c: c.get("has_hot_keys", False),
        "opensearch": lambda c: c["has_full_text"],
        "neptune": lambda c: c["has_graph"],
        "keyspaces": lambda c: c.get("has_wide_column", False),
    }

    for agent_type, criterion in agent_criteria.items():
        if criterion(classification):
            selected.append(agent_type)
        else:
            skipped.append(agent_type)

    return {"selected": selected, "skipped": skipped}
```

### Triage Output Format (Step Functions Contract)

The triage output is read by Step Functions via S3 GetObject. The `selected_agents` array drives the Map state — each item MUST have an `agent_type` field. Changing this format breaks the orchestration.

Required fields:

- `selected_agents` — array of objects, each with `agent_type` (string). Step Functions iterates this.
- `skipped_agents` — array of objects, each with `agent_type` and `reason`. For logging/UI only.
- `confidence` — float 0.0-1.0. Step Functions reads this for safeguard checks.

```json
{
  "selected_agents": [
    {"agent_type": "dynamodb", "reason": "95% key-value access patterns"},
    {"agent_type": "elasticache", "reason": "Hot key patterns, TTL usage"},
    {"agent_type": "documentdb", "reason": "Nested JSON columns"}
  ],
  "skipped_agents": [
    {"agent_type": "neptune", "reason": "No graph traversal patterns"},
    {"agent_type": "opensearch", "reason": "No full-text search queries"},
    {"agent_type": "keyspaces", "reason": "No wide-column access patterns"},
    {"agent_type": "aurora", "reason": "Source is PostgreSQL — lateral move"}
  ],
  "confidence": 0.87
}
```

### Triage Safeguards

1. **Full analysis override** — users can bypass triage and run all 7 agents
2. **Minimum 2 agents** — if triage selects fewer, fall back to full analysis
3. **Confidence threshold** — if triage confidence < 0.7, fall back to full analysis
4. **Triage logging** — all decisions persisted to S3, visible in UI

---

## 3. Referee-Synthesis Agent

### Purpose

Read all analysis outputs from S3, produce a weighted ranking with confidence scores, and generate the final modernization report. May request deeper analysis (capped at 2 iterations).

### Entrypoint

```python
import os
import json
import boto3
from strands import Agent

def main():
    database_name = os.environ["DATABASE_NAME"]
    job_id = os.environ["JOB_ID"]
    bucket = os.environ["S3_BUCKET"]

    s3 = boto3.client("s3")

    # Read triage output to know which agents ran
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    triage = json.loads(s3.get_object(Bucket=bucket, Key=triage_key)["Body"].read())

    # Read analysis outputs for each selected agent
    analysis_outputs = {}
    for agent_info in triage["selected_agents"]:
        agent_type = agent_info["agent_type"]
        key = f"{database_name}/{job_id}/analysis-{agent_type}/analysis.json"
        data = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        analysis_outputs[agent_type] = data

    # Read collector output for context
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = json.loads(
        s3.get_object(Bucket=bucket, Key=collector_key)["Body"].read()
    )

    # Run synthesis
    agent = create_synthesis_agent()
    report = agent(json.dumps({
        "collector_output": collector_output,
        "analysis_outputs": analysis_outputs,
        "triage": triage,
    }))

    # Write report to S3
    report_key = f"{database_name}/{job_id}/referee-synthesis/report.json"
    s3.put_object(
        Bucket=bucket,
        Key=report_key,
        Body=json.dumps(report, indent=2),
        ContentType="application/json",
    )


if __name__ == "__main__":
    main()
```

### Synthesis Agent Definition

```python
from strands import Agent

def create_synthesis_agent() -> Agent:
    return Agent(
        system_prompt=SYNTHESIS_SYSTEM_PROMPT,
        tools=[
            prioritize_recommendations,
            calculate_tco,
            assess_risk,
            request_deeper_analysis,
        ],
    )

SYNTHESIS_SYSTEM_PROMPT = """
You are a database modernization synthesis expert.

Given analysis outputs from multiple agents, produce a weighted ranking
of migration recommendations with confidence scores.

Your task:
1. Review all analysis agent outputs
2. Produce a weighted ranking (each recommendation gets a weight and confidence)
3. Identify quick wins (high impact, low effort)
4. Identify strategic initiatives (high impact, medium effort)
5. Identify long-term projects (transformational, high effort)
6. Calculate total cost of ownership (TCO)
7. Assess migration risks and mitigations
8. If analysis quality is insufficient, request deeper analysis (max 2 iterations)

Output format: ModernizationReport with weighted rankings and confidence scores
"""
```

### Synthesis Output Format (Step Functions Contract)

The synthesis output is read by Step Functions via S3 GetObject. These fields drive the Choice and Map states downstream — changing them breaks the orchestration.

Required fields:

- `needs_deeper_analysis` — boolean. Step Functions Choice state reads this to decide if re-analysis is needed.
- `recommended_schema_designs` — array of strings (e.g., `["dynamodb"]`). Step Functions Map state iterates this for schema design agents. If absent, schema design is skipped.
- `ranking` — array of objects with target, confidence, weight. For reporting/UI.

### Deeper Analysis Loop

Synthesis can request deeper analysis if the initial results are insufficient. This is capped at 2 iterations to prevent runaway loops. The Step Functions workflow handles this via a Choice state that checks the synthesis output.

```python
def request_deeper_analysis(
    current_analyses: dict, gaps: list[str]
) -> dict:
    """Request deeper analysis for specific agents.

    Returns a signal that Step Functions uses to re-run selected agents
    with additional parameters. Max 2 iterations enforced by Step Functions.
    """
    return {
        "action": "deeper_analysis_requested",
        "agents": gaps,
        "reason": "Insufficient confidence in initial analysis",
        "iteration": current_analyses.get("iteration", 0) + 1,
    }
```

---

## 4. Shared Tools (TCO, Risk Assessment)

### TCO Calculation

```python
def calculate_tco(analysis_outputs: dict) -> dict:
    """Calculate total cost of ownership across all recommended targets."""
    target_costs = []
    for agent_type, output in analysis_outputs.items():
        cost = output.get("estimated_monthly_cost", 0)
        target_costs.append({"service": agent_type, "monthly_cost": cost})

    total_target = sum(c["monthly_cost"] for c in target_costs)
    current_cost = analysis_outputs.get("current_monthly_cost", 0)
    monthly_savings = current_cost - total_target
    annual_savings = monthly_savings * 12

    migration_cost = 50000  # Estimate
    roi_months = (
        int(migration_cost / monthly_savings) if monthly_savings > 0 else float("inf")
    )

    return {
        "current_monthly_cost": current_cost,
        "target_monthly_cost": total_target,
        "monthly_savings": monthly_savings,
        "annual_savings": annual_savings,
        "roi_months": roi_months,
        "breakdown": target_costs,
    }
```

### Risk Assessment

```python
def assess_risk(collector_output: dict, analysis_outputs: dict) -> dict:
    """Assess migration risk based on workload characteristics."""
    table_count = collector_output.get("table_count", 0)
    has_stored_procedures = len(collector_output.get("stored_procedures", [])) > 0
    has_triggers = len(collector_output.get("triggers", [])) > 0

    if table_count > 500 or has_stored_procedures or has_triggers:
        complexity = "high"
    elif table_count > 100:
        complexity = "medium"
    else:
        complexity = "low"

    risks = []
    if has_stored_procedures:
        risks.append("Stored procedures require refactoring")
    if has_triggers:
        risks.append("Triggers require alternative implementation")
    if table_count > 1000:
        risks.append("Large database requires phased migration")

    return {
        "complexity": complexity,
        "key_risks": risks,
        "recommended_approach": "phased" if complexity == "high" else "big_bang",
    }
```

### Prioritization

```python
def prioritize_recommendations(recommendations: list[dict]) -> dict:
    """Categorize recommendations by priority."""
    quick_wins = []
    strategic = []
    long_term = []

    for rec in recommendations:
        impact = rec.get("estimated_impact", "medium")
        effort = rec.get("effort", "medium")

        if impact == "high" and effort == "low":
            quick_wins.append(rec)
        elif impact == "high" and effort == "medium":
            strategic.append(rec)
        elif impact == "high" and effort == "high":
            long_term.append(rec)
        else:
            strategic.append(rec)

    return {
        "quick_wins": quick_wins,
        "strategic": strategic,
        "long_term": long_term,
    }
```

---

## 5. Testing Strategy

### Triage Unit Tests

```python
import pytest

def test_triage_selects_dynamodb_for_key_value():
    """Triage selects DynamoDB when key-value patterns dominate."""
    collector_output = {
        "query_patterns": {"key_value_lookups": 500, "joins": 2},
        "database_schema": {"users": {}, "sessions": {}},
    }

    classification = classify_workload(collector_output)
    selection = select_agents(classification)

    assert "dynamodb" in selection["selected"]


def test_triage_skips_neptune_without_graph():
    """Triage skips Neptune when no graph patterns exist."""
    collector_output = {
        "query_patterns": {"key_value_lookups": 100, "graph_traversals": 0},
        "database_schema": {"users": {}},
    }

    classification = classify_workload(collector_output)
    selection = select_agents(classification)

    assert "neptune" in selection["skipped"]


def test_triage_fallback_on_low_confidence():
    """If confidence < 0.7, safeguard triggers full analysis."""
    triage_output = {
        "selected_agents": [{"agent_type": "dynamodb", "reason": "maybe"}],
        "skipped_agents": [],
        "confidence": 0.5,
    }

    # Step Functions Choice state checks this
    assert triage_output["confidence"] < 0.7
```

### Synthesis Unit Tests

```python
def test_synthesis_produces_weighted_ranking():
    """Synthesis produces ranked recommendations with weights."""
    analysis_outputs = {
        "dynamodb": {"confidence": 0.85, "estimated_monthly_cost": 200},
        "elasticache": {"confidence": 0.70, "estimated_monthly_cost": 100},
    }

    tco = calculate_tco(analysis_outputs)
    assert tco["target_monthly_cost"] == 300


def test_deeper_analysis_capped_at_2():
    """Deeper analysis requests are capped at 2 iterations."""
    result = request_deeper_analysis(
        {"iteration": 2}, ["opensearch"]
    )
    assert result["iteration"] == 3  # Step Functions enforces max 2
```

### Integration Tests

```python
@pytest.mark.asyncio
async def test_triage_to_synthesis_flow():
    """Test triage output feeds correctly into synthesis."""
    # Mock triage output
    triage = {
        "selected_agents": [
            {"agent_type": "dynamodb", "reason": "key-value patterns"},
            {"agent_type": "documentdb", "reason": "nested JSON"},
        ],
        "skipped_agents": [
            {"agent_type": "neptune", "reason": "no graph patterns"},
        ],
        "confidence": 0.87,
    }

    # Mock analysis outputs (only for selected agents)
    analysis_outputs = {
        "dynamodb": {"confidence": 0.85, "estimated_monthly_cost": 200},
        "documentdb": {"confidence": 0.70, "estimated_monthly_cost": 300},
    }

    tco = calculate_tco(analysis_outputs)
    risk = assess_risk({"table_count": 50}, analysis_outputs)

    assert tco["target_monthly_cost"] == 500
    assert risk["complexity"] == "low"
```

See [ADR-009: Testing Infrastructure](../architecture/decisions/ADR-009-testing-infrastructure.md)

---

## Related Documentation

- [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- [ADR-007: Referee Orchestration](../architecture/decisions/ADR-007-referee-orchestration.md) (superseded by ADR-016 triage/synthesis split)
- [Analysis Agent Guide](analysis-agent-guide.md)
- [Storage Architecture Guide](storage-architecture-guide.md)

---

**Last Updated:** February 18, 2026
**Maintained By:** Database Modernizer Assessment Engineering Team
