# ADR-004: RDS Tools and AWS Integration

**Status:** Accepted
**Date:** 2026-02-01
**Deciders:** Architecture Team
**Related Issues:** Architecture Review Point #5
**Related ADRs:** ADR-001 (State Management), ADR-002 (Structured Output), ADR-003 (Progress Reporting)

---

## Context

Database Modernizer Assessment collector agents need to gather data from multiple sources to provide comprehensive analysis:

1. **Direct database connection** (MySQL, PostgreSQL, etc.)
   - Schema metadata (tables, columns, indexes)
   - Query patterns and statistics
   - Database-specific configurations

2. **AWS RDS API** (boto3)
   - Instance configuration (storage, engine, version)
   - Parameter groups and settings
   - Backup and maintenance windows

3. **CloudWatch Metrics**
   - Performance metrics (CPU, IOPS, connections)
   - Historical trends (last 7 days)
   - Resource utilization patterns

4. **Performance Insights**
   - Top SQL queries by load
   - Wait events and bottlenecks
   - Database load analysis

### Customer Deployment Patterns

Customers deploy RDS instances in various configurations:

**Same-Account Deployment:**

- RDS instance in same AWS account as application
- Direct access via ECS task role
- Simpler IAM configuration

**Cross-Account Deployment:**

- RDS instance in different AWS account (common for security/compliance)
- Application in separate account
- Requires cross-account IAM role with AssumeRole

**Network Isolation:**

- RDS in private VPC (no public access)
- Database credentials in AWS Secrets Manager
- Network access via VPC peering or PrivateLink

### Challenges

1. **Credential Management**: How do agents authenticate to AWS APIs and databases?
2. **Multi-Account Support**: How to handle both same-account and cross-account deployments?
3. **Tool Organization**: Monolithic vs granular tools?
4. **Error Handling**: What if AWS APIs are unavailable?
5. **Data Volume**: How much data should tools return?

---

## Decision

We will implement **granular RDS tools with unified credential management** that transparently supports both same-account and cross-account deployments:

### 1. Tool Organization: Granular Tools

**Five specialized tools:**

1. `connect_mysql` - Database connection using Secrets Manager
2. `get_rds_instance_config` - RDS API (instance configuration)
3. `get_cloudwatch_metrics` - CloudWatch (performance metrics)
4. `get_performance_insights` - Performance Insights (top queries)
5. `collect_schema` - Direct database (schema metadata)

**Rationale:**

- ✅ Agent chooses what to collect (flexibility)
- ✅ Faster execution (only collects needed data)
- ✅ Clear separation of concerns
- ✅ Easier to test and maintain

### 2. Credential Management: Unified Approach

**AWSCredentialManager class:**

- Auto-detects same-account vs cross-account based on input
- Same-account: Uses ECS task role (no AssumeRole)
- Cross-account: Uses AssumeRole with temporary credentials
- Transparent to agent code (same interface for both)

**Rationale:**

- ✅ Single code path for both deployment modes
- ✅ Secure (no long-lived credentials)
- ✅ Customer controls access (IAM roles)
- ✅ Supports multi-account architectures

### 3. Error Handling: Graceful Degradation

**Critical tools (fail fast):**

- `connect_mysql` - Database connection is required
- `collect_schema` - Schema metadata is required

**Optional tools (graceful degradation):**

- `get_rds_instance_config` - Returns `{"available": false}` on failure
- `get_cloudwatch_metrics` - Returns `{"available": false}` on failure
- `get_performance_insights` - Returns `{"available": false}` on failure

**Rationale:**

- ✅ Agent can continue with partial data
- ✅ Better user experience (doesn't fail entire job)
- ✅ Handles permission issues gracefully

### 4. Data Volume: Pre-Aggregated Summaries

**CloudWatch and Performance Insights return summaries:**

- Last 7 days of data (balance between insight and cost)
- Hourly aggregation (not raw data points)
- Key metrics only (CPU, IOPS, connections, top queries)
- Average and maximum statistics

**Rationale:**

- ✅ Small, focused data (low token cost)
- ✅ Fast retrieval
- ✅ Agent can process easily
- ✅ Sufficient for analysis

### 5. Authentication: IAM Roles (No Passwords)

**Database credentials:**

- Stored in AWS Secrets Manager
- Retrieved via IAM role (no passwords in code)
- Automatic rotation supported

**AWS API access:**

- Same-account: ECS task role
- Cross-account: AssumeRole with ExternalId

**Rationale:**

- ✅ Secure (no credentials in code or environment)
- ✅ AWS best practice
- ✅ Automatic credential rotation
- ✅ Audit trail via CloudTrail

---

## Architecture

### Same-Account Deployment

The Modernizer runs in the same AWS account as the customer's data.

```
┌─────────────────────────────────────────────────────────────┐
│              Customer AWS Account (Modernizer + Data)         │
│                                                              │
│  ┌──────────────────────┐         ┌────────────────────┐   │
│  │ ECS Fargate Task     │────────►│ RDS Instance       │   │
│  │ (Modernizer)         │  Direct │ (Private VPC)      │   │
│  │                      │  Access │                    │   │
│  │ Uses ECS Task Role   │         └────────────────────┘   │
│  └──────────┬───────────┘                                   │
│             │                                                │
│             ▼                                                │
│  ┌──────────────────────┐         ┌────────────────────┐   │
│  │ Secrets Manager      │         │ CloudWatch         │   │
│  │ (DB credentials)     │         │ (Metrics)          │   │
│  └──────────────────────┘         └────────────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Cross-Account Deployment

The Modernizer runs in one customer account but accesses data in a separate customer account. We do not host any infrastructure — both accounts are owned by the customer.

```
Customer Data Account                   Customer Modernizer Account
┌─────────────────────────┐            ┌──────────────────────────┐
│                         │            │                          │
│  ┌──────────────────┐  │            │  ┌────────────────────┐ │
│  │ RDS Instance     │  │            │  │ ECS Fargate Task   │ │
│  │ (Private VPC)    │  │            │  │ (Strands Workflow) │ │
│  └────────▲─────────┘  │            │  └─────────┬──────────┘ │
│           │             │            │            │            │
│  ┌────────┴─────────┐  │            │  ┌─────────▼──────────┐ │
│  │ Secrets Manager  │  │            │  │ ECS Task Role      │ │
│  │ (DB credentials) │  │            │  │ (AssumeRole)       │ │
│  └──────────────────┘  │            │  └─────────┬──────────┘ │
│           ▲             │            │            │            │
│  ┌────────┴─────────┐  │            │            │            │
│  │ IAM Role         │◄─┼────────────┼────────────┘            │
│  │ (Cross-account)  │  │  Assumes   │                          │
│  │ + ExternalId     │  │            │                          │
│  └──────────────────┘  │            │                          │
│           ▲             │            │                          │
│  ┌────────┴─────────┐  │            │                          │
│  │ CloudWatch       │  │            │                          │
│  │ Performance      │  │            │                          │
│  │ Insights         │  │            │                          │
│  └──────────────────┘  │            │                          │
│           ▲             │            │                          │
│  ┌────────┴─────────┐  │            │                          │
│  │ VPC Peering or   │◄─┼────────────┼──────────────────────────┤
│  │ PrivateLink      │  │  Network   │                          │
│  └──────────────────┘  │            │                          │
└─────────────────────────┘            └──────────────────────────┘
```

---

## Input Contract

### Same-Account Configuration

```json
{
  "job_id": "job-123",
  "source_database": {
    "type": "mysql",
    "aws_config": {
      "region": "us-west-2",
      "rds_instance_id": "my-prod-db",
      "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789:secret:<SECRET_NAME>"  # pragma: allowlist secret
    },
    "network_config": {
      "private_endpoint": "my-prod-db.abc123.us-west-2.rds.amazonaws.com"
    }
  }
}
```

**Note:** No `cross_account_role_arn` → Uses ECS task role directly

### Cross-Account Configuration

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
      "secret_arn": "arn:aws:secretsmanager:us-west-2:987654321:secret:<SECRET_NAME>"  # pragma: allowlist secret
    },
    "network_config": {
      "vpc_peering_id": "pcx-12345678",
      "private_endpoint": "my-prod-db.abc123.us-west-2.rds.amazonaws.com"
    }
  }
}
```

**Note:** `cross_account_role_arn` present → Uses AssumeRole with ExternalId

---

## Implementation

### Component 1: Unified Credential Manager

```python
# src/tools/aws/credential_manager.py

import boto3
from botocore.exceptions import ClientError
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class AWSCredentialManager:
    """
    Manages AWS credentials for both same-account and cross-account access.

    Automatically detects deployment mode based on input configuration:
    - Same-account: Uses ECS task role (no AssumeRole)
    - Cross-account: Uses AssumeRole with temporary credentials
    """

    def __init__(
        self,
        region: str,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None
    ):
        """
        Initialize credential manager.

        Args:
            region: AWS region
            role_arn: Cross-account role ARN (None for same-account)
            external_id: External ID for AssumeRole (None for same-account)
        """
        self.region = region
        self.role_arn = role_arn
        self.external_id = external_id
        self._session = None
        self._mode = "cross-account" if role_arn else "same-account"

        logger.info(f"Credential manager initialized in {self._mode} mode")

    def get_session(self) -> boto3.Session:
        """
        Get boto3 session with appropriate credentials.

        Returns:
            boto3.Session configured for same-account or cross-account access
        """
        if self._session:
            return self._session

        if self._mode == "same-account":
            # Use ECS task role (no AssumeRole needed)
            self._session = boto3.Session(region_name=self.region)
            logger.info("Using same-account credentials (ECS task role)")

        else:
            # Use cross-account AssumeRole
            self._session = self._assume_role()
            logger.info(f"Using cross-account credentials (assumed {self.role_arn})")

        return self._session

    def _assume_role(self) -> boto3.Session:
        """Assume cross-account role and return session with temporary credentials"""
        sts = boto3.client('sts')

        try:
            assume_role_params = {
                'RoleArn': self.role_arn,
                'RoleSessionName': 'ModernizerCollector',
                'DurationSeconds': 3600  # 1 hour (sufficient for collection)
            }

            # Add ExternalId if provided (prevents confused deputy attack)
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

        except ClientError as e:
            raise ToolExecutionError(
                f"Failed to assume role {self.role_arn}: {e}. "
                f"Ensure the role exists and trusts the Modernizer account."
            )
```

---

### Component 2: RDS Tools Implementation

```python
# src/tools/aws/rds_tools.py

import boto3
from botocore.exceptions import ClientError
import json
from typing import Dict
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

def get_database_credentials(
    secret_arn: str,
    credential_manager: 'AWSCredentialManager'
) -> Dict:
    """
    Retrieve database credentials from AWS Secrets Manager.

    Args:
        secret_arn: ARN of secret containing database credentials
        credential_manager: Credential manager for AWS access

    Returns:
        Dict with host, port, username, password, database

    Raises:
        ToolExecutionError: If credentials cannot be retrieved (CRITICAL)

    Note: This is a CRITICAL tool - fails fast if credentials unavailable
    """
    try:
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

    except ClientError as e:
        raise ToolExecutionError(
            f"Failed to retrieve credentials from {secret_arn}: {e}. "
            "Ensure the IAM role has secretsmanager:GetSecretValue permission."
        )
    except (KeyError, json.JSONDecodeError) as e:
        raise ToolExecutionError(
            f"Invalid secret format in {secret_arn}: {e}. "
            "Expected JSON with host, username, password fields."
        )


def get_rds_instance_config(
    instance_id: str,
    credential_manager: 'AWSCredentialManager'
) -> Dict:
    """
    Get RDS instance configuration from AWS RDS API.

    Args:
        instance_id: RDS instance identifier
        credential_manager: Credential manager for AWS access

    Returns:
        Dict with instance configuration or error details

    Note: This is an OPTIONAL tool - returns partial data on failure
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
            'allocated_storage': instance['AllocatedStorage'],
            'iops': instance.get('Iops'),
            'multi_az': instance['MultiAZ'],
            'backup_retention': instance['BackupRetentionPeriod'],
            'parameter_group': instance['DBParameterGroups'][0]['DBParameterGroupName'],
            'vpc_id': instance['DBSubnetGroup']['VpcId']
        }

    except ClientError as e:
        logger.warning(f"Failed to get RDS config for {instance_id}: {e}")
        return {
            'available': False,
            'error': str(e),
            'message': 'RDS API access failed - continuing with database-only analysis'
        }


def get_cloudwatch_metrics(
    instance_id: str,
    credential_manager: 'AWSCredentialManager',
    days: int = 7
) -> Dict:
    """
    Get CloudWatch metrics for RDS instance (last 7 days, hourly aggregation).

    Args:
        instance_id: RDS instance identifier
        credential_manager: Credential manager for AWS access
        days: Number of days to retrieve (default: 7)

    Returns:
        Dict with pre-aggregated metrics or error details

    Note: This is an OPTIONAL tool - returns partial data on failure
    """
    try:
        session = credential_manager.get_session()
        cloudwatch = session.client('cloudwatch')

        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)

        # Key metrics for database analysis
        metrics = {
            'CPUUtilization': '%',
            'DatabaseConnections': 'Count',
            'ReadIOPS': 'Count/Second',
            'WriteIOPS': 'Count/Second',
            'FreeableMemory': 'Bytes',
            'FreeStorageSpace': 'Bytes'
        }

        results = {}

        for metric_name, unit in metrics.items():
            response = cloudwatch.get_metric_statistics(
                Namespace='AWS/RDS',
                MetricName=metric_name,
                Dimensions=[
                    {'Name': 'DBInstanceIdentifier', 'Value': instance_id}
                ],
                StartTime=start_time,
                EndTime=end_time,
                Period=3600,  # 1 hour aggregation
                Statistics=['Average', 'Maximum']
            )

            datapoints = response['Datapoints']

            if datapoints:
                results[metric_name] = {
                    'average': sum(d['Average'] for d in datapoints) / len(datapoints),
                    'maximum': max(d['Maximum'] for d in datapoints),
                    'unit': unit,
                    'datapoints': len(datapoints)
                }

        return {
            'available': True,
            'period_days': days,
            'metrics': results
        }

    except ClientError as e:
        logger.warning(f"Failed to get CloudWatch metrics for {instance_id}: {e}")
        return {
            'available': False,
            'error': str(e),
            'message': 'CloudWatch access failed - continuing without performance metrics'
        }


def get_performance_insights(
    instance_id: str,
    credential_manager: 'AWSCredentialManager',
    days: int = 7
) -> Dict:
    """
    Get Performance Insights data (top queries, wait events).

    Args:
        instance_id: RDS instance identifier
        credential_manager: Credential manager for AWS access
        days: Number of days to retrieve (default: 7)

    Returns:
        Dict with top queries and wait events or error details

    Note: This is an OPTIONAL tool - returns partial data on failure
    """
    try:
        session = credential_manager.get_session()
        pi = session.client('pi')
        rds = session.client('rds')

        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)

        # Get resource ARN for Performance Insights
        response = rds.describe_db_instances(DBInstanceIdentifier=instance_id)
        resource_id = response['DBInstances'][0]['DbiResourceId']

        # Get top SQL queries by database load
        response = pi.describe_dimension_keys(
            ServiceType='RDS',
            Identifier=f"db-{resource_id}",
            StartTime=start_time,
            EndTime=end_time,
            Metric='db.load.avg',
            GroupBy={'Group': 'db.sql'},
            MaxResults=10
        )

        top_queries = [
            {
                'sql_hash': key['Dimensions']['db.sql'],
                'load': key['Total']
            }
            for key in response.get('Keys', [])
        ]

        return {
            'available': True,
            'period_days': days,
            'top_queries': top_queries[:10],
            'message': 'Performance Insights data retrieved successfully'
        }

    except ClientError as e:
        logger.warning(f"Failed to get Performance Insights for {instance_id}: {e}")
        return {
            'available': False,
            'error': str(e),
            'message': 'Performance Insights not available or not enabled on this instance'
        }
```

---

### Component 3: Collector Agent Integration

```python
# src/agents/collector/mysql_collector.py

from strands import Agent, Tool
from tools.database.mysql_tools import connect_mysql, collect_schema
from tools.aws.rds_tools import (
    AWSCredentialManager,
    get_rds_instance_config,
    get_database_credentials,
    get_cloudwatch_metrics,
    get_performance_insights
)

class MySQLCollectorAgent:
    """
    MySQL Collector supporting both same-account and cross-account deployments.

    Transparently handles credential management based on input configuration.
    """

    def __init__(self, input_contract: dict):
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']

        # Setup credentials (auto-detects same-account vs cross-account)
        aws_config = input_contract['source_database']['aws_config']
        self.credential_manager = AWSCredentialManager(
            region=aws_config['region'],
            role_arn=aws_config.get('cross_account_role_arn'),  # None = same-account
            external_id=aws_config.get('external_id')  # None = same-account
        )

        # Create Strands Agent with tools
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
        """Create Strands tools with credential manager injected"""
        aws_config = self.input_contract['source_database']['aws_config']

        return [
            # Tool 1: Connect to MySQL (CRITICAL)
            Tool(
                name="connect_mysql",
                description="Connect to MySQL database using Secrets Manager credentials",
                function=lambda: self._connect_mysql()
            ),

            # Tool 2: Collect schema (CRITICAL)
            Tool(
                name="collect_schema",
                description="Collect database schema: tables, columns, indexes, constraints",
                function=lambda: collect_schema()
            ),

            # Tool 3: Get RDS config (OPTIONAL)
            Tool(
                name="get_rds_config",
                description="Get RDS instance configuration (storage, engine, version). Returns available=false if fails.",
                function=lambda: get_rds_instance_config(
                    aws_config['rds_instance_id'],
                    self.credential_manager
                )
            ),

            # Tool 4: Get CloudWatch metrics (OPTIONAL)
            Tool(
                name="get_cloudwatch_metrics",
                description="Get CloudWatch metrics (CPU, IOPS, connections) for last 7 days. Returns available=false if fails.",
                function=lambda: get_cloudwatch_metrics(
                    aws_config['rds_instance_id'],
                    self.credential_manager,
                    days=7
                )
            ),

            # Tool 5: Get Performance Insights (OPTIONAL)
            Tool(
                name="get_performance_insights",
                description="Get Performance Insights (top queries, wait events). Returns available=false if fails.",
                function=lambda: get_performance_insights(
                    aws_config['rds_instance_id'],
                    self.credential_manager,
                    days=7
                )
            )
        ]

    def _connect_mysql(self):
        """Connect to MySQL using Secrets Manager credentials"""
        aws_config = self.input_contract['source_database']['aws_config']

        # Get credentials from Secrets Manager
        creds = get_database_credentials(
            aws_config['secret_arn'],
            self.credential_manager
        )

        # Connect to database
        return connect_mysql(
            host=creds['host'],
            port=creds['port'],
            username=creds['username'],
            password=creds['password'],
            database=creds['database']
        )

    def _create_system_prompt(self) -> str:
        return f"""You are a MySQL Database Collector Agent.

Your mission: Collect comprehensive data from MySQL RDS instance.

Your Tools:
1. connect_mysql - Connect using Secrets Manager credentials (REQUIRED)
2. collect_schema - Gather table structures, columns, indexes (REQUIRED)
3. get_rds_config - Get RDS instance configuration (OPTIONAL)
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
This is OK - continue with database-only analysis. Include the error message
in your output so users know what data is missing.

Output Format: CollectorOutputContract (validated automatically)
"""

    def collect(self) -> dict:
        """
        Execute collection with automatic validation.

        Returns:
            Dict matching CollectorOutputContract (guaranteed valid)

        Raises:
            ValidationError: If output fails validation after 3 retries
        """
        result = self.agent(self._format_input())
        return result  # Guaranteed valid by validation hook

    def _format_input(self) -> str:
        """Format input for agent"""
        return f"""Collect data from MySQL RDS instance.

Job ID: {self.job_id}
Instance: {self.input_contract['source_database']['aws_config']['rds_instance_id']}
Region: {self.input_contract['source_database']['aws_config']['region']}

Execute all tools and return comprehensive analysis.
"""
```

---

## IAM Permissions

### Same-Account: ECS Task Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RDSReadAccess",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBInstances",
        "rds:DescribeDBParameters",
        "rds:DescribeDBParameterGroups",
        "rds:DescribeDBSubnetGroups"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:rds-*"
    },
    {
      "Sid": "CloudWatchReadAccess",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PerformanceInsightsAccess",
      "Effect": "Allow",
      "Action": [
        "pi:GetResourceMetrics",
        "pi:DescribeDimensionKeys"
      ],
      "Resource": "arn:aws:pi:*:*:metrics/rds/*"
    }
  ]
}
```

---

### Cross-Account: ECS Task Role + Customer Role

**ECS Task Role (Modernizer Account):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeCustomerRole",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::*:role/ModernizerAccessRole"
    }
  ]
}
```

**Customer Role (Customer Account) - Trust Policy:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::MODERNIZER_ACCOUNT:role/ModernizerTaskRole"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "unique-customer-id-12345"
        }
      }
    }
  ]
}
```

**Customer Role (Customer Account) - Permissions:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RDSReadAccess",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBInstances",
        "rds:DescribeDBParameters",
        "rds:DescribeDBParameterGroups"
      ],
      "Resource": "arn:aws:rds:*:CUSTOMER_ACCOUNT:db:*"
    },
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:*:CUSTOMER_ACCOUNT:secret:rds-*"
    },
    {
      "Sid": "CloudWatchReadAccess",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PerformanceInsightsAccess",
      "Effect": "Allow",
      "Action": [
        "pi:GetResourceMetrics",
        "pi:DescribeDimensionKeys"
      ],
      "Resource": "arn:aws:pi:*:CUSTOMER_ACCOUNT:metrics/rds/*"
    }
  ]
}
```

---

## Customer Onboarding

### Same-Account Setup (Simple)

**Step 1:** Customer provides RDS details

```json
{
  "rds_instance_id": "my-prod-db",
  "region": "us-west-2",
  "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789:secret:<SECRET_NAME>"
}
```

**Step 2:** Modernizer validates access (test connection)

**Step 3:** Job executes with ECS task role

---

### Cross-Account Setup (Requires IAM Role)

**Step 1:** Customer creates IAM role using CloudFormation template

```yaml
# customer-role-template.yaml

AWSTemplateFormatVersion: '2010-09-09'
Description: 'IAM role for Database Modernizer Assessment cross-account access'

Parameters:
  ModernizerAccountId:
    Type: String
    Description: 'AWS Account ID of Database Modernizer Assessment'
  ExternalId:
    Type: String
    Description: 'Unique external ID for this customer'

Resources:
  ModernizerAccessRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: ModernizerAccessRole
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              AWS: !Sub 'arn:aws:iam::${ModernizerAccountId}:role/ModernizerTaskRole'
            Action: 'sts:AssumeRole'
            Condition:
              StringEquals:
                'sts:ExternalId': !Ref ExternalId
      ManagedPolicyArns:
        - !Ref ModernizerAccessPolicy

  ModernizerAccessPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Sid: RDSReadAccess
            Effect: Allow
            Action:
              - 'rds:DescribeDBInstances'
              - 'rds:DescribeDBParameters'
              - 'rds:DescribeDBParameterGroups'
            Resource: '*'
          - Sid: SecretsManagerAccess
            Effect: Allow
            Action:
              - 'secretsmanager:GetSecretValue'
            Resource: 'arn:aws:secretsmanager:*:*:secret:rds-*'
          - Sid: CloudWatchReadAccess
            Effect: Allow
            Action:
              - 'cloudwatch:GetMetricStatistics'
              - 'cloudwatch:ListMetrics'
            Resource: '*'
          - Sid: PerformanceInsightsAccess
            Effect: Allow
            Action:
              - 'pi:GetResourceMetrics'
              - 'pi:DescribeDimensionKeys'
            Resource: 'arn:aws:pi:*:*:metrics/rds/*'

Outputs:
  RoleArn:
    Description: 'ARN of the created IAM role'
    Value: !GetAtt ModernizerAccessRole.Arn
```

**Step 2:** Customer provides configuration

```json
{
  "cross_account_role_arn": "arn:aws:iam::987654321:role/ModernizerAccessRole",
  "external_id": "unique-customer-id-12345",
  "rds_instance_id": "my-prod-db",
  "region": "us-west-2",
  "secret_arn": "arn:aws:secretsmanager:us-west-2:987654321:secret:<SECRET_NAME>"
}
```

**Step 3:** Modernizer validates access (test AssumeRole + connection)

**Step 4:** Job executes with cross-account access

---

## Tool Output Examples

### Successful RDS Config Retrieval

```json
{
  "available": true,
  "engine": "mysql",
  "engine_version": "8.0.35",
  "instance_class": "db.r5.xlarge",
  "storage_type": "gp3",
  "allocated_storage": 500,
  "iops": 3000,
  "multi_az": true,
  "backup_retention": 7,
  "parameter_group": "default.mysql8.0",
  "vpc_id": "vpc-abc123"
}
```

### Failed RDS Config Retrieval (Graceful)

```json
{
  "available": false,
  "error": "AccessDenied: User is not authorized to perform: rds:DescribeDBInstances",
  "message": "RDS API access failed - continuing with database-only analysis"
}
```

### CloudWatch Metrics

```json
{
  "available": true,
  "period_days": 7,
  "metrics": {
    "CPUUtilization": {
      "average": 45.2,
      "maximum": 89.1,
      "unit": "%",
      "datapoints": 168
    },
    "DatabaseConnections": {
      "average": 42,
      "maximum": 87,
      "unit": "Count",
      "datapoints": 168
    },
    "ReadIOPS": {
      "average": 1250,
      "maximum": 3500,
      "unit": "Count/Second",
      "datapoints": 168
    }
  }
}
```

---

## Consequences

### Positive

✅ **Unified credential management**: Single code path for both deployment modes
✅ **Secure**: No passwords in code, IAM roles only, temporary credentials
✅ **Flexible**: Supports customer multi-account architectures
✅ **Graceful degradation**: Continues with partial data if AWS APIs fail
✅ **Granular tools**: Agent chooses what to collect
✅ **Pre-aggregated data**: Low token cost, fast retrieval
✅ **AWS best practices**: IAM roles, Secrets Manager, ExternalId

### Negative

⚠️ **Customer setup**: Cross-account requires IAM role creation
⚠️ **Network complexity**: VPC peering or PrivateLink required for private RDS
⚠️ **Permission management**: Customer must grant appropriate IAM permissions

### Neutral

🔶 **7-day metrics**: Balance between insight and cost
🔶 **Optional AWS data**: Can work with database-only access

---

## Testing Strategy

### Unit Tests

```python
def test_credential_manager_same_account():
    """Test same-account credential manager"""
    manager = AWSCredentialManager(region='us-west-2')
    session = manager.get_session()
    assert session.region_name == 'us-west-2'

def test_credential_manager_cross_account():
    """Test cross-account credential manager"""
    manager = AWSCredentialManager(
        region='us-west-2',
        role_arn='arn:aws:iam::123456789:role/TestRole',
        external_id='test-123'
    )
    # Mock STS AssumeRole
    session = manager.get_session()
    assert session is not None

def test_rds_config_graceful_failure():
    """Test RDS config returns available=false on error"""
    result = get_rds_instance_config('invalid-instance', mock_manager)
    assert result['available'] == False
    assert 'error' in result
```

### Integration Tests

```python
def test_collector_same_account():
    """Test collector with same-account configuration"""
    input_contract = create_same_account_contract()
    agent = MySQLCollectorAgent(input_contract)
    result = agent.collect()
    assert result['collector_version'] == '2.0.0-strands'

def test_collector_cross_account():
    """Test collector with cross-account configuration"""
    input_contract = create_cross_account_contract()
    agent = MySQLCollectorAgent(input_contract)
    result = agent.collect()
    assert result['collector_version'] == '2.0.0-strands'
```

---

## Monitoring & Observability

### CloudWatch Metrics

1. **AssumeRole Success Rate**: % of successful AssumeRole calls
2. **Secrets Manager Access**: Count of GetSecretValue calls
3. **RDS API Calls**: Count of DescribeDBInstances calls
4. **Tool Availability**: % of tools returning available=true

### CloudWatch Alarms

1. **AssumeRole Failures**: Alert if >10% failure rate
2. **Secrets Manager Errors**: Alert if GetSecretValue fails
3. **RDS API Throttling**: Alert if API calls throttled

---

## Related Documents

- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)
- [ADR-002: Structured Output and Validation](ADR-002-structured-output-and-validation.md)
- [ADR-003: Progress Reporting Architecture](ADR-003-progress-reporting-architecture.md)
- [High-Level Design](../high-level-design.md)
- [Architecture Review](../ARCHITECTURE_REVIEW.md) - Point #5

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-01 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**
