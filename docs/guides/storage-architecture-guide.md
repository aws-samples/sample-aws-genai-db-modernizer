# Storage Architecture Implementation Guide

**Document Type:** Implementation Guide
**Last Updated:** February 18, 2026
**Status:** Draft

---

## Overview

This guide provides implementation patterns for the storage abstraction layer that supports multiple storage backends (local filesystem, S3). All agent artifacts follow the S3 path convention from ADR-016:

```
<database-name>/<job_id>/<agent-name>/artifact.json
```

There is no intra-step checkpointing. If a step fails, it restarts from scratch (ADR-016).

---

## Storage Abstraction Layer

### Purpose

Support multiple storage backends without changing agent code:

- Local filesystem (Docker Compose)
- S3 + DynamoDB (AWS deployment)

### Architecture Pattern

```python
from abc import ABC, abstractmethod
from typing import Any, Dict
import json
import os
from datetime import datetime

class StorageBackend(ABC):
    """Abstract storage backend"""

    @abstractmethod
    def save_result(self, database_name: str, job_id: str, agent_name: str, filename: str, data: dict):
        """Save agent result"""
        pass

    @abstractmethod
    def load_result(self, database_name: str, job_id: str, agent_name: str, filename: str) -> dict:
        """Load agent result"""
        pass

    @abstractmethod
    def list_jobs(self, database_name: str) -> list:
        """List all jobs for a database"""
        pass

    @abstractmethod
    def update_job_status(self, job_id: str, status: str, **kwargs):
        """Update job status"""
        pass
```

---

## Local Filesystem Storage

### Implementation

```python
class LocalFilesystemStorage(StorageBackend):
    """Local filesystem storage backend (Docker Compose dev)"""

    def __init__(self, base_dir: str = "/data"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def save_result(self, database_name: str, job_id: str, agent_name: str, filename: str, data: dict):
        agent_dir = os.path.join(self.base_dir, database_name, job_id, agent_name)
        os.makedirs(agent_dir, exist_ok=True)

        output_file = os.path.join(agent_dir, filename)
        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)

    def load_result(self, database_name: str, job_id: str, agent_name: str, filename: str) -> dict:
        output_file = os.path.join(self.base_dir, database_name, job_id, agent_name, filename)
        with open(output_file, "r") as f:
            return json.load(f)

    def list_jobs(self, database_name: str) -> list:
        db_dir = os.path.join(self.base_dir, database_name)
        if not os.path.exists(db_dir):
            return []
        return [
            d for d in os.listdir(db_dir)
            if os.path.isdir(os.path.join(db_dir, d))
        ]

    def update_job_status(self, job_id: str, status: str, **kwargs):
        # For local dev, write status to a known location
        # In AWS, this goes to DynamoDB (see S3Storage)
        status_dir = os.path.join(self.base_dir, "_status")
        os.makedirs(status_dir, exist_ok=True)

        status_file = os.path.join(status_dir, f"{job_id}.json")

        if os.path.exists(status_file):
            with open(status_file, "r") as f:
                status_data = json.load(f)
        else:
            status_data = {"job_id": job_id}

        status_data["status"] = status
        status_data["updated_at"] = datetime.now().isoformat()
        status_data.update(kwargs)

        with open(status_file, "w") as f:
            json.dump(status_data, f, indent=2)
```

### File Structure

Mirrors the S3 path convention locally:

```
/data/
├── <database-name>/
│   └── <job_id>/
│       ├── collector/
│       │   └── output.json
│       ├── referee-triage/
│       │   └── triage.json
│       ├── analysis-dynamodb/
│       │   └── analysis.json
│       ├── analysis-documentdb/
│       │   └── analysis.json
│       ├── analysis-elasticache/
│       │   └── analysis.json
│       ├── referee-synthesis/
│       │   └── report.json
│       └── schema-design-dynamodb/
│           └── schema.json
├── _status/
│   └── <job_id>.json
├── config/
│   └── application.yaml
└── logs/
    └── agents/
        └── <agent-name>/
            └── <job_id>.log
```

---

## S3 Storage

### Implementation

```python
class S3Storage(StorageBackend):
    """S3 storage backend for AWS deployments"""

    def __init__(self, bucket: str):
        import boto3
        self.s3 = boto3.client("s3")
        self.bucket = bucket

    def save_result(self, database_name: str, job_id: str, agent_name: str, filename: str, data: dict):
        key = f"{database_name}/{job_id}/{agent_name}/{filename}"
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(data, indent=2),
            ContentType="application/json",
        )

    def load_result(self, database_name: str, job_id: str, agent_name: str, filename: str) -> dict:
        key = f"{database_name}/{job_id}/{agent_name}/{filename}"
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(response["Body"].read())

    def list_jobs(self, database_name: str) -> list:
        prefix = f"{database_name}/"
        response = self.s3.list_objects_v2(
            Bucket=self.bucket,
            Prefix=prefix,
            Delimiter="/",
        )

        jobs = []
        for common_prefix in response.get("CommonPrefixes", []):
            job_id = common_prefix["Prefix"].rstrip("/").split("/")[-1]
            jobs.append(job_id)

        return jobs

    def update_job_status(self, job_id: str, status: str, **kwargs):
        import boto3
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table("modernizer-jobs")

        update_expr = "SET #status = :status, updated_at = :updated_at"
        expr_names = {"#status": "status"}
        expr_values = {
            ":status": status,
            ":updated_at": datetime.now().isoformat(),
        }

        for k, v in kwargs.items():
            update_expr += f", {k} = :{k}"
            expr_values[f":{k}"] = v

        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
```

### S3 Structure

```
s3://<bucket>/
├── <database-name>/
│   └── <job_id (KSUID)>/
│       ├── collector/
│       │   └── output.json
│       ├── referee-triage/
│       │   └── triage.json
│       ├── analysis-dynamodb/
│       │   └── analysis.json
│       ├── analysis-documentdb/
│       │   └── analysis.json
│       ├── referee-synthesis/
│       │   └── report.json
│       └── schema-design-dynamodb/
│           └── schema.json
```

Examples:

```
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/collector/output.json
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/referee-triage/triage.json
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/analysis-dynamodb/analysis.json
s3://modernizer-dev-data/myapp-postgres/2GxZLsnP00Y2BwR0000000001/referee-synthesis/report.json
```

KSUID provides time-ordered, globally unique job IDs without coordination. The `<database-name>` prefix enables easy browsing and lifecycle policies per source database.

---

## Storage Factory

### Implementation

```python
class StorageFactory:
    """Factory for creating storage backends"""

    @staticmethod
    def create(backend_type: str, **kwargs) -> StorageBackend:
        if backend_type == "local":
            return LocalFilesystemStorage(**kwargs)
        elif backend_type == "s3":
            return S3Storage(**kwargs)
        else:
            raise ValueError(f"Unknown storage backend: {backend_type}")
```

### Usage

```python
import os

storage = StorageFactory.create(
    os.getenv("STORAGE_BACKEND", "local"),
    base_dir="/data",       # for local
    # bucket="my-bucket",   # for s3
)

# Save result (follows path convention)
storage.save_result("myapp-postgres", job_id, "collector", "output.json", collector_output)

# Load result
collector_output = storage.load_result("myapp-postgres", job_id, "collector", "output.json")

# Update status
storage.update_job_status(job_id, "COMPLETED")
```

---

## Metadata Storage

### DynamoDB (AWS Deployment)

```
Table: modernizer-jobs
Primary Key: job_id (String)
Attributes:
  - status (String)
  - database_name (String)
  - source_database_type (String)
  - execution_arn (String)          # Step Functions execution ARN
  - created_at (String - ISO 8601)
  - updated_at (String - ISO 8601)
  - completed_at (String - ISO 8601)
  - error_message (String)
  - metadata (Map)

GSI: status-created_at-index
  - Partition Key: status
  - Sort Key: created_at
```

### Restart Strategy

Per ADR-016, there is no intra-step checkpointing. If a step fails, it restarts from scratch. Each agent declares its mini-steps (restart points). Restarting a previous mini-step invalidates all subsequent mini-steps and downstream agents.

This keeps agent code simple — no partial state recovery logic. The cost of re-running a step is acceptable given the <6 hour total job target.

---

## Related Documentation

- [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- [High-Level Design](../architecture/high-level-design.md)
- [Storage Architecture Diagram](../architecture/architecture-diagrams/07-storage-architecture.md)

---

**Last Updated:** February 18, 2026
**Maintained By:** Database Modernizer Engineering Team
