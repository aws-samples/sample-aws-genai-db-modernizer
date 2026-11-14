#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Lint and type-check, same commands as the pre-commit hooks. mypy takes its paths and
# per-tree rules from [tool.mypy] in pyproject.toml, as the mypy hook does (#2245).
source "$(dirname "$0")/lib.sh"
# The tracked Python files the pre-commit black, isort and ruff hooks select, read from their
# patterns, so this and the hooks check the same files on any checkout (#1844;
# tests/unit/ci/test_security_parity.py).
py_list="$(uv run python ci/python_files.py)"
PY_FILES=()
while IFS= read -r f; do PY_FILES+=("$f"); done <<<"$py_list"
log "ruff";  uv run ruff check "${PY_FILES[@]}"
log "black"; uv run black --check --line-length=100 "${PY_FILES[@]}"
log "isort"; uv run isort --profile=black --line-length=100 --check-only "${PY_FILES[@]}"
log "mypy";  uv run mypy
log "license headers"; uv run python scripts/license_headers.py --check
log "commands"; uv run python scripts/validate_skills.py
log "claim markers"; uv run python ci/claims.py check
log "compressed fixtures"; uv run python ci/llm/private_scan.py tests/fixtures/real_runs
log "ci/README budget"; uv run python ci/readme_budget.py
log "catalog"; uv run python scripts/render_catalog.py --check
log "graph catalog"; uv run python scripts/render_graph_catalog.py --check
log "contract schemas"; uv run python scripts/render_contract_schemas.py --check
log "image requirements"; uv run python scripts/render_atx_requirements.py --check
log "group draft agent"; uv run python scripts/render_group_draft_agent.py --check
log "prices"; uv run python scripts/refresh_prices.py --check-age
log "extension lists"; uv run python scripts/check_extension_list_age.py
