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
#
# TEST_SHARD=N/M (optional, #956): run only shard N of M, split by pytest-split
# from the recorded per-test durations in .test_durations (least_duration, so
# each shard gets about the same total time). The M shards together run exactly
# the tests one unsharded run does. Unset runs everything. Refresh the
# durations after adding or renaming many tests: ./ci/test.sh --store-durations --clean-durations
source "$(dirname "$0")/lib.sh"
# Pull requests run the Hypothesis `ci` profile (fewer examples, see
# tests/conftest.py); main, nightly and local runs keep the full `default`
# profile. An explicit HYPOTHESIS_PROFILE always wins.
if [ "${GITHUB_EVENT_NAME:-}" = "pull_request" ]; then
  export HYPOTHESIS_PROFILE="${HYPOTHESIS_PROFILE:-ci}"
fi
# Coverage (--cov) uses Python's sys.monitoring tracer (PEP 669) unless
# COVERAGE_CORE says otherwise: the same line coverage at about 1.1x the
# uncovered run time, against 2.2x for the default tracer (#1718). Branch
# coverage would need the default tracer on 3.12; this repo measures lines only.
for arg in "$@"; do
  case "$arg" in
    --cov|--cov=*) export COVERAGE_CORE="${COVERAGE_CORE:-sysmon}"; break ;;
  esac
done
shard_args=()
if [ -n "${TEST_SHARD:-}" ]; then
  case "$TEST_SHARD" in
    */*) ;;
    *) echo "error: TEST_SHARD must look like N/M, got '$TEST_SHARD'" >&2; exit 2 ;;
  esac
  shard_args=(--splits "${TEST_SHARD#*/}" --group "${TEST_SHARD%/*}" --splitting-algorithm least_duration)
fi
log "pytest (not integration, not e2e; Hypothesis profile: ${HYPOTHESIS_PROFILE:-default}${TEST_SHARD:+; shard $TEST_SHARD})"
uv run pytest tests/ --ignore=tests/e2e -m "not integration and not e2e" --fail-on-skip -n auto \
  ${shard_args[@]+"${shard_args[@]}"} "$@"
