# Contract Validation Guide

Contracts are Pydantic models that define data structures exchanged between agents. Validation runs automatically on commit via pre-commit hooks and in CI/CD pipelines.

## Writing Contracts

Output contracts must include `contract_version`, `job_id`, and descriptions on all fields:

```python
from pydantic import BaseModel, Field

class MyContract(BaseModel):
    contract_version: str = Field(
        default="1.0",
        pattern=r"^\d+\.\d+$",
        description="Contract version"
    )
    job_id: str = Field(..., description="Unique job identifier")
    # Add fields with descriptions
```

### Enums and Validation

```python
from enum import Enum
from pydantic import field_validator

class DatabaseEngine(str, Enum):
    mysql = "mysql"
    postgresql = "postgresql"

class RDSMetadata(BaseModel):
    engine: DatabaseEngine = Field(..., description="Database engine")
    retention_days: int | None = Field(None, ge=0, le=35, description="Backup retention")
    monitoring_interval: int | None = Field(None, description="Monitoring interval")

    @field_validator("monitoring_interval")
    @classmethod
    def validate_interval(cls, v):
        if v is not None and v not in [0, 1, 5, 10, 15, 30, 60]:
            raise ValueError("Must be one of: 0, 1, 5, 10, 15, 30, 60")
        return v
```

### Naming Rules

- Use `snake_case`
- Avoid BaseModel attribute names (`schema` → `database_schema`)
- Include units in names (`execution_time_ms`, `size_gb`)

### Versioning

Increment MAJOR version on breaking changes:

```python
# Version 2.0 → 3.0 (renamed field)
class CollectorOutputContract(BaseModel):
    contract_version: str = Field(default="3.0", ...)
    database_schema: Schema = Field(...)  # was "schema"
```

## Running Validation

### Manual

```bash
# All contracts
python scripts/validate_contracts.py src/contracts/*.py

# Specific contract
python scripts/validate_contracts.py src/contracts/collector_output.py
```

### Pre-Commit Hook

Install once:

```bash
pip install pre-commit
pre-commit install
```

Runs automatically on commit. To run manually:

```bash
pre-commit run --all-files
pre-commit run validate-pydantic-contracts --files src/contracts/collector_output.py
```

Bypass (use sparingly):

```bash
git commit --no-verify -m "WIP: Incomplete changes"
```

## Writing Tests

### Unit Tests

```python
import pytest
from pydantic import ValidationError
from src.contracts.collector_output import CollectorOutputContract

def test_valid_contract(valid_collector_output_data):
    contract = CollectorOutputContract(**valid_collector_output_data)
    assert contract.contract_version == "3.0"

def test_missing_required_field():
    with pytest.raises(ValidationError) as exc:
        CollectorOutputContract(contract_version="3.0")  # Missing job_id
    assert "job_id" in str(exc.value)
```

### Property-Based Tests

```python
from hypothesis import given, strategies as st

@given(st.builds(CollectorOutputContract, ...))
def test_serialization_round_trip(contract):
    json_str = contract.model_dump_json()
    deserialized = CollectorOutputContract.model_validate_json(json_str)
    assert deserialized == contract
```

### Fixtures (conftest.py)

```python
@pytest.fixture
def valid_collector_output_data():
    return {
        "contract_version": "3.0",
        "job_id": "test-123",
        "metadata": {...},
        "database_schema": {...},
        "queries": {...},
        "metrics": {...}
    }
```

### Run Tests

```bash
pytest tests/contract/ -v
pytest tests/contract/test_collector_output_contract.py -v
pytest tests/contract/ --cov=src/contracts
```

## CI/CD Integration

Contract validation should run in your CI/CD pipeline to catch errors before code is merged. This section provides complete examples for common CI/CD systems and guidance on integrating validation into existing pipelines.

### GitHub Actions

#### Complete Workflow Example

Create `.github/workflows/contract-validation.yml`:

```yaml
name: Contract Validation

on:
  push:
    branches: [ main, develop ]
    paths:
      - 'src/contracts/**'
      - 'tests/contract/**'
      - 'scripts/validate_contracts.py'
  pull_request:
    branches: [ main, develop ]
    paths:
      - 'src/contracts/**'
      - 'tests/contract/**'
      - 'scripts/validate_contracts.py'

jobs:
  validate-contracts:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.13'
          cache: 'pip'

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install pydantic>=2.0.0 pytest hypothesis pytest-cov

      - name: Run validation script
        run: |
          echo "Validating contract models..."
          python scripts/validate_contracts.py src/contracts/*.py

      - name: Run contract tests
        run: |
          echo "Running contract test suite..."
          pytest tests/contract/ -v --cov=src/contracts --cov-report=term-missing

      - name: Upload coverage reports
        uses: codecov/codecov-action@v3
        if: always()
        with:
          files: ./coverage.xml
          flags: contracts
```

#### Integration with Existing Workflow

Add to your existing `.github/workflows/ci.yml`:

```yaml
jobs:
  # ... existing jobs ...

  validate-contracts:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.13'
      - run: pip install pydantic>=2.0.0 pytest hypothesis
      - run: python scripts/validate_contracts.py src/contracts/*.py
      - run: pytest tests/contract/ -v

  # Make other jobs depend on contract validation
  build:
    needs: validate-contracts
    # ... rest of build job ...
```

### Integration Best Practices

#### 1. Run Validation Early

Place contract validation as one of the first stages in your pipeline to fail fast:

```yaml
# Good: Validate before expensive operations
stages:
  - validate-contracts
  - build
  - test
  - deploy

# Bad: Validate after expensive operations
stages:
  - build
  - test
  - validate-contracts  # Too late!
  - deploy
```

#### 2. Use Path Filters

Only run validation when contract files change:

```yaml
# GitHub Actions
on:
  push:
    paths:
      - 'src/contracts/**'
      - 'tests/contract/**'
```

#### 3. Cache Dependencies

Speed up builds by caching Python packages:

```yaml
# GitHub Actions
- uses: actions/setup-python@v4
  with:
    python-version: '3.12'
    cache: 'pip'
```

#### 4. Fail Fast

Configure validation to stop the pipeline immediately on failure:

```yaml
# GitHub Actions
- name: Validate contracts
  run: python scripts/validate_contracts.py src/contracts/*.py
  # Pipeline stops here if validation fails
```

#### 5. Parallel Execution

Run validation in parallel with other checks:

```yaml
# GitHub Actions
jobs:
  validate-contracts:
    # ... contract validation ...

  lint:
    # ... linting ...

  type-check:
    # ... type checking ...

  # All run in parallel
```

#### 6. Required Status Checks

Make contract validation a required check before merging:

- **GitHub**: Settings → Branches → Branch protection rules → Require status checks

#### 7. Notifications

Configure notifications for validation failures:

```yaml
# GitHub Actions — notify on failure
- name: Notify on failure
  if: failure()
  run: echo "Contract validation failed! Check the logs."
  # Add Slack/email notification step here
```

### Troubleshooting CI/CD Issues

**"Module not found" errors**

- Ensure dependencies are installed: `pip install pydantic>=2.0.0`
- Check Python version matches development environment

**"No such file or directory" errors**

- Verify paths are relative to repository root
- Check file paths in validation command

**Tests pass locally but fail in CI**

- Check Python version consistency
- Verify all dependencies are in requirements file
- Check for environment-specific configurations

**Slow pipeline execution**

- Use dependency caching
- Run validation only on contract file changes
- Use parallel job execution

**Permission errors**

- Ensure scripts have execute permissions: `chmod +x scripts/validate_contracts.py`
- Check file ownership in Docker containers

## Troubleshooting

### Common Errors

**"No Pydantic models found"**

- Ensure class inherits from `BaseModel`

**"Missing 'contract_version' field"**

- Add to output contracts: `contract_version: str = Field(default="1.0", ...)`

**"Fields without descriptions"**

- Add descriptions: `Field(..., description="...")`

**"Field shadows BaseModel attribute"**

- Rename field: `schema` → `database_schema`

**ValidationError during testing**

- Check error details: `print(e.json())`
- Common causes: missing fields, wrong types, invalid ranges/enums

**Pre-commit hook not running**

- Install hooks: `pre-commit install`

**Import errors**

- Install dependencies: `pip install pydantic>=2.0.0`
- Run from project root

### Debug Tips

- Use `pytest -vv` for verbose output
- Test incrementally, field by field
- Check [Pydantic docs](https://docs.pydantic.dev/) for advanced features
