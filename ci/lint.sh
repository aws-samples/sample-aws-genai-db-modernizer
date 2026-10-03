#!/usr/bin/env bash
# Lint and type-check, same commands as the pre-commit hooks.
source "$(dirname "$0")/lib.sh"
log "ruff";  uv run ruff check src/ tests/
log "black"; uv run black --check --line-length=100 src/ tests/
log "isort"; uv run isort --profile=black --line-length=100 --check-only src/ tests/
log "mypy";  uv run mypy src/
log "commands"; uv run python scripts/validate_skills.py
