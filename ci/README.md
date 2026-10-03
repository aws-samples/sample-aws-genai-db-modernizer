# `ci/`

CI-agnostic entry points. Every CI system (GitHub Actions today, GitLab CI in a
later plan) calls these scripts rather than inlining commands, so "what does CI
run" has one answer and a passing local run means a passing CI run.

Run any of them locally with `make lint`, `make test`, `make e2e` (or invoke the
scripts directly — they all accept extra pytest args after the script name).

## `lib.sh`

Shared helpers (`log`, `require_env`). Sourced by the other scripts, not run
directly. `set -euo pipefail` and `cd` to the repo root live here so every
script behaves the same regardless of the caller's working directory.

## `lint.sh`

Lint and type-check: ruff, black, isort, mypy, and the Claude Code command
validator (`scripts/validate_skills.py`). Same commands and arguments as the
pre-commit hooks and the `lint` CI job, so a clean `pre-commit run --all-files`
and a clean `ci/lint.sh` agree. No env vars. No prerequisites beyond `uv sync`.

## `test.sh`

Unit, contract, property and graph tests (`tests/` minus `tests/integration`
and `tests/e2e`), selected two ways: `--ignore=tests/e2e` so pytest never
*imports* `tests/e2e/*` (it needs the `e2e` extra -- playwright, pypdf --
which this script's prerequisites do not install), and
`-m "not integration and not e2e"` as a second, belt-and-suspenders guard for
anyone running this script with the `e2e` extra already installed. Plus
`--fail-on-skip`, so nothing in this run may silently skip. No network, no
credentials. Accepts extra pytest args, e.g. `./ci/test.sh --cov=src
--cov-report=term --cov-fail-under=65` for the coverage gate CI runs with.

## `e2e.sh`

The deterministic end-to-end suite (`tests/e2e/`): runs the full pipeline
(collect → triage → analysis per engine → assignment → reality check →
synthesis → report) with no LLM and no AWS credentials, then checks the HTML
reports in real browsers (Chromium + WebKit), the PDF content, and a local-UI
smoke test against the produced artifacts.

Prerequisites: `uv`, Node 22, and Playwright's browser binaries (the script
installs them). The UI build step honors whatever npm registry is already
configured (an internal mirror locally, the public registry in CI) — it is
never hardcoded here.

Runs pytest **twice**, not once:

1. `tests/e2e --ignore=tests/e2e/test_ui.py` with `--browser chromium --browser webkit`
   → `$E2E_OUTPUT/e2e-reports-junit.xml`
2. `tests/e2e/test_ui.py` with `--browser chromium` only
   → `$E2E_OUTPUT/e2e-ui-junit.xml`

`test_ui.py` is Chromium-only (`pytest.mark.only_browser("chromium")`), and
pytest-playwright implements that marker as a runtime skip rather than a
deselection. Under a single `--browser chromium --browser webkit` invocation
combined with `--fail-on-skip`, the webkit parametrizations of the UI suite
would be skipped-then-failed. Splitting into two invocations keeps
`--fail-on-skip` meaningful for both suites. The cost: the deterministic
pipeline fixture is session-scoped, so it reruns once per pytest session —
once per invocation here (~13s each), rather than once overall. Accepted as
cheap relative to the correctness it buys.

Env vars read:

- `E2E_OUTPUT` — root directory for everything this script produces. Defaults
  to `test-results/`. Under it:
  - `e2e-reports-junit.xml`, `e2e-ui-junit.xml` — one junit file per invocation.
  - `deliverables/<db>/` — a copy of each pipeline run's `synthesis/v*/` files
    (the HTML reports, PDF, pptx), written by a session-scoped autouse
    finalizer in `tests/e2e/conftest.py`, so reviewers can download the
    rendered output straight from a CI artifact without re-running anything.
  - `pw-reports/`, `pw-ui/` — Playwright's own `--output` directories
    (screenshots on failure, traces on failure), one per invocation. They are
    *not* both named `$E2E_OUTPUT` directly: pytest-playwright's
    `delete_output_dir` autouse fixture `rmtree()`s whatever `--output` points
    at on every session start, and the two invocations below are two separate
    pytest sessions. Pointing `--output` straight at `$E2E_OUTPUT` would let
    the second invocation delete the first invocation's junit file and
    already-copied deliverables. Giving each invocation its own subdirectory
    keeps all of it under one `$E2E_OUTPUT` root for CI to upload as a single
    artifact, without the second run clobbering the first.

Installing browsers: `uv run playwright install --with-deps chromium webkit`
in CI (`CI=true`, set automatically by GitHub Actions) — `--with-deps` pulls
OS-level dependencies via `apt` and needs root, which Linux CI runners have.
Locally (including macOS, where there is no `apt`) it drops `--with-deps` and
installs just the browser binaries.
