#!/usr/bin/env bash
# Zero-token replay of the saved real-model run (#1409): the transcript checks and
# the external-mode e2e checks over its report.json, re-rendered by the current
# code. No model call, no browser, about 15 seconds; needs only the `dev` extra.
# See ci/llm/replay.py.
# Usage: ci/replay.sh
#
# Env vars read:
#   REPLAY_FIXTURE - the saved run. Default: tests/fixtures/real_runs/wordpress-both.
#   E2E_OUTPUT     - where the replayed job, its junit and summary land
#                    ($E2E_OUTPUT/replay/). Default: test-results/.
source "$(dirname "$0")/lib.sh"

FIXTURE="${REPLAY_FIXTURE:-tests/fixtures/real_runs/wordpress-both}"
OUT="${E2E_OUTPUT:-test-results}/replay"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"  # absolute: the e2e fixtures run scripts from other folders
ROOT="$OUT/artifacts"

log "replay: lay out $FIXTURE and render its deliverables"
# Assigned first, so a failed prepare stops here with its own error (`read` from a
# here-string would hide its exit status).
ids="$(uv run python ci/llm/replay.py prepare --fixture "$FIXTURE" --root "$ROOT")"
read -r DB JOB MODE <<<"$ids"

log "replay: transcript checks ($MODE)"
uv run python ci/llm/run.py check-transcript "$FIXTURE/transcript.jsonl" --mode "$MODE" >"$OUT/summary.json"

# pytest-playwright (the `e2e` extra) deletes its --output folder at the start of a
# session, so it gets one of its own. The replayed checks open no browser page, so
# on a dev-only install (no plugin, no --output option) they run without it.
pw_args=()
if uv run python -c 'import pytest_playwright' >/dev/null 2>&1; then
  pw_args=(--output="$OUT/pw")
fi

deselect=()
while IFS= read -r arg; do deselect+=("$arg"); done < <(uv run python ci/llm/replay.py deselect)

log "replay: external-mode e2e checks over $DB/$JOB"
# -k external: the checks parametrized over the replayed job; the other e2e tests
# run their own pipelines and belong to the deterministic shards.
E2E_ARTIFACT_ROOT="$ROOT" E2E_DB="$DB" E2E_JOB="$JOB" E2E_OUTPUT="$OUT" \
  uv run pytest tests/e2e -m "e2e and not deterministic" -k external -p no:xdist --fail-on-skip \
  --ignore=tests/e2e/test_ui.py --ignore=tests/e2e/test_docs_site.py \
  --junitxml="$OUT/replay-junit.xml" -q ${pw_args[@]+"${pw_args[@]}"} "${deselect[@]}"
