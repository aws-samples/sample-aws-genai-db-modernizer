# ADR-011: Monorepo Structure for Database Modernizer Assessment

**Status:** Accepted
**Date:** February 4, 2026
**Deciders:** tebanieo, Database Modernizer Assessment Team
**Related:** ADR-002 (Structured Output with Pydantic), ADR-008 (Contract Versioning)

---

## Context

Database Modernizer Assessment consists of multiple components:

- Backend (Python): Agents, API server, orchestrator
- Frontend (TypeScript/React): React UI
- Contracts (Pydantic models): Shared data structures
- Infrastructure (CloudFormation/Docker): Deployment configurations
- Documentation: Design docs, specifications, guides

We need to decide on repository structure: monorepo vs multiple repositories.

## Decision

We will use a **monorepo structure** with all components in a single repository.

**Repository Structure:**

```
database-modernizer/
├── src/
│   ├── contracts/       # Pydantic models (shared)
│   ├── agents/          # Backend agents
│   ├── api/             # Backend API server
│   ├── orchestrator/    # Backend orchestration
│   ├── tools/           # Backend tools
│   └── ui/              # Frontend UI (React)
├── tests/
│   ├── contract/        # Contract validation tests
│   ├── unit/            # Unit tests
│   └── integration/     # Integration tests
├── infrastructure/
│   ├── cloudformation/  # AWS CloudFormation templates
│   └── docker/          # Docker configurations
├── docs/                # Documentation
├── scripts/             # Utility scripts
├── pyproject.toml       # Python dependencies
└── README.md            # Root README
```

## Rationale

### Why Monorepo?

1. **Single Source of Truth for Contracts**
   - Contracts in `src/contracts/` accessible by both backend and frontend
   - Atomic commits across frontend + backend when contracts change
   - No version synchronization issues between repos

2. **Simplified Customer Experience**
   - Single GitHub repository to clone
   - One command setup: `./scripts/setup_dev.sh`
   - No need to clone multiple repos
   - Easier to deploy

3. **Easier Development**
   - See full picture in one place
   - Shared documentation and specifications
   - Single CI/CD pipeline
   - Easier to onboard new developers

4. **Contract Synchronization**
   - Backend and frontend always use same contract version
   - No risk of frontend using outdated contracts
   - TypeScript types can be generated from Pydantic models in same repo

5. **Small Team Efficiency**
   - Team of 4-5 people can manage monorepo easily
   - Simpler than coordinating multiple repos
   - Faster iteration during Phase 0

### Why Not Multiple Repos?

**Considered but rejected:**

```
database-modernizer-backend/
database-modernizer-frontend/
database-modernizer-contracts/  # Would need to publish as package
database-modernizer-infra/
```

**Reasons for rejection:**

- Contract synchronization complexity
- Need to publish contracts as npm + pip packages
- More overhead for small team
- Harder for customers to get started
- Overkill for Phase 0

## Consequences

### Positive

- ✅ Simpler customer onboarding (one repo to clone)
- ✅ Contracts always in sync
- ✅ Atomic commits across stack
- ✅ Single CI/CD pipeline
- ✅ Easier for small team to manage
- ✅ Can generate TypeScript types from Pydantic models
- ✅ Shared documentation and specifications

### Negative

- ❌ Larger repository size (but manageable)
- ❌ Frontend and backend CI/CD run together (can be optimized with path filters)
- ❌ Need to be careful about dependencies (Python vs Node.js)

### Mitigation Strategies

**For CI/CD Performance:**

- Use GitHub Actions path filters:

  ```yaml
  on:
    push:
      paths:
        - 'src/api/**'
        - 'src/agents/**'
        # Only run backend tests when backend changes
  ```

**For Dependency Management:**

- Separate `pyproject.toml` (Python) and `package.json` (Node.js)
- Clear separation in `src/` directory
- Document which tools to use where

**For Future Scaling:**

- Structure allows easy split later if needed
- Clear boundaries between components
- Can extract contracts as package if team grows

## Implementation

### Contract Location

**Moved from:** `do../contracts/models/`
**Moved to:** `src/contracts/`

**Imports:**

```python
# Backend code
from src.contracts.collector_output import CollectorOutputContract

# Or with PYTHONPATH
from contracts.collector_output import CollectorOutputContract
```

**TypeScript Generation:**

```bash
# Generate TypeScript types from Pydantic models
python scripts/generate_types.py \
  --input src/contracts/ \
  --output src/ui/src/types/contracts.ts
```

### Documentation

**Keep in do../contracts/:**

- JSON schemas (for reference and external tools)
- Contract specification document
- Contract governance documentation

**Source code in src/contracts/:**

- Pydantic models (actual implementation)
- Used by backend and frontend (via TypeScript generation)

## Release Strategy

**Single GitHub Release:**

- Tag: `v1.0.0`
- Includes: Backend + Frontend + Infrastructure + Documentation
- Customers clone one repo and deploy everything

**Docker Images:**

- `database-modernizer/backend:v1.0.0`
- `database-modernizer/frontend:v1.0.0`
- `database-modernizer/worker:v1.0.0`

**Deployment:**

```bash
# Customer experience
git clone https://github.com/aws-samples/sample-aws-genai-db-modernizer.git
cd sample-aws-genai-db-modernizer
./scripts/setup_dev.sh  # Or deploy to AWS
```

## Review and Approval

- **Proposed by:** tebanieo
- **Reviewed by:** Database Modernizer Assessment Team
- **Approved by:** Architecture Team
- **Date:** February 4, 2026

## Future Considerations

### When to Split (If Ever)

Consider splitting into multiple repos if:

- Team grows to 10+ people with clear frontend/backend split
- Frontend and backend have different release cycles
- CI/CD becomes too slow (>15 minutes)
- Clear ownership boundaries emerge
- External teams want to contribute to only one component

### How to Split (If Needed)

1. Extract contracts as separate package
2. Publish to PyPI and npm
3. Split backend and frontend into separate repos
4. Each repo depends on contracts package

**But for Phase 0 and likely Phase 1, monorepo is the right choice.**

---

## References

- Release Management: `docs/RELEASE_MANAGEMENT.md`
- Contributing Guide: `CONTRIBUTING.md`

---

**Status:** Accepted
**Last Updated:** February 11, 2026
