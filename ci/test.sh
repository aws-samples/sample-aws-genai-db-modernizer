#!/usr/bin/env bash
# Unit, contract, property and graph tests. No network, no credentials.
source "$(dirname "$0")/lib.sh"
log "pytest (not integration, not e2e)"
uv run pytest tests/ -m "not integration and not e2e" --fail-on-skip -n auto "$@"
