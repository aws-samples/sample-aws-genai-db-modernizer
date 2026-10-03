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
`--fail-on-skip` meaningful for both suites. The cost: each invocation is its
own pytest session, so the deterministic pipeline runs the two sample
pipelines once each per invocation — ~15s total (wordpress ~6s, discourse
~9s, measured), rather than once overall across both invocations. Accepted as
cheap relative to the correctness it buys.

`tests/e2e/conftest.py`'s `run`/`all_runs` fixtures cache each sample's
pipeline result in a module-level dict rather than relying on pytest's own
fixture caching: `run` is both session-scoped *and* parametrized (one value
per sample), and pytest-playwright's `--browser chromium --browser webkit`
effectively parametrizes `browser_name` too, which made pytest tear down and
re-create `run` up to 13 times a session instead of twice (measured via
`--setup-plan`) — rerunning the full pipeline every time. The module-level
cache makes each sample's pipeline run exactly once per invocation regardless
of how pytest schedules fixture teardown.

`$@` passed to this script goes to BOTH pytest invocations below; `-k` or any
other filter that only matches one suite's tests makes the other invocation
collect zero items and exit 5. Use `E2E_REPORT_ARGS` / `E2E_UI_ARGS` to target
extra args at only the first or second invocation respectively.

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
- `E2E_REPORT_ARGS`, `E2E_UI_ARGS` — extra pytest args appended to only the
  first (report checks) or second (UI smoke) invocation, respectively.

Installing browsers: `uv run playwright install --with-deps chromium webkit`
in CI (`CI=true`, set automatically by GitHub Actions) — `--with-deps` pulls
OS-level dependencies via `apt` and needs root, which Linux CI runners have.
Locally (including macOS, where there is no `apt`) it drops `--with-deps` and
installs just the browser binaries.

## `../.claude/settings.ci.json`

The permission allowlist for headless `claude -p "/modernize ..." --permission-mode
dontAsk` runs. JSON has no comment syntax, so the rationale for its less
obvious entries lives here instead:

- `Bash(uv run python scripts/start_local_ui.py)` and `...py *)` (both forms):
  this is the only command `/modernize --mode ui|both` runs to start the local
  API + UI — see `scripts/start_local_ui.py`'s module docstring for why the
  multi-command shell pipeline it replaced couldn't be allowlisted at all.
  Both the bare and `*`-suffixed forms are listed because it's untested
  whether a headless run ever invokes the script with zero arguments (it
  always does, today) — keeping both avoids relying on that.
- `Bash(rm *)`, `Bash(sudo *)`, `Bash(wget *)`, `Bash(nc *)` in `deny`: nothing
  in the pipeline needs any of these. They're explicit denies, not just
  absent from `allow`, as defense in depth against a prompt-injected or
  hallucinated command slipping through — an explicit deny always wins over
  an allow rule, so even a future overly broad `Bash(* )`-style allow
  addition couldn't reopen this door by accident.
