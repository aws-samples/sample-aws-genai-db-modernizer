# Strands Agent Development Guide

**Document Type:** Implementation Guide
**Last Updated:** February 6, 2026
**Status:** Draft

---

## Overview

This guide provides detailed implementation patterns for building agents using the Strands SDK framework.

---

## Strands SDK Overview

**Strands SDK** is an open-source agentic framework that provides:

- Agent orchestration
- Tool management
- LLM integration
- Built-in hooks for progress tracking

**Why Strands SDK?**

- Simplified agent creation without inheritance hierarchies
- Reusable tools across different agents
- Clear separation between behavior (prompts) and capabilities (tools)
- Built-in LLM orchestration
- Independently testable components

---

## Agent Entrypoint Pattern (ADR-016)

Step Functions launches ECS tasks with environment variables for routing. The agent container reads these to determine what to run and where to find its input.

### Environment Variables (set by Step Functions)

| Variable | Source | Example |
|----------|--------|---------|
| `AGENT_TYPE` | State machine definition or triage output | `collector`, `dynamodb`, `referee-triage` |
| `JOB_ID` | Execution input | `2GxZLsnP00Y2BwR0000000001` |
| `DATABASE_NAME` | Execution input | `myapp-postgres` |
| `EVENT_BUS_NAME` | Task definition | `modernizer-dev-notifications` |
| `ENVIRONMENT` | Task definition | `dev` |
| `PROJECT_NAME` | Task definition | `modernizer` |
| `TARGET_TYPE` | Schema design only | `dynamodb` |

### Entrypoint Dispatcher

Step Functions uses `ecs:runTask.sync` — it launches the ECS task and waits for the container to exit. The agent does NOT need to call any Step Functions API. The exit code is the signal:

- **Exit 0** (normal completion) → Step Functions sees `TaskSucceeded`, moves to next state
- **Exit non-zero** (error) → Step Functions sees `TaskFailed`, triggers Retry or Catch
- **Never exits** (e.g., running a web server) → execution hangs forever

The agent must be a run-to-completion process, not a long-running server. Do your work, write output to S3, exit.

```python
# entrypoint.py — container CMD
import os
import sys
import traceback

AGENT_TYPE = os.environ["AGENT_TYPE"]
JOB_ID = os.environ["JOB_ID"]
DATABASE_NAME = os.environ["DATABASE_NAME"]

ANALYSIS_AGENTS = {
    "dynamodb", "documentdb", "elasticache",
    "opensearch", "neptune", "keyspaces", "aurora"
}

def main():
    if AGENT_TYPE == "collector":
        from agents.collector import run_collector
        run_collector(JOB_ID, DATABASE_NAME)
    elif AGENT_TYPE == "referee-triage":
        from agents.referee_triage import run_triage
        run_triage(JOB_ID, DATABASE_NAME)
    elif AGENT_TYPE in ANALYSIS_AGENTS:
        from agents.analysis import run_analysis
        run_analysis(JOB_ID, DATABASE_NAME, AGENT_TYPE)
    elif AGENT_TYPE == "referee-synthesis":
        from agents.referee_synthesis import run_synthesis
        run_synthesis(JOB_ID, DATABASE_NAME)
    elif AGENT_TYPE == "schema-design":
        from agents.schema_design import run_schema_design
        run_schema_design(JOB_ID, DATABASE_NAME, os.environ["TARGET_TYPE"])
    else:
        print(f"Unknown AGENT_TYPE: {AGENT_TYPE}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
        # Exit 0 = success → Step Functions moves to next state
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)  # Exit non-zero = failure → Step Functions retries or catches
```

### Data Flow: S3 as Data Plane

Step Functions passes small routing info (env vars). Rich data lives in S3.

```
Step Functions (control plane)
  → env vars: JOB_ID, DATABASE_NAME, AGENT_TYPE
    → Agent constructs S3 path: s3://bucket/{DATABASE_NAME}/{JOB_ID}/{agent}/
      → Reads input from upstream agent's S3 output
        → Runs Strands agent logic
          → Writes output to own S3 path
            → Container exits 0
              → Step Functions moves to next state
```

Each agent reads its input from the previous agent's S3 output:

| Agent | Reads from | Writes to |
|-------|-----------|-----------|
| Collector | RDS/CloudWatch/PI (direct) | `{db}/{job}/collector/output.json` |
| Referee-Triage | `{db}/{job}/collector/output.json` | `{db}/{job}/referee-triage/triage.json` |
| Analysis (e.g., dynamodb) | `{db}/{job}/collector/output.json` | `{db}/{job}/analysis-dynamodb/analysis.json` |
| Referee-Synthesis | `{db}/{job}/analysis-*/analysis.json` (all) | `{db}/{job}/referee-synthesis/report.json` |
| Schema Design | `{db}/{job}/referee-synthesis/report.json` | `{db}/{job}/schema-dynamodb/schema.json` |

### S3 Path Helper

```python
# storage.py
import os

BUCKET = f"{os.environ['PROJECT_NAME']}-{os.environ['ENVIRONMENT']}-storage-bucket"

def s3_path(database_name: str, job_id: str, agent_name: str, filename: str) -> str:
    return f"{database_name}/{job_id}/{agent_name}/{filename}"

def read_agent_output(s3_client, database_name: str, job_id: str, agent_name: str) -> dict:
    import json
    key = s3_path(database_name, job_id, agent_name, "output.json")
    response = s3_client.get_object(Bucket=BUCKET, Key=key)
    return json.loads(response["Body"].read())

def write_agent_output(s3_client, database_name: str, job_id: str, agent_name: str, data: dict):
    import json
    key = s3_path(database_name, job_id, agent_name, "output.json")
    s3_client.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(data))
```

---

## Creating Custom Tools

### Tool Structure

```python
from strands import Tool
import mysql.connector
from typing import Dict, Any

def connect_to_mysql(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Connect to MySQL database and return connection metadata.

    Args:
        config: Database connection configuration
            {
                'endpoint': 'db.example.com',
                'port': 3306,
                'database_name': 'mydb',
                'username': 'user',
                'password': '<PASSWORD>'
            }

    Returns:
        Dict with connection status and metadata
    """
    try:
        connection = mysql.connector.connect(
            host=config['endpoint'],
            port=config['port'],
            database=config['database_name'],
            user=config['username'],
            password=config['password'],
            connect_timeout=30
        )

        # Get database version
        cursor = connection.cursor()
        cursor.execute("SELECT VERSION()")
        version = cursor.fetchone()[0]
        cursor.close()

        return {
            'status': 'connected',
            'database_type': 'mysql',
            'version': version,
            'connection': connection
        }

    except Exception as e:
        return {
            'status': 'error',
            'error': str(e)
        }

# Create Strands Tool from function
connect_mysql = Tool(
    name="connect_mysql",
    description="Connect to MySQL database. Returns connection status and metadata.",
    function=connect_to_mysql
)
```

### Schema Collection Tool

```python
def collect_mysql_schema(connection: Any) -> Dict[str, Any]:
    """
    Collect comprehensive schema from MySQL database.

    Returns:
        Dict with schema information (tables, columns, indexes, etc.)
    """
    cursor = connection.cursor(dictionary=True)

    # Collect tables with metadata
    cursor.execute("""
        SELECT
            table_name,
            table_rows as row_count,
            data_length as data_size_bytes,
            index_length as index_size_bytes
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
        AND table_type = 'BASE TABLE'
    """)
    tables = cursor.fetchall()

    schema_data = {'tables': []}

    # For each table, collect columns and indexes
    for table in tables:
        table_name = table['table_name']

        # Get columns
        cursor.execute(f"""
            SELECT
                column_name,
                data_type,
                is_nullable,
                column_key
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
            AND table_name = '{table_name}'
            ORDER BY ordinal_position
        """)
        columns = cursor.fetchall()

        schema_data['tables'].append({
            'table_name': table_name,
            'row_count': table['row_count'],
            'data_size_mb': table['data_size_bytes'] / 1024 / 1024,
            'columns': columns
        })

    cursor.close()
    return schema_data

collect_schema = Tool(
    name="collect_schema",
    description="Collect complete schema from MySQL database including tables, columns, indexes",
    function=collect_mysql_schema
)
```

---

## Building Collector Agents

### MySQL Collector Example

```python
from strands import Agent
from tools.database.mysql_tools import (
    connect_mysql,
    collect_schema,
    collect_query_patterns
)
from tools.validation.contract_validation import validate_output
from typing import Dict, Any

class MySQLCollectorAgent:
    """
    MySQL Collector Agent using Strands SDK.

    This wrapper provides a clean interface for ECS Fargate orchestrator
    while using Strands Agent internally.
    """

    def __init__(self, input_contract: Dict[str, Any]):
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']
        self.database_config = input_contract['source_database']

        # Create Strands Agent with system prompt and tools
        self.agent = Agent(
            system_prompt=self._create_system_prompt(),
            callback_handler=None,  # Optional progress tracking
            tools=[
                connect_mysql,
                collect_schema,
                collect_query_patterns,
                validate_output
            ]
        )

    def _create_system_prompt(self) -> str:
        """
        System prompt defines agent behavior.

        Instructs the agent on:
        1. Role and responsibilities
        2. How to use tools
        3. Output format requirements
        4. Error handling
        """
        return f"""You are a MySQL Database Collector Agent.

Your mission: Collect comprehensive metadata, schema, and query patterns from MySQL.

**Your Tools:**
1. connect_mysql - Establish database connection
2. collect_schema - Gather table structures, columns, indexes
3. collect_query_patterns - Analyze query performance from performance_schema
4. validate_output - Validate final output against contract

**Execution Steps:**
1. Connect to database using provided configuration
2. If connection fails, return error with partial results
3. Collect database metadata (version, size, table count)
4. Collect comprehensive schema
5. Collect query patterns from performance_schema
6. Validate output against CollectorOutputContract
7. Return structured JSON output

**Output Format:**
{{
    "job_id": "{self.job_id}",
    "collector_version": "2.0.0-strands",
    "collection_timestamp": "ISO 8601 timestamp",
    "database_metadata": {{}},
    "schema": {{}},
    "query_patterns": []
}}

**Error Handling:**
- Connection fails: Return error with empty data
- Schema collection fails: Return partial results with warning
- Always validate output before returning

Begin collection when you receive the database configuration."""

    def collect(self) -> Dict[str, Any]:
        """
        Execute collection workflow using Strands Agent.

        Returns:
            Dict containing collector output matching CollectorOutputContract
        """
        # Format input for agent
        agent_input = f"""Collect data from this MySQL database:

Job ID: {self.job_id}

Database Configuration:
- Endpoint: {self.database_config['endpoint']}
- Port: {self.database_config['port']}
- Database: {self.database_config['database_name']}

Execute the collection workflow using your tools."""

        # Execute Strands Agent
        response = self.agent(agent_input)

        # Parse response (Strands returns string by default)
        import json
        output = json.loads(str(response))

        return output

# Factory function
def create_mysql_collector(input_contract: Dict[str, Any]) -> MySQLCollectorAgent:
    """Factory function to create MySQL Collector Agent."""
    return MySQLCollectorAgent(input_contract)
```

---

## Contract Validation Pattern

### Validation Tools

```python
from strands import Tool
import jsonschema
import json

def validate_input_contract(input_data: dict) -> dict:
    """
    Validate input against CollectorInputContract schema

    Args:
        input_data: Input JSON to validate

    Returns:
        {'status': 'valid'} or {'status': 'invalid', 'errors': [...]}
    """
    with open('contracts/schemas/collector-input.json') as f:
        schema = json.load(f)

    try:
        jsonschema.validate(instance=input_data, schema=schema)
        return {'status': 'valid'}
    except jsonschema.ValidationError as e:
        return {
            'status': 'invalid',
            'errors': [e.message],
            'path': list(e.path)
        }

validate_input = Tool(
    name="validate_input_contract",
    description="Validate input JSON against CollectorInputContract schema",
    function=validate_input_contract
)

def validate_output_contract(output_data: dict) -> dict:
    """
    Validate output against CollectorOutputContract schema

    Args:
        output_data: Output JSON to validate

    Returns:
        {'status': 'valid'} or {'status': 'invalid', 'errors': [...]}
    """
    with open('contracts/schemas/collector-output.json') as f:
        schema = json.load(f)

    try:
        jsonschema.validate(instance=output_data, schema=schema)
        return {'status': 'valid'}
    except jsonschema.ValidationError as e:
        return {
            'status': 'invalid',
            'errors': [e.message],
            'path': list(e.path)
        }

validate_output = Tool(
    name="validate_output_contract",
    description="Validate output JSON against CollectorOutputContract schema",
    function=validate_output_contract
)
```

---

## Error Handling Pattern

### Standard Error Handler

```python
def handle_collection_error(error: Exception, context: dict) -> dict:
    """
    Standard error handling for all collectors

    Args:
        error: The exception that occurred
        context: Context information (job_id, table_name, etc.)

    Returns:
        Error details dict
    """
    return {
        'error_type': type(error).__name__,
        'error_message': str(error),
        'context': context,
        'timestamp': datetime.now().isoformat(),
        'recoverable': is_transient_error(error)
    }

def is_transient_error(error: Exception) -> bool:
    """Determine if error is transient (should retry)"""
    transient_errors = [
        'ConnectionError',
        'TimeoutError',
        'OperationalError',
        'ThrottlingException'
    ]
    return type(error).__name__ in transient_errors
```

---

## Progress Reporting (ADR-016)

Agents report progress at each mini-step boundary via EventBridge. The API server (FastAPI) subscribes to these events and pushes them to connected WebSocket clients.

### Flow

```
Agent mini-step → EventBridge (PutEvents) → EventBridge Rule
  → Lambda → API Gateway WebSocket / FastAPI WebSocket → Browser
```

### EventBridge Event Format

```python
{
    "Source": "modernizer.agent",
    "DetailType": "AgentProgress",
    "EventBusName": os.environ["EVENT_BUS_NAME"],
    "Detail": json.dumps({
        "job_id": "2GxZLsnP00Y...",
        "agent_name": "collector",
        "mini_step": "collect_schema",
        "status": "completed",       # started | completed | failed
        "timestamp": "2026-02-18T...",
        "metadata": {
            "tables_processed": 150,
            "total_tables": 300,
            "percent_complete": 50
        }
    })
}
```

### ProgressReporter (used by all agents)

```python
import boto3
import json
import os
from datetime import datetime, timezone

class ProgressReporter:
    """Publishes mini-step progress to EventBridge."""

    def __init__(self, job_id: str, agent_name: str):
        self.job_id = job_id
        self.agent_name = agent_name
        self.event_bus = os.environ["EVENT_BUS_NAME"]
        self.client = boto3.client("events")

    def report(self, mini_step: str, status: str, **metadata):
        self.client.put_events(Entries=[{
            "Source": "modernizer.agent",
            "DetailType": "AgentProgress",
            "EventBusName": self.event_bus,
            "Detail": json.dumps({
                "job_id": self.job_id,
                "agent_name": self.agent_name,
                "mini_step": mini_step,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata
            })
        }])
```

### Usage in Agent Code

```python
def run_collector(job_id: str, database_name: str):
    progress = ProgressReporter(job_id, "collector")

    progress.report("connect", "started")
    connection = connect_to_rds(database_name)
    progress.report("connect", "completed")

    progress.report("collect_schema", "started")
    schema = collect_schema(connection)
    progress.report("collect_schema", "completed", tables_processed=len(schema))

    progress.report("collect_metrics", "started")
    metrics = collect_cloudwatch_metrics(database_name)
    progress.report("collect_metrics", "completed")

    # ... write output to S3
    progress.report("save_output", "completed")
```

### Strands Hook Integration

For agents using Strands SDK conversation loop, register hooks to auto-report:

```python
from strands.hooks import HookEvent

def register_progress_hooks(agent, progress: ProgressReporter, step_name: str):
    @agent.hooks.register(HookEvent.AGENT_START)
    def on_start(context):
        progress.report(step_name, "started")

    @agent.hooks.register(HookEvent.AGENT_END)
    def on_complete(context):
        progress.report(step_name, "completed")
```

### Restart Points

Each agent declares its mini-steps. These are the restart points per ADR-016. Restarting a mini-step cascades to all subsequent mini-steps and downstream agents.

```python
# Collector restart points
COLLECTOR_MINI_STEPS = ["connect", "collect_schema", "collect_metrics",
                        "collect_samples", "collect_pi", "save_output"]

# Analysis restart points
ANALYSIS_MINI_STEPS = ["load_input", "analyze", "score", "save_output"]

# Referee-Triage restart points
TRIAGE_MINI_STEPS = ["load_collector", "evaluate_patterns", "select_agents", "save_triage"]
```

---

## Standard Execution Flow

All collector agents follow this pattern:

```
1. validate_input_contract(input)
   ├─ Valid → Continue
   └─ Invalid → Return error

2. connect_to_rds(connection_config)
   ├─ Success → Continue
   └─ Failure → Return error with partial results

3. collect_rds_metadata(rds_api)
   ├─ Success → Continue
   └─ Failure → Log warning, continue

4. collect_cloudwatch_metrics(cloudwatch_api)
   ├─ Success → Continue
   └─ Failure → Log warning, continue

5. collect_performance_insights(pi_api)
   ├─ Success → Continue
   └─ Failure → Log warning, continue

6. collect_schema(database_connection)
   ├─ Success → Continue
   └─ Failure → Return error (schema is required)

7. collect_sample_data(database_connection)
   ├─ Success → Continue
   └─ Failure → Log warning, continue (optional)

8. validate_output_contract(output)
   ├─ Valid → Return output
   └─ Invalid → Fix and retry, or return error
```

---

## Related Documentation

- [High-Level Design](../architecture/high-level-design.md)
- [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- [Agent Framework Diagram](../architecture/architecture-diagrams/04-agent-framework.md)
- [Orchestration Architecture](../architecture/architecture-diagrams/11-orchestration-architecture.md)
- [Progress Reporting Diagram](../architecture/architecture-diagrams/10-progress-reporting.md)
- [Strands Collector Guide](strands-collector-guide.md)
- [Contract Specifications](../../contracts/README.md)

---

**Last Updated:** February 18, 2026
**Maintained By:** Database Modernizer Assessment Engineering Team
