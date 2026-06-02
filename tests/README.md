# Tests

This directory contains all test suites.

## Structure

- **unit/** - Unit tests for individual functions and classes
- **integration/** - Integration tests for component interactions
- **contract/** - Contract validation tests using JSON schemas

## Running Tests

```bash
# Run all tests
uv run pytest tests/ -v --cov=src

# Run specific test suite
uv run pytest tests/unit/ -v
uv run pytest tests/integration/ -v
uv run pytest tests/contract/ -v

# Run with coverage report
uv run pytest tests/ -v --cov=src --cov-report=html
```

## Test Guidelines

- All new code must include tests
- Contract tests validate agent input/output against schemas
- Integration tests use real database connections (when available)
