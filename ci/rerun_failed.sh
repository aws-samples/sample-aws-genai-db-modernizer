#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Re-run the failed tests of a pull request's last CI run, locally (#1849).
#
#   ./ci/rerun_failed.sh [--list] [PR]
#
# Reads the failed test ids of the newest completed CI run of PR (default: the current
# branch's pull request) with gh, and runs them: the unit, contract and property ids
# with uv run pytest -n 4 and the Hypothesis profile and per-test limit of a pull
# request's ci/test.sh run (both from ci/lib.sh). tests/e2e ids need the e2e extra and
# a built pipeline, so their command is printed, not run. --list prints the ids only.
# Exits 0 when the re-run passes or there is nothing to re-run (no PR, no completed
# run, the last run ended other than failure or timed_out, or it failed with no test
# id), pytest's exit code when a re-run test fails, and 2 when gh is missing or not
# authenticated or an API call failed. Logic: ci/rerun_failed.py.
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

list=0
pr=""
for arg in "$@"; do
  case "$arg" in
    --list) list=1 ;;
    -h | --help) sed -n '5,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "usage: ci/rerun_failed.sh [--list] [PR]" >&2; exit 2 ;;
    *) pr="$arg" ;;
  esac
done

ids_rc=0
ids_out="$(uv run python ci/rerun_failed.py ids ${pr:+"$pr"})" || ids_rc=$?
[ "$ids_rc" -eq 0 ] || exit "$ids_rc"
unit=()
e2e=()
while IFS= read -r id; do
  [ -n "$id" ] || continue
  case "$id" in
    tests/e2e/*) e2e+=("$id") ;;
    *) unit+=("$id") ;;
  esac
done <<<"$ids_out"

if [ "$list" -eq 1 ]; then
  [ -z "$ids_out" ] || printf '%s\n' "$ids_out"
  exit 0
fi
if [ "${#e2e[@]}" -gt 0 ]; then
  echo "rerun_failed: ${#e2e[@]} tests/e2e id(s) need the e2e extra (see ci/e2e.sh); run them with:"
  printf '  uv run pytest -p no:xdist -q'
  printf ' %q' "${e2e[@]}"
  printf '\n'
fi
if [ "${#unit[@]}" -eq 0 ]; then
  exit 0
fi
# As a pull request's ci/test.sh run does: its Hypothesis profile and per-test limit.
use_pull_request_hypothesis_profile
log "re-running ${#unit[@]} failed test(s) from the last CI run (Hypothesis profile: $HYPOTHESIS_PROFILE)"
uv run pytest -q -n 4 "${PYTEST_TIME_LIMIT[@]}" "${unit[@]}"
