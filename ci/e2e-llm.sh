#!/usr/bin/env bash
# Headless /modernize run + deliverable checks + quality judge.
# Usage: ci/e2e-llm.sh <chat|ui|both> <wordpress|discourse>
#
# Needs model access: CLAUDE_CODE_USE_BEDROCK=1 + AWS credentials + AWS_REGION,
# or ANTHROPIC_API_KEY. Not needed at all in dry-run mode (see below).
#
# Env vars read:
#   E2E_OUTPUT             - root for everything this script writes. Default: test-results/.
#   E2E_LLM_ARTIFACT_ROOT  - dry-run only: artifact root the deliverable checks
#                            read. Default: $REPO_ROOT/artifacts. Outside dry-run
#                            anything else is an error (exit 2): the headless
#                            run's scripts default to ./artifacts, the permission
#                            allowlist only lets it write artifacts/**, and
#                            MODERNIZER_CI_SANDBOX refuses roots outside the repo,
#                            so a different root would just check the wrong job.
#   E2E_LLM_TIMEOUT        - wall-clock timeout (seconds) for the claude call. Default: 3600.
#   E2E_LLM_MAX_TURNS      - only passed as --max-turns if the installed CLI's
#                            --help mentions that flag (see "CLI flag
#                            feature-detection" below). Default: 200.
#   E2E_LLM_MAX_BUDGET_USD - only passed as --max-budget-usd if --help mentions
#                            it. Default: 20.
#   CLAUDE_BIN             - the claude CLI to invoke. Default: claude.
#   CLAUDE_CODE_VERSION    - npm version to install when $CLAUDE_BIN is missing.
#                            Default: 2.1.288 (pinned; bump deliberately).
#   ANTHROPIC_MODEL        - model id for the headless run.
#   E2E_LLM_DRY_RUN        - "1" skips the claude call entirely (see below).
#   E2E_LLM_TRANSCRIPT     - required when E2E_LLM_DRY_RUN=1: path to a transcript
#                            .jsonl file to use in place of a real claude run.
#
# CLI flag feature-detection: the locally installed CLI build at the time this
# script was written (v2.1.288, an internal build) does not list --max-turns
# in its --help output; CI installs the public @anthropic-ai/claude-code npm
# package, which does. Rather than hardcode either behavior, this script
# captures `"$CLAUDE_BIN" --help` once and only adds --max-turns,
# --max-budget-usd, --setting-sources project, --strict-mcp-config and
# --verbose if the help text mentions them (warning when --max-turns is
# missing: the budget and wall-clock timeout are then the only caps), logging
# the flags it chose into $OUT/claude-flags.txt alongside `claude --version`
# in $OUT/claude-version.txt.
#
# Sandbox: the claude process (and so every Bash tool call it makes) runs with
# MODERNIZER_CI_SANDBOX=1, which makes the allowlisted scripts refuse paths
# outside the repo and unsafe --db/--job-id values (scripts/_sandbox.py).
#
# results.json is always written: every step after the preflight goes through
# step(), and if the script exits before the results step (a preflight
# failure such as missing credentials, an npm install or unzip error), the
# EXIT trap writes {"schema_version": 1, "pass": false, "error": "<step>
# failed before the run"}.
#
# Dry-run mode (E2E_LLM_DRY_RUN=1): skips the claude invocation and copies
# $E2E_LLM_TRANSCRIPT to $OUT/transcript.jsonl instead, so the rest of this
# script -- check-transcript, the deliverable checks, the judge (still a real
# subprocess call, typically pointed at a stub via CLAUDE_BIN), and results.json
# -- can be exercised locally without model access. The transcript's own
# MODERNIZE_RESULT line must name a job/db that actually exists under
# $E2E_LLM_ARTIFACT_ROOT (e.g. produced by tests/e2e/pipeline.py's
# run_pipeline()) and must match the <mode>/<fixture> arguments given here.
source "$(dirname "$0")/lib.sh"

MODE="${1:?mode: chat|ui|both}"
FIXTURE="${2:?fixture: wordpress|discourse}"
case "$MODE" in
  chat | ui | both) ;;
  *)
    echo "error: mode must be chat, ui, or both (got '$MODE')" >&2
    exit 2
    ;;
esac
case "$FIXTURE" in
  wordpress | discourse) ;;
  *)
    echo "error: fixture must be wordpress or discourse (got '$FIXTURE')" >&2
    exit 2
    ;;
esac

OUT="${E2E_OUTPUT:-test-results}/llm-${MODE}-${FIXTURE}"
mkdir -p "$OUT"
rm -f "$OUT/results.json"
DRY_RUN="${E2E_LLM_DRY_RUN:-0}"

# PHASE names the preflight step in progress, for the EXIT trap's fallback
# results.json. CLAUDE_STARTED gates the trap's UI cleanup: before the run
# there is nothing of ours to stop (and a developer's own local UI must not be
# touched by a run that never started).
PHASE="setup"
CLAUDE_STARTED=0
on_exit() {
  local rc=$?
  if [ "$CLAUDE_STARTED" = "1" ]; then
    # The run may have left its own local API/UI up (ui/both modes); stop them
    # via the same allowlisted script /modernize uses, not pkill -- see
    # scripts/start_local_ui.py's module docstring.
    uv run python scripts/start_local_ui.py --stop >/dev/null 2>&1 || true
  fi
  if [ ! -s "$OUT/results.json" ]; then
    printf '{"schema_version": 1, "pass": false, "error": "%s failed before the run"}\n' \
      "$PHASE" >"$OUT/results.json"
    [ "$rc" -ne 0 ] || rc=1
  fi
  exit "$rc"
}
trap on_exit EXIT

PHASE="artifact-root-check"
DEFAULT_ARTIFACT_ROOT="$REPO_ROOT/artifacts"
mkdir -p "$DEFAULT_ARTIFACT_ROOT"
if [ "$DRY_RUN" = "1" ]; then
  ARTIFACT_ROOT="${E2E_LLM_ARTIFACT_ROOT:-$DEFAULT_ARTIFACT_ROOT}"
else
  ARTIFACT_ROOT="$DEFAULT_ARTIFACT_ROOT"
  if [ -n "${E2E_LLM_ARTIFACT_ROOT:-}" ]; then
    want="$(cd "$DEFAULT_ARTIFACT_ROOT" && pwd -P)"
    got="$( (cd "$E2E_LLM_ARTIFACT_ROOT" 2>/dev/null && pwd -P) || printf '%s' "$E2E_LLM_ARTIFACT_ROOT")"
    if [ "$got" != "$want" ]; then
      echo "error: E2E_LLM_ARTIFACT_ROOT=$E2E_LLM_ARTIFACT_ROOT is only honoured with E2E_LLM_DRY_RUN=1;" \
        "a real run always writes to $DEFAULT_ARTIFACT_ROOT (see ci/README.md)" >&2
      exit 2
    fi
  fi
fi
mkdir -p "$ARTIFACT_ROOT"

# step() runs a command, records its exit code, and -- unlike a bare command
# under this script's `set -e` (inherited from lib.sh) -- always continues to
# the next step. Reviewers need results.json even when an earlier step (the
# claude run, a pytest invocation, the judge) failed, so the script collects
# every step's outcome and only exits non-zero at the very end.
#
# Parallel arrays (STEP_NAMES/STEP_CODES), not an associative array: macOS's
# shipped /bin/bash is 3.2, which has no `declare -A` (that needs bash 4+).
STEP_NAMES=()
STEP_CODES=()

step() {  # step NAME CMD...
  local name="$1" rc
  shift
  log "step: $name"
  if "$@"; then
    rc=0
  else
    rc=$?
    echo "warning: step '$name' failed (exit $rc)" >&2
  fi
  STEP_NAMES+=("$name")
  STEP_CODES+=("$rc")
}

step_status() {  # step_status NAME -- prints the recorded exit code, or nothing if NAME never ran
  local name="$1" i
  for i in "${!STEP_NAMES[@]}"; do
    if [ "${STEP_NAMES[$i]}" = "$name" ]; then
      echo "${STEP_CODES[$i]}"
      return 0
    fi
  done
  return 1
}

write_steps_json() {
  {
    echo "{"
    local first=1 i
    for i in "${!STEP_NAMES[@]}"; do
      [ "$first" -eq 1 ] || echo ","
      first=0
      printf '  "%s": %s' "${STEP_NAMES[$i]}" "${STEP_CODES[$i]}"
    done
    echo
    echo "}"
  } >"$OUT/steps.json"
}

# --- UI bundle (ui/both) -----------------------------------------------------

# Built before the claude call, not inside it: scripts/start_local_ui.py only
# builds when src/ui/build/ is missing, and an npm ci + react-scripts build
# inside one Bash tool call would race the tool's timeout. The UI smoke tests
# below reuse the same bundle.
if [ "$MODE" != "chat" ]; then
  step build-ui build_ui
fi

# --- the headless /modernize run (or its dry-run stand-in) -----------------

if [ "$DRY_RUN" = "1" ]; then
  PHASE="dry-run-transcript"
  : "${E2E_LLM_TRANSCRIPT:?E2E_LLM_DRY_RUN=1 requires E2E_LLM_TRANSCRIPT=<path to a transcript .jsonl>}"
  run_claude() {
    log "dry run: using fixture transcript $E2E_LLM_TRANSCRIPT (no claude call, no model access needed)"
    cp "$E2E_LLM_TRANSCRIPT" "$OUT/transcript.jsonl"
    echo "claude exit=0 duration=0s (dry run)" | tee "$OUT/claude-exit.txt" >/dev/null
  }
  CLAUDE_STARTED=1
  step claude run_claude
else
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-global.anthropic.claude-sonnet-5-5}"
  export DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1
  PHASE="require-env"
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    require_env CLAUDE_CODE_USE_BEDROCK AWS_REGION
  fi

  CLAUDE_BIN="${CLAUDE_BIN:-claude}"
  CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.288}"
  if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    PHASE="install-claude"
    log "installing Claude Code (@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION})"
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
  fi
  PHASE="claude-version"
  "$CLAUDE_BIN" --version </dev/null | tee "$OUT/claude-version.txt"

  HELP_TEXT="$("$CLAUDE_BIN" --help </dev/null 2>&1 || true)"
  CLAUDE_FLAGS=(--output-format stream-json --settings "$REPO_ROOT/.claude/settings.ci.json" --permission-mode dontAsk)
  if grep -q -- '--max-turns' <<<"$HELP_TEXT"; then
    CLAUDE_FLAGS+=(--max-turns "${E2E_LLM_MAX_TURNS:-200}")
  else
    echo "warning: $CLAUDE_BIN --help does not list --max-turns; the run is capped only by" \
      "--max-budget-usd (if supported) and the ${E2E_LLM_TIMEOUT:-3600}s wall-clock timeout" >&2
  fi
  if grep -q -- '--max-budget-usd' <<<"$HELP_TEXT"; then
    CLAUDE_FLAGS+=(--max-budget-usd "${E2E_LLM_MAX_BUDGET_USD:-20}")
  fi
  if grep -q -- '--setting-sources' <<<"$HELP_TEXT"; then
    CLAUDE_FLAGS+=(--setting-sources project)
  fi
  if grep -q -- '--strict-mcp-config' <<<"$HELP_TEXT"; then
    CLAUDE_FLAGS+=(--strict-mcp-config)
  fi
  if grep -q -- '--verbose' <<<"$HELP_TEXT"; then
    CLAUDE_FLAGS+=(--verbose)
  fi
  {
    printf 'flags:'
    printf ' %q' "${CLAUDE_FLAGS[@]}"
    printf '\n'
  } | tee "$OUT/claude-flags.txt"

  PHASE="prepare-input"
  log "prepare input"
  WORK="$OUT/input"
  mkdir -p "$WORK"
  unzip -o -q "$REPO_ROOT/docs/examples/$FIXTURE/$FIXTURE.zip" -d "$WORK"
  # This is the job cursor the orchestrator reads/writes, not a data source of
  # truth -- removing it only resets which job the NEXT run picks up from, and
  # never touches anything under $ARTIFACT_ROOT. See ci/README.md.
  rm -f "$REPO_ROOT/.modernizer-state.json"

  TIMEOUT_BIN=""
  if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_BIN="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_BIN="gtimeout"
  else
    log "no timeout/gtimeout found on PATH; running claude with no wall-clock timeout"
  fi

  CLAUDE_CMD=(env MODERNIZER_CI_SANDBOX=1)
  [ -n "$TIMEOUT_BIN" ] && CLAUDE_CMD+=("$TIMEOUT_BIN" "${E2E_LLM_TIMEOUT:-3600}")
  CLAUDE_CMD+=("$CLAUDE_BIN" -p "/modernize $WORK/$FIXTURE-collection.json --auto --mode $MODE" "${CLAUDE_FLAGS[@]}")

  run_claude() {
    local start_ts rc
    start_ts=$(date +%s)
    # </dev/null: `claude -p` reads stdin as extra prompt input; never let it
    # block on (or swallow) the CI runner's stdin.
    if "${CLAUDE_CMD[@]}" >"$OUT/transcript.jsonl" </dev/null; then
      rc=0
    else
      rc=$?
    fi
    echo "claude exit=$rc duration=$(($(date +%s) - start_ts))s" | tee "$OUT/claude-exit.txt" >/dev/null
    return "$rc"
  }
  log "headless /modernize ($MODE, $FIXTURE)"
  CLAUDE_STARTED=1
  step claude run_claude
fi
PHASE="after-claude"

# --- transcript health + mode assertion -------------------------------------

run_check_transcript() {
  uv run python ci/llm/run.py check-transcript "$OUT/transcript.jsonl" --mode "$MODE" >"$OUT/summary.json"
}
step check-transcript run_check_transcript

DB=""
JOB=""
if [ -s "$OUT/summary.json" ]; then
  DB_JOB="$(
    uv run python - "$OUT/summary.json" <<'PYEOF' || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get("db") or "", d.get("job_id") or "")
except Exception:
    print("", "")
PYEOF
  )"
  read -r DB JOB <<<"$DB_JOB"
fi

# --- deliverable checks + judge, only if we have a job to point them at -----

if [ -n "$DB" ] && [ -n "$JOB" ]; then
  log "deliverable checks for db=$DB job=$JOB"

  # The headless run may have left its own local API/UI up (ui/both); the e2e
  # fixtures below start their own on the same ports, so stop any leftovers first.
  uv run python scripts/start_local_ui.py --stop >/dev/null 2>&1 || true

  run_install_browsers() {
    if [ "${CI:-}" = "true" ]; then
      uv run playwright install --with-deps chromium webkit
    else
      uv run playwright install chromium webkit
    fi
  }
  step install-browsers run_install_browsers

  run_report_tests() {
    E2E_ARTIFACT_ROOT="$ARTIFACT_ROOT" E2E_DB="$DB" E2E_JOB="$JOB" E2E_OUTPUT="$OUT" \
      uv run pytest tests/e2e -m "e2e and not deterministic" -p no:xdist --fail-on-skip \
      --ignore=tests/e2e/test_ui.py --browser chromium --browser webkit \
      --output="$OUT/pw-reports" --junitxml="$OUT/reports-junit.xml" -q
  }
  step report-tests run_report_tests

  if [ "$MODE" != "chat" ]; then
    run_ui_tests() {
      E2E_ARTIFACT_ROOT="$ARTIFACT_ROOT" E2E_DB="$DB" E2E_JOB="$JOB" E2E_OUTPUT="$OUT" \
        uv run pytest tests/e2e/test_ui.py -m e2e -p no:xdist --fail-on-skip --browser chromium \
        --output="$OUT/pw-ui" --junitxml="$OUT/ui-junit.xml" -q
    }
    step ui-tests run_ui_tests
  fi

  run_judge() {
    uv run python ci/llm/judge.py --artifact-root "$ARTIFACT_ROOT" --db "$DB" --job "$JOB" --out "$OUT/judge.json"
  }
  step judge run_judge
else
  log "no job_id/db parsed from the transcript (check-transcript failed) -- skipping deliverable checks and judge"
fi

# --- merge everything into results.json -------------------------------------

JUNIT_ARGS=("$OUT/reports-junit.xml")
[ "$MODE" != "chat" ] && JUNIT_ARGS+=("$OUT/ui-junit.xml")

PHASE="results"
run_results() {
  uv run python ci/llm/run.py results --out "$OUT/results.json" \
    --transcript-summary "$OUT/summary.json" \
    --pytest-junit "${JUNIT_ARGS[@]}" \
    --judge "$OUT/judge.json" \
    --mode "$MODE" --fixture "$FIXTURE" \
    --claude-exit "$OUT/claude-exit.txt"
}
step results run_results

write_steps_json
cat "$OUT/results.json" 2>/dev/null || echo "(no results.json produced)"

EXIT_CODE=0
[ "$(step_status claude)" = "0" ] || EXIT_CODE=1
[ "$(step_status results)" = "0" ] || EXIT_CODE=1
if [ "$EXIT_CODE" -ne 0 ]; then
  log "e2e-llm failed -- see $OUT/steps.json and $OUT/results.json"
fi
exit "$EXIT_CODE"
