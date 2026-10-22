#!/usr/bin/env bash
# Deterministic end-to-end run: pipeline with no LLM -> deliverables -> browser and
# PDF checks -> UI smoke -> help site checks.
# Usage: ci/e2e.sh [extra pytest args]     (needs: uv, node 22, playwright browsers,
#   the `docs` extra for the mkdocs build this script does itself)
#
# Env vars read:
#   E2E_OUTPUT       - where Playwright artifacts (screenshots, traces, junit) and
#                       the copied-out deliverables land. Defaults to test-results/.
#   E2E_INSTALL_ATTEMPTS, E2E_INSTALL_BACKOFF, PLAYWRIGHT_FALLBACK_HOST - the browser
#                       download retry (playwright_install_with_retry in lib.sh, #1346);
#                       E2E_INSTALL_TIMEOUT stops one hung attempt (default 300s, #1360).
#   E2E_REPORT_ARGS  - extra pytest args for the first invocation ONLY (below).
#   E2E_UI_ARGS      - extra pytest args for the second invocation ONLY (below).
#   E2E_INSTALL_ATTEMPTS, E2E_INSTALL_BACKOFF, PLAYWRIGHT_FALLBACK_HOST - the browser
#                       download retry (see playwright_install_with_retry in lib.sh).
#   E2E_SHARD        - run one CI shard instead of everything (#956): `wordpress`,
#                       `discourse` or `trailgear` (the report checks whose test id
#                       names that sample), `hashseed` (test_hashseed_determinism.py),
#                       `determinism` (test_deliverable_determinism.py), `cross` (the
#                       other report checks, tied to no sample), or `ui` (UI smoke +
#                       help site, the only shard that builds the UI and the site).
#                       Unset runs everything, as before. The six report shards' -k
#                       expressions partition the report suite: each test lands in
#                       exactly one of them (see e2e_shard_k).
#
# Runs pytest TWICE (see tests/e2e/test_ui.py's module docstring): pytest-playwright's
# only_browser marker is a runtime skip, not a deselection, so under
# `--browser chromium --browser webkit` combined with --fail-on-skip the webkit
# parametrizations of the (Chromium-only) UI suite would be skipped-then-failed.
# Splitting into two invocations keeps --fail-on-skip meaningful for both: the
# report checks run cross-browser, the UI smoke runs Chromium-only.
#
# Any "$@" args passed to this script go to BOTH invocations below -- so a `-k`
# that only matches one suite's tests makes the OTHER invocation collect zero
# items and exit 5 ("no tests collected"), failing the whole script. Use
# E2E_REPORT_ARGS / E2E_UI_ARGS instead when an arg should apply to only one of
# the two pytest runs (e.g. `-k` for a single test name).
#
# Each invocation is its own pytest session, so the deterministic pipeline
# fixture (tests/e2e/conftest.py's `run`/`all_runs`, cached per sample at module
# level to survive pytest's own fixture-teardown churn under multi-browser
# parametrization) runs each sample pipeline once per invocation -- ~25s total
# (measured: wordpress ~6s, discourse ~9s, trailgear ~10s) -- rather than once
# overall across both invocations. An acceptable, explicit, now-correctly-sized
# cost: this used to be mis-stated as "~13s each" and, before the fixture-cache
# fix, was actually up to 13 reruns of one sample's pipeline per invocation.
source "$(dirname "$0")/lib.sh"

export E2E_OUTPUT="${E2E_OUTPUT:-test-results}"
export E2E_REPORT_ARGS="${E2E_REPORT_ARGS:-}"
export E2E_UI_ARGS="${E2E_UI_ARGS:-}"
export E2E_SHARD="${E2E_SHARD:-}"
mkdir -p "$E2E_OUTPUT"

# The -k expression for one report shard. Each test goes to the first rule it
# matches: `hashseed` (test_hashseed_determinism.py) and `determinism`
# (test_deliverable_determinism.py), the two slowest suites, which rerun samples
# on purpose; then the first sample its id names, in the order wordpress,
# discourse, trailgear; then `cross` for a test that names no sample. So the six
# shards never overlap and never drop a test: `cross` takes everything the other
# five do not.
e2e_shard_k() {
  local det="hashseed_determinism or deliverable_determinism"
  case "$1" in
    hashseed) echo "hashseed_determinism" ;;
    determinism) echo "deliverable_determinism and not hashseed_determinism" ;;
    wordpress) echo "wordpress and not ($det)" ;;
    discourse) echo "discourse and not wordpress and not ($det)" ;;
    trailgear) echo "trailgear and not wordpress and not discourse and not ($det)" ;;
    cross) echo "not ($det) and not (wordpress or discourse or trailgear)" ;;
    *) echo "error: unknown E2E_SHARD '$1' (wordpress, discourse, trailgear, hashseed, determinism, cross, ui)" >&2; return 2 ;;
  esac
}

run_reports=1
run_ui=1
report_k=()
browsers=(chromium webkit)
case "$E2E_SHARD" in
  "") ;;
  ui) run_reports=0; browsers=(chromium) ;;
  # The two determinism suites run the pipeline scripts in subprocesses and open
  # no browser page, so they skip the browser install (about 45s on CI, and the
  # step that hung a whole shard in #1360).
  hashseed|determinism) run_ui=0; browsers=(); report_k=(-k "$(e2e_shard_k "$E2E_SHARD")") ;;
  *) run_ui=0; report_k=(-k "$(e2e_shard_k "$E2E_SHARD")") ;;
esac

# The UI build (about 90s on CI) does not depend on the browsers or the help
# site, so it runs in the background while those install and build. Its output
# goes to a log file, printed once it finishes, so the logs do not interleave.
ui_pid=""
ui_log="$E2E_OUTPUT/ui-build.log"
if [ "$run_ui" = 1 ]; then
  log "build UI in the background (log: $ui_log)"
  build_ui > "$ui_log" 2>&1 &
  ui_pid=$!
fi

if [ ${#browsers[@]} -gt 0 ]; then
  log "install browsers (${browsers[*]})"
  playwright_install_with_retry "${browsers[@]}"
  if [ "${CI:-}" = "true" ]; then
    # OS libraries need apt and root/sudo: fine on GitHub's Linux runners (CI=true is
    # set automatically), nothing to do on a developer's macOS machine. They are
    # installed only when a browser cannot launch, and then with apt timeouts and a
    # mirror fallback, so a slow package mirror cannot hold the job (#1360).
    playwright_ensure_deps "${browsers[@]}"
  fi
fi

if [ "$run_ui" = 1 ]; then
  log "build the help site (#325)"
  # tests/e2e/test_docs_site.py only serves and checks site/ -- same split as
  # build_ui/test_ui.py above. Needs the `docs` extra (mkdocs, mkdocs-material).
  uv run mkdocs build --strict
  uv run python scripts/build_docs_sample.py --site-dir site

  log "wait for the UI build"
  ui_rc=0
  wait "$ui_pid" || ui_rc=$?
  cat "$ui_log"
  if [ "$ui_rc" -ne 0 ]; then
    echo "error: the UI build failed (exit $ui_rc), see the log above" >&2
    exit "$ui_rc"
  fi
fi

if [ "$run_reports" = 1 ]; then
log "pipeline + report checks (chromium, webkit${E2E_SHARD:+, shard $E2E_SHARD})"
# --output is its own subdirectory, not $E2E_OUTPUT itself: pytest-playwright's
# delete_output_dir autouse fixture rmtree()s whatever --output points at, at the
# start of EVERY session. Pointing both invocations' --output straight at
# $E2E_OUTPUT would let the second invocation silently delete the first
# invocation's junit file and the deliverables already copied there.
uv run pytest tests/e2e --ignore=tests/e2e/test_ui.py --ignore=tests/e2e/test_docs_site.py \
  -m e2e -p no:xdist --fail-on-skip \
  --browser chromium --browser webkit \
  --output="$E2E_OUTPUT/pw-reports" --screenshot=only-on-failure --tracing=retain-on-failure \
  --junitxml="$E2E_OUTPUT/e2e-reports-junit.xml" ${report_k[@]+"${report_k[@]}"} "$@" $E2E_REPORT_ARGS
fi

if [ "$run_ui" = 1 ]; then
log "UI smoke + help site checks (chromium only)"
uv run pytest tests/e2e/test_ui.py tests/e2e/test_docs_site.py -m e2e -p no:xdist --fail-on-skip \
  --browser chromium \
  --output="$E2E_OUTPUT/pw-ui" --screenshot=only-on-failure --tracing=retain-on-failure \
  --junitxml="$E2E_OUTPUT/e2e-ui-junit.xml" "$@" $E2E_UI_ARGS
fi
