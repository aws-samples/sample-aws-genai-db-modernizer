# ADR-006: Analysis Agent Architecture (Multi-Agent Pattern)

**Status:** Accepted
**Date:** 2026-02-02
**Deciders:** Architecture Team
**Related ADRs:** ADR-001 (State Management), ADR-002 (Pydantic Output), ADR-005 (Mini-Collectors)

---

## Context

The "Analysis Agent" is actually a **category of specialized agents**, not a single monolithic agent. Each analyzes different aspects of database modernization:

- **Schema analysis** (structure, relationships, constraints)
- **Performance analysis** (query patterns, indexes, bottlenecks)
- **Target-specific analysis** (Aurora features, RDS optimizations, etc.)
- **Cost analysis** (resource utilization, optimization opportunities)
- **Security analysis** (encryption, access patterns, compliance)

### The Problem with Single Analysis Agent

A monolithic analysis agent would:

- ❌ Be too complex (trying to do everything)
- ❌ Be hard to extend (adding new analysis types)
- ❌ Be target-locked (Aurora-only, PostgreSQL-only, etc.)
- ❌ Limit creativity (implementation decisions made too early)

### Requirements

- **Extensible**: Easy to add new analysis agent types
- **Specialized**: Each agent focuses on one analysis domain
- **Composable**: Agents can be combined for comprehensive analysis
- **Target-agnostic**: Support multiple modernization targets (Aurora, RDS, etc.)
- **Implementation-flexible**: Don't lock into specific agent splits upfront

---

## Decision

We will implement **Analysis as a Multi-Agent Category** with:

1. **Analysis Agent Interface**: Common contract all analysis agents implement
2. **Specialized Analysis Agents**: Domain-specific agents (schema, performance, target-specific)
3. **Analysis Orchestrator**: Coordinates multiple analysis agents
4. **Extensible Registry**: Easy to add new analysis agent types

### Key Principle: Defer Implementation Details

This ADR defines:

- ✅ The **pattern** for analysis agents (interface, orchestration)
- ✅ The **categories** of analysis (schema, performance, target-specific)
- ✅ The **contract** (input/output models)

This ADR does NOT define:

- ❌ Exact number of agents (decided during implementation)
- ❌ Agent splitting strategy (decided per analysis type)
- ❌ Internal agent architecture (room for creativity)

---

## Architecture

### High-Level Pattern

```
Collector Output (Pydantic)
    ↓
┌─────────────────────────────────────────────────────────┐
│         Analysis Orchestrator                           │
│                                                         │
│  Coordinates multiple specialized analysis agents       │
│  Manages checkpoints and progress reporting            │
└─────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────┐
│         Analysis Agent Army (Parallel Execution)        │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │   Schema     │  │ Performance  │  │   Target     │ │
│  │   Analysis   │  │   Analysis   │  │   Specific   │ │
│  │   Agent(s)   │  │   Agent(s)   │  │   Agent(s)   │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
│         ↓                 ↓                 ↓          │
│  SchemaAnalysis   PerformanceAnalysis  TargetAnalysis │
│  (Pydantic)       (Pydantic)           (Pydantic)     │
└─────────────────────────────────────────────────────────┘
    ↓
Merge all analysis outputs
    ↓
AnalysisOutput (Pydantic model)
    ↓
Referee Agent
```

### Analysis Agent Categories

**1. Schema Analysis Agents**

- Understand database structure
- Identify relationships, constraints, keys
- Detect schema anti-patterns
- *Implementation: Could be 1 agent or split into sub-agents*

**2. Performance Analysis Agents**

- Analyze query patterns
- Identify missing indexes
- Detect N+1 queries
- Find bottlenecks
- *Implementation: Could be 1 agent or split by concern*

**3. Target-Specific Analysis Agents**

- **Aurora Analysis Agent**: Aurora-specific features and optimizations
- **RDS Analysis Agent**: RDS-specific recommendations
- **PostgreSQL Analysis Agent**: PostgreSQL-specific patterns
- *Implementation: One agent per target, or shared with target parameter*

**4. Cost Analysis Agents** (Future)

- Resource utilization analysis
- Cost optimization opportunities
- *Implementation: TBD*

**5. Security Analysis Agents** (Future)

- Encryption analysis
- Access pattern review
- Compliance checks
- *Implementation: TBD*

---

## Implementation Pattern

### Analysis Agent Interface

```python
# src/agents/analysis/base_analysis_agent.py

from abc import ABC, abstractmethod
from pydantic import BaseModel
from contracts.models.collector_output import CollectorOutput

class AnalysisAgentInterface(ABC):
    """
    Base interface all analysis agents must implement.

    This defines the contract, not the implementation.
    """

    @abstractmethod
    async def analyze(
        self,
        collector_output: CollectorOutput,
        context: dict
    ) -> BaseModel:
        """
        Execute analysis and return structured output.

        Args:
            collector_output: Output from collector agent
            context: Additional context (target type, user preferences, etc.)

        Returns:
            Pydantic model with analysis results
        """
        pass

    @abstractmethod
    def get_analysis_type(self) -> str:
        """Return analysis type identifier (e.g., 'schema', 'performance')"""
        pass

    @abstractmethod
    def get_output_model(self) -> type[BaseModel]:
        """Return the Pydantic model class for this agent's output"""
        pass
```

---

### Analysis Orchestrator

```python
# src/agents/analysis/analysis_orchestrator.py

from typing import List, Dict, Type
from contracts.models.analysis_output import AnalysisOutput
from contracts.models.collector_output import CollectorOutput

class AnalysisOrchestrator:
    """
    Orchestrates multiple analysis agents.

    Responsibilities:
    - Discover available analysis agents
    - Execute agents in parallel (when possible)
    - Checkpoint individual agent results
    - Merge outputs into final AnalysisOutput
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.agents: List[AnalysisAgentInterface] = []

    def register_agent(self, agent: AnalysisAgentInterface):
        """Register an analysis agent"""
        self.agents.append(agent)
        logger.info(f"Registered analysis agent: {agent.get_analysis_type()}")

    async def analyze(
        self,
        collector_output: CollectorOutput,
        context: dict
    ) -> AnalysisOutput:
        """
        Execute all registered analysis agents.

        Agents run in parallel where possible.
        Each agent result is checkpointed individually.
        """
        results = {}

        # Check for existing checkpoints
        for agent in self.agents:
            analysis_type = agent.get_analysis_type()
            checkpoint_key = f"analysis_{analysis_type}"

            checkpoint = load_checkpoint(self.job_id, checkpoint_key)
            if checkpoint:
                logger.info(f"Job {self.job_id}: {analysis_type} resumed from checkpoint")
                results[analysis_type] = checkpoint

        # Run agents that don't have checkpoints (parallel)
        pending_agents = [
            agent for agent in self.agents
            if agent.get_analysis_type() not in results
        ]

        if pending_agents:
            logger.info(f"Job {self.job_id}: Running {len(pending_agents)} analysis agents")

            tasks = [
                self._run_agent(agent, collector_output, context)
                for agent in pending_agents
            ]

            new_results = await asyncio.gather(*tasks)

            # Add new results
            for agent, result in zip(pending_agents, new_results):
                results[agent.get_analysis_type()] = result

        # Merge all results into final output
        final_output = self._merge_results(results)

        # Final checkpoint
        save_checkpoint(self.job_id, "analysis_complete", final_output)

        return final_output

    async def _run_agent(
        self,
        agent: AnalysisAgentInterface,
        collector_output: CollectorOutput,
        context: dict
    ) -> BaseModel:
        """Run a single analysis agent with checkpoint"""
        analysis_type = agent.get_analysis_type()

        logger.info(f"Job {self.job_id}: Starting {analysis_type} analysis")

        # Execute agent
        result = await agent.analyze(collector_output, context)

        # Checkpoint
        checkpoint_key = f"analysis_{analysis_type}"
        save_checkpoint(self.job_id, checkpoint_key, result)

        # Publish progress
        publish_progress(self.job_id, checkpoint_key, "completed")

        logger.info(f"Job {self.job_id}: Completed {analysis_type} analysis")

        return result

    def _merge_results(self, results: Dict[str, BaseModel]) -> AnalysisOutput:
        """
        Merge individual analysis results into final AnalysisOutput.

        Implementation detail: Decided during implementation phase.
        """
        return AnalysisOutput(
            job_id=self.job_id,
            analyses=results,
            timestamp=datetime.utcnow()
        )
```

---

### Example: Schema Analysis Agent

```python
# src/agents/analysis/schema_analysis_agent.py

from pydantic import BaseModel, Field
from typing import List

class SchemaAnalysisOutput(BaseModel):
    """Output from schema analysis"""
    table_count: int
    relationships: List[dict]
    primary_keys: dict
    indexes: dict
    # ... other schema analysis results

class SchemaAnalysisAgent(AnalysisAgentInterface):
    """
    Analyzes database schema structure.

    Implementation note: This could be split into multiple sub-agents:
    - RelationshipAnalysisAgent
    - IndexAnalysisAgent
    - ConstraintAnalysisAgent

    Decision deferred to implementation phase.
    """

    def get_analysis_type(self) -> str:
        return "schema"

    def get_output_model(self) -> type[BaseModel]:
        return SchemaAnalysisOutput

    async def analyze(
        self,
        collector_output: CollectorOutput,
        context: dict
    ) -> SchemaAnalysisOutput:
        """
        Execute schema analysis.

        Internal implementation: TBD
        - Could use single Strands agent
        - Could use multiple sub-agents
        - Could use multi-phase approach

        Flexibility preserved for implementation phase.
        """
        # Implementation details decided later
        pass
```

---

### Example: Target-Specific Analysis Agent

```python
# src/agents/analysis/aurora_analysis_agent.py

from pydantic import BaseModel, Field
from typing import List

class AuroraRecommendation(BaseModel):
    """Aurora-specific recommendation"""
    feature: str = Field(description="Aurora feature name")
    description: str
    estimated_impact: str
    implementation_steps: List[str]

class AuroraAnalysisOutput(BaseModel):
    """Output from Aurora-specific analysis"""
    recommendations: List[AuroraRecommendation]
    compatibility_score: float = Field(ge=0.0, le=1.0)
    migration_complexity: str

class AuroraAnalysisAgent(AnalysisAgentInterface):
    """
    Analyzes Aurora-specific optimization opportunities.

    This is ONE example of a target-specific agent.
    Other targets (RDS, PostgreSQL, etc.) would have similar agents.
    """

    def get_analysis_type(self) -> str:
        return "aurora"

    def get_output_model(self) -> type[BaseModel]:
        return AuroraAnalysisOutput

    async def analyze(
        self,
        collector_output: CollectorOutput,
        context: dict
    ) -> AuroraAnalysisOutput:
        """
        Analyze Aurora-specific opportunities.

        Focuses on:
        - Aurora Serverless suitability
        - Read replica opportunities
        - Parallel query candidates
        - Fast cloning use cases
        """
        # Implementation details decided later
        pass
```

---

## Usage Example

```python
# Example: Setting up analysis for Aurora modernization

orchestrator = AnalysisOrchestrator(job_id)

# Register analysis agents (extensible!)
orchestrator.register_agent(SchemaAnalysisAgent())
orchestrator.register_agent(PerformanceAnalysisAgent())
orchestrator.register_agent(AuroraAnalysisAgent())  # Target-specific

# Execute all analyses (parallel)
context = {
    "target": "aurora",
    "source_engine": "mysql",
    "user_preferences": {...}
}

analysis_output = await orchestrator.analyze(collector_output, context)

# analysis_output contains:
# - schema analysis results
# - performance analysis results
# - Aurora-specific recommendations
```

---

## Extensibility Examples

### Adding New Analysis Type

```python
# Easy to add new analysis agents!

class CostAnalysisAgent(AnalysisAgentInterface):
    """Analyzes cost optimization opportunities"""

    def get_analysis_type(self) -> str:
        return "cost"

    def get_output_model(self) -> type[BaseModel]:
        return CostAnalysisOutput

    async def analyze(self, collector_output, context):
        # Implementation
        pass

# Register it
orchestrator.register_agent(CostAnalysisAgent())
```

### Adding New Target

```python
# Easy to add new target-specific agents!

class PostgreSQLAnalysisAgent(AnalysisAgentInterface):
    """PostgreSQL-specific analysis"""

    def get_analysis_type(self) -> str:
        return "postgresql"

    def get_output_model(self) -> type[BaseModel]:
        return PostgreSQLAnalysisOutput

    async def analyze(self, collector_output, context):
        # Implementation
        pass

# Register it
orchestrator.register_agent(PostgreSQLAnalysisAgent())
```

---

## Output Contract

### AnalysisOutput (Final Output)

```python
# src/contracts/models/analysis_output.py

from pydantic import BaseModel, Field
from typing import Dict, Any
from datetime import datetime

class AnalysisOutput(BaseModel):
    """
    Final output from Analysis Orchestrator.

    Contains results from all registered analysis agents.
    Consumed by Referee Agent for validation and report generation.
    """
    job_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Flexible: Contains outputs from all analysis agents
    # Key = analysis_type (e.g., "schema", "performance", "aurora")
    # Value = Agent-specific Pydantic model
    analyses: Dict[str, Any] = Field(
        description="Results from all analysis agents, keyed by analysis type"
    )

    # Summary metadata
    total_analyses: int = Field(description="Number of analysis agents executed")
    analysis_types: List[str] = Field(description="List of analysis types performed")
```

**Example structure:**

```python
{
    "job_id": "job-123",
    "timestamp": "2026-02-02T10:00:00Z",
    "analyses": {
        "schema": SchemaAnalysisOutput(...),
        "performance": PerformanceAnalysisOutput(...),
        "aurora": AuroraAnalysisOutput(...)
    },
    "total_analyses": 3,
    "analysis_types": ["schema", "performance", "aurora"]
}
```

---

## Design Principles

### 1. Extensibility Over Completeness

**Don't define everything upfront:**

- ✅ Define the pattern (interface, orchestration)
- ✅ Define example agents (schema, performance, target-specific)
- ❌ Don't lock into exact agent count
- ❌ Don't lock into agent splitting strategy

**Room for creativity:**

- Implementation team decides agent granularity
- Can split agents further if needed
- Can combine agents if simpler
- Can add new agent types easily

### 2. Target-Agnostic Core

**Core analysis agents** (schema, performance) are target-agnostic.

**Target-specific agents** (Aurora, RDS, PostgreSQL) are pluggable:

```python
# Aurora modernization
orchestrator.register_agent(AuroraAnalysisAgent())

# RDS optimization
orchestrator.register_agent(RDSAnalysisAgent())

# PostgreSQL migration
orchestrator.register_agent(PostgreSQLAnalysisAgent())
```

### 3. Parallel Execution by Default

Analysis agents run in parallel unless they have dependencies:

- ✅ Schema + Performance + Aurora = parallel (independent)
- ✅ Cost + Security = parallel (independent)
- ⚠️ If agent B needs agent A output = sequential (orchestrator handles)

### 4. Checkpoint Granularity

Each analysis agent checkpointed individually:

- Resume from any failed agent
- Don't re-run completed agents
- Progress visibility per agent

---

## Implementation Flexibility

### Example: Schema Analysis Could Be

**Option 1: Single Agent**

```python
class SchemaAnalysisAgent(AnalysisAgentInterface):
    """Does all schema analysis in one agent"""
    pass
```

**Option 2: Multiple Sub-Agents**

```python
class SchemaAnalysisOrchestrator(AnalysisAgentInterface):
    """Coordinates multiple schema sub-agents"""
    def __init__(self):
        self.sub_agents = [
            RelationshipAnalysisAgent(),
            IndexAnalysisAgent(),
            ConstraintAnalysisAgent()
        ]
```

**Option 3: Multi-Phase Agent**

```python
class SchemaAnalysisAgent(AnalysisAgentInterface):
    """Uses phases internally"""
    async def analyze(self, ...):
        phase1 = await self._analyze_relationships()
        phase2 = await self._analyze_indexes(phase1)
        return self._merge(phase1, phase2)
```

**Decision:** Made during implementation based on complexity and performance.

---

## Performance Considerations

### Large Database Optimization

For databases with 1000+ tables (using mini-collectors from ADR-005):

```python
# Analysis agents receive mini-collector outputs
# Can process them in parallel

if using_mini_collectors:
    # Option 1: Analyze each mini-collector output separately
    partial_analyses = await asyncio.gather(*[
        schema_agent.analyze(mini_output) for mini_output in mini_outputs
    ])
    final_analysis = merge_partial_analyses(partial_analyses)

    # Option 2: Merge mini-collector outputs first, then analyze
    merged_collector_output = merge_mini_outputs(mini_outputs)
    final_analysis = await schema_agent.analyze(merged_collector_output)
```

**Decision:** Made per analysis agent type during implementation.

---

## Consequences

### Positive

✅ **Extensible**: Easy to add new analysis agent types
✅ **Flexible**: Implementation details deferred
✅ **Target-agnostic**: Core agents work for any target
✅ **Parallel**: Agents run concurrently
✅ **Resumable**: Checkpoint per agent
✅ **Testable**: Each agent tested independently
✅ **Creative freedom**: Room for implementation innovation

### Negative

⚠️ **Abstraction overhead**: Interface adds complexity
⚠️ **Coordination needed**: Orchestrator must manage agents

### Neutral

🔶 **Agent count**: Decided during implementation
🔶 **Agent splitting**: Decided per analysis type
🔶 **Sub-agent architecture**: Decided per agent

---

## Alternatives Considered

### Alternative 1: Single Monolithic Analysis Agent (Rejected)

**Rejected because:**

- ❌ Not extensible (hard to add new analysis types)
- ❌ Target-locked (Aurora-only or RDS-only)
- ❌ Hard to test individual analysis concerns
- ❌ No parallel execution

### Alternative 2: Fixed Agent Set (Rejected)

**Rejected because:**

- ❌ Locks into specific agent count upfront
- ❌ Limits creativity during implementation
- ❌ Hard to adapt to new requirements

### Alternative 3: No Orchestrator (Rejected)

**Rejected because:**

- ❌ No coordination of multiple agents
- ❌ No checkpoint management
- ❌ No progress reporting
- ❌ Caller must manage agent lifecycle

---

## Related Documents

- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)
- [ADR-002: Structured Output with Pydantic](ADR-002-structured-output-and-validation.md)
- [ADR-005: Mini-Collectors for Large Databases](ADR-005-mini-collectors-for-large-databases.md)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-02 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**
