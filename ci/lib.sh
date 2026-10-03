#!/usr/bin/env bash
# Shared helpers for ci/*.sh. Source, don't execute.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\n==> %s\n' "$*"; }

require_env() {  # require_env VAR [VAR...]
  local missing=()
  for v in "$@"; do [ -n "${!v:-}" ] || missing+=("$v"); done
  if [ ${#missing[@]} -gt 0 ]; then
    echo "error: missing required environment variable(s): ${missing[*]} (see ci/README.md)" >&2
    exit 2
  fi
}
