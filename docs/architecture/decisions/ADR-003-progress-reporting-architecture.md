# ADR-003: Progress Reporting Architecture

**Status:** Accepted
**Date:** 2026-02-01
**Updated:** 2026-02-06
**Deciders:** Architecture Team
**Related Issues:** Architecture Review Point #3
**Related ADRs:** ADR-001 (State Management), ADR-002 (Structured Output)

---

## Context

Database Modernizer jobs run 1-6 hours with 11 distinct stages (collector + 7 analysis agents + referee + 2 schema design). Users need real-time progress updates to monitor job execution. We need to design a progress reporting pipeline that:

1. **Real-time updates**: Users see progress as jobs execute
2. **Stage-level granularity**: 11 progress updates per job (aligned with checkpoints from ADR-001)
3. **Minimal operational overhead**: Prefer managed AWS services
4. **Cost-effective**: Optimize for low-volume writes (~11 events per job)
5. **Integrates with Strands**: Leverage Strands SDK built-in capabilities
6. **WebSocket support**: Push updates to connected clients

### Alternatives Considered

We evaluated three approaches for orchestrating the multi-agent workflow and reporting progress:

**Option A: Celery + Redis (Not Selected)**

Traditional task queue approach with Celery workers and Redis as the message broker.

- ❌ Requires Redis cluster management and maintenance
- ❌ Celery worker scaling complexity
- ❌ Higher operational cost (~$120-200/month)
- ❌ More operational overhead for monitoring and scaling
- ❌ Additional infrastructure to secure and maintain

**Option B: Bedrock AgentCore (Not Selected)**

AWS managed agent orchestration service with built-in session management.

- ❌ 15-minute inactivity timeout (risky for long-running stages)
- ❌ Memory not designed for job state/checkpoints
- ❌ More expensive than Fargate (~$172-272/month)
- ❌ Still requires S3 + DynamoDB + EventBridge + WebSocket
- ❌ No spot instance support
- ❌ Less control over execution environment

**Option C: ECS Fargate + EventBridge (SELECTED)**

Event-driven orchestration using AWS managed services with ECS Fargate for compute.

- ✅ Fully managed AWS services (no infrastructure to maintain)
- ✅ No session timeout risk (tasks can run for hours)
- ✅ Most cost-effective (~$72-122/month)
- ✅ Spot instance ready (future optimization potential)
- ✅ No vendor lock-in (standard AWS services)
- ✅ Integrates cleanly with Strands SDK hooks
- ✅ Native event-driven architecture with automatic retry and DLQ
- ✅ Scales automatically with workload

---

## Decision

**We selected Option C: ECS Fargate + EventBridge + API Gateway WebSocket** for progress reporting and workflow orchestration.

### Rationale

After evaluating Celery/Redis (Option A) and Bedrock AgentCore (Option B), we chose EventBridge-based orchestration because:

1. **Operational Simplicity**: Fully managed services eliminate infrastructure maintenance overhead
2. **Cost Efficiency**: 40-60% lower monthly cost compared to alternatives
3. **Reliability**: No timeout risks, built-in retry logic, and Dead Letter Queue support
4. **Scalability**: Auto-scales with workload without manual intervention
5. **Integration**: Works seamlessly with Strands SDK hooks and existing AWS services

### Architecture Overview

The chosen architecture uses event-driven orchestration:

1. **Strands Hooks** publish progress events to EventBridge
2. **EventBridge** routes events to Lambda
3. **Lambda** pushes updates to API Gateway WebSocket
4. **DynamoDB** stores job metadata and WebSocket connection IDs
5. **S3** stores checkpoints (from ADR-001)

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      API Layer                               │
│  ┌──────────────┐         ┌──────────────────┐             │
│  │ API Gateway  │◄────────│ WebSocket API    │             │
│  │ (REST)       │         │ (for progress)   │             │
│  └──────┬───────┘         └────────▲─────────┘             │
│         │                           │                        │
└─────────┼───────────────────────────┼────────────────────────┘
          │                           │
          ▼                           │
┌─────────────────────────────────────┼────────────────────────┐
│           Execution Layer           │                         │
│  ┌──────────────────────┐           │                        │
│  │ ECS Fargate Task     │           │                        │
│  │ (Strands Workflow)   │───────────┘                        │
│  │                      │  Progress Events                   │
│  │  ┌────────────────┐ │                                     │
│  │  │ Strands Hooks  │─┼──► EventBridge                     │
│  │  └────────────────┘ │                                     │
│  │                      │                                     │
│  │  Checkpoints ────────┼──► S3                              │
│  └──────────────────────┘                                     │
└───────────────────────────────────────────────────────────────┘
          │
          ▼
┌───────────────────────────────────────────────────────────────┐
│                  Storage & Observability                       │
│  ┌─────────┐  ┌──────────────┐  ┌────────────────┐          │
│  │   S3    │  │  DynamoDB    │  │  CloudWatch    │          │
│  │(checkpts)│  │ (job metadata)│  │ (logs/metrics) │          │
│  └─────────┘  └──────────────┘  └────────────────┘          │
└───────────────────────────────────────────────────────────────┘
```

---

## Progress Event Format

### Stage-Level Granularity

```json
{
  "job_id": "job-123",
  "stage": "collector",
  "status": "started",
  "progress": "1/11",
  "timestamp": "2026-02-01T21:30:00Z",
  "metadata": {
    "stage_index": 1,
    "total_stages": 11
  }
}
```

**Status Values:**

- `started`: Stage execution began
- `completed`: Stage finished successfully
- `failed`: Stage encountered error

**11 Stages (aligned with ADR-001 checkpoints):**

1. collector
2. dynamodb_analysis
3. documentdb_analysis
4. elasticache_analysis
5. opensearch_analysis
6. neptune_analysis
7. keyspaces_analysis
8. aurora_analysis
9. referee
10. schema_design_primary
11. schema_design_secondary

---

## Implementation

### Component 1: Strands Workflow with Progress Hooks

```python
# src/workflows/modernizer_workflow.py

from strands import Agent
from strands.hooks import HookRegistry, HookEvent
import boto3
import json
from datetime import datetime

class ModernizerWorkflow:
    """
    Strands workflow with progress reporting via EventBridge.
    """

    def __init__(self, job_id: str, input_contract: dict):
        self.job_id = job_id
        self.input_contract = input_contract
        self.events_client = boto3.client('events')
        self.stage_index = 0
        self.total_stages = 11

    def publish_progress(self, stage: str, status: str):
        """Publish progress event to EventBridge"""
        self.events_client.put_events(
            Entries=[{
                'Source': 'modernizer.workflow',
                'DetailType': 'StageProgress',
                'Detail': json.dumps({
                    'job_id': self.job_id,
                    'stage': stage,
                    'status': status,
                    'progress': f"{self.stage_index}/{self.total_stages}",
                    'timestamp': datetime.utcnow().isoformat(),
                    'metadata': {
                        'stage_index': self.stage_index,
                        'total_stages': self.total_stages
                    }
                })
            }]
        )

    def create_agent_with_hooks(self, agent_config: dict, stage_name: str):
        """Create Strands agent with progress hooks"""
        agent = Agent(**agent_config)

        @agent.hooks.register(HookEvent.AGENT_START)
        def on_start(context):
            self.stage_index += 1
            self.publish_progress(stage_name, "started")

        @agent.hooks.register(HookEvent.AGENT_END)
        def on_complete(context):
            self.publish_progress(stage_name, "completed")

        @agent.hooks.register(HookEvent.AGENT_ERROR)
        def on_error(context):
            self.publish_progress(stage_name, "failed")

        return agent

    def run(self):
        """Execute workflow with progress tracking"""
        # Stage 1: Collector
        collector_agent = self.create_agent_with_hooks(
            agent_config={
                'system_prompt': "You are a MySQL collector...",
                'tools': [connect_mysql, collect_schema],
                'response_format': load_json_schema("collector-output.json")
            },
            stage_name="collector"
        )
        collector_output = collector_agent(self.input_contract)

        # Stages 2-8: Analysis agents
        analysis_stages = [
            ("dynamodb_analysis", create_dynamodb_agent),
            ("documentdb_analysis", create_documentdb_agent),
            ("elasticache_analysis", create_elasticache_agent),
            ("opensearch_analysis", create_opensearch_agent),
            ("neptune_analysis", create_neptune_agent),
            ("keyspaces_analysis", create_keyspaces_agent),
            ("aurora_analysis", create_aurora_agent),
        ]

        analysis_results = []
        for stage_name, agent_factory in analysis_stages:
            agent = self.create_agent_with_hooks(
                agent_config=agent_factory(collector_output),
                stage_name=stage_name
            )
            result = agent(collector_output)
            analysis_results.append(result)

        # Stage 9: Referee
        referee_agent = self.create_agent_with_hooks(
            agent_config={
                'system_prompt': "You aggregate analysis...",
                'tools': [aggregate_recommendations]
            },
            stage_name="referee"
        )
        referee_output = referee_agent(analysis_results)

        # Stages 10-11: Schema design
        schema_primary_agent = self.create_agent_with_hooks(
            agent_config=create_schema_design_agent("primary"),
            stage_name="schema_design_primary"
        )
        primary_schema = schema_primary_agent(referee_output)

        schema_secondary_agent = self.create_agent_with_hooks(
            agent_config=create_schema_design_agent("secondary"),
            stage_name="schema_design_secondary"
        )
        secondary_schema = schema_secondary_agent(referee_output)

        return {
            'primary_schema': primary_schema,
            'secondary_schema': secondary_schema,
            'recommendations': referee_output
        }
```

---

### Component 2: DynamoDB Schema

```python
# DynamoDB Table: modernizer-jobs

{
    "job_id": "job-123",  # Partition key (String)
    "status": "in_progress",  # pending | in_progress | completed | failed
    "current_stage": "collector",
    "progress": "1/11",
    "created_at": "2026-02-01T20:00:00Z",
    "updated_at": "2026-02-01T21:30:00Z",
    "websocket_connection_id": "abc123",  # For WebSocket push
    "checkpoint_s3_key": "checkpoints/job-123/collector.json",
    "input_contract": {...},  # Original request
    "ttl": 1738454400  # Auto-delete after 30 days
}
```

**Indexes:**

- Primary Key: `job_id`
- GSI: `status-created_at-index` (for querying jobs by status)
- TTL: `ttl` field (auto-cleanup after 30 days)

---

### Component 3: EventBridge Rule

```json
{
  "Name": "modernizer-progress-rule",
  "EventPattern": {
    "source": ["modernizer.workflow"],
    "detail-type": ["StageProgress"]
  },
  "Targets": [
    {
      "Arn": "arn:aws:lambda:us-west-2:123456789:function:progress-publisher",
      "Id": "ProgressPublisher"
    }
  ]
}
```

---

### Component 4: Lambda Progress Publisher

```python
# lambda/progress_publisher.py

import boto3
import json
import os
from datetime import datetime

dynamodb = boto3.resource('dynamodb')
apigateway = boto3.client('apigatewaymanagementapi',
    endpoint_url=os.environ['WEBSOCKET_API_ENDPOINT']
)
table = dynamodb.Table(os.environ['JOBS_TABLE'])

def handler(event, context):
    """
    Triggered by EventBridge when Strands publishes progress.
    Updates DynamoDB and pushes to WebSocket client.
    """
    detail = event['detail']
    job_id = detail['job_id']
    stage = detail['stage']
    status = detail['status']
    progress = detail['progress']

    # Update DynamoDB
    table.update_item(
        Key={'job_id': job_id},
        UpdateExpression='SET current_stage = :stage, progress = :progress, updated_at = :updated',
        ExpressionAttributeValues={
            ':stage': stage,
            ':progress': progress,
            ':updated': datetime.utcnow().isoformat()
        }
    )

    # Get WebSocket connection_id
    response = table.get_item(Key={'job_id': job_id})
    connection_id = response.get('Item', {}).get('websocket_connection_id')

    if not connection_id:
        print(f"No WebSocket connection for job {job_id}")
        return {'statusCode': 200}

    # Push to WebSocket
    try:
        apigateway.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps({
                'type': 'progress',
                'job_id': job_id,
                'stage': stage,
                'status': status,
                'progress': progress,
                'timestamp': detail['timestamp']
            })
        )
        print(f"Pushed progress to connection {connection_id}")
    except apigateway.exceptions.GoneException:
        # Connection closed, remove from DynamoDB
        table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='REMOVE websocket_connection_id'
        )
        print(f"Connection {connection_id} closed, removed from DynamoDB")

    return {'statusCode': 200}
```

---

### Component 5: API Gateway WebSocket

**Routes:**

- `$connect`: Store connection_id in DynamoDB
- `$disconnect`: Remove connection_id from DynamoDB
- `$default`: Handle client messages (optional)

**Connect Handler:**

```python
# lambda/websocket_connect.py

import boto3
import json
import os

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['JOBS_TABLE'])

def handler(event, context):
    """
    Store WebSocket connection_id when client connects.
    Client must provide job_id in query string.
    """
    connection_id = event['requestContext']['connectionId']
    job_id = event['queryStringParameters'].get('job_id')

    if not job_id:
        return {'statusCode': 400, 'body': 'Missing job_id'}

    # Store connection_id in DynamoDB
    table.update_item(
        Key={'job_id': job_id},
        UpdateExpression='SET websocket_connection_id = :conn_id',
        ExpressionAttributeValues={':conn_id': connection_id}
    )

    return {'statusCode': 200}
```

**Disconnect Handler:**

```python
# lambda/websocket_disconnect.py

import boto3
import os

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['JOBS_TABLE'])

def handler(event, context):
    """Remove WebSocket connection_id when client disconnects"""
    connection_id = event['requestContext']['connectionId']

    # Find job_id by connection_id (requires GSI)
    # For simplicity, we'll let the progress publisher handle cleanup

    return {'statusCode': 200}
```

---

### Component 6: ECS Task Entry Point

```python
# ecs_task.py

import os
import sys
import boto3
from workflows.modernizer_workflow import ModernizerWorkflow

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['JOBS_TABLE'])

def update_job_status(job_id: str, status: str):
    """Update job status in DynamoDB"""
    table.update_item(
        Key={'job_id': job_id},
        UpdateExpression='SET #status = :status',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={':status': status}
    )

def main():
    job_id = os.environ['JOB_ID']

    # Load input contract from DynamoDB
    response = table.get_item(Key={'job_id': job_id})
    input_contract = response['Item']['input_contract']

    # Update status to in_progress
    update_job_status(job_id, 'in_progress')

    try:
        # Execute workflow
        workflow = ModernizerWorkflow(job_id, input_contract)
        result = workflow.run()

        # Save result to S3
        s3 = boto3.client('s3')
        s3.put_object(
            Bucket=os.environ['RESULTS_BUCKET'],
            Key=f"results/{job_id}/final.json",
            Body=json.dumps(result)
        )

        # Update status to completed
        update_job_status(job_id, 'completed')

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        update_job_status(job_id, 'failed')
        sys.exit(1)

if __name__ == "__main__":
    main()
```

---

## Client Integration

### WebSocket Connection

```javascript
// Client-side JavaScript

const jobId = 'job-123';
const wsUrl = `wss://api.example.com/ws?job_id=${jobId}`;

const ws = new WebSocket(wsUrl);

ws.onopen = () => {
  console.log('Connected to progress updates');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === 'progress') {
    console.log(`Stage: ${data.stage}`);
    console.log(`Status: ${data.status}`);
    console.log(`Progress: ${data.progress}`);

    // Update UI
    updateProgressBar(data.progress);
    updateStageStatus(data.stage, data.status);
  }
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Disconnected from progress updates');
};
```

---

## Cost Analysis

### Cost Comparison (1000 jobs/month)

| Component | Celery/Redis | Bedrock AgentCore | EventBridge (Selected) |
|-----------|--------------|-------------------|------------------------|
| Compute | $80-120 | $120-200 | $50-100 |
| Storage | $15 | $15 | $15 |
| Orchestration | $25-60 (Redis) | $37-57 (AgentCore) | $7 (EventBridge + Lambda) |
| **Total** | **$120-200** | **$172-272** | **$72-122** |
| **Per Job** | **$0.12-0.20** | **$0.17-0.27** | **$0.07-0.12** |

**EventBridge provides 40-60% cost savings** compared to alternatives while offering better reliability and operational simplicity.

---

## Consequences

### Positive

✅ **Cost-effective**: 40-60% cheaper than Celery/Redis or Bedrock AgentCore alternatives
✅ **Fully managed**: No Redis clusters or Celery workers to maintain
✅ **Real-time updates**: WebSocket push notifications to clients
✅ **Scalable**: Auto-scales with load without manual configuration
✅ **Reliable**: Built-in retry logic with exponential backoff and DLQ
✅ **Integrates with ADR-001**: Works seamlessly with S3 checkpoints
✅ **Integrates with ADR-002**: Works with validation hooks
✅ **No vendor lock-in**: Uses standard AWS services
✅ **Spot-ready**: Can optimize with spot instances for additional savings
✅ **Event-driven**: Native support for fan-out to multiple targets

### Negative

⚠️ **WebSocket complexity**: Requires connection management and reconnection logic
⚠️ **EventBridge latency**: ~1-2 second delay for event propagation
⚠️ **DynamoDB costs**: Scales with job volume (though still cost-effective)
⚠️ **AWS-specific**: Tied to AWS ecosystem (though uses standard services)

### Trade-offs vs Alternatives

**vs Celery/Redis:**

- ✅ Lower operational overhead (no Redis cluster management)
- ✅ Lower cost
- ⚠️ Less familiar to teams with Celery experience

**vs Bedrock AgentCore:**

- ✅ No timeout limitations
- ✅ Lower cost
- ✅ More control over execution environment
- ⚠️ Requires more custom integration code

### Neutral

🔶 **Stage-level granularity**: 11 updates per job (simple, clear)
🔶 **Learning curve**: Teams need to understand EventBridge patterns

---

## Monitoring & Observability

### CloudWatch Metrics

1. **Progress Events Published**: Count of EventBridge events
2. **WebSocket Connections**: Active connections
3. **Lambda Invocations**: Progress publisher executions
4. **DynamoDB Read/Write**: Job metadata operations
5. **ECS Task Duration**: Workflow execution time

### CloudWatch Alarms

1. **Failed Progress Events**: Alert if EventBridge delivery fails
2. **Lambda Errors**: Alert if progress publisher fails
3. **WebSocket Errors**: Alert if connection failures spike
4. **Job Timeout**: Alert if job exceeds 6 hours

---

## Testing Strategy

### Unit Tests

```python
def test_publish_progress():
    """Test progress event publishing"""
    workflow = ModernizerWorkflow("test-job", {})
    workflow.publish_progress("collector", "started")
    # Verify EventBridge event

def test_progress_publisher_lambda():
    """Test Lambda handler"""
    event = create_test_event("collector", "completed")
    result = handler(event, {})
    assert result['statusCode'] == 200
```

### Integration Tests

```python
def test_end_to_end_progress():
    """Test complete progress flow"""
    # Start ECS task
    # Connect WebSocket
    # Verify 11 progress updates received
    # Verify DynamoDB updated
```

---

## Related Documents

- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)
- [ADR-002: Structured Output and Validation](ADR-002-structured-output-and-validation.md)
- [High-Level Design](../high-level-design.md)
- [Architecture Review](../ARCHITECTURE_REVIEW.md) - Point #3

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-01 | Architecture Team | Initial decision |
| 1.1 | 2026-02-06 | Architecture Team | Updated to clarify EventBridge as selected option, expanded alternatives comparison, added cost comparison table |

---

**Status: Accepted and Ready for Implementation**
