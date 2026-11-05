#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Zero-token replay of the saved real-model runs (#1409, #1847): for each fixture, the
# transcript checks and the external-mode e2e checks over its report.json, re-rendered
# by the current code. No model call, no browser; needs only the `dev` extra.
# See ci/llm/replay.py.
# Usage: ci/replay.sh
#
# Env vars read:
#   REPLAY_FIXTURE  - one saved run to replay; it must exist. Default: every fixture in
#                     REPLAY_FIXTURES.
#   REPLAY_FIXTURES - the fixtures replayed when REPLAY_FIXTURE is unset, in order; one
#                     not saved yet (no run.json) is skipped with a line saying so.
#                     Default: tests/fixtures/real_runs/wordpress-both and
#                     tests/fixtures/real_runs/discourse-both (replay.py's FIXTURES).
#   E2E_OUTPUT      - each fixture's replayed job, junit and summary land in
#                     $E2E_OUTPUT/replay/<fixture name>/. Default: test-results/.
source "$(dirname "$0")/lib.sh"

DEFAULT_FIXTURES="tests/fixtures/real_runs/wordpress-both tests/fixtures/real_runs/discourse-both"

# Replay one saved run into $2. Runs as its own process (the script calls itself with
# REPLAY_FIXTURE set), so `set -e` stops it at the first failure.
replay_one() {
  local fixture="$1" out="$2"
  mkdir -p "$out"
  out="$(cd "$out" && pwd)"  # absolute: the e2e fixtures run scripts from other folders
  local root="$out/artifacts"

  log "replay: lay out $fixture and render its deliverables"
  # Assigned first, so a failed prepare stops here with its own error (`read` from a
  # here-string would hide its exit status). prepare unpacks every .gz fixture file,
  # the transcript included.
  local ids
  ids="$(uv run python ci/llm/replay.py prepare --fixture "$fixture" --root "$root" \
    --transcript-out "$out/transcript.jsonl")"
  read -r DB JOB MODE <<<"$ids"

  log "replay: transcript checks ($MODE)"
  uv run python ci/llm/run.py check-transcript "$out/transcript.jsonl" --mode "$MODE" >"$out/summary.json"

  # pytest-playwright (the `e2e` extra) deletes its --output folder at the start of a
  # session, so it gets one of its own. The replayed checks open no browser page, so
  # on a dev-only install (no plugin, no --output option) they run without it.
  local pw_args=()
  if uv run python -c 'import pytest_playwright' >/dev/null 2>&1; then
    pw_args=(--output="$out/pw")
  fi

  log "replay: external-mode e2e checks over $DB/$JOB"
  # -k external: the checks parametrized over the replayed job; the other e2e tests
  # run their own pipelines and belong to the deterministic shards. replay_stale: a check
  # of what synthesis writes, which the saved report.json predates (ci/README.md).
  E2E_ARTIFACT_ROOT="$root" E2E_DB="$DB" E2E_JOB="$JOB" E2E_OUTPUT="$out" \
    uv run pytest tests/e2e -m "e2e and not deterministic and not replay_stale" -k external -p no:xdist --fail-on-skip \
    --ignore=tests/e2e/test_ui.py --ignore=tests/e2e/test_docs_site.py \
    --junitxml="$out/replay-junit.xml" -q ${pw_args[@]+"${pw_args[@]}"}
}

OUT="${E2E_OUTPUT:-test-results}/replay"

if [ -n "${REPLAY_FIXTURE:-}" ]; then
  if [ ! -f "$REPLAY_FIXTURE/run.json" ]; then
    echo "error: REPLAY_FIXTURE=$REPLAY_FIXTURE has no run.json (save one with ci/llm/replay.py save)" >&2
    exit 2
  fi
  replay_one "$REPLAY_FIXTURE" "$OUT/$(basename "$REPLAY_FIXTURE")"
  exit 0
fi

passed=() skipped=() failed=()
for fixture in ${REPLAY_FIXTURES:-$DEFAULT_FIXTURES}; do
  name="$(basename "$fixture")"
  if [ ! -f "$fixture/run.json" ]; then
    log "replay: skip $name: not saved yet (no $fixture/run.json)"
    skipped+=("$name")
  elif REPLAY_FIXTURE="$fixture" "$REPO_ROOT/ci/replay.sh"; then
    passed+=("$name")
  else
    failed+=("$name")
  fi
done

# One summary line, last, for ci/pre_merge.sh to print.
summary="replay: passed ${#passed[@]} (${passed[*]:-none}), skipped ${#skipped[@]} (${skipped[*]:-none}), failed ${#failed[@]} (${failed[*]:-none})"
echo "$summary"
if [ ${#failed[@]} -gt 0 ]; then
  exit 1
fi
if [ ${#passed[@]} -eq 0 ]; then
  echo "error: no fixture was replayed" >&2
  exit 1
fi
