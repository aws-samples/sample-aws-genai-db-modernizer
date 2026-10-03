# `ci/`

CI-agnostic entry points. Every CI system (GitHub Actions today, GitLab CI in a
later plan) calls these scripts rather than inlining commands, so "what does CI
run" has one answer and a passing local run means a passing CI run.

Run any of them locally with `make lint`, `make test`, `make e2e` (or invoke the
scripts directly — they all accept extra pytest args after the script name).

## `lib.sh`

Shared helpers (`log`, `require_env`, `build_ui`). Sourced by the other
scripts, not run directly. `set -euo pipefail` and `cd` to the repo root live
here so every script behaves the same regardless of the caller's working
directory. `build_ui` (install deps, build the production bundle under
`src/ui/build/`) is used by both `e2e.sh` and `e2e-llm.sh` so the UI build
step has one implementation.

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

## `e2e-llm.sh`

Headless `/modernize` run on model access (Bedrock or the Anthropic API):
`claude -p "/modernize ... --auto --mode <chat|ui|both>" --permission-mode
dontAsk --output-format stream-json` against one of the two sample fixtures,
then the same deliverable checks `e2e.sh` runs (contracts, HTML, PDF, and --
for `ui`/`both` -- the UI smoke test) pointed at the job the headless run
produced, plus a rubric-based quality judge (`ci/llm/judge.py` /
`ci/llm/rubric.md`) grading the rendered deliverables. Everything is merged
into one `results.json` row by `ci/llm/run.py results`.

```
make e2e-llm                                        # chat / wordpress (defaults)
make e2e-llm E2E_LLM_MODE=ui E2E_LLM_FIXTURE=discourse
./ci/e2e-llm.sh both wordpress                       # or invoke the script directly
```

**What it checks**: the transcript's final `type == "result"` line
(`ci/llm/run.py check-transcript`) is healthy -- no `is_error`, no
`permission_denials`, a `MODERNIZE_RESULT: complete job_id=... db=... mode=...`
line -- and that the run's mode matches what actually happened (`chat` never
started the local UI; `ui`/`both` started it via
`scripts/start_local_ui.py` and got back a `{"status": "ready"}` tool
result). Then the deliverable checks (`tests/e2e`, pointed at the headless
job via `E2E_ARTIFACT_ROOT`/`E2E_DB`/`E2E_JOB` -- see "external-job mode"
under `e2e.sh`'s own test suite) and the judge run exactly as they do for a
deterministic job.

**Outputs**, all under `$E2E_OUTPUT/llm-<mode>-<fixture>/` (default
`test-results/llm-<mode>-<fixture>/`):

- `claude-version.txt`, `claude-flags.txt` -- the installed CLI's version and
  the exact flags this run chose (see "CLI flag feature-detection" below).
- `input/` -- the sample fixture, unzipped here rather than into the repo
  root.
- `transcript.jsonl` -- the full stream-json transcript.
- `claude-exit.txt`, `summary.json` -- the claude process's exit code/duration,
  and `ci/llm/run.py check-transcript`'s parsed summary (or `{"error": ...}`).
- `reports-junit.xml`, `ui-junit.xml` (ui/both only), `pw-reports/`, `pw-ui/` --
  same shapes as `e2e.sh`'s outputs, produced by the same pytest invocations.
- `judge.json` -- `ci/llm/judge.py`'s rubric scores.
- `steps.json` -- exit code of every step (`build-ui` (ui/both, run *before*
  the claude call so `scripts/start_local_ui.py` finds `src/ui/build/` and
  never builds inside a Bash tool call), `claude`, `check-transcript`,
  `install-browsers`, `report-tests`, `ui-tests`, `judge`, `results`); a step
  failing never stops the script early. Preflight failures before the run
  (missing credentials, the CLI install, `claude --version`, unzipping the
  fixture, a rejected `E2E_LLM_ARTIFACT_ROOT`) exit early, and the EXIT trap
  then writes a minimal `{"schema_version": 1, "pass": false, "error": "<step>
  failed before the run"}` -- so `results.json` always exists.
- `results.json` -- the one merged row (`schema_version`, `timestamp`,
  `git_sha`, `mode`, `fixture`, `model`, `job_id`, `db`, `cost_usd`,
  `num_turns`, `tokens_in`/`tokens_out`, `duration_s`, `checks: {transcript,
  contracts, html, pdf, ui, judge}` (`ui` is `null` for `chat`),
  `transcript_error` (`null` unless check-transcript failed; cost, turns and
  tokens are reported either way), `judge: {mean, scores}`, `pass`). Plan 3b uploads this to the results store, so it's
  kept flat and JSON-shaped on purpose.

**Env vars**: `CLAUDE_BIN` (default `claude`), `CLAUDE_CODE_VERSION` (npm
version to install when `$CLAUDE_BIN` is missing, default pinned to
`2.1.288`, the published version when this was written -- bump it
deliberately), `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY` *or*
`CLAUDE_CODE_USE_BEDROCK` + `AWS_REGION`, `E2E_LLM_TIMEOUT` (seconds, default
3600), `E2E_LLM_MAX_TURNS` (default 200) and `E2E_LLM_MAX_BUDGET_USD`
(default 20), each only passed if the CLI's own `--help` mentions the flag,
`E2E_LLM_ARTIFACT_ROOT` (dry-run only, default `$REPO_ROOT/artifacts`; see
below), `E2E_OUTPUT` (as in `e2e.sh`).

**`E2E_LLM_ARTIFACT_ROOT`** is honoured only with `E2E_LLM_DRY_RUN=1`. A
real run always uses `$REPO_ROOT/artifacts` and exits 2 if the variable names
anything else: the headless run's scripts write `./artifacts` by default, the
allowlist only permits `Edit`/`Write` under `artifacts/**`, and the sandbox
(below) refuses roots outside the repo -- so a different root could only make
the deliverable checks look at a different (or no) job than the one the run
produced.

**Sandbox**: the claude process runs with `MODERNIZER_CI_SANDBOX=1` (only
that process and its tool calls, not the rest of this script). Every
allowlisted script then refuses `--file`/`--artifact-root` values that
resolve outside the repo and `--db`/`--job-id` values that aren't a single
`[A-Za-z0-9_.-]+` path component (`scripts/_sandbox.py`), exiting with its
normal JSON error. This covers what permission rules can't: a `Read` deny
rule doesn't stop an allowlisted Python script from opening a path itself.
The claude call's stdin is `/dev/null`, so it never reads the runner's stdin.

**CLI flag feature-detection**: the CLI build installed locally while writing
this script (an internal v2.1.288 build) does not list `--max-turns` in its
own `--help`; CI installs the public `@anthropic-ai/claude-code` npm package,
which does, and additionally requires `--verbose` alongside
`--output-format stream-json`. Rather than hardcode either CLI's behavior,
the script captures `"$CLAUDE_BIN" --help` once and only adds `--max-turns`,
`--max-budget-usd`, `--setting-sources project` (load only the project's
settings files, plus `--settings`), `--strict-mcp-config` (ignore MCP servers
configured outside `--mcp-config`) and `--verbose` if the help text mentions
them, logging the chosen flags (and `claude --version`) into the output
directory either way, and warning on stderr when `--max-turns` is missing.
`ci/llm/judge.py` feature-detects `--setting-sources`/`--strict-mcp-config`
the same way.

**Dry-run mode** (`E2E_LLM_DRY_RUN=1`, plus `E2E_LLM_TRANSCRIPT=<path>`):
skips the claude call entirely and copies the given transcript `.jsonl` in
its place, so the rest of the script -- `check-transcript`, the deliverable
checks, the judge (still a real subprocess call, typically pointed at a stub
via `CLAUDE_BIN`), and `results.json` -- can be exercised locally with no
model access. The transcript's `MODERNIZE_RESULT` line must name a job/db
that actually exists under `E2E_LLM_ARTIFACT_ROOT` and must match the
`<mode> <fixture>` arguments given on the command line.

**Cost and time**: to be measured on the first internal-pipeline run (see
`ci/llm/run.py`'s module docstring -- the stream-json field names it parses
are assumptions from public docs, not yet confirmed against a real recorded
transcript; Step 1 of the task that introduced this script, recording one,
was explicitly skipped to avoid spending tokens outside of model access the
user has approved).

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
- No `Read`/`Glob`/`Grep` allow rule at all: per the permissions docs,
  `dontAsk` still runs "file reads in your working directories" without a
  rule, and a bare `Read` allow would extend that to the whole filesystem.
  The `Read(...)` denies (`//proc`, `//sys`, `//run/secrets`,
  `//var/run/secrets`, `//root`, `~/.aws`, `~/.ssh`, `~/.config`,
  `~/.docker`, `~/.npmrc`, `~/.netrc`, `~/.claude.json`, `.env`, `.env.*`,
  `.npmrc` anywhere) cover reads that would otherwise be reachable through an
  `--add-dir` or a future broadening. `//` anchors an absolute path and `~/`
  the home directory (gitignore-style patterns). Read denies apply to the
  built-in file tools and to file commands Claude Code recognizes in Bash
  (`cat`, `head`, ...), **not** to what an allowlisted script opens itself --
  that is what `MODERNIZER_CI_SANDBOX=1` / `scripts/_sandbox.py` is for (see
  `e2e-llm.sh` below).
- `Edit`/`Write` of `.local-ui/**` (and the old `artifacts/.local-ui/**`) are
  denied: that is `scripts/start_local_ui.py`'s pid file, and `--stop` signals
  the pids it lists.
- `Skill(reality-check)`, `Skill(synthesize)`, `Skill(design-schema-*)` plus
  each `Skill(design-schema-<engine>)` by exact name: the sub-commands
  `/modernize` dispatches to subagents. The prefix form is documented, but
  whether a `.claude/commands/*.md` command dispatched from a subagent goes
  through the `Skill` tool (and so needs these rules at all) is **to be
  confirmed on the first real run** -- a missing rule shows up as a
  `permission_denials` entry, which fails `check-transcript`.
- `BASH_DEFAULT_TIMEOUT_MS=600000` / `BASH_MAX_TIMEOUT_MS=900000` in `env`:
  analysis and schema-design phases can run for minutes inside one Bash tool
  call, well past the 2-minute default. These variables are not on the
  public env-var page as of this writing; to be confirmed on the first real
  run (a timeout shows up as a killed Bash call in the transcript).
- `Bash(curl *)` joins `wget`/`nc` in `deny` for the same reason.
