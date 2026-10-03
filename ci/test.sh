#!/usr/bin/env bash
# Unit, contract, property and graph tests. No network, no credentials.
#
# --ignore=tests/e2e: the `test` CI job (and this script's own prerequisites,
# see ci/README.md) installs only the `dev` extra, not `e2e` -- so
# tests/e2e/* can't even be IMPORTED (playwright, pypdf) here, let alone run.
# -m "not integration and not e2e" alone is not enough to prevent that: pytest
# imports every collected module to evaluate its markers, so collection itself
# fails before the marker ever gets a chance to deselect anything. The marker
# stays as a second, belt-and-suspenders guard for anyone who *does* have the
# e2e extra installed and runs this script directly.
source "$(dirname "$0")/lib.sh"
log "pytest (not integration, not e2e)"
uv run pytest tests/ --ignore=tests/e2e -m "not integration and not e2e" --fail-on-skip -n auto "$@"
