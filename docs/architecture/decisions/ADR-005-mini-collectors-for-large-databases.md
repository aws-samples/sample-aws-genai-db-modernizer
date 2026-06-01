# ADR-005: Mini-Collectors for Large Database Scalability

**Status:** Accepted
**Date:** 2026-02-02
**Deciders:** Architecture Team
**Related Issues:** Architecture Review - Large Database Optimization
**Related ADRs:** ADR-001 (State Management), ADR-002 (Pydantic Output), ADR-004 (RDS Tools)

---

## Context

Database Modernizer collector agents need to analyze databases of varying sizes:

- **Small databases**: 10-100 tables (fast, single collector)
- **Medium databases**: 100-500 tables (manageable, single collector)
- **Large databases**: 500-5,000+ tables (slow, needs parallelization)

### Problem

For large databases (1000+ tables), a single collector agent:

- ❌ Takes too long (hours for schema collection)
- ❌ Risks timeout (ECS Fargate 8-hour limit)
- ❌ Inefficient (sequential processing)
- ❌ Poor user experience (no progress for long periods)

### Requirements

- Support databases with 5,000+ tables
- Complete collection in <2 hours (even for large databases)
- Parallel processing for scalability
- Maintain contract compliance (same output format)
- Resume capability (from ADR-001)

---

## Decision

We will implement **Mini-Collectors with Parallel Execution**:

1. **Threshold-based splitting**: Databases >100 tables split into chunks
2. **Parallel mini-collectors**: Each processes a subset of tables
3. **Result aggregation**: Merge mini-collector outputs into single CollectorOutput
4. **Checkpoint per mini-collector**: Resume individual mini-collectors if failed

### Splitting Strategy

```python
# Determine if splitting needed
if table_count <= 100:
    # Single collector (fast path)
    output = collector.collect()
else:
    # Mini-collectors (parallel path)
    chunk_size = 100  # Tables per mini-collector
    chunks = split_tables_into_chunks(tables, chunk_size)

    # Run mini-collectors in parallel
    outputs = await asyncio.gather(*[
        mini_collector.collect(chunk) for chunk in chunks
    ])

    # Merge results
    output = merge_collector_outputs(outputs)
```

---

## Architecture

### Parallel Execution Flow

```
Main Collector Agent
    ↓
Detect table count > 100
    ↓
Split into chunks (100 tables each)
    ↓
┌─────────────────────────────────────────────────────────┐
│         Parallel Mini-Collectors                        │
│                                                         │
│  Mini-Collector 1    Mini-Collector 2    Mini-Collector 3
│  (tables 1-100)      (tables 101-200)    (tables 201-300)
│        ↓                    ↓                    ↓
│   Checkpoint 1.1       Checkpoint 1.2       Checkpoint 1.3
│        ↓                    ↓                    ↓
│   Partial Output       Partial Output       Partial Output
└─────────────────────────────────────────────────────────┘
    ↓
Merge partial outputs
    ↓
Final CollectorOutput (Pydantic model)
    ↓
Checkpoint (stage 1 complete)
```

---

## Implementation

### Main Collector with Mini-Collectors

```python
# src/agents/collector/mysql_collector.py

from strands import Agent
from contracts.models.collector_output import CollectorOutput
import asyncio

class MySQLCollectorAgent:
    """
    MySQL Collector with mini-collector support for large databases.
    """

    MINI_COLLECTOR_THRESHOLD = 100  # Tables
    MINI_COLLECTOR_CHUNK_SIZE = 100  # Tables per mini-collector

    def __init__(self, input_contract: dict):
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']
        self.credential_manager = create_credential_manager(input_contract)

    async def collect(self) -> CollectorOutput:
        """
        Execute collection with automatic mini-collector splitting.

        Returns:
            CollectorOutput (Pydantic model, guaranteed valid)
        """
        # Step 1: Get table list
        tables = await self._get_table_list()

        # Step 2: Decide strategy
        if len(tables) <= self.MINI_COLLECTOR_THRESHOLD:
            # Fast path: Single collector
            logger.info(f"Job {self.job_id}: Using single collector ({len(tables)} tables)")
            return await self._collect_single(tables)
        else:
            # Parallel path: Mini-collectors
            logger.info(f"Job {self.job_id}: Using mini-collectors ({len(tables)} tables)")
            return await self._collect_parallel(tables)

    async def _collect_single(self, tables: List[str]) -> CollectorOutput:
        """Single collector for small databases"""
        agent = Agent(
            system_prompt=self._create_system_prompt(tables),
            tools=self._create_tools(),
            response_format=CollectorOutput
        )

        result = agent(self._format_input(tables))
        return result

    async def _collect_parallel(self, tables: List[str]) -> CollectorOutput:
        """Parallel mini-collectors for large databases"""
        # Split tables into chunks
        chunks = self._split_into_chunks(tables, self.MINI_COLLECTOR_CHUNK_SIZE)

        logger.info(f"Job {self.job_id}: Split into {len(chunks)} mini-collectors")

        # Check for existing mini-collector checkpoints (resume)
        partial_outputs = []
        tasks = []

        for i, chunk in enumerate(chunks):
            checkpoint_key = f"mini_collector_{i}"

            # Try to load checkpoint
            checkpoint = load_checkpoint(self.job_id, checkpoint_key)

            if checkpoint:
                logger.info(f"Job {self.job_id}: Mini-collector {i} already complete (resumed)")
                partial_outputs.append(checkpoint)
            else:
                # Need to run this mini-collector
                tasks.append(self._run_mini_collector(i, chunk))

        # Run remaining mini-collectors in parallel
        if tasks:
            new_outputs = await asyncio.gather(*tasks)
            partial_outputs.extend(new_outputs)

        # Merge all partial outputs
        final_output = self._merge_outputs(partial_outputs)

        return final_output

    async def _run_mini_collector(
        self,
        index: int,
        tables: List[str]
    ) -> Dict:
        """
        Run a single mini-collector for a chunk of tables.

        Returns partial output that will be merged later.
        """
        logger.info(f"Job {self.job_id}: Mini-collector {index} starting ({len(tables)} tables)")

        # Create mini-collector agent
        agent = Agent(
            system_prompt=self._create_mini_collector_prompt(tables),
            tools=self._create_tools(),
            response_format=PartialCollectorOutput  # Subset of CollectorOutput
        )

        # Execute
        partial_output = agent(self._format_input(tables))

        # Checkpoint this mini-collector
        checkpoint_key = f"mini_collector_{index}"
        save_checkpoint(self.job_id, checkpoint_key, partial_output)

        logger.info(f"Job {self.job_id}: Mini-collector {index} complete")

        # Publish progress (from ADR-003)
        publish_progress(
            self.job_id,
            f"collector_mini_{index}",
            "completed",
            metadata={'tables_processed': len(tables)}
        )

        return partial_output

    def _split_into_chunks(
        self,
        tables: List[str],
        chunk_size: int
    ) -> List[List[str]]:
        """Split tables into chunks for parallel processing"""
        return [
            tables[i:i + chunk_size]
            for i in range(0, len(tables), chunk_size)
        ]

    def _merge_outputs(self, partial_outputs: List[Dict]) -> CollectorOutput:
        """
        Merge partial outputs from mini-collectors into final CollectorOutput.

        Combines:
        - Database metadata (from first mini-collector)
        - Schemas (merge all table schemas)
        - Query patterns (merge all patterns)
        - AWS metadata (from first mini-collector)
        """
        # Start with first output as base
        merged = partial_outputs[0].copy()

        # Merge schemas from all mini-collectors
        all_schemas = {}
        for output in partial_outputs:
            all_schemas.update(output['database_schema'])

        merged['database_schema'] = all_schemas

        # Merge query patterns
        all_patterns = []
        for output in partial_outputs:
            if output.get('query_patterns'):
                all_patterns.extend(output['query_patterns'])

        merged['query_patterns'] = all_patterns if all_patterns else None

        # Update table count in metadata
        merged['database_metadata']['table_count'] = len(all_schemas)

        # Convert to Pydantic model
        return CollectorOutput(**merged)

    def _create_mini_collector_prompt(self, tables: List[str]) -> str:
        """System prompt for mini-collector (subset of tables)"""
        return f"""You are a MySQL Mini-Collector Agent.

Your mission: Collect metadata for a SUBSET of tables in the database.

Tables to process ({len(tables)} total):
{', '.join(tables[:10])}{'...' if len(tables) > 10 else ''}

Your Tools:
1. connect_mysql - Establish connection
2. collect_schema - Gather schema for ONLY the specified tables
3. collect_query_patterns - Analyze queries for ONLY the specified tables

IMPORTANT: Only collect data for the tables listed above.
Do NOT collect data for other tables in the database.

Output Format: PartialCollectorOutput (subset of CollectorOutput)
"""
```

---

### Partial Output Model

```python
# src/contracts/models/partial_collector_output.py

from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from datetime import datetime

class PartialCollectorOutput(BaseModel):
    """
    Partial output from a mini-collector.

    Contains data for a subset of tables.
    Will be merged with other partial outputs to create final CollectorOutput.
    """
    job_id: str
    mini_collector_index: int = Field(description="Index of this mini-collector")
    tables_processed: List[str] = Field(description="Tables processed by this mini-collector")

    database_metadata: Dict = Field(description="Database metadata (same for all mini-collectors)")
    schema: Dict[str, Dict] = Field(description="Schemas for subset of tables")
    query_patterns: Optional[List[Dict]] = Field(default=None)
    aws_metadata: Optional[Dict] = Field(default=None)
```

---

## Performance Analysis

### Single Collector (Baseline)

**Database:** 1,000 tables
**Time:** ~2 hours (sequential processing)
**Parallelization:** None

### Mini-Collectors (10 parallel)

**Database:** 1,000 tables
**Chunks:** 10 mini-collectors × 100 tables each
**Time:** ~15 minutes (parallel processing)
**Speedup:** 8x faster

### Mini-Collectors (50 parallel)

**Database:** 5,000 tables
**Chunks:** 50 mini-collectors × 100 tables each
**Time:** ~20 minutes (parallel processing)
**Speedup:** 6x faster (with overhead)

---

## Consequences

### Positive

✅ **Scalable**: Handles databases with 5,000+ tables
✅ **Fast**: 8x speedup for large databases
✅ **Resumable**: Each mini-collector checkpointed individually
✅ **Progress visibility**: Users see progress per mini-collector
✅ **Same contract**: Final output matches CollectorOutput
✅ **Automatic**: Threshold-based (no user configuration)

### Negative

⚠️ **Complexity**: More code to maintain
⚠️ **Overhead**: Merging partial outputs adds time
⚠️ **Concurrency**: Need to manage parallel execution

### Neutral

🔶 **Threshold**: 100 tables (configurable)
🔶 **Chunk size**: 100 tables per mini-collector (configurable)

---

## Alternatives Considered

### Alternative 1: Single Collector Only (Rejected)

**Rejected because:**

- ❌ Too slow for large databases (hours)
- ❌ Risks timeout
- ❌ Poor user experience

### Alternative 2: Always Use Mini-Collectors (Rejected)

**Rejected because:**

- ❌ Unnecessary overhead for small databases
- ❌ More complex for common case
- ❌ Slower for small databases

### Alternative 3: User-Configured Parallelization (Rejected)

**Rejected because:**

- ❌ Requires user to understand database size
- ❌ More configuration complexity
- ❌ Automatic is better UX

---

## Related Documents

- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)
- [ADR-002: Structured Output with Pydantic](ADR-002-structured-output-and-validation.md)
- [ADR-003: Progress Reporting Architecture](ADR-003-progress-reporting-architecture.md)
- [ADR-004: RDS Tools and AWS Integration](ADR-004-rds-tools-and-aws-integration.md)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-02 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**
