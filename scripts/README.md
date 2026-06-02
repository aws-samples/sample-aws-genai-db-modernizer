# Scripts

This directory contains utility scripts for development and deployment.

## Quick Start

### setup_dev.sh (Recommended for New Contributors)

Automated development environment setup script.

**Usage:**

```bash
# Run from repository root
./scripts/setup_dev.sh
```

**What it does:**

1. Checks Python 3.12+ installation
2. Installs uv (if not present)
3. Checks AWS CLI configuration
4. Creates Python virtual environment
5. Installs all dependencies (production + development)
6. Configures git commit template
7. Installs pre-commit hooks
8. Runs initial validation
9. Optionally runs test suite

**First time setup:**

```bash
git clone https://github.com/aws/database-modernizer.git
cd database-modernizer
./scripts/setup_dev.sh
```

## Available Scripts

### Development Scripts

#### validate_schemas.py

Validates JSON schema files for correctness.

**Usage:**

```bash
# Validate specific schema files
python scripts/validate_schemas.py docs/03-contracts/schemas/v1/collector/collector-output.json

# Validate all schemas in a directory
python scripts/validate_schemas.py docs/03-contracts/schemas/v1/**/*.json

# Run via pre-commit (automatic)
git commit  # Pre-commit hook runs validation automatically
```

**What it checks:**

- Valid JSON syntax
- Conforms to JSON Schema Draft 07
- Has required metadata fields (title, version, description, type)
- Version follows semantic versioning (MAJOR.MINOR.PATCH)

## Planned Scripts

- [ ] **validate_all_schemas.py** - Batch validate all schemas
- [ ] **generate_types.py** - Generate Python/TypeScript types from schemas
- [ ] **test_contract_examples.py** - Test example data against contracts
- [ ] **setup_dev.sh** - Set up local development environment
- [ ] **run_tests.sh** - Run test suites
- [ ] **deploy.sh** - Deploy to AWS
- [ ] **seed_data.sh** - Seed test data

## Pre-commit Integration

The `validate_schemas.py` script is integrated with pre-commit hooks and runs automatically on JSON files in the schemas directory.

## Adding New Scripts

When adding new scripts:

1. Create script in `scripts/` directory
2. Add shebang line: `#!/usr/bin/env python3` or `#!/bin/bash`
3. Make executable: `chmod +x scripts/your-script.py`
4. Add documentation to this README
5. Add to pre-commit config if applicable
6. Add tests in `tests/scripts/`

---

**Last Updated:** January 22, 2026
