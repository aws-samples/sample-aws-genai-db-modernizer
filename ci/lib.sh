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
  ( cd "$REPO_ROOT/src/ui" && npm ci --no-audit --no-fund && REACT_APP_API_URL="http://localhost:${E2E_API_PORT:-8000}/api/v1/" CI=false npx react-scripts build )
}

# Install Playwright browsers, retrying with backoff (#1346). The CDN has served a
# certificate for the wrong host (ERR_TLS_CERT_ALTNAME_INVALID) before any test ran.
# TLS verification is never relaxed. Attempts 1 and 2 use Playwright's own host
# list; the last attempt points PLAYWRIGHT_DOWNLOAD_HOST at Playwright's other
# official download host. Usage: playwright_install_with_retry [install args...]
#   E2E_INSTALL_ATTEMPTS   attempts (default 3)
#   E2E_INSTALL_BACKOFF    seconds before retry n is n * this value (default 15)
#   PLAYWRIGHT_FALLBACK_HOST  host for the last attempt (default: the official
#                          playwright.download.prss.microsoft.com mirror)
playwright_install_with_retry() {
  local attempts="${E2E_INSTALL_ATTEMPTS:-3}"
  local backoff="${E2E_INSTALL_BACKOFF:-15}"
  local fallback="${PLAYWRIGHT_FALLBACK_HOST:-https://playwright.download.prss.microsoft.com/dbazure/download/playwright}"
  local n=1
  while :; do
    if [ "$n" -ge "$attempts" ] && [ "$n" -gt 1 ] && [ -z "${PLAYWRIGHT_DOWNLOAD_HOST:-}" ]; then
      log "playwright install, attempt $n of $attempts, from the fallback host"
      if PLAYWRIGHT_DOWNLOAD_HOST="$fallback" uv run playwright install "$@"; then return 0; fi
    else
      log "playwright install, attempt $n of $attempts"
      if uv run playwright install "$@"; then return 0; fi
    fi
    if [ "$n" -ge "$attempts" ]; then
      echo "error: playwright install failed after $attempts attempts" >&2
      return 1
    fi
    sleep "$((n * backoff))"
    n=$((n + 1))
  done
}
