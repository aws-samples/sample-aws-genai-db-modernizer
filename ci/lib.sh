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

# Print the pids of every descendant of $1. A killed `uv run
# playwright install --with-deps` leaves its root-owned apt-get running, which holds
# the dpkg lock and fails every retry with exit 100 (seen on PR #1363), so the whole
# tree has to go.
_descendants() {
  local c
  for c in $(pgrep -P "$1" 2>/dev/null); do _descendants "$c"; echo "$c"; done
}

# Signal $2 (TERM or KILL) to every pid listed in file $1, the parent first so it
# cannot report success once its children are gone; root-owned ones go through sudo
# when the plain kill is refused (CI runners have passwordless sudo). The list is
# taken once, before the first TERM: once the parent dies its children are
# reparented and can no longer be found by walking down from it.
_kill_listed() {
  local p
  for p in $(cat "$1" 2>/dev/null); do
    kill "-$2" "$p" 2>/dev/null || sudo -n kill "-$2" "$p" 2>/dev/null || true
  done
}

# Run a command, TERM it and its descendants after $1 seconds, and KILL whatever is
# left once it returns or 10 seconds later, whichever comes first. Returns its
# status, or 124 when it was stopped. Pure bash: macOS has no `timeout`.
_run_with_limit() {
  local limit="$1"; shift
  if [ "$limit" -le 0 ]; then "$@"; return; fi
  "$@" &
  local pid=$! rc=0 stamp
  stamp="$(mktemp)"; rm -f "$stamp"
  ( nap=""
    trap '[ -z "$nap" ] || kill "$nap" 2>/dev/null; exit 0' TERM
    sleep "$limit" & nap=$!; wait "$nap" || exit 0
    echo "error: '$*' still running after ${limit}s, stopping it" >&2
    { echo "$pid"; _descendants "$pid"; } > "$stamp.pids"
    : > "$stamp"
    _kill_listed "$stamp.pids" TERM
    sleep 10 & nap=$!; wait "$nap"
    _kill_listed "$stamp.pids" KILL ) &
  local dog=$!
  wait "$pid" || rc=$?
  kill "$dog" 2>/dev/null; wait "$dog" 2>/dev/null || true
  if [ -e "$stamp" ]; then
    # The parent is gone but its orphaned children (apt-get) may not be: KILL them.
    _kill_listed "$stamp.pids" KILL
    rm -f "$stamp" "$stamp.pids"
    return 124
  fi
  return "$rc"
}

# Run a command that must end by the epoch second in $E2E_LLM_DEADLINE_TS (#1593):
# skipped with 124 when the deadline has passed, stopped (with its descendants) and
# 124 when it would outlast it, else its own status. ci/e2e-llm.sh sets the deadline
# from E2E_LLM_JOB_BUDGET, so a slow test step cannot run the job into the CI
# runner's limit, which kills it before results.json and the test results exist.
run_before_deadline() {
  local left=$(( ${E2E_LLM_DEADLINE_TS:?run_before_deadline needs E2E_LLM_DEADLINE_TS} - $(date +%s) ))
  if [ "$left" -le 0 ]; then
    echo "error: the job budget is used up; skipping '$*'" >&2
    return 124
  fi
  _run_with_limit "$left" "$@"
}

# Install Playwright browsers, retrying with backoff (#1346). The CDN has served a
# certificate for the wrong host (ERR_TLS_CERT_ALTNAME_INVALID) before any test ran.
# TLS verification is never relaxed. Attempts 1 and 2 use Playwright's own host
# list; the last attempt points PLAYWRIGHT_DOWNLOAD_HOST at Playwright's other
# official download host. Usage: playwright_install_with_retry [install args...]
#   E2E_INSTALL_ATTEMPTS   attempts (default 3)
#   E2E_INSTALL_BACKOFF    seconds before retry n is n * this value (default 15)
#   E2E_INSTALL_TIMEOUT    seconds one attempt may run before it is killed and
#                          retried (default 300; a normal install takes about 45).
#                          A hung apt-get update once held a shard for the whole 45
#                          minute job timeout (#1360). 0 turns the limit off.
#   PLAYWRIGHT_FALLBACK_HOST  host for the last attempt (default: the official
#                          playwright.download.prss.microsoft.com mirror)
playwright_install_with_retry() {
  local attempts="${E2E_INSTALL_ATTEMPTS:-3}"
  local backoff="${E2E_INSTALL_BACKOFF:-15}"
  local fallback="${PLAYWRIGHT_FALLBACK_HOST:-https://playwright.download.prss.microsoft.com/dbazure/download/playwright}"
  local limit="${E2E_INSTALL_TIMEOUT:-300}"
  local n=1
  while :; do
    if [ "$n" -ge "$attempts" ] && [ "$n" -gt 1 ] && [ -z "${PLAYWRIGHT_DOWNLOAD_HOST:-}" ]; then
      log "playwright install, attempt $n of $attempts, from the fallback host"
      if PLAYWRIGHT_DOWNLOAD_HOST="$fallback" _run_with_limit "$limit" uv run playwright install "$@"; then return 0; fi
    else
      log "playwright install, attempt $n of $attempts"
      if _run_with_limit "$limit" uv run playwright install "$@"; then return 0; fi
    fi
    if [ "$n" -ge "$attempts" ]; then
      echo "error: playwright install failed after $attempts attempts" >&2
      return 1
    fi
    sleep "$((n * backoff))"
    n=$((n + 1))
  done
}

# True when every named browser launches headless, i.e. its browser binary and the
# OS libraries it links against are all present. Fonts do not matter to a launch,
# which is the point: `playwright install-deps` also wants extra font packages
# (fonts-unifont, fonts-tlwg-loma-otf, ...) that no e2e test needs.
playwright_browsers_launch() {
  _run_with_limit "${E2E_LAUNCH_TIMEOUT:-120}" uv run python -c '
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    for name in sys.argv[1:]:
        getattr(p, name).launch().close()
' "$@"
}

# Write stdin to root-owned file $1, through sudo when it is not writable.
_write_system_file() {
  if [ -w "$1" ] || { [ ! -e "$1" ] && [ -w "$(dirname "$1")" ]; }; then cat > "$1"; else sudo -n tee "$1" > /dev/null; fi
}

# CI only (#1360): make sure the browsers' OS libraries are present, touching apt
# only when they are not. GitHub's Ubuntu runners ship chromium's and miss some of
# webkit's, so a webkit launch can fail. `--with-deps` used to run apt-get update plus
# a font download that stalled for the whole job on a slow Azure mirror; now:
#  - nothing runs when every browser launches (fonts are never needed to launch);
#  - otherwise `playwright install-deps` runs under the install watchdog, with apt
#    told to give up on a silent server after 30s (and retry 3 times), and from the
#    second attempt on with apt pointed at archive.ubuntu.com instead of the runner's
#    mirror list (APT_MIRRORS_FILE and APT_CONF_FILE can be redirected for tests).
# Usage: playwright_ensure_deps browser...
playwright_ensure_deps() {
  if playwright_browsers_launch "$@"; then
    log "browser system libraries are present, skipping apt"
    return 0
  fi
  local attempts="${E2E_INSTALL_ATTEMPTS:-3}" backoff="${E2E_INSTALL_BACKOFF:-15}"
  # Longer than a download: webkit pulls about 120 MB of packages, and apt keeps the
  # ones already downloaded, so a stopped attempt is not wasted.
  local limit="${E2E_DEPS_TIMEOUT:-600}"
  local mirrors="${APT_MIRRORS_FILE:-/etc/apt/apt-mirrors.txt}"
  local conf="${APT_CONF_FILE:-/etc/apt/apt.conf.d/99-e2e-timeouts}"
  printf 'Acquire::Retries "3";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' | _write_system_file "$conf"
  local n=1
  while :; do
    if [ "$n" -gt 1 ] && [ -e "$mirrors" ]; then
      log "apt: using archive.ubuntu.com instead of the runner's mirror list"
      printf 'http://archive.ubuntu.com/ubuntu/\tpriority:1\n' | _write_system_file "$mirrors"
    fi
    # A stopped apt-get can leave dpkg half-way through (it then refuses every apt call).
    if [ "$n" -gt 1 ]; then sudo -n dpkg --configure -a >/dev/null 2>&1 || true; fi
    log "a browser failed to launch, playwright install-deps, attempt $n of $attempts"
    if _run_with_limit "$limit" uv run playwright install-deps "$@"; then return 0; fi
    if [ "$n" -ge "$attempts" ]; then
      echo "error: playwright install-deps failed after $attempts attempts" >&2
      return 1
    fi
    sleep "$((n * backoff))"
    n=$((n + 1))
  done
}
