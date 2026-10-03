# Tests

This directory contains all test suites.

## Structure

- **unit/** - Unit tests for individual functions and classes
- **integration/** - Integration tests for component interactions (need a live stack or manually generated artifacts; deselected from the gated run)
- **contract/** - Contract validation tests using JSON schemas
- **property/** - Hypothesis-based property tests
- **graph/** - Context graph layer tests
- **e2e/** - Deterministic end-to-end suite: runs the full pipeline with no LLM, then checks the rendered HTML/PDF deliverables in real browsers and a local-UI smoke test. Needs the `e2e` extra, Node 22, and Playwright's browsers -- not run by plain `pytest tests/` (see the root `tests/conftest.py`'s `pytest_ignore_collect`)

## Running Tests

```bash
# Run all tests (unit, contract, property, graph; no integration or e2e)
make test               # or: ./ci/test.sh --cov=src --cov-report=term

# Deterministic end-to-end (pipeline + rendered HTML/PDF + UI smoke)
make e2e                # or: ./ci/e2e.sh

# Run a specific suite directly
uv run pytest tests/unit/ -v
uv run pytest tests/integration/ -v
uv run pytest tests/contract/ -v
```

`make test` runs the `atx_orchestrator` tests, which need the AWS Transform
SDK (`uv pip install "agent-builder-sdk-aws-transform>=1.0.0"`; see
`tests/unit/atx_orchestrator/conftest.py`) to pass rather than fail loudly.
`make e2e` needs `uv sync --extra e2e` plus Node 22 and Playwright's browsers
(`ci/e2e.sh` installs the browsers and builds the UI itself).

A bare `uv run pytest tests/` is safe to run (it skips `tests/e2e/` by
default so it never needs the `e2e` extra), but prefer `make test` / `./ci/test.sh`
since that is what CI actually gates on.

## Test Guidelines

- All new code must include tests
- Contract tests validate agent input/output against schemas
- Integration tests use real database connections (when available)
