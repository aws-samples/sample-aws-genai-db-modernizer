# ADR-008: Contract Versioning Strategy

**Status:** Accepted
**Date:** 2026-02-02
**Deciders:** Architecture Team
**Related ADRs:** ADR-002 (Pydantic Output)

---

## Context

Database Modernizer Assessment uses contracts (Pydantic models) to define input/output between agents. As the system evolves:

- **New fields** may be added to contracts (new analysis types, additional metadata)
- **Field types** may change (string → enum, optional → required)
- **Breaking changes** may be necessary (rename fields, restructure data)
- **Multiple versions** may coexist (old jobs resuming, new jobs starting)

### Key Distinction: Agent Version vs Contract Version

**Agent Version** (e.g., `collector_version: "2.3.1"`):

- Version of the agent code itself
- Can change frequently (bug fixes, optimizations)
- Multiple agent versions can produce the same contract version

**Contract Version** (e.g., `contract_version: "1.2"`):

- Version of the input/output data structure
- Changes only when contract structure changes
- Decoupled from agent version

**Example:**

```python
# Agent v2.3.1 produces contract v1.2
output = CollectorOutput(
    collector_version="2.3.1",  # Agent version
    contract_version="1.2",      # Contract version
    ...
)

# Agent v2.4.0 also produces contract v1.2 (no contract change)
output = CollectorOutput(
    collector_version="2.4.0",  # Different agent version
    contract_version="1.2",      # Same contract version
    ...
)
```

### Audience: Internal Development

**Contract versioning is primarily for internal development:**

- ✅ **Developers**: Understand contract compatibility when making changes
- ✅ **Testing**: Test contract migrations and compatibility
- ✅ **Debugging**: Identify which contract version caused issues
- ❌ **End users**: Don't need to know contract versions

**Users see:**

- Deployment version (e.g., "Database Modernizer Assessment v1.5.0")
- Release notes (features, bug fixes)

**Users don't see:**

- Contract versions (internal implementation detail)
- Agent versions (internal implementation detail)

**Why internal?**

- Contract versions are implementation details
- Users care about features, not data structure versions
- Simplifies user experience (one version number to track)

### Deployment Model: Single Version at a Time

**Important:** Database Modernizer Assessment deploys as a **single version** (not multiple versions simultaneously).

- ✅ One agent version deployed at a time
- ✅ All agents in deployment use same codebase version
- ✅ User doesn't choose agent versions (deployment is atomic)
- ❌ No mixing agent versions in same deployment

**Why single version?**

- Simpler deployment (no version routing)
- Easier testing (one version to test)
- Clearer support (one version to debug)

### Version Mismatch Scenarios

**Scenario 1: Job Resume After Upgrade (Common)**

```
1. Job starts with deployment v1.0 (contract v1.0)
2. Job checkpoints at collector stage
3. Deployment upgraded to v1.1 (contract v1.1)
4. Job resumes with v1.1 code
   → Contract adapter handles v1.0 → v1.1 migration
```

**Scenario 2: Rollback (Rare)**

```
1. Job starts with deployment v1.1 (contract v1.1)
2. Job checkpoints at collector stage
3. Deployment rolled back to v1.0 (contract v1.0)
4. Job resumes with v1.0 code
   → Pydantic ignores unknown fields (forward compatibility)
```

**Scenario 3: No Mismatch (Most Common)**

```
1. Job starts with deployment v1.1
2. Job completes with deployment v1.1
   → No version mismatch
```

### Requirements

- **Backward compatibility**: Old checkpoints can resume with new code (Scenario 1)
- **Forward compatibility**: New checkpoints can be read by old code (Scenario 2)
- **Version detection**: System knows which contract version it's working with
- **Migration path**: Clear upgrade path for breaking changes
- **Minimal overhead**: Versioning shouldn't complicate simple changes

---

## Decision

We will implement **Semantic Versioning for Contracts** with:

1. **Version field in all contracts** (e.g., `contract_version: "1.0"`)
2. **Separate agent version field** (e.g., `collector_version: "2.3.1"`)
3. **Semantic versioning** (MAJOR.MINOR format for contracts)
   - MAJOR: Breaking changes (incompatible)
   - MINOR: Backward-compatible additions
4. **Version adapters** for MAJOR version migrations
5. **Optional fields by default** (prefer additions over changes)
6. **Deprecation warnings** (not immediate removal)
7. **Single deployment version** (no multi-version deployments)

### Agent Version Support Requirements

**Every agent MUST:**

- ✅ Include `contract_version` field in output
- ✅ Include agent-specific version field (e.g., `collector_version`)
- ✅ Support reading previous contract versions (via adapters)
- ✅ Produce current contract version in output

**Example:**

```python
class MySQLCollectorAgent:
    """MySQL Collector Agent"""

    AGENT_VERSION = "2.3.1"      # Agent code version
    CONTRACT_VERSION = "1.2"     # Contract version this agent produces

    def collect(self, input_contract: dict) -> CollectorOutput:
        """
        Collect database metadata.

        Returns CollectorOutput with version fields.
        """
        output = CollectorOutput(
            collector_version=self.AGENT_VERSION,    # Agent version
            contract_version=self.CONTRACT_VERSION,  # Contract version
            job_id=input_contract['job_id'],
            ...
        )
        return output
```

---

## Deployment Model

### Single Version Deployment

**Database Modernizer Assessment deploys as a single version:**

```
┌─────────────────────────────────────────────────────────┐
│         Database Modernizer Assessment Deployment v1.1             │
│                                                         │
│  All agents use same codebase version:                  │
│  - Collector agents: v1.1 code                          │
│  - Analysis agents: v1.1 code                           │
│  - Referee agent: v1.1 code                             │
│                                                         │
│  All produce same contract versions:                    │
│  - CollectorOutput: contract v1.2                       │
│  - AnalysisOutput: contract v1.0                        │
│  - ModernizationReport: contract v1.0                   │
└─────────────────────────────────────────────────────────┘
```

**User does NOT choose agent versions:**

- Deployment is atomic (all agents upgraded together)
- No version routing or selection
- Simpler deployment and testing

### Version Mismatch Scenarios

**When do versions mismatch?**

Only during **job resume after deployment upgrade/rollback**.

#### Scenario 1: Job Resume After Upgrade (Common)

```
Timeline:
─────────────────────────────────────────────────────────
Day 1: Deployment v1.0 (contract v1.0)
  ├─ Job-123 starts
  ├─ Collector completes → checkpoint saved (contract v1.0)
  └─ Job paused (waiting for user input)

Day 2: Deployment upgraded to v1.1 (contract v1.1)
  ├─ Job-123 resumes
  ├─ Load checkpoint (contract v1.0)
  ├─ Adapter migrates v1.0 → v1.1
  └─ Analysis continues with v1.1 code
```

**How handled:**

- Checkpoint loader detects contract v1.0
- Adapter migrates v1.0 → v1.1
- Job continues with v1.1 code

#### Scenario 2: Rollback (Rare)

```
Timeline:
─────────────────────────────────────────────────────────
Day 1: Deployment v1.1 (contract v1.1)
  ├─ Job-456 starts
  ├─ Collector completes → checkpoint saved (contract v1.1)
  └─ Job paused

Day 2: Deployment rolled back to v1.0 (contract v1.0)
  ├─ Job-456 resumes
  ├─ Load checkpoint (contract v1.1)
  ├─ Pydantic ignores unknown fields (forward compatibility)
  └─ Job continues with v1.0 code (graceful degradation)
```

**How handled:**

- Checkpoint loader reads contract v1.1
- Pydantic ignores unknown fields (v1.1 additions)
- Job continues with v1.0 code (some data lost, but functional)

#### Scenario 3: No Mismatch (Most Common)

```
Timeline:
─────────────────────────────────────────────────────────
Day 1: Deployment v1.1 (contract v1.1)
  ├─ Job-789 starts
  ├─ Collector completes → checkpoint saved (contract v1.1)
  ├─ Analysis completes → checkpoint saved (contract v1.1)
  └─ Job completes (same deployment version)
```

**No version handling needed** (same version throughout).

### Preventing Version Mismatches

**Best practices:**

1. **Minimize breaking changes** (use optional fields)
2. **Test upgrades** with in-progress jobs
3. **Graceful degradation** (forward compatibility)
4. **Clear migration path** (adapters for MAJOR versions)

---

## Architecture

### Contract Versioning Pattern

```python
# src/contracts/models/collector_output.py

from pydantic import BaseModel, Field
from typing import Optional

class CollectorOutput(BaseModel):
    """
    Collector output contract.

    Version history:
    - 1.0: Initial version
    - 1.1: Added query_patterns field (optional)
    - 1.2: Added aws_metadata field (optional)
    """
    # Version fields (REQUIRED)
    contract_version: str = Field(default="1.2", description="Contract version")
    collector_version: str = Field(description="Agent version that produced this output")

    # Core fields (v1.0)
    job_id: str
    database_metadata: dict
    schema: dict

    # Added in v1.1 (optional for backward compatibility)
    query_patterns: Optional[dict] = Field(default=None)

    # Added in v1.2 (optional for backward compatibility)
    aws_metadata: Optional[dict] = Field(default=None)
```

### Agent Implementation Pattern

```python
# src/agents/collector/mysql_collector.py

from strands import Agent
from contracts.models.collector_output import CollectorOutput

class MySQLCollectorAgent:
    """
    MySQL Collector Agent.

    Agent version: 2.3.1
    Produces contract version: 1.2
    Supports reading contract versions: 1.0, 1.1, 1.2
    """

    AGENT_VERSION = "2.3.1"      # This agent's code version
    CONTRACT_VERSION = "1.2"     # Contract version this agent produces

    def __init__(self, input_contract: dict):
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']

    def collect(self) -> CollectorOutput:
        """
        Collect database metadata.

        Returns:
            CollectorOutput with version fields populated
        """
        # ... collection logic ...

        output = CollectorOutput(
            # Version fields (REQUIRED)
            collector_version=self.AGENT_VERSION,    # Agent version
            contract_version=self.CONTRACT_VERSION,  # Contract version

            # Data fields
            job_id=self.job_id,
            database_metadata={...},
            schema={...},
            query_patterns={...},  # v1.1+ field
            aws_metadata={...}     # v1.2+ field
        )

        return output
```

---

## Versioning Rules

### Rule 1: Increment MINOR for Backward-Compatible Changes

**Backward-compatible changes (MINOR bump):**

- ✅ Add optional field
- ✅ Add new enum value
- ✅ Expand field type (int → float)
- ✅ Add new nested object (optional)

**Example:**

```python
# v1.0 → v1.1: Add optional field
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.1")
    job_id: str
    schema: dict
    query_patterns: Optional[dict] = None  # NEW (optional)
```

### Rule 2: Increment MAJOR for Breaking Changes

**Breaking changes (MAJOR bump):**

- ❌ Remove field
- ❌ Rename field
- ❌ Change field type (incompatible)
- ❌ Make optional field required
- ❌ Change field semantics

**Example:**

```python
# v1.2 → v2.0: Rename field (breaking)
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="2.0")
    job_id: str
    database_schema: dict  # RENAMED from 'schema' (breaking)
```

### Rule 3: Use Optional Fields for Additions

**Prefer optional fields for new additions:**

```python
# Good: Optional field (backward compatible)
new_field: Optional[str] = None

# Avoid: Required field (breaking change)
new_field: str  # Forces MAJOR version bump
```

### Rule 4: Deprecate Before Removing

**Deprecation process:**

1. Mark field as deprecated (add comment)
2. Add deprecation warning in code
3. Wait 1+ major version
4. Remove in next MAJOR version

```python
# v1.3: Deprecate field
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.3")

    # Deprecated in v1.3, will be removed in v2.0
    # Use 'database_schema' instead
    schema: Optional[dict] = Field(
        default=None,
        deprecated=True,
        description="DEPRECATED: Use database_schema instead"
    )

    database_schema: Optional[dict] = None  # Replacement field
```

---

## Version Adapters

### Adapter Pattern for MAJOR Version Migrations

```python
# src/contracts/adapters/collector_output_adapter.py

from typing import Dict, Any

class CollectorOutputAdapter:
    """
    Adapts CollectorOutput between versions.
    """

    @staticmethod
    def adapt(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Adapt collector output to latest version.

        Args:
            data: Raw collector output data

        Returns:
            Adapted data (latest version)
        """
        version = data.get("contract_version", "1.0")

        # v1.x → v2.0 migration
        if version.startswith("1."):
            data = CollectorOutputAdapter._migrate_v1_to_v2(data)

        return data

    @staticmethod
    def _migrate_v1_to_v2(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Migrate v1.x to v2.0.

        Changes:
        - Rename 'schema' → 'database_schema'
        """
        if "schema" in data and "database_schema" not in data:
            data["database_schema"] = data.pop("schema")

        data["contract_version"] = "2.0"
        return data
```

### Using Adapters

```python
# Load checkpoint with version adaptation
def load_checkpoint(job_id: str, checkpoint_key: str) -> Any:
    """
    Load checkpoint with automatic version adaptation.
    """
    raw_data = storage.load(job_id, checkpoint_key)

    # Detect contract type
    if "collector_version" in raw_data:
        # CollectorOutput
        adapted_data = CollectorOutputAdapter.adapt(raw_data)
        return CollectorOutput(**adapted_data)

    elif "analyses" in raw_data:
        # AnalysisOutput
        adapted_data = AnalysisOutputAdapter.adapt(raw_data)
        return AnalysisOutput(**adapted_data)

    # ... other contract types
```

---

## Version Detection

### Automatic Version Detection

```python
# src/contracts/version_detector.py

def detect_contract_version(data: Dict[str, Any]) -> str:
    """
    Detect contract version from data.

    Args:
        data: Raw contract data

    Returns:
        Version string (e.g., "1.2")
    """
    # Explicit version field (preferred)
    if "contract_version" in data:
        return data["contract_version"]

    # Fallback: Infer from fields (for old contracts without version field)
    if "query_patterns" in data:
        return "1.1"  # Has query_patterns → v1.1+

    return "1.0"  # Default to v1.0
```

---

## Checkpoint Compatibility

### Backward Compatibility (Old Checkpoints, New Code)

**Scenario:** Job started with v1.0, resumed with v1.2 code

```python
# Checkpoint saved with v1.0 (no query_patterns field)
checkpoint_data = {
    "contract_version": "1.0",
    "job_id": "job-123",
    "schema": {...}
    # No query_patterns field
}

# Load with v1.2 code (has optional query_patterns field)
output = CollectorOutput(**checkpoint_data)
# Works! query_patterns defaults to None
```

### Forward Compatibility (New Checkpoints, Old Code)

**Scenario:** Job started with v1.2, resumed with v1.0 code

```python
# Checkpoint saved with v1.2 (has query_patterns field)
checkpoint_data = {
    "contract_version": "1.2",
    "job_id": "job-123",
    "schema": {...},
    "query_patterns": {...}  # New field
}

# Load with v1.0 code (doesn't know about query_patterns)
# Pydantic ignores unknown fields by default
output = CollectorOutput(**checkpoint_data)
# Works! query_patterns ignored (graceful degradation)
```

**Note:** Pydantic's `extra="ignore"` allows forward compatibility.

---

## Implementation Guidelines

### Adding New Field (MINOR Version Bump)

```python
# Step 1: Add optional field
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.3")  # Bump MINOR

    # Existing fields
    job_id: str
    schema: dict

    # New field (optional)
    performance_metrics: Optional[dict] = None  # NEW in v1.3
```

### Breaking Change (MAJOR Version Bump)

```python
# Step 1: Deprecate old field in v1.x
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.4")

    schema: Optional[dict] = Field(
        default=None,
        deprecated=True,
        description="DEPRECATED: Use database_schema"
    )
    database_schema: Optional[dict] = None  # Replacement

# Step 2: Create v2.0 with breaking change
class CollectorOutputV2(BaseModel):
    contract_version: str = Field(default="2.0")

    # 'schema' removed, only 'database_schema' remains
    database_schema: dict  # Now required

# Step 3: Create adapter
class CollectorOutputAdapter:
    @staticmethod
    def _migrate_v1_to_v2(data: Dict) -> Dict:
        if "schema" in data:
            data["database_schema"] = data.pop("schema")
        data["contract_version"] = "2.0"
        return data
```

---

## Contract Version Matrix

| Contract | Current Version | Agent Versions Producing It | Breaking Changes | Next MAJOR |
|----------|----------------|------------------------------|------------------|------------|
| CollectorOutput | 1.2 | 2.3.x, 2.4.x | None planned | 2.0 (TBD) |
| AnalysisOutput | 1.0 | 1.0.x, 1.1.x | None planned | 2.0 (TBD) |
| ModernizationReport | 1.0 | 1.0.x | None planned | 2.0 (TBD) |

**Key Points:**

- Multiple agent versions can produce same contract version
- Agent version changes frequently (bug fixes, optimizations)
- Contract version changes only when structure changes
- Deployment uses single agent version (all agents same version)

---

## Consequences

### Positive

✅ **Backward compatible**: Old checkpoints work with new code
✅ **Forward compatible**: New checkpoints degrade gracefully with old code
✅ **Clear versioning**: Semantic versioning easy to understand
✅ **Migration path**: Adapters handle MAJOR version changes
✅ **Minimal overhead**: Optional fields keep changes simple
✅ **Single deployment**: No multi-version complexity
✅ **Agent version tracking**: Know which agent produced output

### Negative

⚠️ **Adapter maintenance**: Need to maintain migration code
⚠️ **Version sprawl**: Multiple versions coexist (during job resume)
⚠️ **Testing complexity**: Test across versions
⚠️ **Rollback limitations**: Forward compatibility may lose data

### Neutral

🔶 **MAJOR versions rare**: Most changes are MINOR (optional fields)
🔶 **Deprecation period**: 1+ major version before removal
🔶 **Version detection**: Automatic from contract_version field
🔶 **Single deployment**: User doesn't choose agent versions
🔶 **Version mismatch**: Only during job resume after upgrade/rollback

---

## Alternatives Considered

### Alternative 1: No Versioning (Rejected)

**Rejected because:**

- ❌ Breaking changes break old checkpoints
- ❌ No migration path
- ❌ Can't evolve contracts safely

### Alternative 2: Full Semantic Versioning (MAJOR.MINOR.PATCH) (Rejected)

**Rejected because:**

- ❌ PATCH not needed (Pydantic handles bug fixes)
- ❌ Unnecessary complexity
- ❌ MAJOR.MINOR sufficient

### Alternative 3: Separate Contract Versions (v1/, v2/) (Rejected)

**Rejected because:**

- ❌ Code duplication (v1 and v2 contracts)
- ❌ Hard to maintain multiple versions
- ❌ Adapters simpler than parallel versions

---

## Release Management (Out of Scope)

**This ADR does NOT cover:**

- ❌ Release versioning (e.g., v1.5.0, v2.0.0)
- ❌ Release tagging strategy (git tags)
- ❌ Cherry-picking process
- ❌ Hotfix procedures
- ❌ Release notes format

**Why out of scope?**

- Contract versioning is internal (data structure compatibility)
- Release management is external (user-facing versions)
- Different concerns, different audiences

**Future ADR needed:**

- ADR-0XX: Release Management and Versioning Strategy
  - Release version format (semantic versioning for releases)
  - Git tagging strategy (e.g., `v1.5.0`, `v2.0.0`)
  - Branch strategy (mainline, release branches, hotfix branches)
  - Cherry-picking policy (when and how)
  - Release notes format
  - Upgrade path for users

**Relationship to contract versioning:**

```
Release v1.5.0 (user-facing)
  ├─ Agent versions: 2.3.1, 1.1.0, 1.0.5 (internal)
  └─ Contract versions: 1.2, 1.0, 1.0 (internal)

Release v2.0.0 (user-facing, breaking changes)
  ├─ Agent versions: 3.0.0, 2.0.0, 2.0.0 (internal)
  └─ Contract versions: 2.0, 2.0, 2.0 (internal, breaking)
```

**Key point:** Release versions are user-facing, contract versions are internal.

---

## Related Documents

- [ADR-002: Structured Output with Pydantic](ADR-002-structured-output-and-validation.md)
- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-02 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**
