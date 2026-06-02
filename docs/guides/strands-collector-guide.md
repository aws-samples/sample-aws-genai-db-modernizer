# Collector Agent Implementation Guide with Strands SDK

## Document Information

**Version:** 2.0.0 (Strands SDK)
**Date:** January 22, 2026
**Status:** Draft
**Owner:** Database Modernizer Assessment Engineering Team
**Audience:** Backend Engineers implementing Collector Agents with Strands

---

## Executive Summary

This guide provides comprehensive implementation details for building **Collector Agents using the Strands SDK**. Collector Agents connect to source databases, execute analysis queries, and produce standardized outputs for Analysis Agents.

**Key Changes from v1.0:**

- ✅ Uses **Strands Agent framework** instead of custom base classes
- ✅ Implements **Strands Tools** for database operations
- ✅ Leverages **Strands system prompts** for agent behavior
- ✅ Simpler architecture with less boilerplate

**Core Pattern:**

```python
from strands import Agent
from database_modernizer_tools import connect_mysql, collect_schema

collector = Agent(
    system_prompt="You are a MySQL collector agent...",
    tools=[connect_mysql, collect_schema, collect_query_patterns]
)

output = collector(input_contract)
```

---

## Table of Contents

1. Architecture Decisions (ADRs)
2. Strands SDK Overview
3. Architecture with Strands
4. Creating Database Tools
5. Building Collector Agents
6. Contract Validation
7. Error Handling
8. Progress Reporting
9. AWS Integration and RDS Tools
10. Testing Strategy
11. Complete Example: MySQLCollectorAgent
12. Deployment Considerations

---

## 1. Architecture Decisions (ADRs)

Before implementing collector agents, review these critical architecture decisions:

### ADR-001: State Management and Checkpoints

**Decision:** Hybrid approach with Strands in-memory state + S3 checkpoints

**Key Points:**

- ✅ Strands Workflow manages agent-to-agent communication (fast, in-memory)
- ✅ 11 checkpoint stages for resume capability (spot-optimized)
- ✅ Single retry with formatting instructions (not multiple retries)
- ✅ Storage abstraction (S3 production, local filesystem dev)

**Implementation Impact:**

```python
# Agents don't manage state - Strands handles it
# Just implement checkpoint hooks
@agent.hooks.register(HookEvent.AGENT_END)
def on_complete(context):
    save_checkpoint(job_id, stage_name, context.result)
```

**Full Details:** [ADR-001](../architecture/decisions/ADR-001-state-management-and-checkpoints.md)

---

### ADR-002: Structured Output and Validation

**Decision:** LLM structured output + post-execution hook validation

**Key Points:**

- ✅ LLM enforces JSON structure via `response_format` (primary)
- ✅ Post-execution hook validates schema in Python (mandatory, 0 tokens)
- ✅ 3 internal retries at agent level (self-healing)
- ✅ No format retries needed (LLM knows format upfront)
- ✅ Type-safe with Pydantic models

**Implementation Impact:**

```python
# Pydantic model defines output
from pydantic import BaseModel

class CollectorOutput(BaseModel):
    job_id: str
    collector_version: str
    database_metadata: dict
    schema: dict

# Strands with Pydantic
agent = Agent(
    response_format=CollectorOutput  # Pydantic model
)

# Output is guaranteed valid or raises ValidationError
output = agent(input_data)
```

**Full Details:** [ADR-002](../architecture/decisions/ADR-002-structured-output-and-validation.md)

---

### ADR-003: Progress Reporting Architecture

**Decision:** ECS Fargate + EventBridge + API Gateway WebSocket

**Key Points:**

- ✅ Strands hooks publish progress events to EventBridge
- ✅ EventBridge routes to Lambda → WebSocket
- ✅ Stage-level granularity (11 updates per job)
- ✅ DynamoDB stores job metadata + WebSocket connection IDs
- ✅ Fully managed, event-driven architecture

**Implementation Impact:**

```python
# Agents publish progress via hooks
@agent.hooks.register(HookEvent.AGENT_START)
def on_start(context):
    publish_progress(job_id, stage_name, "started")

@agent.hooks.register(HookEvent.AGENT_END)
def on_complete(context):
    publish_progress(job_id, stage_name, "completed")
```

**Full Details:** [ADR-003](../architecture/decisions/ADR-003-progress-reporting-architecture.md)

---

### ADR-004: RDS Tools and AWS Integration

**Decision:** Granular RDS tools with unified credential management

**Key Points:**

- ✅ 5 granular tools (connect, schema, RDS API, CloudWatch, Performance Insights)
- ✅ Unified credential manager (same-account + cross-account transparent)
- ✅ Graceful degradation for optional AWS tools
- ✅ Secrets Manager for database credentials (no passwords)
- ✅ IAM roles for AWS API access

**Implementation Impact:**

```python
# Credential manager auto-detects deployment mode
credential_manager = AWSCredentialManager(
    region=aws_config['region'],
    role_arn=aws_config.get('cross_account_role_arn'),  # None = same-account
    external_id=aws_config.get('external_id')
)

# Tools use credential manager
tools = [
    connect_mysql,  # CRITICAL (fail fast)
    collect_schema,  # CRITICAL (fail fast)
    get_rds_config,  # OPTIONAL (graceful degradation)
    get_cloudwatch_metrics,  # OPTIONAL (graceful degradation)
    get_performance_insights  # OPTIONAL (graceful degradation)
]
```

**Full Details:** [ADR-004](../architecture/decisions/ADR-004-rds-tools-and-aws-integration.md)

---

## 2. Strands SDK Overview

### 2.1 What is Strands?

**Strands** is an agentic AI framework that provides:

- `Agent` class for creating specialized agents
- `Tool` system for agent capabilities
- Built-in LLM orchestration
- Simple sequential workflows

**Installation:**

```bash
uv add strands-agents
```

### 1.2 Core Strands Concepts

**Agent:**

```python
from strands import Agent

agent = Agent(
    system_prompt="You are a specialist in...",
    callback_handler=None,  # Optional progress tracking
    tools=[tool1, tool2]    # Agent capabilities
)

# Execute agent
response = agent("Do this task with this input")
```

**Tool:**

```python
from strands_tools import http_request  # Built-in tool

# Or create custom tool (we'll show this later)
from strands import Tool

my_tool = Tool(
    name="my_tool",
    description="What this tool does",
    function=my_function
)
```

**Workflow:**

```python
# Sequential agent workflow
result1 = agent1(input_data)
result2 = agent2(f"Process this: {result1}")
result3 = agent3(f"Finalize: {result2}")
```

---

## 2. Architecture with Strands

### 2.1 Database Modernizer Assessment Agent Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI REST API                             │
│                 (Receives job requests)                         │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              EventBridge Orchestrator                           │
│     (Event-driven workflow with automatic retry & DLQ)          │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              STRANDS COLLECTOR AGENTS                           │
│                                                                 │
│  MySQLCollectorAgent (Strands Agent)                            │
│  ├── System Prompt: "You are MySQL collector..."                │
│  └── Tools:                                                     │
│      ├── connect_mysql          (Custom Tool)                   │
│      ├── collect_schema         (Custom Tool)                   │
│      ├── collect_query_patterns (Custom Tool)                   │
│      └── validate_output        (Custom Tool)                   │
│                                                                 │
│  PostgreSQLCollectorAgent (Strands Agent)                       │
│  ├── System Prompt: "You are PostgreSQL collector..."           │
│  └── Tools: [connect_postgres, collect_schema, ...]             │
│                                                                 │
│  SQLServerCollectorAgent (Strands Agent)                        │
│  └── Tools: [connect_sqlserver, collect_schema, ...]            │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
                  CollectorOutputContract
                  (JSON validation)
```

### 2.2 Key Differences from Custom Base Class Approach

**Before (Custom Base Class):**

```python
class BaseCollectorAgent(ABC):
    @abstractmethod
    def _connect_to_database(self):
        pass

    @abstractmethod
    def _collect_schema(self):
        pass

    def collect(self):
        # Complex orchestration logic
        pass

class MySQLCollectorAgent(BaseCollectorAgent):
    def _connect_to_database(self):
        # Implementation
        pass
```

**After (Strands SDK):**

```python
from strands import Agent
from database_modernizer_tools import connect_mysql, collect_schema

mysql_collector = Agent(
    system_prompt="You are a MySQL collector...",
    tools=[connect_mysql, collect_schema]
)

# That's it! No inheritance, no abstract methods
```

**Benefits:**

- ✅ Less boilerplate code
- ✅ Strands handles orchestration
- ✅ Tools are reusable across agents
- ✅ Easier to test individual tools
- ✅ System prompt defines behavior clearly

---

## 3. Creating Database Tools

### 3.1 Tool Structure

Database Modernizer Assessment uses **custom Strands tools** for database operations:

```
src/tools/
├── __init__.py
├── database/
│   ├── __init__.py
│   ├── mysql_tools.py
│   ├── postgresql_tools.py
│   └── sqlserver_tools.py
├── validation/
│   ├── __init__.py
│   └── contract_validation_tools.py
└── utilities/
    ├── __init__.py
    └── progress_reporting_tools.py
```

### 3.2 Creating a Custom Tool

**Pattern:**

```python
# src/tools/database/mysql_tools.py

from typing import Dict, Any
import mysql.connector
from strands import Tool

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
            'connection': connection  # Store for later use
        }

    except Exception as e:
        return {
            'status': 'error',
            'error': str(e)
        }

# Create Strands Tool
connect_mysql = Tool(
    name="connect_mysql",
    description="Connect to MySQL database. Returns connection status and metadata.",
    function=connect_to_mysql
)
```

### 3.3 Schema Collection Tool

```python
# src/tools/database/mysql_tools.py

def collect_mysql_schema(connection: Any) -> Dict[str, Any]:
    """
    Collect comprehensive schema from MySQL database.

    Args:
        connection: Active MySQL connection

    Returns:
        Dict with schema information (tables, columns, indexes, etc.)
    """
    cursor = connection.cursor(dictionary=True)

    # Collect tables
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
                column_key,
                column_default,
                extra
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
            AND table_name = '{table_name}'
            ORDER BY ordinal_position
        """)
        columns = cursor.fetchall()

        # Get indexes
        cursor.execute(f"SHOW INDEX FROM {table_name}")
        indexes = cursor.fetchall()

        # Organize table data
        schema_data['tables'].append({
            'table_id': f"{connection.database}.{table_name}",
            'table_name': table_name,
            'row_count': table['row_count'],
            'data_size_mb': table['data_size_bytes'] / 1024 / 1024,
            'index_size_mb': table['index_size_bytes'] / 1024 / 1024,
            'columns': columns,
            'indexes': indexes
        })

    cursor.close()
    return schema_data

# Create Strands Tool
collect_schema = Tool(
    name="collect_schema",
    description="Collect complete schema from MySQL database including tables, columns, indexes",
    function=collect_mysql_schema
)
```

### 3.4 Query Pattern Collection Tool

```python
# src/tools/database/mysql_tools.py

def collect_mysql_query_patterns(connection: Any, hours: int = 24) -> list:
    """
    Collect query patterns from MySQL performance_schema.

    Args:
        connection: Active MySQL connection
        hours: Number of hours to analyze (default 24)

    Returns:
        List of query patterns with metrics
    """
    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
            DIGEST_TEXT as query_text,
            COUNT_STAR as execution_count,
            AVG_TIMER_WAIT/1000000000 as avg_time_ms,
            MIN_TIMER_WAIT/1000000000 as min_time_ms,
            MAX_TIMER_WAIT/1000000000 as max_time_ms,
            SUM_ROWS_AFFECTED as total_rows_affected,
            SUM_ROWS_SENT as total_rows_returned
        FROM performance_schema.events_statements_summary_by_digest
        WHERE SCHEMA_NAME = DATABASE()
        ORDER BY COUNT_STAR DESC
        LIMIT 100
    """

    cursor.execute(query)
    patterns = cursor.fetchall()

    # Process and enrich patterns
    processed_patterns = []
    for pattern in patterns:
        processed_patterns.append({
            'query_id': hashlib.sha256(pattern['query_text'].encode()).hexdigest()[:16],
            'query_text': pattern['query_text'],
            'query_type': extract_query_type(pattern['query_text']),
            'frequency_per_hour': pattern['execution_count'] / hours,
            'execution_time_ms_avg': pattern['avg_time_ms'],
            'execution_time_ms_min': pattern['min_time_ms'],
            'execution_time_ms_max': pattern['max_time_ms'],
            'rows_returned_avg': pattern['total_rows_returned'] / pattern['execution_count'],
            'tables_accessed': extract_tables(pattern['query_text'])
        })

    cursor.close()
    return processed_patterns

# Create Strands Tool
collect_query_patterns = Tool(
    name="collect_query_patterns",
    description="Collect query patterns from MySQL performance_schema with frequency and timing metrics",
    function=collect_mysql_query_patterns
)
```

### 3.5 Contract Validation Tool

```python
# src/tools/validation/contract_validation_tools.py

import jsonschema
from typing import Dict, Any

def validate_collector_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate collector output against CollectorOutputContract.

    Args:
        output: Collector output to validate

    Returns:
        Dict with validation status and errors (if any)
    """
    # Load schema (assume it's available)
    schema = load_collector_output_schema()

    try:
        jsonschema.validate(output, schema)
        return {
            'valid': True,
            'errors': []
        }
    except jsonschema.ValidationError as e:
        return {
            'valid': False,
            'errors': [str(e)]
        }

# Create Strands Tool
validate_output = Tool(
    name="validate_output",
    description="Validate collector output against CollectorOutputContract JSON schema",
    function=validate_collector_output
)
```

---

## 4. Building Collector Agents

### 4.1 MySQL Collector Agent

**File: `src/agents/collector/mysql_collector.py`**

```python
#!/usr/bin/env python3
"""
MySQL Collector Agent using Strands SDK

Collects comprehensive database metadata, schema, and query patterns from MySQL databases.
"""

from typing import Dict, Any
from strands import Agent
from tools.database.mysql_tools import (
    connect_mysql,
    collect_schema,
    collect_query_patterns
)
from tools.validation.contract_validation_tools import validate_output


class MySQLCollectorAgent:
    """
    MySQL Collector Agent wrapper that orchestrates Strands Agent execution.

    This class provides a clean interface for the EventBridge orchestrator while
    using Strands Agent internally for the collection logic.
    """

    def __init__(self, input_contract: Dict[str, Any]):
        """
        Initialize MySQL Collector Agent.

        Args:
            input_contract: CollectorInputContract with database config
        """
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']
        self.database_config = input_contract['source_database']

        # Create Strands Agent with system prompt and tools
        self.agent = Agent(
            system_prompt=self._create_system_prompt(),
            callback_handler=None,  # TODO: Add progress callback
            tools=[
                connect_mysql,
                collect_schema,
                collect_query_patterns,
                validate_output
            ]
        )

    def _create_system_prompt(self) -> str:
        """
        Create system prompt that defines agent behavior.

        The prompt instructs the agent on:
        1. Its role and responsibilities
        2. How to use tools
        3. Output format requirements
        4. Error handling approach
        """
        return f"""You are a MySQL Database Collector Agent specialized in analyzing MySQL databases.

Your mission: Collect comprehensive metadata, schema, and query patterns from the target MySQL database.

**Your Tools:**
1. connect_mysql - Establish database connection
2. collect_schema - Gather table structures, columns, indexes, foreign keys
3. collect_query_patterns - Analyze query performance from performance_schema
4. validate_output - Validate final output against contract

**Execution Steps:**
1. Connect to database using provided configuration
2. If connection fails, return error with partial results
3. Collect database metadata (version, size, table count)
4. Collect comprehensive schema (tables, columns, indexes, views, procedures)
5. Collect query patterns from performance_schema (last 24 hours)
6. Validate output against CollectorOutputContract
7. Return structured JSON output

**Output Format:**
{{
    "job_id": "{self.job_id}",
    "collector_version": "2.0.0-strands",
    "collection_timestamp": "ISO 8601 timestamp",
    "database_metadata": {{}},
    "schema": {{}},
    "query_patterns": [],
    "collection_stats": {{}}
}}

**Error Handling:**
- If connection fails: Return error with empty data
- If schema collection fails: Return partial results with warning
- If query patterns unavailable: Continue with empty patterns array
- Always validate output before returning

**Critical Rules:**
1. Never expose passwords in output
2. Anonymize query parameters if requested
3. Include source URLs for all metrics
4. Report progress at each major step
5. Handle timeouts gracefully with partial results

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
- Authentication: {self.database_config['authentication_method']}

Collection Options:
{self.input_contract.get('collection_options', {})}

Query Analysis Options:
{self.input_contract.get('query_analysis_options', {})}

Execute the collection workflow using your tools."""

        # Execute Strands Agent
        response = self.agent(agent_input)

        # Strands Agent returns string by default
        # Parse it as JSON if needed, or have agent return structured data
        import json
        output = json.loads(str(response))

        return output


# Convenience function for direct usage
def create_mysql_collector(input_contract: Dict[str, Any]) -> MySQLCollectorAgent:
    """Factory function to create MySQL Collector Agent."""
    return MySQLCollectorAgent(input_contract)
```

### 4.2 PostgreSQL Collector Agent

**File: `src/agents/collector/postgresql_collector.py`**

```python
from strands import Agent
from tools.database.postgresql_tools import (
    connect_postgres,
    collect_schema,
    collect_query_patterns
)

class PostgreSQLCollectorAgent:
    """PostgreSQL Collector Agent using Strands SDK."""

    def __init__(self, input_contract: Dict[str, Any]):
        self.input_contract = input_contract
        self.agent = Agent(
            system_prompt=self._create_system_prompt(),
            tools=[
                connect_postgres,
                collect_schema,
                collect_query_patterns,
                validate_output
            ]
        )

    def _create_system_prompt(self) -> str:
        return """You are a PostgreSQL Database Collector Agent...

        [Similar prompt structure as MySQL agent, but PostgreSQL-specific]
        """

    def collect(self) -> Dict[str, Any]:
        response = self.agent(self._format_input())
        return json.loads(str(response))
```

---

## 5. Contract Validation

### 5.1 Why Contracts Matter with Strands

Even with Strands SDK, **contracts are critical** because:

- ✅ Strands agents can return arbitrary text
- ✅ Analysis agents expect structured JSON
- ✅ Contract validation ensures data quality
- ✅ Prevents integration failures

### 5.2 Validation Tool

```python
# src/tools/validation/contract_validation_tools.py

import jsonschema
from pathlib import Path

def load_collector_output_schema() -> dict:
    """Load CollectorOutputContract JSON schema."""
    schema_path = Path(__file__).parent.parent.parent / 'contracts' / 'schemas' / 'collector_output.json'
    with open(schema_path, 'r') as f:
        return json.load(f)

def validate_collector_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate collector output against CollectorOutputContract.

    This tool is provided to Strands agents so they can self-validate.
    """
    schema = load_collector_output_schema()

    try:
        jsonschema.validate(output, schema)
        return {
            'valid': True,
            'errors': [],
            'message': 'Output is valid'
        }
    except jsonschema.ValidationError as e:
        return {
            'valid': False,
            'errors': [
                {
                    'path': list(e.path),
                    'message': e.message
                }
            ],
            'message': f'Validation failed: {e.message}'
        }

validate_output = Tool(
    name="validate_output",
    description="Validate collector output against CollectorOutputContract JSON schema",
    function=validate_collector_output
)
```

---

## 6. AWS Integration and RDS Tools

**Architecture Decision:** See [ADR-004: RDS Tools and AWS Integration](../architecture/decisions/ADR-004-rds-tools-and-aws-integration.md)

### 6.1 Unified Credential Manager

The `AWSCredentialManager` transparently handles both same-account and cross-account deployments:

```python
# src/tools/aws/credential_manager.py

import boto3
from typing import Optional

class AWSCredentialManager:
    """
    Manages AWS credentials for both same-account and cross-account access.
    Auto-detects deployment mode based on input configuration.
    """

    def __init__(
        self,
        region: str,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None
    ):
        self.region = region
        self.role_arn = role_arn
        self.external_id = external_id
        self._session = None
        self._mode = "cross-account" if role_arn else "same-account"

    def get_session(self) -> boto3.Session:
        """Get boto3 session with appropriate credentials"""
        if self._session:
            return self._session

        if self._mode == "same-account":
            # Use ECS task role
            self._session = boto3.Session(region_name=self.region)
        else:
            # Use AssumeRole for cross-account
            self._session = self._assume_role()

        return self._session

    def _assume_role(self) -> boto3.Session:
        """Assume cross-account role"""
        sts = boto3.client('sts')

        assume_role_params = {
            'RoleArn': self.role_arn,
            'RoleSessionName': 'ModernizerCollector',
            'DurationSeconds': 3600
        }

        if self.external_id:
            assume_role_params['ExternalId'] = self.external_id

        response = sts.assume_role(**assume_role_params)
        credentials = response['Credentials']

        return boto3.Session(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=self.region
        )
```

### 6.2 RDS Tools Implementation

**Five granular tools:**

1. **connect_mysql** (CRITICAL - fail fast)
2. **collect_schema** (CRITICAL - fail fast)
3. **get_rds_instance_config** (OPTIONAL - graceful degradation)
4. **get_cloudwatch_metrics** (OPTIONAL - graceful degradation)
5. **get_performance_insights** (OPTIONAL - graceful degradation)

```python
# src/tools/aws/rds_tools.py

from tools.aws.credential_manager import AWSCredentialManager

def get_database_credentials(
    secret_arn: str,
    credential_manager: AWSCredentialManager
) -> dict:
    """
    Retrieve database credentials from Secrets Manager.
    CRITICAL tool - fails fast if credentials unavailable.
    """
    session = credential_manager.get_session()
    secrets = session.client('secretsmanager')

    response = secrets.get_secret_value(SecretId=secret_arn)
    secret_data = json.loads(response['SecretString'])

    return {
        'host': secret_data['host'],
        'port': secret_data.get('port', 3306),
        'username': secret_data['username'],
        'password': secret_data['password'],
        'database': secret_data.get('database', 'mysql')
    }


def get_rds_instance_config(
    instance_id: str,
    credential_manager: AWSCredentialManager
) -> dict:
    """
    Get RDS instance configuration.
    OPTIONAL tool - returns available=false on failure.
    """
    try:
        session = credential_manager.get_session()
        rds = session.client('rds')

        response = rds.describe_db_instances(
            DBInstanceIdentifier=instance_id
        )

        instance = response['DBInstances'][0]

        return {
            'available': True,
            'engine': instance['Engine'],
            'engine_version': instance['EngineVersion'],
            'instance_class': instance['DBInstanceClass'],
            'storage_type': instance['StorageType'],
            'allocated_storage': instance['AllocatedStorage']
        }

    except Exception as e:
        logger.warning(f"Failed to get RDS config: {e}")
        return {
            'available': False,
            'error': str(e),
            'message': 'RDS API access failed - continuing with database-only analysis'
        }


def get_cloudwatch_metrics(
    instance_id: str,
    credential_manager: AWSCredentialManager,
    days: int = 7
) -> dict:
    """
    Get CloudWatch metrics (last 7 days, hourly aggregation).
    OPTIONAL tool - returns available=false on failure.
    """
    try:
        session = credential_manager.get_session()
        cloudwatch = session.client('cloudwatch')

        # Get metrics for last 7 days
        # Returns pre-aggregated summary (average, maximum)
        # See ADR-004 for full implementation

        return {
            'available': True,
            'period_days': days,
            'metrics': {
                'CPUUtilization': {'average': 45.2, 'maximum': 89.1},
                'DatabaseConnections': {'average': 42, 'maximum': 87}
            }
        }

    except Exception as e:
        logger.warning(f"Failed to get CloudWatch metrics: {e}")
        return {
            'available': False,
            'error': str(e),
            'message': 'CloudWatch access failed - continuing without metrics'
        }
```

### 6.3 Collector Agent Integration

```python
# src/agents/collector/mysql_collector.py

from tools.aws.credential_manager import AWSCredentialManager
from tools.aws.rds_tools import (
    get_database_credentials,
    get_rds_instance_config,
    get_cloudwatch_metrics,
    get_performance_insights
)

class MySQLCollectorAgent:
    """
    MySQL Collector supporting both same-account and cross-account.
    """

    def __init__(self, input_contract: dict):
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']

        # Setup credentials (auto-detects mode)
        aws_config = input_contract['source_database']['aws_config']
        self.credential_manager = AWSCredentialManager(
            region=aws_config['region'],
            role_arn=aws_config.get('cross_account_role_arn'),  # None = same-account
            external_id=aws_config.get('external_id')
        )

        # Create Strands Agent
        self.agent = Agent(
            system_prompt=self._create_system_prompt(),
            tools=self._create_tools(),
            response_format={
                "type": "json_schema",
                "json_schema": load_json_schema("collector-output.json"),
                "strict": True
            }
        )

        # Attach validation hook (from ADR-002)
        self.agent.post_execution_hook = create_validation_hook(
            agent=self.agent,
            schema_name="collector-output.json",
            max_retries=3
        )

    def _create_tools(self) -> list:
        """Create tools with credential manager injected"""
        aws_config = self.input_contract['source_database']['aws_config']

        return [
            # CRITICAL tools (fail fast)
            Tool(
                name="connect_mysql",
                description="Connect to MySQL using Secrets Manager credentials",
                function=lambda: self._connect_mysql()
            ),
            Tool(
                name="collect_schema",
                description="Collect database schema",
                function=lambda: collect_schema()
            ),

            # OPTIONAL tools (graceful degradation)
            Tool(
                name="get_rds_config",
                description="Get RDS config. Returns available=false if fails.",
                function=lambda: get_rds_instance_config(
                    aws_config['rds_instance_id'],
                    self.credential_manager
                )
            ),
            Tool(
                name="get_cloudwatch_metrics",
                description="Get CloudWatch metrics. Returns available=false if fails.",
                function=lambda: get_cloudwatch_metrics(
                    aws_config['rds_instance_id'],
                    self.credential_manager
                )
            ),
            Tool(
                name="get_performance_insights",
                description="Get Performance Insights. Returns available=false if fails.",
                function=lambda: get_performance_insights(
                    aws_config['rds_instance_id'],
                    self.credential_manager
                )
            )
        ]

    def _connect_mysql(self):
        """Connect using Secrets Manager credentials"""
        aws_config = self.input_contract['source_database']['aws_config']

        creds = get_database_credentials(
            aws_config['secret_arn'],
            self.credential_manager
        )

        return connect_mysql(
            host=creds['host'],
            port=creds['port'],
            username=creds['username'],
            password=creds['password'],
            database=creds['database']
        )

    def _create_system_prompt(self) -> str:
        return f"""You are a MySQL Database Collector Agent.

Your Tools:
1. connect_mysql - Connect using Secrets Manager (REQUIRED)
2. collect_schema - Gather schema metadata (REQUIRED)
3. get_rds_config - Get RDS configuration (OPTIONAL)
4. get_cloudwatch_metrics - Get performance metrics (OPTIONAL)
5. get_performance_insights - Get top queries (OPTIONAL)

Execution Steps:
1. Call connect_mysql (REQUIRED - fail if this fails)
2. Call collect_schema (REQUIRED - fail if this fails)
3. Call get_rds_config (OPTIONAL - continue if returns available=false)
4. Call get_cloudwatch_metrics (OPTIONAL - continue if returns available=false)
5. Call get_performance_insights (OPTIONAL - continue if returns available=false)
6. Return structured JSON output

IMPORTANT: Tools 3-5 may return {{"available": false}} if AWS access fails.
This is OK - continue with database-only analysis.

Output Format: CollectorOutputContract (validated automatically)
"""
```

### 6.4 Input Contract Examples

**Same-Account:**

```json
{
  "job_id": "job-123",
  "source_database": {
    "type": "mysql",
    "aws_config": {
      "region": "us-west-2",
      "rds_instance_id": "my-prod-db",
      "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789:secret:<SECRET_NAME>"
    }
  }
}
```

**Cross-Account:**

```json
{
  "job_id": "job-123",
  "source_database": {
    "type": "mysql",
    "aws_config": {
      "region": "us-west-2",
      "rds_instance_id": "my-prod-db",
      "cross_account_role_arn": "arn:aws:iam::987654321:role/ModernizerAccessRole",
      "external_id": "unique-customer-id-12345",
      "secret_arn": "arn:aws:secretsmanager:us-west-2:987654321:secret:<SECRET_NAME>" # pragma: allowlist secret
    }
  }
}
```

---

## 7. Error Handling

### 7.1 Error Handling in Tools

```python
def connect_mysql(config: Dict[str, Any]) -> Dict[str, Any]:
    """Connect to MySQL with comprehensive error handling."""
    try:
        connection = mysql.connector.connect(**config)
        return {'status': 'success', 'connection': connection}

    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
            return {
                'status': 'error',
                'error_type': 'authentication_failed',
                'message': 'Invalid credentials'
            }
        elif err.errno == errorcode.ER_BAD_DB_ERROR:
            return {
                'status': 'error',
                'error_type': 'database_not_found',
                'message': f'Database does not exist'
            }
        else:
            return {
                'status': 'error',
                'error_type': 'connection_failed',
                'message': str(err)
            }

    except Exception as e:
        return {
            'status': 'error',
            'error_type': 'unexpected_error',
            'message': str(e)
        }
```

### 7.2 System Prompt Error Guidance

Include error handling instructions in system prompt:

```python
system_prompt = """...

**Error Handling:**
If connection fails:
1. Return output with error in collection_stats
2. Set all data fields to empty/defaults
3. Include error message and type

If schema collection fails:
1. Continue with partial results
2. Add warning to collection_stats
3. Mark missing sections

If query patterns unavailable:
1. Return empty query_patterns array
2. Add info message explaining why

Always return valid JSON even on errors.
"""
```

---

## 7. Progress Reporting

### 7.1 Callback Handler for Progress

```python
# src/agents/collector/mysql_collector.py

from strands import Agent
from typing import Callable

class ProgressCallbackHandler:
    """Custom callback handler for progress reporting."""

    def __init__(self, job_id: str, progress_fn: Callable):
        self.job_id = job_id
        self.progress_fn = progress_fn
        self.current_step = 0
        self.total_steps = 5

    def on_tool_start(self, tool_name: str):
        """Called when tool starts executing."""
        self.current_step += 1
        progress = (self.current_step / self.total_steps) * 100

        self.progress_fn({
            'job_id': self.job_id,
            'status': 'running',
            'progress_percent': progress,
            'current_step': tool_name,
            'message': f'Executing {tool_name}...'
        })

    def on_tool_end(self, tool_name: str, result: Any):
        """Called when tool completes."""
        self.progress_fn({
            'job_id': self.job_id,
            'status': 'running',
            'progress_percent': (self.current_step / self.total_steps) * 100,
            'current_step': tool_name,
            'message': f'Completed {tool_name}'
        })

# Usage in agent
class MySQLCollectorAgent:
    def __init__(self, input_contract: Dict[str, Any], progress_callback: Callable = None):
        self.input_contract = input_contract

        callback_handler = None
        if progress_callback:
            callback_handler = ProgressCallbackHandler(
                job_id=input_contract['job_id'],
                progress_fn=progress_callback
            )

        self.agent = Agent(
            system_prompt=self._create_system_prompt(),
            callback_handler=callback_handler,
            tools=[...]
        )
```

---

## 8. Testing Strategy

### 8.1 Tool Unit Tests

```python
# tests/unit/tools/test_mysql_tools.py

import pytest
from tools.database.mysql_tools import connect_mysql, collect_schema

def test_connect_mysql_success(mock_mysql_connection):
    """Test successful MySQL connection."""
    config = {
        'endpoint': 'localhost',
        'port': 3306,
        'database_name': 'testdb',
        'username': 'user',
        'password': '<PASSWORD>'
    }

    result = connect_mysql.function(config)

    assert result['status'] == 'success'
    assert 'connection' in result

def test_connect_mysql_auth_failure():
    """Test connection with invalid credentials."""
    config = {
        'endpoint': 'localhost',
        'port': 3306,
        'database_name': 'testdb',
        'username': 'invalid',
        'password': '<PASSWORD>'
    }

    result = connect_mysql.function(config)

    assert result['status'] == 'error'
    assert result['error_type'] == 'authentication_failed'

def test_collect_schema(mock_mysql_connection):
    """Test schema collection."""
    result = collect_schema.function(mock_mysql_connection)

    assert 'tables' in result
    assert isinstance(result['tables'], list)
    assert len(result['tables']) > 0
```

### 8.2 Agent Integration Tests

```python
# tests/integration/test_mysql_collector_agent.py

import pytest
from agents.collector.mysql_collector import MySQLCollectorAgent

@pytest.mark.integration
def test_mysql_collector_full_workflow(test_mysql_database):
    """Test complete MySQL collection workflow."""
    input_contract = {
        'job_id': 'test-001',
        'source_database': {
            'database_type': 'mysql',
            'endpoint': test_mysql_database['endpoint'],
            'port': 3306,
            'database_name': 'testdb',
            'authentication_method': 'credentials',
            'username': 'test_user',
            'password': '<PASSWORD>'
        },
        'collection_options': {
            'include_tables': True,
            'include_views': True
        }
    }

    agent = MySQLCollectorAgent(input_contract)
    output = agent.collect()

    # Verify output structure
    assert output['job_id'] == 'test-001'
    assert output['database_metadata']['database_type'] == 'mysql'
    assert len(output['database_schema']['tables']) > 0
    assert isinstance(output['query_patterns'], list)

    # Verify contract compliance
    from tools.validation.contract_validation_tools import validate_output
    validation = validate_output.function(output)
    assert validation['valid'] is True
```

---

## 9. Complete Example: MySQLCollectorAgent

See the full implementation in Section 4.1 above.

**Key Files:**

```
src/
├── agents/
│   └── collector/
│       └── mysql_collector.py          # Agent wrapper
├── tools/
│   ├── database/
│   │   └── mysql_tools.py              # connect_mysql, collect_schema, etc.
│   └── validation/
│       └── contract_validation_tools.py # validate_output
└── contracts/
    └── schemas/
        └── collector_output.json        # JSON Schema
```

---

## 10. Deployment Considerations

### 10.1 EventBridge Integration

```python
# src/orchestrator/eventbridge_handler.py

import boto3
import json
import os
from agents.collector.mysql_collector import MySQLCollectorAgent

events_client = boto3.client('events')

def lambda_handler(event, context):
    """
    Lambda function triggered by EventBridge to run collector agent.

    Event structure:
    {
        "Source": "modernizer.api",
        "DetailType": "JobStarted",
        "Detail": {
            "job_id": "...",
            "input_contract": {...}
        }
    }
    """

    # Parse event
    detail = event['detail']
    job_id = detail['job_id']
    input_contract = detail['input_contract']

    # Progress callback publishes to EventBridge
    def update_progress(progress_data):
        events_client.put_events(
            Entries=[{
                'Source': 'modernizer.collector',
                'DetailType': 'CollectorProgress',
                'Detail': json.dumps({
                    'job_id': job_id,
                    **progress_data
                })
            }]
        )

    # Create agent with progress callback
    database_type = input_contract['source_database']['database_type']

    if database_type == 'mysql':
        agent = MySQLCollectorAgent(input_contract, progress_callback=update_progress)
    elif database_type == 'postgresql':
        agent = PostgreSQLCollectorAgent(input_contract, progress_callback=update_progress)
    # ... other database types

    # Execute collection
    output = agent.collect()

    # Publish completion event
    events_client.put_events(
        Entries=[{
            'Source': 'modernizer.collector',
            'DetailType': 'CollectorCompleted',
            'Detail': json.dumps({
                'job_id': job_id,
                'output_location': f's3://bucket/jobs/{job_id}/collector-output.json',
                'status': 'success'
            })
        }]
    )

    return {
        'statusCode': 200,
        'body': json.dumps({'job_id': job_id, 'status': 'completed'})
    }
```

### 10.2 ECS Fargate Task Entry Point

```python
# src/orchestrator/ecs_task.py

import boto3
import json
import os
from agents.collector.mysql_collector import MySQLCollectorAgent

def main():
    """
    ECS Fargate task entry point triggered by EventBridge.
    Event details passed via environment variable.
    """

    # Get event from environment (passed by EventBridge)
    event_detail = json.loads(os.environ['EVENT_DETAIL'])
    job_id = event_detail['job_id']
    input_contract = event_detail['input_contract']

    # Initialize EventBridge client
    events_client = boto3.client('events')

    # Progress callback
    def update_progress(progress_data):
        events_client.put_events(
            Entries=[{
                'Source': 'modernizer.collector',
                'DetailType': 'CollectorProgress',
                'Detail': json.dumps({
                    'job_id': job_id,
                    **progress_data
                })
            }]
        )

    # Create and execute agent
    database_type = input_contract['source_database']['database_type']

    if database_type == 'mysql':
        agent = MySQLCollectorAgent(input_contract, progress_callback=update_progress)
    elif database_type == 'postgresql':
        agent = PostgreSQLCollectorAgent(input_contract, progress_callback=update_progress)

    output = agent.collect()

    # Publish completion event
    events_client.put_events(
        Entries=[{
            'Source': 'modernizer.collector',
            'DetailType': 'CollectorCompleted',
            'Detail': json.dumps({
                'job_id': job_id,
                'output_location': f's3://bucket/jobs/{job_id}/collector-output.json'
            })
        }]
    )

if __name__ == "__main__":
    main()
```

### 10.3 Environment Variables

```bash
# .env

# Strands SDK Configuration
STRANDS_MODEL=anthropic/claude-3-5-sonnet
STRANDS_API_KEY=your-api-key

# AWS Configuration
AWS_REGION=us-east-1
EVENT_BUS_NAME=database-modernizer

# Storage
S3_BUCKET=database-modernizer-outputs
DYNAMODB_TABLE=database-modernizer-jobs
```

---

## Summary

### ✅ What You Built

1. **Custom Database Tools** - Reusable tools for MySQL operations
2. **Strands Collector Agent** - Agent that orchestrates tools
3. **Contract Validation** - Ensures output quality
4. **Progress Reporting** - Real-time status updates via EventBridge
5. **Error Handling** - Graceful failure with partial results
6. **Event-Driven Architecture** - EventBridge orchestration with automatic retry

---

## 10. Checkpoint Strategy (ADR-001)

### When to Checkpoint

Checkpoint after collector completes successfully:

```python
from storage import save_checkpoint

def collect(self) -> CollectorOutput:
    """Collect database metadata with checkpoint"""

    # Execute collection
    output = self._run_collection()

    # Checkpoint (for resume capability)
    save_checkpoint(
        job_id=self.job_id,
        checkpoint_key="collector_complete",
        data=output.model_dump()
    )

    return output
```

### Resume from Checkpoint

```python
from storage import load_checkpoint

def resume_or_collect(job_id: str, input_contract: dict) -> CollectorOutput:
    """Resume from checkpoint or start new collection"""

    # Check for existing checkpoint
    checkpoint = load_checkpoint(job_id, "collector_complete")

    if checkpoint:
        logger.info(f"Job {job_id}: Resuming from checkpoint")
        return CollectorOutput(**checkpoint)

    # No checkpoint, start new collection
    logger.info(f"Job {job_id}: Starting new collection")
    collector = MySQLCollectorAgent(input_contract)
    return collector.collect()
```

**See:** [ADR-001: State Management and Checkpoints](../architecture/decisions/ADR-001-state-management-and-checkpoints.md)

---

## 11. Mini-Collectors for Large Databases (ADR-005)

### When to Use Mini-Collectors

**Threshold:** Databases with >100 tables

```python
class MySQLCollectorAgent:
    MINI_COLLECTOR_THRESHOLD = 100

    async def collect(self) -> CollectorOutput:
        """Collect with automatic mini-collector splitting"""

        tables = await self._get_table_list()

        if len(tables) <= self.MINI_COLLECTOR_THRESHOLD:
            # Single collector (fast path)
            return await self._collect_single(tables)
        else:
            # Mini-collectors (parallel path)
            return await self._collect_parallel(tables)
```

### Parallel Collection Pattern

```python
async def _collect_parallel(self, tables: List[str]) -> CollectorOutput:
    """Run mini-collectors in parallel"""

    # Split into chunks (100 tables each)
    chunks = [tables[i:i+100] for i in range(0, len(tables), 100)]

    logger.info(f"Job {self.job_id}: Split into {len(chunks)} mini-collectors")

    # Run mini-collectors in parallel
    partial_outputs = await asyncio.gather(*[
        self._run_mini_collector(i, chunk)
        for i, chunk in enumerate(chunks)
    ])

    # Merge results
    return self._merge_outputs(partial_outputs)
```

### Performance Benefits

| Database Size | Single Collector | Mini-Collectors | Speedup |
|---------------|------------------|-----------------|---------|
| 100 tables | 15 min | 15 min | 1x |
| 1,000 tables | 2 hours | 15 min | 8x |
| 5,000 tables | 10 hours | 20 min | 30x |

**See:** [ADR-005: Mini-Collectors for Large Databases](../architecture/decisions/ADR-005-mini-collectors-for-large-databases.md)

---

## 12. Cross-Account RDS Access (ADR-004)

### Same-Account Access

```python
import boto3

# Use IAM role attached to ECS task
rds_client = boto3.client('rds')
token = rds_client.generate_db_auth_token(
    DBHostname='mydb.abc123.us-east-1.rds.amazonaws.com',
    Port=3306,
    DBUsername='iam_user'
)

connection = mysql.connector.connect(
    host='mydb.abc123.us-east-1.rds.amazonaws.com',
    user='iam_user',
    password=token,
    database='mydb'
)
```

### Cross-Account Access

```python
# Assume role in target account
sts_client = boto3.client('sts')

assumed_role = sts_client.assume_role(
    RoleArn='arn:aws:iam::TARGET_ACCOUNT:role/ModernizerAccessRole',
    RoleSessionName='modernizer-session',
    ExternalId='unique-external-id-123'
)

# Use temporary credentials
credentials = assumed_role['Credentials']
rds_client = boto3.client(
    'rds',
    aws_access_key_id=credentials['AccessKeyId'],
    aws_secret_access_key=credentials['SecretAccessKey'],
    aws_session_token=credentials['SessionToken']
)

token = rds_client.generate_db_auth_token(...)
```

**See:** [ADR-004: RDS Tools and AWS Integration](../architecture/decisions/ADR-004-rds-tools-and-aws-integration.md)

---

## 13. Testing Your Collector (ADR-009)

### Unit Tests (Mock RDS)

```python
# tests/unit/test_mysql_collector.py

import pytest
from unittest.mock import Mock, patch
from agents.collector.mysql_collector import MySQLCollectorAgent

@patch('mysql.connector.connect')
def test_connect_mysql(mock_connect):
    """Test MySQL connection (mocked RDS)"""
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
```

### Agent Tests (Mock LLM)

```python
@patch('strands.Agent')
def test_mysql_collector_agent(mock_agent_class):
    """Test collector with mocked LLM response"""
    mock_agent = Mock()
    mock_agent.return_value = CollectorOutput(
        collector_version="2.3.1",
        contract_version="1.2",
        job_id="test-job",
        database_metadata={...},
        schema={...}
    )
    mock_agent_class.return_value = mock_agent

    collector = MySQLCollectorAgent(input_contract)
    output = collector.collect()

    assert isinstance(output, CollectorOutput)
    assert output.contract_version == "1.2"
```

**See:** [ADR-009: Testing Infrastructure](../architecture/decisions/ADR-009-testing-infrastructure.md)

---

### 🚀 Next Steps

1. **Implement PostgreSQL tools** - `src/tools/database/postgresql_tools.py`
2. **Implement SQL Server tools** - `src/tools/database/sqlserver_tools.py`
3. **Create Analysis Agents** - Using similar Strands pattern
4. **Build Referee Agent** - Orchestrates multiple analysis agents
5. **Add UI** - React frontend to trigger jobs

### 📚 Key Takeaways

- ✅ Strands simplifies agent creation (no inheritance needed)
- ✅ Tools are reusable and testable independently
- ✅ System prompts define agent behavior clearly
- ✅ Pydantic models enforce contracts (no JSON Schema)
- ✅ Checkpoints enable resume capability
- ✅ Mini-collectors handle large databases (8x-30x speedup)
- ✅ EventBridge event-driven orchestration with automatic retry and DLQ

---

**Ready to implement your first Strands Collector Agent!** 🎉
