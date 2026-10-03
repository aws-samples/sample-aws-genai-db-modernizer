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

build_ui() {  # Install UI deps and build the production bundle (src/ui/build/).
  log "build UI"
  # No registry is hardcoded here: locally npm may be configured to use an internal
  # mirror (.npmrc / NPM_CONFIG_REGISTRY), and CI uses the public default. Both are
  # respected by leaving npm's registry resolution alone.
  ( cd "$REPO_ROOT/src/ui" && npm ci --no-audit --no-fund && REACT_APP_API_URL=http://localhost:8000/api/v1/ CI=false npx react-scripts build )
}
