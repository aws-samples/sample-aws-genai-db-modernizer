# Contract Specification Quick Start

## For Developers

### Reading Contracts

1. **Start here**: [agent-contracts-spec.md](agent-contracts-spec.md) - Overview and guidelines
2. **Pydantic models**: `src/contracts/` directory - Type-safe contract definitions (source of truth)
3. **Deprecated schemas**: `schemas/` directory - Old JSON schemas (deprecated in v2.0, kept for reference only)

### Using Contracts in Code

```python
# Example: Validating collector output with Pydantic
from contracts.models.collector_output import CollectorOutput

# Create and validate output (automatic validation)
output = CollectorOutput(
    collector_version="2.3.1",
    contract_version="1.2",
    job_id="job-123",
    database_metadata={"engine": "mysql", "version": "8.0.32"},
    schema={"users": {"columns": ["id", "name"]}}
)

# Or validate from dict
data = {...}
output = CollectorOutput(**data)  # Raises ValidationError if invalid

# Or from JSON
output = CollectorOutput.model_validate_json(json_string)

print("✓ Output conforms to contract")
```

### Using with Strands SDK

```python
from strands import Agent
from contracts.models.collector_output import CollectorOutput

# Agent automatically validates output
collector = Agent(
    system_prompt="You are a MySQL collector...",
    tools=[connect_mysql, collect_schema],
    response_format=CollectorOutput  # Pydantic model
)

output: CollectorOutput = collector(input_data)
# Output is guaranteed to match contract
```

### Contract Versions

Current contract versions:

- **Collector Output**: 1.2
- **Analysis Output**: 1.0
- **Modernization Report**: 1.0

Version format: **MAJOR.MINOR** (no PATCH)

- Breaking changes → Increment MAJOR version (1.2 → 2.0)
- New features → Increment MINOR version (1.2 → 1.3)

**See:** [ADR-008: Contract Versioning](../architecture/decisions/ADR-008-contract-versioning.md)

## For Contract Maintainers

### Updating Contracts

1. **Propose change** - Create issue describing the change
2. **Update Pydantic model** - Modify model file in `src/contracts/`
3. **Update version** - Increment version number appropriately
4. **Update spec** - Update main specification document
5. **Test** - Run contract validation tests
6. **Review** - Get approval from affected teams
7. **Merge** - Merge changes to main branch

### Breaking Changes

Breaking changes require:

- MAJOR version increment (e.g., 1.2 → 2.0)
- Migration guide
- Version adapter for backward compatibility
- Approval from all affected teams

See [agent-contracts-spec.md](agent-contracts-spec.md) section 8 for complete breaking change policy.

## Key Concepts

### Contract Types

1. **Input Contracts** - Define what data agents receive
2. **Output Contracts** - Define what data agents produce (Pydantic models)

### Agent Types

1. **Collector Agents** - Collect data from source databases
2. **Analysis Agents** - Analyze data for specific target databases (multi-agent category)
3. **Referee Agent** - Orchestrates analysis and produces final report
4. **Schema Design Agents** - Generate detailed schema designs

### Data Flow

```
Collector → Analysis Agents (parallel) → Referee → Schema Design (optional)
```

Each arrow represents a contract:

- Collector outputs → Analysis inputs
- Analysis outputs → Referee input
- Referee output → Schema Design input

## Common Tasks

### Validate Agent Output

```python
from pydantic import ValidationError
from contracts.models.collector_output import CollectorOutput

try:
    output = CollectorOutput(**data)
    print("✓ Valid!")
except ValidationError as e:
    print(f"✗ Validation failed: {e}")
```

### Check Contract Version

```python
from contracts.models.collector_output import CollectorOutput

# Version is in the model
print(f"Contract version: {CollectorOutput.model_fields['contract_version'].default}")
```

### Find Contract Changes

```bash
# View contract history
git log --oneline -- src/contracts/
```

## Resources

- **Main Spec**: [agent-contracts-spec.md](agent-contracts-spec.md)
- **Pydantic Models**: [src/contracts/](../../src/contracts/)
- **Deprecated Schemas**: [schemas/README.md](schemas/README.md)
- **ADR-002**: [Structured Output with Pydantic](../architecture/decisions/ADR-002-structured-output-and-validation.md)
- **ADR-008**: [Contract Versioning](../architecture/decisions/ADR-008-contract-versioning.md)

## Questions?

1. Check the main specification document
2. Review the Pydantic models README
3. Consult ADR-002 and ADR-008
4. Open a GitHub issue for questions
