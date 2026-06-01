# API Development Guide

**Document Type:** Implementation Guide
**Last Updated:** February 18, 2026
**Status:** Draft

---

## Overview

This guide provides implementation patterns for the FastAPI-based REST API and WebSocket endpoints. The API starts jobs via `states:StartExecution` (Step Functions). Real-time progress is delivered via EventBridge → Lambda → API Gateway WebSocket.

---

## API Architecture

### Technology Stack

- **Framework:** FastAPI
- **Server:** Uvicorn
- **Load Balancer:** Application Load Balancer (ALB)
- **Deployment:** ECS Fargate containers
- **Orchestration:** Step Functions (API calls `states:StartExecution`)
- **WebSocket:** API Gateway WebSocket (EventBridge → Lambda → WebSocket push)

---

## FastAPI Application

### Basic Structure

```python
from fastapi import FastAPI, HTTPException, WebSocket
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import boto3
import json
import os

app = FastAPI(title="Database Modernizer API")

# No CORS middleware needed — UI and API share the same ALB with
# path-based routing (/api/* → API, /* → UI). Same origin, same
# Cognito session cookie.
```

---

## Request/Response Models

### Analysis Request

```python
from ksuid import ksuid

class AnalysisRequest(BaseModel):
    source_database_type: str  # mysql, postgresql, etc.
    database_name: str         # Used in S3 path convention
    connection: dict           # {host, port, database, credentials}
    options: Optional[dict] = {
        "anonymize_pii": True,
        "include_sample_data": True,
        "query_log_days": 7,
    }
    full_analysis: Optional[bool] = False  # Bypass triage, run all 7 agents

class AnalysisResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    estimated_completion_time: str
    execution_arn: str  # Step Functions execution ARN
```

---

## REST API Endpoints

### Start Analysis

Jobs are started by calling `states:StartExecution` on the Step Functions state machine:

```python
sfn_client = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

@app.post("/api/v1/analyses", response_model=AnalysisResponse, status_code=202)
async def create_analysis(request: AnalysisRequest):
    """Start new database analysis via Step Functions."""
    job_id = str(ksuid())

    # Start Step Functions execution
    sfn_input = {
        "job_id": job_id,
        "database_name": request.database_name,
        "source_database_type": request.source_database_type,
        "connection": request.connection,
        "options": request.options,
        "full_analysis": request.full_analysis,
    }

    response = sfn_client.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=job_id,
        input=json.dumps(sfn_input),
    )

    return AnalysisResponse(
        job_id=job_id,
        status="PENDING",
        created_at=datetime.now().isoformat(),
        estimated_completion_time=(datetime.now() + timedelta(hours=6)).isoformat(),
        execution_arn=response["executionArn"],
    )
```

### Get Analysis Status

```python
@app.get("/api/v1/analyses/{job_id}")
async def get_analysis_status(job_id: str):
    """Get analysis status from DynamoDB."""
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ.get("JOBS_TABLE", "modernizer-jobs"))

    response = table.get_item(Key={"job_id": job_id})
    item = response.get("Item")

    if not item:
        raise HTTPException(status_code=404, detail="Job not found")

    return item
```

### Get Analysis Results

```python
@app.get("/api/v1/analyses/{job_id}/results")
async def get_analysis_results(job_id: str):
    """Get analysis results from S3."""
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ.get("JOBS_TABLE", "modernizer-jobs"))

    response = table.get_item(Key={"job_id": job_id})
    item = response.get("Item")

    if not item:
        raise HTTPException(status_code=404, detail="Job not found")

    if item.get("status") != "COMPLETED":
        raise HTTPException(status_code=400, detail="Analysis not completed yet")

    # Load synthesis report from S3
    s3 = boto3.client("s3")
    bucket = os.environ["S3_BUCKET"]
    database_name = item["database_name"]
    report_key = f"{database_name}/{job_id}/referee-synthesis/report.json"

    obj = s3.get_object(Bucket=bucket, Key=report_key)
    report = json.loads(obj["Body"].read())

    return {"job_id": job_id, "status": "COMPLETED", "results": report}
```

---

## WebSocket Support

### EventBridge → WebSocket Push

Real-time progress is delivered via EventBridge. Each agent publishes progress events at mini-step boundaries. An EventBridge rule routes these to a Lambda function that pushes updates through API Gateway WebSocket connections.

```
Agent mini-step → EventBridge event → Lambda → API Gateway WebSocket → UI
```

The FastAPI server does not poll for status. Instead, the WebSocket connection is managed by API Gateway, and a Lambda function handles the push:

```python
# Lambda function (triggered by EventBridge rule)
import boto3
import json
import os

def handler(event, context):
    """Push progress event to WebSocket connections."""
    apigw = boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=os.environ["WEBSOCKET_ENDPOINT"],
    )

    job_id = event["detail"]["job_id"]
    connections = get_connections_for_job(job_id)  # From DynamoDB connections table

    for connection_id in connections:
        try:
            apigw.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(event["detail"]).encode(),
            )
        except apigw.exceptions.GoneException:
            remove_connection(connection_id)
```

### WebSocket Connection Registration

The FastAPI server provides an endpoint for clients to register their WebSocket connection ID with a job:

```python
@app.post("/api/v1/analyses/{job_id}/subscribe")
async def subscribe_to_job(job_id: str, connection_id: str):
    """Register a WebSocket connection for job progress updates."""
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ.get("CONNECTIONS_TABLE", "modernizer-ws-connections"))

    table.put_item(Item={
        "job_id": job_id,
        "connection_id": connection_id,
    })

    return {"status": "subscribed"}
```

---

## Health Check

```python
@app.get("/health")
async def health_check():
    return {"status": "healthy"}
```

---

## Error Handling

### Custom Exception Handler

```python
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": str(exc),
            "path": request.url.path,
        },
    )
```

---

## API Specifications

### Base URLs

- **Docker Compose:** `http://localhost:8000/api/v1`
- **AWS CloudFormation:** `https://{alb-url}/api/v1`

### Endpoints

#### POST /analyses

Start new database analysis.

**Request:**

```json
{
  "source_database_type": "mysql",
  "database_name": "ecommerce-mysql",
  "connection": {
    "host": "mydb.abc123.us-east-1.rds.amazonaws.com",
    "port": 3306,
    "database": "ecommerce",
    "username": "readonly_user",
    "password": "<PASSWORD>"
  },
  "options": {
    "anonymize_pii": true,
    "include_sample_data": true,
    "query_log_days": 7
  },
  "full_analysis": false
}
```

**Response (202 Accepted):**

```json
{
  "job_id": "2GxZLsnP00Y2BwR0000000001",
  "status": "PENDING",
  "created_at": "2026-03-15T10:30:00Z",
  "estimated_completion_time": "2026-03-15T16:30:00Z",
  "execution_arn": "arn:aws:states:us-east-1:123456789:execution:modernizer-workflow:2GxZLsnP00Y2BwR0000000001"
}
```

#### GET /analyses/{job_id}

Get analysis status.

**Response (200 OK):**

```json
{
  "job_id": "2GxZLsnP00Y2BwR0000000001",
  "status": "ANALYSIS_RUNNING",
  "progress": {
    "current_stage": "analysis_agents",
    "percent_complete": 45,
    "stages_completed": ["collector", "referee_triage"],
    "stages_remaining": ["analysis_agents", "referee_synthesis"]
  },
  "created_at": "2026-03-15T10:30:00Z",
  "updated_at": "2026-03-15T12:45:00Z"
}
```

#### GET /analyses/{job_id}/results

Get analysis results (only available when status is COMPLETED).

**Response (200 OK):**

```json
{
  "job_id": "2GxZLsnP00Y2BwR0000000001",
  "status": "COMPLETED",
  "results": {
    "recommended_architecture": {
      "databases": [
        {
          "service": "DynamoDB",
          "tables": ["users", "sessions"],
          "confidence": 0.85,
          "rationale": "High key-value query patterns"
        }
      ]
    },
    "tco_analysis": {
      "current_monthly_cost": 5000,
      "projected_monthly_cost": 2800,
      "savings_percent": 44
    }
  }
}
```

---

## Development Setup

### Running Locally

```bash
# Install dependencies
uv sync

# Start API server
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Note: Step Functions is not available in local dev. For local testing, mock the `sfn_client.start_execution` call or use the Docker Compose setup which runs agents directly.

### Testing

```bash
uv run pytest tests/ -v --cov=app --cov-report=html
```

---

## Related Documentation

- [ADR-016: Compute and Orchestration Strategy](../architecture/decisions/ADR-016-compute-and-orchestration-strategy.md)
- [High-Level Design](../architecture/high-level-design.md)
- [Deployment Guide](deployment-guide.md)
- [Storage Architecture Guide](storage-architecture-guide.md)

---

**Last Updated:** February 18, 2026
**Maintained By:** Database Modernizer Engineering Team
