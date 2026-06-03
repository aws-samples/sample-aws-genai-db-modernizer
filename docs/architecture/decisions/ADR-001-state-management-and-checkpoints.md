# ADR-001: Hybrid State Management with Spot-Optimized Checkpoints

**Status:** Superseded by [ADR-016](ADR-016-compute-and-orchestration-strategy.md)
**Date:** 2026-02-01
**Deciders:** Architecture Team
**Related Issues:** Architecture Review Point #1

---

> **Superseded:** The checkpoint-based state management approach described here has been replaced by a restart-from-scratch model. See [ADR-016](ADR-016-compute-and-orchestration-strategy.md) for the current compute and orchestration strategy.

## Context

Database Modernizer Assessment agents need to communicate data between stages (Collector → Analysis → Referee → Schema Design). We had two conflicting approaches:

1. **Pure JSON files**: Serialize between every agent (slow, but resumable)
2. **Pure Strands state**: In-memory only (fast, but not resumable)

### Requirements

- Jobs run 1-6 hours (need reliability)
- Must resume from major stages (collector, analysis, referee)
- Debug at major stage boundaries
- Run on ECS Fargate initially
- **Future optimization**: Run on spot instances (cost savings)
- Enable parallel development (teams assume valid inputs)

---

## Decision

We will use **Hybrid State Management**:

1. **Strands Workflow state** for agent-to-agent communication (in-memory, fast)
2. **Spot-optimized checkpoints** at 11 stages (S3/local storage)
3. **Structured output + validation** with single auto-retry
4. **Storage abstraction** supporting S3 (priority) and local filesystem

### Checkpoint Strategy (11 Total)

```
Stage 1:  collector_complete
Stage 2:  dynamodb_analysis_complete
Stage 3:  documentdb_analysis_complete
Stage 4:  elasticache_analysis_complete
Stage 5:  opensearch_analysis_complete
Stage 6:  neptune_analysis_complete
Stage 7:  keyspaces_analysis_complete
Stage 8:  aurora_analysis_complete
Stage 9:  referee_complete
Stage 10: schema_design_complete
Stage 11: final_output
```

**Rationale for 11 checkpoints:**

- Optimized for spot instances from day one
- Each analysis agent checkpointed individually (enables granular resume)
- Minimal overhead (async writes, non-blocking)
- Balances resume granularity vs checkpoint overhead

---

## Architecture

### Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    Strands Workflow                         │
│                  (In-Memory State Passing)                  │
│                                                             │
│  Collector Agent                                            │
│      ↓ (Strands state: fast)                                │
│      ↓ (checkpoint to S3: async) ← Stage 1                  │
│                                                             │
│  Analysis Agents (7 parallel)                               │
│      ↓ (Strands state: fast)                                │
│      ↓ (checkpoint each: async) ← Stages 2-8                │
│                                                             │
│  Referee Agent                                              │
│      ↓ (Strands state: fast)                                │
│      ↓ (checkpoint to S3: async) ← Stage 9                  │
│                                                             │
│  Schema Design Agent                                        │
│      ↓ (final output to S3) ← Stage 10-11                   │
└─────────────────────────────────────────────────────────────┘
```

### Validation with Auto-Retry

Every agent output is validated against its contract schema:

```python
def validate_and_checkpoint(stage_name: str):
    """
    Validate output against contract, retry once if invalid
    """
    def hook(result, task_context):
        # 1. Validate against schema
        validation = validate_output(result, schema=f"{stage_name}-output.json")

        if validation['valid']:
            # 2. Checkpoint to storage (async)
            storage = task_context.invocation_state['storage']
            storage.checkpoint(job_id, stage_name, result)
            return result
        else:
            # 3. Single retry with explicit formatting instructions
            formatting_prompt = f"""
Your previous output failed validation with these errors:
{validation['errors']}

Required schema:
{load_schema(f"{stage_name}-output.json")}

Please reformat your output to match the schema exactly.
Focus on:
- Correct field names
- Correct data types
- Required fields present
- No extra fields (unless allowed)
"""
            # Re-execute agent with formatting instructions
            retry_result = task_context.agent(formatting_prompt)

            # Validate again (fail if still invalid)
            retry_validation = validate_output(retry_result, schema=f"{stage_name}-output.json")
            if not retry_validation['valid']:
                raise ValidationError(
                    f"Agent '{stage_name}' failed validation after retry. "
                    f"Errors: {retry_validation['errors']}"
                )

            # Checkpoint valid result
            storage.checkpoint(job_id, stage_name, retry_result)
            return retry_result

    return hook
```

**Why Single Retry (Not Multiple)?**

We chose **single retry with explicit formatting instructions** instead of multiple retries because:

1. **Fast failure detection**: If an agent can't format correctly after explicit schema instructions, it won't succeed on retry 3
2. **Faster debugging**: Developers see failures quickly rather than waiting for 3 retries
3. **Simpler logic**: Less code, easier to understand and maintain
4. **Clear signal**: Multiple failures indicate a fundamental agent problem (bad prompt, broken tool), not a formatting issue
5. **Cost efficiency**: Fewer LLM calls when agent is fundamentally broken

**When retry succeeds:**

- Agent had minor formatting issue (missing field, wrong type)
- Explicit schema instructions fix the problem
- Job continues normally

**When retry fails:**

- Agent has fundamental problem (bad system prompt, broken tool, wrong data)
- Multiple retries won't help
- Fail fast, alert developer, fix root cause

---

## Storage Abstraction

Simple abstraction supporting S3 (production) and local filesystem (development):

```python
class StorageBackend(ABC):
    @abstractmethod
    def checkpoint(self, job_id: str, stage: str, data: dict):
        """Save checkpoint (async, non-blocking)"""
        pass

    @abstractmethod
    def load_checkpoint(self, job_id: str, stage: str) -> dict:
        """Load checkpoint for resume"""
        pass

    @abstractmethod
    def list_checkpoints(self, job_id: str) -> list[str]:
        """List available checkpoints (for resume logic)"""
        pass

# S3 implementation (production)
class S3Storage(StorageBackend):
    def checkpoint(self, job_id, stage, data):
        key = f"jobs/{job_id}/checkpoints/{stage}.json"
        s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data))

    def load_checkpoint(self, job_id, stage):
        key = f"jobs/{job_id}/checkpoints/{stage}.json"
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj['Body'].read())

    def list_checkpoints(self, job_id):
        prefix = f"jobs/{job_id}/checkpoints/"
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        return [obj['Key'].split('/')[-1].replace('.json', '')
                for obj in response.get('Contents', [])]

# Local implementation (development)
class LocalStorage(StorageBackend):
    def checkpoint(self, job_id, stage, data):
        path = f"/data/jobs/{job_id}/checkpoints/{stage}.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    def load_checkpoint(self, job_id, stage):
        path = f"/data/jobs/{job_id}/checkpoints/{stage}.json"
        with open(path, 'r') as f:
            return json.load(f)

    def list_checkpoints(self, job_id):
        checkpoint_dir = f"/data/jobs/{job_id}/checkpoints"
        if not os.path.exists(checkpoint_dir):
            return []
        return [f.replace('.json', '') for f in os.listdir(checkpoint_dir)]
```

**Design Principle:** Keep it simple. If complexity arises, prioritize S3 implementation.

---

## Resume Logic

When a job restarts (spot interruption, failure, manual restart):

```python
def determine_resume_stage(job_id: str, storage: StorageBackend) -> str:
    """Determine which stage to resume from"""
    checkpoints = storage.list_checkpoints(job_id)

    # Resume from last completed stage
    stage_order = [
        "collector",
        "dynamodb_analysis",
        "documentdb_analysis",
        "elasticache_analysis",
        "opensearch_analysis",
        "neptune_analysis",
        "keyspaces_analysis",
        "aurora_analysis",
        "referee",
        "schema_design",
        "final"
    ]

    # Find last completed stage
    for stage in reversed(stage_order):
        if stage in checkpoints:
            # Resume from next stage
            next_index = stage_order.index(stage) + 1
            if next_index < len(stage_order):
                return stage_order[next_index]
            else:
                return None  # Job complete

    # No checkpoints found, start from beginning
    return "collector"

# Usage in workflow
resume_from = determine_resume_stage(job_id, storage)
if resume_from:
    logger.info(f"Resuming job {job_id} from stage: {resume_from}")
    # Load checkpoint data and skip completed stages
else:
    logger.info(f"Job {job_id} already complete")
```

---

## Consequences

### Positive

✅ **Fast execution**: In-memory state passing between agents (no I/O overhead)
✅ **Spot-optimized**: 11 checkpoints enable resume from any analysis agent
✅ **Resumable**: Restart from last completed stage (handles spot interruptions)
✅ **Debuggable**: Inspect JSON checkpoints at stage boundaries
✅ **Audit trail**: S3 checkpoints provide compliance evidence
✅ **Contract compliance**: Validation + auto-retry enforces schemas
✅ **Parallel development**: Teams can assume valid inputs (validation guarantees)
✅ **Self-healing**: Auto-retry fixes minor formatting issues
✅ **Fast failure**: Single retry detects fundamental problems quickly
✅ **Cost efficient**: Fewer retries = fewer LLM calls

### Negative

⚠️ **Complexity**: More complex than pure JSON or pure Strands state
⚠️ **Storage dependency**: Requires S3 or local filesystem
⚠️ **Checkpoint overhead**: 11 async writes per job (minimal, but not zero)
⚠️ **Resume logic**: Need to track completed stages and skip them

### Neutral

🔶 **Validation strictness**: Single retry means agents must be well-designed
🔶 **Storage abstraction**: Simple now, may need enhancement later

---

## Implementation Notes

### Strands Workflow Integration

```python
from strands import Workflow, Task, Agent

# Create workflow with validation hooks
collector_task = Task(
    name="collector",
    agent=collector_agent,
    output_key="collector_data",
    post_execution_hook=validate_and_checkpoint("collector")
)

dynamodb_task = Task(
    name="dynamodb_analysis",
    agent=dynamodb_agent,
    depends_on=[collector_task],
    input_mapping={"collector_output": "collector_data"},
    post_execution_hook=validate_and_checkpoint("dynamodb_analysis")
)

# ... 6 more analysis agents

referee_task = Task(
    name="referee",
    agent=referee_agent,
    depends_on=all_analysis_tasks,
    post_execution_hook=validate_and_checkpoint("referee")
)

workflow = Workflow(tasks=[collector_task] + analysis_tasks + [referee_task])

# Execute with storage backend
result = workflow.execute(
    input_data=input_contract,
    invocation_state={
        'job_id': job_id,
        'storage': storage_backend,
        'resume_from': determine_resume_stage(job_id, storage_backend)
    }
)
```

### ECS Fargate Integration

**Note:** See [ADR-003: Progress Reporting Architecture](ADR-003-progress-reporting-architecture.md) for complete orchestration details.

```python
# ECS Task Entry Point
# ecs_task.py

import os
import boto3
from workflows.modernizer_workflow import ModernizerWorkflow

def main():
    """Execute Strands workflow with checkpoints on ECS Fargate"""
    job_id = os.environ['JOB_ID']

    # Determine storage backend
    storage = S3Storage(bucket=os.getenv('S3_BUCKET')) \
              if os.getenv('STORAGE_BACKEND') == 's3' \
              else LocalStorage()

    # Load input contract
    input_contract = load_input_contract(job_id, storage)

    # Check for resume
    resume_from = determine_resume_stage(job_id, storage)

    if resume_from is None:
        logger.info(f"Job {job_id} already complete")
        return storage.load_checkpoint(job_id, "final")

    # Execute workflow with progress reporting (EventBridge)
    try:
        workflow = ModernizerWorkflow(job_id, input_contract, storage)

        if resume_from:
            logger.info(f"Resuming job {job_id} from stage: {resume_from}")
            result = workflow.resume_from(resume_from)
        else:
            logger.info(f"Starting job {job_id} from beginning")
            result = workflow.run()

        # Save final result
        storage.save_checkpoint(job_id, "final", result)
        update_job_status(job_id, "completed")

        return result

    except ValidationError as e:
        # Validation failed after retry
        logger.error(f"Job {job_id} failed validation: {e}")
        update_job_status(job_id, "failed")
        raise

    except Exception as e:
        # Other errors (will resume from last checkpoint on restart)
        logger.error(f"Job {job_id} failed: {e}")
        update_job_status(job_id, "failed")
        raise

if __name__ == "__main__":
    main()
```

---

## Alternatives Considered

### Alternative 1: Pure Strands State (No Checkpoints)

**Rejected because:**

- No resume capability (spot interruptions require full restart)
- No debugging at intermediate stages
- Not suitable for 1-6 hour jobs

### Alternative 2: Pure JSON Files (No Strands State)

**Rejected because:**

- Slow (I/O between every agent)
- Loses Strands benefits (shared context, in-memory state)
- More code (manual serialization)

### Alternative 3: Checkpoint Only at Major Stages (4 checkpoints)

**Rejected because:**

- Not optimized for spot instances
- Analysis agents run 1-2 hours each (losing 2 hours on interruption)
- Spot-optimized checkpoints (11 total) have minimal overhead

### Alternative 4: Multiple Retries (3 attempts)

**Rejected because:**

- Slower failure detection (wait for 3 retries)
- More LLM costs (3x calls for broken agents)
- Doesn't fix fundamental agent problems
- Single retry with explicit instructions is sufficient

---

## Related Documents

- [High-Level Design](../high-level-design.md) - Section 3.5 (Agent Communication)
- [Architecture Review](../ARCHITECTURE_REVIEW.md) - Point #1
- [Strands Collector Guide](../guides/strands-collector-guide.md)
- [Agent Contracts Spec](../contracts/agent-contracts-spec.md)

---

## Future Optimizations

### Spot Instance Enhancements (When Implemented)

1. **Graceful shutdown handler**

   ```python
   signal.signal(signal.SIGTERM, handle_spot_termination)

   def handle_spot_termination(signum, frame):
       logger.warning("Spot termination notice received")
       # Current checkpoint is already saved (async)
       # Just exit gracefully
       sys.exit(0)
   ```

2. **Checkpoint frequency tuning**
   - Monitor checkpoint overhead vs resume time saved
   - Adjust checkpoint frequency based on metrics
   - Consider checkpointing within long-running agents (e.g., every 100 tables)

3. **Cost analysis**
   - Track spot interruption frequency
   - Measure time saved by checkpoints
   - Calculate ROI of spot instances vs on-demand

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-01 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**
