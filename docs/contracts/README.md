# Agent Contracts Documentation

This directory contains the formal contract specifications for all agents in the Database Modernizer system.

## 📚 Documentation Index

### Core Documents

| Document                                           | Purpose                                                      | Audience                      |
| -------------------------------------------------- | ------------------------------------------------------------ | ----------------------------- |
| [agent-contracts-spec.md](agent-contracts-spec.md) | Main specification with overview, versioning, and guidelines | All developers                |
| [QUICK_START.md](QUICK_START.md)                   | Quick reference for common tasks                             | Developers                    |
| [../../src/contracts/](../../src/contracts/)       | Pydantic model implementations (source code)                 | Developers, Tools             |
| [schemas/](schemas/)                               | JSON Schema definitions (reference only)                     | External tools, documentation |

## 🚀 Quick Start

### For Developers

1. **Read the spec**: Start with [agent-contracts-spec.md](agent-contracts-spec.md)
2. **Find your contract**: Check [../../src/contracts/](../../src/contracts/) directory
3. **Validate your code**: Use Pydantic models for validation
4. **Quick reference**: See [QUICK_START.md](QUICK_START.md)

### For Contributors

3. **Update contracts**: Follow guidelines in main spec
4. **Test changes**: Run contract validation tests

## 📋 Contract Overview

### Agent Types and Contracts

```
┌─────────────────────────────────────────────────────────────┐
│                    COLLECTOR AGENTS                         │
│  Input:  collector-input.json                               │
│  Output: collector-output.json                              │
│  Types:  MySQL, PostgreSQL, MariaDB                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ANALYSIS AGENTS                           │
│  Input:  analysis-input.json                                │
│  Output: analysis-output.json                               │
│  Types:  DynamoDB, DocumentDB, ElastiCache, OpenSearch,     │
│          Aurora PostgreSQL, Aurora MySQL                    │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    REFEREE AGENT                            │
│  Input:  referee-input.json                                 │
│  Output: referee-output.json                                │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                SCHEMA DESIGN AGENTS                         │
│  Input:  schema-design-input.json                           │
│  Output: schema-design-output.json                          │
└─────────────────────────────────────────────────────────────┘
```

### Contract Files

| Contract            | Source of Truth                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------- |
| Collector           | [src/contracts/collector_output.py](../../src/contracts/collector_output.py)                 |
| Analysis            | [src/contracts/analysis_output.py](../../src/contracts/analysis_output.py)                   |
| Referee (Triage)    | [src/contracts/referee_triage_output.py](../../src/contracts/referee_triage_output.py)       |
| Referee (Synthesis) | [src/contracts/referee_synthesis_output.py](../../src/contracts/referee_synthesis_output.py) |
| Assignment          | [src/contracts/assignment_output.py](../../src/contracts/assignment_output.py)               |
| Schema Design       | [src/contracts/schema_design_output.py](../../src/contracts/schema_design_output.py)         |
| Load Test           | [src/contracts/load_test_output.py](../../src/contracts/load_test_output.py)                 |

## 🔄 Current Status

**Version:** 2.0.0
**Last Updated:** January 22, 2026
**Status:** Modernized - Using Pydantic models

See [agent-contracts-spec.md](agent-contracts-spec.md) for detailed status.

## 📖 Key Concepts

### Contract-First Development

1. **Define contracts** before implementation
2. **Validate** all inputs and outputs
3. **Version** contracts semantically
4. **Test** contract compliance in CI/CD

### Semantic Versioning

- **MAJOR** (1.x.x → 2.0.0): Breaking changes
- **MINOR** (1.0.x → 1.1.0): New features (backward compatible)
- **PATCH** (1.0.0 → 1.0.1): Bug fixes (backward compatible)

### Validation

All agents must validate:

- ✅ Input conforms to input contract
- ✅ Output conforms to output contract
- ✅ Required fields are present
- ✅ Data types are correct
- ✅ Enum values are valid

## 🛠️ Tools and Usage

### Python Validation

```python
from src.contracts.collector_output import CollectorOutputContract
from pydantic import ValidationError

# Load and validate
try:
    output = CollectorOutputContract(**data)
    print("Validation successful!")
except ValidationError as e:
    print(f"Validation failed: {e}")
```

### Command Line Validation

```bash
# Using Pydantic models
python -c "from src.contracts.collector_output import CollectorOutputContract; CollectorOutputContract.model_validate_json(open('data.json').read())"
```

## 📝 Contributing

### Updating Contracts

1. Create issue describing change
2. Update Pydantic model file
3. Update main specification
4. Increment version appropriately
5. Run validation tests
6. Get team approval
7. Merge changes

### Breaking Changes

Breaking changes require:

- MAJOR version bump
- Migration guide
- 4-week transition period
- Team approval

See section 8 of [agent-contracts-spec.md](agent-contracts-spec.md) for complete policy.

## 🔗 Related Documentation

- **Architecture**: [../architecture/high-level-design.md](../architecture/high-level-design.md)
- **Data Specifications**: [../data-specs/](../data-specs/)

## ❓ Questions?

1. Check [QUICK_START.md](QUICK_START.md) for common tasks
2. Review [agent-contracts-spec.md](agent-contracts-spec.md) for detailed information
4. Open a GitHub issue for questions

---

**Last Updated:** June 2026
**Maintained By:** Database Modernizer Team
