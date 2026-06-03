# Database Modernizer Assessment - Agent Contracts Specification

## Document Information

**Version:** 2.0.0
**Date:** February 2, 2026
**Status:** Draft
**Owner:** Database Modernizer Assessment Engineering Team

---

## Executive Summary

This document defines the formal contracts (interfaces) for all agents in the Database Modernizer Assessment system. Each contract is specified using **Pydantic models** to ensure type-safe, validated communication between agents developed by different teams.

**Purpose:**

- Provide strict interface definitions for agent inputs and outputs
- Enable independent development by multiple teams
- Support automated contract testing and validation
- Prevent integration failures through type-safe validation
- Leverage Pydantic for clear type hints and automatic validation

**Key Principles:**

1. **Contract-first development**: Define contracts before implementation
2. **Semantic versioning**: Version all contracts with MAJOR.MINOR (see ADR-008)
3. **Backward compatibility**: Never break existing consumers
4. **Pydantic validation**: All agents use Pydantic models for structured output
5. **Documentation as code**: Contracts are the source of truth

**Related ADRs:**

- [ADR-002: Structured Output with Pydantic](../architecture/decisions/ADR-002-structured-output-and-validation.md)
- [ADR-008: Contract Versioning](../architecture/decisions/ADR-008-contract-versioning.md)

---

## Table of Contents

1. [Contract Overview](#1-contract-overview)
2. [Versioning Strategy](#2-versioning-strategy)
3. [Collector Agent Contracts](#3-collector-agent-contracts)
4. [Analysis Agent Contracts](#4-analysis-agent-contracts)
5. [Referee Agent Contracts](#5-referee-agent-contracts)
6. [Schema Design Agent Contracts](#6-schema-design-agent-contracts)
7. [Contract Testing Framework](#7-contract-testing-framework)
8. [Breaking Change Policy](#8-breaking-change-policy)
9. [Implementation Guidelines](#9-implementation-guidelines)

---

## 1. Contract Overview

### 1.1 Agent Communication Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                     Job Initiation                               │
│  API Server → Step Functions Orchestrator                        │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│                  COLLECTOR AGENT                                 │
│  Input:  CollectorInputContract v1.0.0                           │
│  Output: CollectorOutputContract v1.0.0                          │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│              ANALYSIS AGENTS (Parallel Execution)                │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  DynamoDB Analysis Agent                                   │  │
│  │  Input:  AnalysisInputContract v1.0.0                      │  │
│  │  Output: AnalysisOutputContract v1.0.0                     │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  DocumentDB Analysis Agent                                 │  │
│  │  Input:  AnalysisInputContract v1.0.0                      │  │
│  │  Output: AnalysisOutputContract v1.0.0                     │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ... (5 more analysis agents with same contracts)                │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│                    REFEREE AGENT                                 │
│  Input:  RefereeInputContract v1.0.0                             │
│  Output: RefereeOutputContract v1.0.0                            │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              SCHEMA DESIGN AGENTS (Conditional)                 │
│  Input:  SchemaDesignInputContract v1.0.0                       │
│  Output: SchemaDesignOutputContract v1.0.0                      │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Contract Storage Location

All contracts are stored in the repository at:

```
src/contracts/               # Pydantic models (source of truth)
├── __init__.py
├── collector_output.py
├── analysis_output.py
├── referee_triage_output.py
├── referee_synthesis_output.py
├── assignment_output.py
├── schema_design_output.py
└── load_test_output.py

docs/contracts/
├── agent-contracts-spec.md  (this document)
└── schemas/                 # DEPRECATED (v1.x JSON Schema, reference only)
    └── README.md
```

**Note:** JSON Schema files in `schemas/` are deprecated as of v2.0. Use Pydantic models in `src/contracts/` instead.

---

## 2. Versioning Strategy

### 2.1 Semantic Versioning (MAJOR.MINOR)

All contracts follow **Semantic Versioning** with **MAJOR.MINOR format** (not PATCH):

- **MAJOR.MINOR** (e.g., 1.2)
  - **MAJOR**: Breaking changes (incompatible with previous version)
  - **MINOR**: New features (backward compatible additions)

**Why no PATCH?** Pydantic models handle bug fixes without version changes. PATCH is unnecessary.

**See:** [ADR-008: Contract Versioning](../architecture/decisions/ADR-008-contract-versioning.md)

### 2.2 Agent Version vs Contract Version

**Important Distinction:**

- **Agent Version** (e.g., `collector_version: "2.3.1"`): Version of agent code
- **Contract Version** (e.g., `contract_version: "1.2"`): Version of data structure

Multiple agent versions can produce the same contract version.

**Example:**

```python
# Agent v2.3.1 produces contract v1.2
output = CollectorOutput(
    collector_version="2.3.1",  # Agent version
    contract_version="1.2",      # Contract version
    ...
)

# Agent v2.4.0 also produces contract v1.2
```

### 2.3 Versioning Rules

| Change Type | Version Increment | Example |
|-------------|-------------------|---------|
| Add optional field | MINOR | 1.0 → 1.1 |
| Add new enum value | MINOR | 1.0 → 1.1 |
| Remove field | MAJOR | 1.2 → 2.0 |
| Rename field | MAJOR | 1.2 → 2.0 |
| Change field type | MAJOR | 1.2 → 2.0 |
| Make optional field required | MAJOR | 1.2 → 2.0 |

### 2.4 Contract Version Declaration

Each Pydantic model includes version fields:

```python
from pydantic import BaseModel, Field

class CollectorOutput(BaseModel):
    """
    Output contract for all Collector agents.

    Version history:
    - 1.0: Initial version
    - 1.1: Added query_patterns field (optional)
    - 1.2: Added aws_metadata field (optional)
    """
    # Version fields (REQUIRED)
    contract_version: str = Field(default="1.2", description="Contract version")
    collector_version: str = Field(description="Agent version that produced this output")

    # Data fields
    job_id: str
    database_metadata: dict
    schema: dict
    query_patterns: Optional[dict] = None  # Added in v1.1
    aws_metadata: Optional[dict] = None    # Added in v1.2
```

### 2.5 Backward Compatibility Policy

**Mandatory for MINOR versions:**

- Old agents must work with new contracts (backward compatibility)
- New agents must work with old contract versions (forward compatibility via Pydantic's `extra="ignore"`)
- Version adapters handle MAJOR version migrations

**See:** [ADR-008: Contract Versioning](../architecture/decisions/ADR-008-contract-versioning.md)

---

## 3. Collector Agent Contracts

### 3.1 Collector Input Contract

**Pydantic Model:** `models/collector_input.py` (to be created)

**Purpose:** Configuration for database collection

**Key Fields:**

```python
class CollectorInput(BaseModel):
    job_id: str
    source_database: SourceDatabase  # engine, endpoint, credentials
    collection_options: CollectionOptions  # PII anonymization, sample data
    aws_config: Optional[AWSConfig] = None  # RDS-specific config
```

**Supported Databases:**

- MySQL, PostgreSQL, MariaDB, SQL Server, Oracle, DB2
- RDS instance and cluster support
- AWS Secrets Manager integration for credentials

### 3.2 Collector Output Contract

**Pydantic Model:** `models/collector_output.py`

**Purpose:** Standardized database metadata and schema

**Example:**

```python
from pydantic import BaseModel, Field
from typing import Optional, Dict
from datetime import datetime

class CollectorOutput(BaseModel):
    """Output contract for all Collector agents"""

    # Version fields (REQUIRED)
    contract_version: str = Field(default="1.2")
    collector_version: str = Field(description="Agent version")

    # Core fields
    job_id: str
    database_metadata: Dict  # engine, version, size, table_count
    schema: Dict[str, Dict]  # tables, columns, indexes, constraints

    # Optional fields (v1.1+)
    query_patterns: Optional[Dict] = None
    aws_metadata: Optional[Dict] = None  # v1.2+

    timestamp: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        extra = "ignore"  # Forward compatibility
```

**Key Features:**

- Comprehensive metadata (collection timestamp, version, source database info)
- Schema information (tables, columns, indexes, foreign keys, sample data)
- Views, stored procedures, and triggers
- Query patterns with performance metrics
- RDS-specific metadata (instance class, storage type, Multi-AZ, Performance Insights)
- CloudWatch metrics

### 3.3 Using with Strands SDK

```python
from strands import Agent
from contracts.models.collector_output import CollectorOutput

collector = Agent(
    system_prompt="You are a MySQL collector agent...",
    tools=[connect_mysql, collect_schema],
    response_format=CollectorOutput  # Pydantic model
)

# Agent automatically validates output
output: CollectorOutput = collector(input_contract)
```

---

## 4. Analysis Agent Contracts

### 4.1 Analysis Input Contract

**Pydantic Model:** `models/analysis_input.py` (to be created)

**Purpose:** Configuration for analysis agents

**Key Fields:**

```python
class AnalysisInput(BaseModel):
    job_id: str
    collector_output: CollectorOutput  # Complete collector data
    target_database: str  # DynamoDB, DocumentDB, Aurora, etc.
    analysis_options: AnalysisOptions  # load testing, cost estimation
```

### 4.2 Analysis Output Contract

**Pydantic Model:** `models/analysis_output.py`

**Purpose:** Results from all analysis agents

**Example:**

```python
from pydantic import BaseModel, Field
from typing import Dict, Any, List
from datetime import datetime

class AnalysisOutput(BaseModel):
    """Output contract for Analysis Orchestrator"""

    # Version field
    contract_version: str = Field(default="1.0")

    # Core fields
    job_id: str
    analyses: Dict[str, Any]  # Results keyed by analysis type
    total_analyses: int
    analysis_types: List[str]  # ["schema", "performance", "aurora"]

    timestamp: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        extra = "ignore"
```

**Key Features:**

- Table recommendations with confidence scores
- Workload analysis (patterns and anti-patterns detected)
- Cost estimates with breakdown
- Optional load test results

---

## 5. Referee Agent Contracts

### 5.1 Referee Input Contract

**Pydantic Model:** `models/referee_input.py` (to be created)

**Purpose:** Aggregated analysis results for final recommendations

**Key Fields:**

```python
class RefereeInput(BaseModel):
    job_id: str
    collector_output: CollectorOutput
    analysis_outputs: List[AnalysisOutput]  # 1-7 analysis agents
    current_costs: Optional[float] = None  # For TCO comparison
```

### 5.2 Referee Output Contract (Modernization Report)

**Pydantic Model:** `models/modernization_report.py`

**Purpose:** Final modernization recommendations

**Example:**

```python
from pydantic import BaseModel, Field
from typing import List, Dict
from datetime import datetime

class Recommendation(BaseModel):
    recommendation_id: str
    title: str
    description: str
    category: str
    priority_tier: str  # high, medium, low
    group: str  # quick_wins, strategic, long_term
    estimated_impact: str
    effort: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    implementation_steps: List[str]
    dependencies: List[str] = []

class ModernizationReport(BaseModel):
    """Final modernization report (Referee output)"""

    # Version field
    contract_version: str = Field(default="1.0")

    # Core fields
    job_id: str
    executive_summary: ExecutiveSummary
    quick_wins: List[Recommendation]
    strategic: List[Recommendation]
    long_term: List[Recommendation]
    tco_analysis: TCOAnalysis
    risk_assessment: RiskAssessment

    # Metadata
    source_database: Dict
    target_platform: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        extra = "ignore"
```

**Key Features:**

- Recommended architecture (single database, multi-database, hybrid with cache)
- Table mappings to target databases
- TCO analysis with cost breakdown
- Risk assessment with mitigation strategies
- Prioritized recommendations (quick wins, strategic, long-term)

---

## 6. Schema Design Agent Contracts

### 6.1 Schema Design Input Contract

**Pydantic Model:** `models/schema_design_input.py` (to be created)

**Purpose:** Configuration for schema design agents

**Key Fields:**

```python
class SchemaDesignInput(BaseModel):
    job_id: str
    target_database: str  # DynamoDB, DocumentDB, etc.
    tables_to_design: List[str]
    collector_output: CollectorOutput
    analysis_output: AnalysisOutput
    design_options: DesignOptions  # IaC, SDK samples, single-table design
```

### 6.2 Schema Design Output Contract

**Pydantic Model:** `models/schema_design_output.py` (to be created)

**Purpose:** Generated schema designs and migration artifacts

**Key Fields:**

```python
class SchemaDesignOutput(BaseModel):
    contract_version: str = Field(default="1.0")
    job_id: str
    target_database: str
    schema_designs: Dict[str, SchemaDesign]  # Keyed by table name
    iac_templates: Optional[Dict[str, str]] = None  # CloudFormation templates
    sdk_samples: Optional[Dict[str, str]] = None  # Code samples
    migration_scripts: Optional[List[str]] = None

    class Config:
        extra = "ignore"
```

**Key Features:**

- Schema designs with transformations
- Access patterns
- Infrastructure-as-code (CloudFormation templates)
- SDK samples in multiple languages
- Documentation URLs

---

## 7. Contract Testing Framework

### 7.1 Test Strategy

All agents must pass contract tests before deployment. Tests validate:

1. Agent output conforms to contract schema
2. Required fields are present
3. Data types are correct
4. Enum values are valid
5. Nested objects follow contract structure

### 7.2 Contract Test Implementation

See implementation guidelines in section 9 for complete examples of:

- Base agent class with contract validation
- Contract test examples
- CI/CD integration
- Pre-commit hooks

---

## 8. Breaking Change Policy

### 8.1 Definition of Breaking Change

A breaking change is any modification to a contract that could cause existing agents to fail validation or behave incorrectly.

**Breaking changes include:**

- Removing a field
- Renaming a field
- Changing a field's data type
- Making an optional field required
- Removing an enum value
- Changing the structure of a nested object
- Changing array item types

**Non-breaking changes include:**

- Adding a new optional field
- Adding a new enum value
- Improving field descriptions
- Adding examples
- Fixing typos in documentation

### 8.2 Breaking Change Process

1. **Proposal**: Create issue describing breaking change and rationale
2. **Review**: Get approval from all agent teams affected
3. **Version Bump**: Increment MAJOR version (e.g., 1.x.x → 2.0.0)
4. **Transition Period**: Support both versions for 2 sprints (4 weeks)
5. **Deprecation Notice**: Add deprecation warnings to old version
6. **Migration Guide**: Provide migration guide for agent developers
7. **Remove Old Version**: After transition period, remove support for old version

---

## 9. Implementation Guidelines

### 9.1 Contract-Driven Development Workflow

**Step 1: Start with contracts**

- Review or define contracts before writing agent code
- Ensure contracts cover all required fields and data types

**Step 2: Write contract tests**

- Create tests that validate agent outputs against contracts
- Use jsonschema library for validation

**Step 3: Implement agent**

- Extend base agent class with contract validation
- Implement process() method that returns contract-compliant output

**Step 4: Run contract tests**

- Verify agent conforms to contracts
- Fix any validation errors

**Step 5: Integration testing**

- Test agent in orchestration workflow
- Verify end-to-end data flow

### 9.2 Base Agent Class Pattern

All agents should extend a base agent class that provides:

- Automatic input validation
- Automatic output validation
- Support for multiple contract versions
- Logging and error handling

See the original specification document for complete implementation examples.

---

## Appendices

### Appendix A: Contract Version History

| Contract | Version | Date | Changes |
|----------|---------|------|---------|
| CollectorOutput | 1.0.0 | 2026-01-22 | Initial release |
| CollectorInput | 1.0.0 | 2026-01-22 | Initial release |
| AnalysisOutput | 1.0.0 | 2026-01-22 | Initial release |
| AnalysisInput | 1.0.0 | 2026-01-22 | Initial release |
| RefereeOutput | 1.0.0 | 2026-01-22 | Initial release |
| RefereeInput | 1.0.0 | 2026-01-22 | Initial release |
| SchemaDesignOutput | 1.0.0 | 2026-01-22 | Initial release |
| SchemaDesignInput | 1.0.0 | 2026-01-22 | Initial release |

### Appendix B: Contract Review Checklist

Before approving a contract change:

- [ ] All required fields are clearly documented
- [ ] Data types are appropriate and consistent
- [ ] Enum values cover all necessary cases
- [ ] Descriptions are clear and unambiguous
- [ ] Examples are provided for complex structures
- [ ] Version is correctly incremented (MAJOR/MINOR/PATCH)
- [ ] Breaking changes are documented
- [ ] Migration guide exists (for breaking changes)
- [ ] All affected teams have reviewed and approved
- [ ] Contract tests are updated
- [ ] CI/CD pipeline validates new contract

### Appendix C: Common Contract Validation Errors

**Error: Missing required field**

```
ValidationError: 'metadata' is a required property
```

**Solution:** Ensure all required fields are present in output

**Error: Invalid data type**

```
ValidationError: 1000 is not of type 'string'
```

**Solution:** Check field data types match contract

**Error: Value not in enum**

```
ValidationError: 'mysql8' is not one of ['mysql', 'postgresql', ...]
```

**Solution:** Use only allowed enum values from contract

**Error: Additional properties not allowed**

```
ValidationError: Additional properties are not allowed ('extra_field' was unexpected)
```

**Solution:** Remove fields not defined in contract (or update contract to allow them)
