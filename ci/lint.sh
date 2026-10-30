#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Lint and type-check, same commands as the pre-commit hooks.
source "$(dirname "$0")/lib.sh"
log "ruff";  uv run ruff check src/ tests/
log "black"; uv run black --check --line-length=100 src/ tests/
log "isort"; uv run isort --profile=black --line-length=100 --check-only src/ tests/
log "mypy";  uv run mypy src/
log "license headers"; uv run python scripts/license_headers.py --check
log "commands"; uv run python scripts/validate_skills.py
log "catalog"; uv run python scripts/render_catalog.py --check
log "contract schemas"; uv run python scripts/render_contract_schemas.py --check
log "image requirements"; uv run python scripts/render_atx_requirements.py --check
log "prices"; uv run python scripts/refresh_prices.py --check-age
