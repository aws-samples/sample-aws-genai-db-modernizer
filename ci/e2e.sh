#!/usr/bin/env bash
# Deterministic end-to-end run: pipeline with no LLM -> deliverables -> browser and
# PDF checks -> UI smoke -> help site checks.
# Usage: ci/e2e.sh [extra pytest args]     (needs: uv, node 22, playwright browsers,
#   the `docs` extra for the mkdocs build this script does itself)
#
# Env vars read:
#   E2E_OUTPUT       - where Playwright artifacts (screenshots, traces, junit) and
#                       the copied-out deliverables land. Defaults to test-results/.
#   E2E_REPORT_ARGS  - extra pytest args for the first invocation ONLY (below).
#   E2E_UI_ARGS      - extra pytest args for the second invocation ONLY (below).
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
# parametrization) runs the two sample pipelines once each per invocation --
# ~15s total (measured: wordpress ~6s, discourse ~9s) -- rather than once
# overall across both invocations. An acceptable, explicit, now-correctly-sized
# cost: this used to be mis-stated as "~13s each" and, before the fixture-cache
# fix, was actually up to 13 reruns of one sample's pipeline per invocation.
source "$(dirname "$0")/lib.sh"

export E2E_OUTPUT="${E2E_OUTPUT:-test-results}"
export E2E_REPORT_ARGS="${E2E_REPORT_ARGS:-}"
export E2E_UI_ARGS="${E2E_UI_ARGS:-}"
mkdir -p "$E2E_OUTPUT"

log "install browsers"
if [ "${CI:-}" = "true" ]; then
  # --with-deps installs OS-level browser dependencies via apt and needs root/sudo.
  # Fine on GitHub's Linux runners (CI=true is set automatically); on a developer's
  # macOS machine it has nothing to install and fails looking for apt-get.
  uv run playwright install --with-deps chromium webkit
else
  uv run playwright install chromium webkit
fi

build_ui

log "build the help site (#325)"
# tests/e2e/test_docs_site.py only serves and checks site/ -- same split as
# build_ui/test_ui.py above. Needs the `docs` extra (mkdocs, mkdocs-material).
uv run mkdocs build --strict
uv run python scripts/build_docs_sample.py --site-dir site

log "pipeline + report checks (chromium, webkit)"
# --output is its own subdirectory, not $E2E_OUTPUT itself: pytest-playwright's
# delete_output_dir autouse fixture rmtree()s whatever --output points at, at the
# start of EVERY session. Pointing both invocations' --output straight at
# $E2E_OUTPUT would let the second invocation silently delete the first
# invocation's junit file and the deliverables already copied there.
uv run pytest tests/e2e --ignore=tests/e2e/test_ui.py --ignore=tests/e2e/test_docs_site.py \
  -m e2e -p no:xdist --fail-on-skip \
  --browser chromium --browser webkit \
  --output="$E2E_OUTPUT/pw-reports" --screenshot=only-on-failure --tracing=retain-on-failure \
  --junitxml="$E2E_OUTPUT/e2e-reports-junit.xml" "$@" $E2E_REPORT_ARGS

log "UI smoke + help site checks (chromium only)"
uv run pytest tests/e2e/test_ui.py tests/e2e/test_docs_site.py -m e2e -p no:xdist --fail-on-skip \
  --browser chromium \
  --output="$E2E_OUTPUT/pw-ui" --screenshot=only-on-failure --tracing=retain-on-failure \
  --junitxml="$E2E_OUTPUT/e2e-ui-junit.xml" "$@" $E2E_UI_ARGS
