# `ci/`

CI-agnostic entry points. Every CI system (GitHub Actions today, GitLab CI in a
later plan) calls these scripts rather than inlining commands, so "what does CI
run" has one answer and a passing local run means a passing CI run.

Every CI environment runs the same tree, so a red required check in any CI environment stops merges, even when it fails on code the PR didn't touch: file it and fix it first. A test never branches on which tree it runs in: `tests/unit/ci/test_no_tree_dependent_escapes.py` fails on any test that passes because a repo path does not exist (an early return, `continue`, skip, swallowed `FileNotFoundError`, or asserts wrapped in `if p.exists():`, and `skipif`/`xfail`/`skipUnless` on such a check); assert unconditionally instead. Its docstring lists what it follows and what it cannot see.

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
validator (`scripts/validate_skills.py`), the rule catalog's generated files
(`scripts/render_catalog.py --check`), the `assess()` JSON Schemas
(`scripts/render_contract_schemas.py --check`: `docs/contracts/schemas/` matches the public
models, #1183) and the price table's age
(`scripts/refresh_prices.py --check-age`: it warns, never fails, when
`src/catalog/prices.toml` was retrieved more than 45 days ago; refresh it with
`uv run python scripts/refresh_prices.py`, #1196). The `Refresh prices` workflow
(`.github/workflows/refresh-prices.yml`) runs that refresh on the 1st of every month and
on demand, and opens or updates one pull request from `chore/price-table-refresh` when
the table changes; 45 days is the longest gap between two runs (31) plus two weeks to
merge its pull request. That pull request is opened with the workflow's own token, so
close and reopen it to start the required checks. Same commands and arguments as the
pre-commit hooks and the `lint` CI job, so a clean `pre-commit run --all-files`
and a clean `ci/lint.sh` agree. No env vars. No prerequisites beyond `uv sync`.

## `check_commits.sh`

Checks every non-merge commit added on top of `origin/main`
(`scripts/check_commit_message.py --range origin/main..HEAD`): subject at
most 72 characters, a 2-4 non-blank-line body, a trailing `Closes #N` or
`Refs #N` trailer, and no `Co-authored-by` trailer. Merge, fixup/squash/
amend, and default `git revert` commits are skipped. The CI counterpart of
the local `check-commit-message` pre-commit hook (`commit-msg` stage, so it
only runs locally) -- this script also catches a `--no-verify` commit or a
message edited after the hook ran.

In this `--range` path only, a commit committed before
`RULE_EFFECTIVE_DATE` (2026-10-13T00:00:00Z, the day this check landed, a
named constant in `scripts/check_commit_message.py`) is grandfathered in --
skipped, with `skipped <sha> (before the commit-message rule)` printed --
rather than counted as a violation, so merging this check does not
retroactively turn every already-open PR red for commits nobody could have
made compliant. The local hook has no such cutoff: it only ever validates
the message being written right now.

It also checks every non-merge commit's author (#1408,
`scripts/check_commit_authors.py --range origin/main..HEAD`). Only the commits
the branch adds are judged: history already on main is never re-checked, so the
older commits there under other names stay as they are. The rules, in order:

- a name of one ASCII character, or none, is refused everywhere (`x` once landed
  on main); one non-ASCII letter, such as a one-character CJK name, is a name;
- an identity listed in `ci/allowed_commit_authors.txt` passes (exact name, email
  in any case): the maintainer and the maintainer's older `Esteban Serna` and
  `tebanieo` names, the authors of imported contributions as their commits record
  them, Dependabot;
- a commit under the maintainer's address (`MAINTAINER_EMAILS` in the script) that
  is not listed is refused in every mode, so a contributor pull request's run and
  the merge queue agree and the pull request fails first;
- on a pull request opened by the maintainer account (`MAINTAINER_LOGINS`),
  nothing else passes but that account's GitHub noreply address;
- everywhere else (a contributor's or Dependabot's pull request, a `merge_group`
  run, where the queue rebases several pull requests' commits with the maintainer
  as committer, and a local run before a pull request exists, without `gh`, or
  from a fork) any well-formed author passes: a name that is not an ASCII
  placeholder (`x`, `root`, `user`, `unknown`, `Your Name`, ...) and an email with
  a dotted host that is not a placeholder (`example.com`, `localhost`, a `.local`
  host). Contributors need no allowlist entry.

The script learns the pull request author from the GitHub Actions event file
(`GITHUB_EVENT_PATH`, `pull_request.user.login`) on a `pull_request` run, and
outside Actions from `gh pr view` for the current branch; when neither gives one
it uses the well-formed rule. It prints which case it used. The message check and
the author check both run, and the script fails when either does.

A no-op on a push to main: the range is then empty, since `HEAD` already is
`origin/main`. Needs `origin/main` available locally -- the `lint` CI job's
checkout step uses `fetch-depth: 0` so a pull request build has it (and
every commit the pull request adds) even though the pull request branch and
`main` may not share history within a shallow clone; a normal local clone
already has full history. No env vars of its own; the author check reads GitHub Actions' `GITHUB_EVENT_NAME` and `GITHUB_EVENT_PATH` when they are set.

## `test.sh`

Unit, contract, property and graph tests (`tests/` minus `tests/integration`
and `tests/e2e`), selected two ways: `--ignore=tests/e2e` so pytest never
*imports* `tests/e2e/*` (it needs the `e2e` extra -- playwright, pypdf --
which this script's prerequisites do not install), and
`-m "not integration and not e2e"` as a second, belt-and-suspenders guard for
anyone running this script with the `e2e` extra already installed. Plus
`--fail-on-skip`, so nothing in this run may silently skip. No network, no
credentials. Accepts extra pytest args, e.g. `./ci/test.sh --cov=src
--cov-report=term --cov-fail-under=65` for the coverage gate (CI applies
the same 65% gate to the combined shards, see Sharding below).

Hypothesis profiles: `tests/conftest.py` loads the profile named by `HYPOTHESIS_PROFILE`, so it applies to every entry point, including a plain `uv run pytest tests/`. `default` (100 examples per property) is what runs when the variable is unset: locally, on main and nightly. `ci` (20 examples, and `tests/hypothesis_examples.scaled(n)` shrinks a test's own `max_examples` by the same share) is for pull requests: this script sets `HYPOTHESIS_PROFILE=ci` when `GITHUB_EVENT_NAME` is `pull_request`, unless the variable is already set. Any other CI system that calls pytest directly can set `HYPOTHESIS_PROFILE=ci` on its pull request runs the same way. To run a pull request's budget locally: `HYPOTHESIS_PROFILE=ci ./ci/test.sh`.

Expensive sample runs are shared: `tests/shared_runs.py` builds each one (the deterministic wordpress pipeline, the `run_assessment.py` runs on discourse and wordpress) at most once per pytest session, across every xdist worker, under a file lock in the session's temporary directory. The cache never outlives the session. A test that writes to a shared job copies it first with `copy_run`.

Sharding (#956): `TEST_SHARD=N/M ./ci/test.sh` runs only shard N of M, split by [pytest-split](https://github.com/jerry-git/pytest-split) (`--splits M --group N --splitting-algorithm least_duration`) from the per-test durations recorded in `.test_durations` at the repo root. The M shards together run exactly the tests an unsharded run does; a test missing from the file counts as the average duration, so new tests only make the balance a little worse, never drop a test. The GitHub `test-shard (N/4)` jobs run it with four shards, and the required `test` job passes only when all four did. Coverage (`--cov=src`) runs on pushes to main and in the nightly scheduled run, not on pull requests: each shard uploads its `.coverage` data and the `test` job combines them and applies the same 65% gate to the whole suite.

Refresh the durations after adding, renaming or speeding up many tests (on any machine; only the relative times matter): `./ci/test.sh --store-durations --clean-durations`, then commit `.test_durations`. Record them with the variable unset (the full `default` Hypothesis profile, as main and nightly run); pull requests run the lighter `ci` profile, which shortens the property tests in every shard alike, so the balance holds. `--clean-durations` drops entries for tests that no longer exist. The file is about 1.9 MB, so it is excluded from the pre-commit large-file check, and from the end-of-file fixer because pytest-split writes it without a trailing newline.

The public `assess()` contract's consumer tests run here too (#1184): `tests/contract/test_assess_consumer_contract.py` runs `assess()` on the fixtures in `tests/contract/fixtures/assess_consumer_fixtures.json` for every supported `run_through` (model entry points patched) and checks each fixture's guarded fields against the published result schema, and `tests/contract/test_assess_released_contract.py` fails a breaking change since the last release (`tests/contract/assess_released_snapshot.json`) without a MAJOR bump. The same comparison against main's copy of that file runs in `scripts/resolve_contract_versions.py --check --against` (the `validate-contracts` job and `ci/pre_merge.sh`), so a branch cannot pass by re-recording it. See CONTRIBUTING.md's "The public `assess()` contract".

The raw-query large-server memory regression runs without a platform skip on
Linux, macOS and Windows wherever this test command is run. The GitHub CI
workflow currently gates on Ubuntu only. The worker measures current RSS with
psutil. On Windows, run `ci/test.sh` from Git Bash or WSL, or run
`uv run pytest tests/ --ignore=tests/e2e -m "not integration and not e2e" --fail-on-skip`
directly.

The safety filters' benign-input corpus (#1161) runs here as part of the unit tests: `tests/unit/safety_filters/test_benign_corpus.py` reads `tests/fixtures/safety_filter_corpus/corpus.json` and checks that ordinary SQL, shell and prose pass unchanged through `src/api/secret_guard.py` and `src/shared/prose_style.py`, and that a small set of known-bad strings is still caught. The `safety-filter-corpus` pre-commit hook runs it whenever a commit touches either module or the corpus. A reported false positive or miss goes into the corpus with its fix.

## `../scripts/regenerate_baselines.py` (#1163)

One command for every generated baseline: the routing baseline and snapshot, the contract schema and value-set snapshots, the `assess()` JSON Schemas and the rendered rule catalog. `--check` writes nothing, prints `stale: <name> (<files>)` with that baseline's own command for each stale one, and exits 1 if any is stale. Without `--check` it regenerates only the stale ones by running those same documented commands, in order, so a fresh tree is left byte-identical (the routing commands append to a history on every write); `--reason "..."` is required when a routing baseline is stale, and nothing is written without it. A routing baseline counts as stale only when its guard test fails with its drift message; a failure for any other reason stops the run with an error, so `--reason` never overwrites a baseline over a real bug. `--only <name>` (repeatable) limits the run. A contract snapshot counts as fresh while a pending bump covers the change, exactly as its test does. The released-contract snapshot (`assess_released_snapshot.json`) is recorded at a release with `--write-released <tag>` and is not part of this. The routing checks run the deterministic pipeline on every bundled sample (the same guards as `tests/e2e/test_routing_*.py`), so a full `--check` takes minutes; `--only catalog --only assess-schemas --only contract-schema-snapshot --only value-set-snapshot` takes seconds. The staleness failures in the contract and routing tests, and `pre_merge.sh`'s generated-file warning, name this command. Unit tests: `tests/unit/scripts/test_regenerate_baselines.py` (it also asserts that every file `block_baseline_edit.py` protects, except the released snapshot, is covered here).

## `pre_merge.sh`

A cheap failsafe to run before pushing or asking for a merge: `./ci/pre_merge.sh [--quick] [--strict-main-tests | --no-main-tests]` (`--quick` skips the full test suite and the version-pin check; the main-tests step still runs). In order, each step prints one status line: fetch the public repo's main (`upstream` or `origin`, whichever points at it, `origin` as the fallback); count how many commits the branch is behind main; check every commit's author with `scripts/check_commit_authors.py --range <remote>/main..HEAD` (see `check_commits.sh`; the pull request author comes from `gh pr view` when it is available, else the well-formed rule applies); check for merge conflicts with `git merge-tree --write-tree` (no change to the worktree, index or refs; older git falls back to listing files both sides changed); check contract versions (ADR-008 Decision 8): a changed `src/contracts/*.py` that still has main's version needs a pending bump in `src/contracts/pending/` (declared with `scripts/bump_contract.py`), a hand-written version must be higher than main's (one that collides with main's, or a pending file next to a hand-written version, is an error), and `scripts/resolve_contract_versions.py --check --against <merge base>` fails a contract shape or value change since the merge base with neither a version move nor a pending bump, even when the branch regenerated its own snapshot, and a pending contract whose version the branch also moved by hand; pending bumps are listed as a warning with the exact command to run after the last rebase, `uv run python scripts/resolve_contract_versions.py --base <remote>/main`, where the remote is the one this script fetched (the merge queue refuses a pending file); warn when a generated file (routing snapshot, `tests/contract/schema_snapshot.json`, rendered catalog, `assess()` JSON Schemas) differs from main but its generator check fails; check the ADR touch rule (#1154): a change to a `.py` file under `src/agents/referee/` or `src/shared/` needs an edited file under `docs/architecture/decisions/`, or the line `No ADR change: <reason>` (a real reason, not the template's placeholder) in a commit message or the pull request body (`gh pr view`, the template's ADR field); run the branch's new and changed tests against main's code and list the ones that pass there (see below); run `ci/lint.sh`; run `ci/replay.sh` (below); without `--quick`, run `scripts/check_version_pins.py` (see below) and `ci/test.sh -q`; print the `needs_llm_run.sh` verdict against main. It exits non-zero on a refused commit author, a conflict, a lint, replay or test failure, a contract version problem, an ADR touch rule failure, a test that pins a contract version, or, with `--strict-main-tests`, a new or changed test that passes on main; it ends with a summary. The parsing and verdict logic lives in `ci/pre_merge.py`, covered by `tests/unit/ci/test_pre_merge.py`. No env vars; needs network only for the fetch.

`scripts/check_version_pins.py` (#1226) makes a temporary worktree of HEAD, bumps every contract the resolver can edit by one MINOR, as `resolve_contract_versions.py` does after the last rebase (new default, Version History entry, both snapshots regenerated), and runs every test file that mentions a contract version (`tests/e2e`, `tests/integration` and `tests/unit/atx_orchestrator` aside). A test that fails there compares with a literal version and would fail on the resolved branch; compare with `current_version(Model)` from `src/shared/contract_versioning.py` instead. It checks committed HEAD, takes about a minute, and also runs in the `validate-contracts` job on every pull request. Covered by `tests/unit/scripts/test_check_version_pins.py`.

The main-tests step (#1155) checks AGENTS.md self-review item 1, "every new test fails on main". `ci/main_tests.py changed` lists the test functions the branch adds or changes in committed `tests/**/test_*.py` files (uncommitted edits are not seen): each is compared with the merge base, decorators included, so a new parametrize case counts; a renamed file is compared with its old path; nested `Test*` classes are included; a test whose helper changed but whose own code did not is not listed. The step prunes stale worktree entries, makes a temporary detached worktree of main, checks out the branch's `tests/` tree into it, and runs those tests there with this worktree's Python and `PYTHONPATH` on the temporary worktree (it checks that `src` imports from there first, and warns instead if it does not). It runs one pytest per file, up to four at a time, so a file that fails to import on main does not stop the others, and removes the worktree on exit. The status line counts the tests that fail on main, those that fail only because their file does not import on main (a new test that imports a new function lands here: it is never actually run against main's code, so check it by hand), those skipped on main, and those whose every case passes on main; the step then lists each test by node id and outcome. A test that passes on main either misses the changed path or guards behaviour main already has. Mark the second kind with a `@pytest.mark.passes_on_main` decorator on the test or an enclosing class. The step does not run it, and lists it by name as marked. A module-level `pytestmark` with that marker is not honoured: the file's tests run as usual and the file is reported. `tests/e2e/` and `tests/integration/` are not run (they need the whole pipeline or a live stack). By default the step only warns; `--strict-main-tests` makes a passing test a failure (pre_merge exits non-zero), and `--no-main-tests` skips the step. It runs with `--quick` and costs a few seconds for a handful of files. Its logic is covered by `tests/unit/ci/test_main_tests.py`.

## `e2e.sh`

The deterministic end-to-end suite (`tests/e2e/`): runs the full pipeline
(collect → triage → analysis per engine → assignment → reality check →
synthesis → report) with no LLM and no AWS credentials, then checks the
deliverables folder (the executive report PDF, the engineering report and the figures
they share), the local UI in a real browser (the results page's rendering,
overflow and accessibility included), and the built help site (its rendering of
the sample engineering report included), against the produced artifacts.

Prerequisites: `uv` (with the `docs` extra installed, for the `mkdocs build`
this script runs itself), Node 22, and Playwright's browser binaries (the
script installs them). The UI build step honors whatever npm registry is
already configured (an internal mirror locally, the public registry in CI) —
it is never hardcoded here.

Before either pytest invocation, this script also builds the help site
(`mkdocs build --strict`, then `scripts/build_docs_sample.py --site-dir
site`), the same way it builds the UI — `tests/e2e/test_docs_site.py` only
serves and checks `site/`, it does not build it.

The help site's sample report is not a pipeline run: `scripts/build_docs_sample.py` renders the recorded WordPress model run committed under `docs/examples/wordpress/sample-run/` (its README names the release) with the current renderers, so no model is called. Refresh that run once per release (the release checklist in `docs/RELEASE_MANAGEMENT.md`): run `/modernize` on the WordPress sample, then `uv run python scripts/build_docs_sample.py --refresh-from artifacts/wordpress/<job>`, update the release named in the README and in `docs/sample.md`, and have a person read the refreshed `report.json`'s diff before it merges. `--deterministic` renders a fresh deterministic run instead.

Runs pytest **twice**, not once:

1. `tests/e2e --ignore=tests/e2e/test_ui.py --ignore=tests/e2e/test_docs_site.py`
   with `--browser chromium --browser webkit` → `$E2E_OUTPUT/e2e-reports-junit.xml`
2. `tests/e2e/test_ui.py tests/e2e/test_docs_site.py` with `--browser chromium`
   only → `$E2E_OUTPUT/e2e-ui-junit.xml`

`test_ui.py` and `test_docs_site.py` are both Chromium-only
(`pytest.mark.only_browser("chromium")`), and pytest-playwright implements
that marker as a runtime skip rather than a deselection. Under a single
`--browser chromium --browser webkit` invocation combined with
`--fail-on-skip`, the webkit parametrizations of either suite would be
skipped-then-failed. Splitting into two invocations keeps `--fail-on-skip`
meaningful for all three suites. The cost: each invocation is its own pytest
session, so the deterministic pipeline runs the two sample pipelines once
each per invocation — ~15s total (wordpress ~6s, discourse ~9s, measured),
rather than once overall across both invocations. Accepted as cheap relative
to the correctness it buys.

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

Sharding (#956): `E2E_SHARD=<name> ./ci/e2e.sh` runs one of eight shards, the way the GitHub `e2e-shard (<name>)` jobs do; the required `e2e` job passes only when all eight did. `replay` runs `ci/replay.sh` (below) and installs no browser. `wordpress`, `discourse` and `trailgear` run the report checks (the first invocation) whose test id names that sample; `hashseed` runs `test_hashseed_determinism.py` and `determinism` runs `test_deliverable_determinism.py` (the two slowest suites, which rerun samples on purpose); `cross` runs the report checks that name no sample; and `ui` runs the second invocation (UI smoke + help site). The six report shards' `-k` expressions (`e2e_shard_k` in the script) assign each test to the first rule it matches, so together they select exactly the tests one unsharded run does. Only the `ui` shard builds the UI and the help site, and it installs only Chromium; `hashseed` and `determinism` open no browser page and install none. Whenever the script builds the UI (the `ui` shard or a full run), it does so in the background while the browsers install and the help site builds, writing its output to `$E2E_OUTPUT/ui-build.log` and printing it when done; a failed UI build still fails the script. Unset `E2E_SHARD` runs everything, as before.

Env vars read:

- `E2E_OUTPUT` — root directory for everything this script produces. Defaults
  to `test-results/`. Under it:
  - `e2e-reports-junit.xml`, `e2e-ui-junit.xml` — one junit file per invocation.
  - `deliverables/<db>/` — a copy of each pipeline run's `deliverables/`
    folder (the executive report, the engineering report, the assessment data and
    the index), with the latest synthesis version's own files (`report.json`,
    and in an LLM run the model input and response) under `synthesis/`,
    written by a session-scoped autouse finalizer in `tests/e2e/conftest.py`,
    so reviewers can download the rendered output straight from a CI artifact
    without re-running anything.
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
- `E2E_API_PORT`, `E2E_UI_PORT` — the ports the UI smoke starts its API and UI on (default 8000 and 3000). The UI build bakes in the API port, so set both before the build to run the smoke while another local API/UI holds the defaults.
- `E2E_SHARD` — one shard name (`wordpress`, `discourse`, `trailgear`,
  `hashseed`, `determinism`, `cross`, `ui`, `replay`) to run instead of everything; see Sharding above.

Installing browsers: `uv run playwright install chromium webkit` everywhere, through
`playwright_install_with_retry` in `ci/lib.sh` (#1346): 3 attempts with backoff
(`E2E_INSTALL_ATTEMPTS`, `E2E_INSTALL_BACKOFF`), each attempt stopped, with its whole
process tree, after `E2E_INSTALL_TIMEOUT` seconds (default 300, #1360), the last from
Playwright's other official download host (`PLAYWRIGHT_FALLBACK_HOST`); TLS verification
is never relaxed. In CI (`CI=true`, set automatically by GitHub Actions) `playwright_ensure_deps`
then launches each browser headless (`E2E_LAUNCH_TIMEOUT`, default 120s). Nothing else
runs when they all launch: no `apt-get update`, no package download (the old `--with-deps`
stalled whole shards on a slow Azure mirror, #1360). The runners ship chromium's libraries but
miss some of webkit's (for example `libevent-2.1`), so a webkit launch can fail; then
`playwright install-deps` runs under a watchdog (`E2E_DEPS_TIMEOUT`, default 600s; the roughly 120 MB of webkit packages stay cached between attempts) and retry, with apt told to give up on a
silent server after 30s (`Acquire::http::Timeout`, 3 retries) and, from the second attempt, pointed
at `archive.ubuntu.com` instead of the runner's mirror list. That path also pulls Playwright's extra
font packages (`fonts-unifont`, ...); no e2e test depends on them (the PDF is generated without a
browser, and the browser checks read the DOM), but `install-deps` cannot skip them.
Locally (including macOS, where there is no `apt`) only the browser binaries are installed.
The GitHub workflow also caches `~/.cache/ms-playwright`, keyed on the Playwright version in
`uv.lock`, and, for the shards that install webkit, the `.deb` files `install-deps` downloads
(`~/apt-archives`, keyed on the runner image and the Playwright version, #1375): they are copied into
`/var/cache/apt/archives` before the shard and back out after it on a cache miss, so a slow apt mirror
rarely matters. `ci/e2e-llm.sh` installs its browsers with the same two helpers instead of `--with-deps`.

## `replay.sh`

A zero-token replay of a saved real-model run (#1409): `./ci/replay.sh`, about 15 seconds, no model call and no browser; it needs only the `dev` extra (with the `e2e` extra it also gives pytest-playwright its own `--output` folder). A real-model run costs about 4 USD and ten minutes, but most of its checks are deterministic once the model has answered, so one passing run's model-written artifacts are kept in `tests/fixtures/real_runs/wordpress-both/` (`REPLAY_FIXTURE` picks another) and replayed with the current code: `ci/llm/run.py check-transcript` over its transcript against the current allowlist, then the deliverables re-rendered from its `report.json` by the current renderers and the external-mode e2e checks over them (`tests/e2e`, the tests parametrized over the `external` job: the customer-text guard, the PDF and engineering report checks, the judge facts' agreement with the report). It writes under `$E2E_OUTPUT/replay/`, and both `ci/pre_merge.sh` (with `--quick` too) and the `replay` e2e shard run it.

What it does not re-run: synthesis and the facts layer (`src/shared/report_facts.py`). The saved `report.json`, its `facts` included, is the one that run wrote; only the renderers and the checks are current. So a change to synthesis or to how a fact is computed is not covered here (the deterministic shards and e2e-llm cover it), and the fixture ages. The e2e-llm upload carries the synthesis folder, the deliverables and the transcript, not the rest of the job, so the external checks that read the assignment, collector, triage or reality-check artifacts are deselected: `NEEDS_FULL_JOB` in `ci/llm/replay.py` lists each with the artifact it reads, and a new external check that reads one goes there. Refresh the fixture after a change to the `SynthesisOutputContract` shape or to the facts layer that the renderers read, or when `NEEDS_FULL_JOB` can shrink, and otherwise rarely: each refresh adds about 180 KB (compressed) to the history. To keep a newer passing run, download its `test-results/llm-<mode>-<fixture>/` folder and run `uv run python ci/llm/replay.py save --from <that folder>`: it refuses a run whose `results.json` does not pass, copies the synthesis `report.json`, `llm_input.json` and `llm_response.json`, writes `run.json` (db, job, mode, git sha, cost, judge mean) and a trimmed transcript (the runner's working directory replaced by `/workspace/repo`, home folders by `/home/user`, temp folders and sockets by `/tmp/claude`, tool results and written files over 1,000 characters cut to their ends, thinking emptied; commands and dispatch prompts kept whole, so `check-transcript` reads the same run). It refuses, writing nothing, a run whose files still name an absolute path outside those placeholders, an AWS access key, account id or ARN. The fixture stays scanned by detect-secrets, with only its known id shapes filtered by value (`.pre-commit-config.yaml`). Covered by `tests/unit/ci/test_replay.py`.

## `needs_llm_run.sh`

`./ci/needs_llm_run.sh [BASE]` (default `origin/main`) diffs `BASE...HEAD`
(committed changes only; both sides of a rename count) and prints, per run,
`required` or `not required` plus the matching paths: `e2e-llm` (the headless
Claude Code run: session inputs and the builders and validators of its
`llm_input`/request files) and `bedrock-smoke` (Bedrock-only call sites the
headless run never reaches). The lists are `E2E_LLM` and `BEDROCK_SMOKE` in
`ci/needs_llm_run.py`; a file can be in both. Any git error (bad base, shallow
clone, no merge base) exits 2 with a message and no verdict. A unit test scans
`src/` and `scripts/` for model imports, model-id env reads and
`llm_input`/request builders, and fails if a hit is not listed. A second test follows the imports of the designer-input builders (`prepare_schema_design_input` and the analysis agents) and fails if a module feeding that input is neither listed nor reviewed in the test's `REVIEWED_DETERMINISTIC_IMPORTS`. No model call,
no secrets, not wired into a workflow: a post-merge real-model run on main and
a Bedrock smoke run need the internal validation pipeline.

## `e2e-llm.sh`

Headless `/modernize` run on model access (Bedrock or the Anthropic API):
`claude -p "/modernize ... --auto --mode <chat|ui|both>" --permission-mode
dontAsk --output-format stream-json` against one of the two sample fixtures,
then the same deliverable checks `e2e.sh` runs (contracts, the engineering
report, the executive report PDF (the `pdf` check, `tests/e2e/test_report_pdf.py`), and --
for `ui`/`both` -- the UI smoke test) pointed at the job the headless run
produced, plus a rubric-based quality judge (`ci/llm/judge.py` /
`ci/llm/rubric.md`) grading the rendered deliverables. Everything is merged
into one `results.json` row by `ci/llm/run.py results`.

```
make e2e-llm                                        # both / wordpress (defaults)
make e2e-llm E2E_LLM_MODE=ui E2E_LLM_FIXTURE=discourse
./ci/e2e-llm.sh chat wordpress                       # or invoke the script directly
E2E_LLM_TARGET_DATABASES=dynamodb,aurora_postgresql E2E_LLM_MAX_BUDGET_USD=80 ./ci/e2e-llm.sh chat discourse   # a scoped run
```

**Scoped runs** (#1574): `E2E_LLM_TARGET_DATABASES` (comma-separated engine ids, for example `dynamodb,aurora_postgresql`) is passed to the headless run as `/modernize ... --target-databases <ids>`, the customer's engine scope; unset, every engine is considered. A value that is not comma-separated engine ids exits 2 before the run. Discourse scoped to `dynamodb,aurora_postgresql` costs about $56 (44 DynamoDB design parts, designed twice when the post-schema reroute moves a query), above the default `E2E_LLM_MAX_BUDGET_USD` of 50: raise it for that run.

**The model that answered** (#1574): after the run, the `check-model` step (`ci/llm/run.py check-model`) reads the model the transcript records (the session's `init` record, else the `modelUsage` model with the most output tokens) into `model.json` and fails the run with a clear message when it is not `ANTHROPIC_MODEL`. A `claude` wrapper script on `PATH` can choose its own model whatever `ANTHROPIC_MODEL` says; set `CLAUDE_BIN` to the claude CLI itself to run the model you asked for. `results.json`'s `model` is the same recorded model (it was always `null` before: the `result` lines carry none). Dry runs skip the step.

**What it checks**: the transcript's final `type == "result"` line
(`ci/llm/run.py check-transcript`) is healthy -- no `is_error`, no
`permission_denials`, a `MODERNIZE_RESULT: complete job_id=... db=... mode=...`
line -- and that the run's mode matches what actually happened (`chat` never
started the local UI; `ui`/`both` started it via
`scripts/start_local_ui.py` and got back a `{"status": "ready"}` tool
result). In `ui`/`both` it also checks the UI pointers (#1242): the
orchestrator's own text quotes the summary page's URL, the `Summary page` line
`print_summary.py --gate --mode ui|both` ends with (or the `ui_url`
`start_local_ui.py --page summary` printed), before schema design starts,
and the results page's, the `Results page` line of `print_summary.py --final --mode ui|both` (or the `ui_url` `--page results` printed, #1307). A miss fails the run (`transcript_error` in `results.json`); it only warned
before #1307, while the model composed the gate and completion text. It checks the
URLs only, not the gate table or the summary. Then the deliverable checks (`tests/e2e`, pointed at the headless
job via `E2E_ARTIFACT_ROOT`/`E2E_DB`/`E2E_JOB` -- see "external-job mode"
under `e2e.sh`'s own test suite) and the judge run exactly as they do for a
deterministic job.

**Merge rule after a failed transcript check** (#1408; a process rule, no automated check enforces it): a pull request head that failed an `e2e-llm` transcript check (`transcript_error` in `results.json`) is not covered by a later batch run that passes on the same head, nor by any run on another head that includes it: that pass can be the model taking a different path that time. It stays unmerged until a fix is pushed and an `e2e-llm` run on the fixed head passes the same check. A head that failed the same check twice and then passed once merged in #1358 and had to be reopened as #1401.

**Outputs**, all under `$E2E_OUTPUT/llm-<mode>-<fixture>/` (default
`test-results/llm-<mode>-<fixture>/`):

- `claude-version.txt`, `claude-flags.txt` -- the installed CLI's version and
  the exact flags this run chose (see "CLI flag feature-detection" below).
- `input/` -- the sample fixture, unzipped here rather than into the repo
  root.
- `transcript.jsonl` -- the full stream-json transcript.
- `claude-exit.txt`, `summary.json` -- the claude process's exit code/duration,
  and `ci/llm/run.py check-transcript`'s parsed summary (or `{"error": ...}`).
- `model.json` -- `ci/llm/run.py check-model`'s `{"model", "expected", "match"}` (not in a dry run).
- `reports-junit.xml`, `ui-junit.xml` (ui/both only), `pw-reports/`, `pw-ui/` --
  same shapes as `e2e.sh`'s outputs, produced by the same pytest invocations.
- `judge.json` -- `ci/llm/judge.py`'s rubric scores. The judge grades the same prompt `JUDGE_SAMPLES` times (default 3), one after another, with a pinned model (`JUDGE_MODEL` overrides it), and `scores` are the per-criterion medians the pass rule is applied to. Measured on saved main jobs (2026-10-18, Bedrock, the CLI's prompt cache warm after the first sample): wordpress (119K-char prompt) $0.29 and 96 s for three samples, discourse (145K) $0.32 and 95 s; the first sample costs about $0.20, later ones about $0.05, each about 30 s. It also records `model` (the model that answered, read from the CLI record's `modelUsage`; every `judge.json` before #952 has `null` there), `model_requested`, `model_matches_request` (false when a managed or user CLI setting answered with another model; such scores are not comparable), `rubric_version` and `rubric_sha256`, `samples` (each sample's scores, model, cost and `duration_s`), `spread` (max - min per criterion), `samples_failed`, `notes` (from the sample closest to the medians, `notes_from_sample`) and `defects` (one entry per defect the samples named, with criterion, kind, where, quote and how many samples named it). With `E2E_LLM_JUDGE_BASELINE` set to a saved main `judge.json`, `baseline` holds the per-criterion deltas when both were graded under the same `rubric_version` (otherwise `compared: false` with a note, never a failure), and the judge step fails when one criterion's median drops by 2 or more, or two or more criteria drop by 1: compare criteria, not the mean, because the mean hides moves that cancel; a single one-point drop is within noise. The judge measures the deliverables, not routing: routing quality is the answer-key benchmark's gate (`benchmarks/README.md`).
- `token_usage` in `results.json` (and `job-logs/token_usage.json`) -- `ci/llm/token_usage.py` sums the session's per-turn input-side usage (input, cache-read, cache-write tokens, calls; subagent turns included) per pipeline phase, attributed from the `scripts/run_<phase>.py` calls and design/reality-check/synthesize skills each agent made, in the same key names as the Bedrock `token_usage` in `design_trace.json`. Per-phase `outputTokens` is `null`: the transcript's assistant records only carry the first-chunk output count (about 100x too low), so `total.outputTokens` is the session total from the final result records' `modelUsage`, and `session_model_usage` shows the session figures to compare against (the per-phase sums come out 0.2-3.3% short on cache-read). Grouped runs also carry a `token_usage` run total in the consolidated `design_trace.json`.
- `job-logs/` -- the run's own logs, copied by the EXIT trap (`ci/llm/run.py
  collect-logs`) because only this directory is uploaded: `run_assessment.log`
  from `artifacts/<db>/<job>/_logs/` and the local UI's `local-ui-api.log` /
  `local-ui-serve.log` (ui/both), each capped at its last 1 MB.
- `steps.json` -- exit code of every step (`build-ui` (ui/both, run *before*
  the claude call so `scripts/start_local_ui.py` finds `src/ui/build/` and
  never builds inside a Bash tool call), `claude`, `check-model` (not in a dry run), `check-transcript`,
  `token-usage`, `render`, `judge`, `install-browsers`, `report-tests`, `ui-tests`,
  `results`); a step failing never stops the script early. The judge runs before
  the browser tests (#1593), on the deliverables `render` re-renders the way the
  tests' external-job fixture does, so a slow test step can no longer leave it no
  time. `report-tests` and `ui-tests` run under `run_before_deadline` (`ci/lib.sh`):
  each must end `E2E_LLM_JOB_BUDGET` seconds (default 4800, 80 minutes) after the
  script started, or it is stopped (or, when the budget is already used up,
  skipped) and recorded as 124. The default leaves the `results` step and the
  artifact upload inside the mirror's 90-minute job limit: a scoped discourse run
  that hit that limit during its judge step was killed with no `results.json` and
  no uploaded test results. The script first removes a previous run's `results.json`,
  `steps.json`, `summary.json`, `judge.json` and junit files from `$OUT`, so a skipped
  or stopped step can never be scored from a reused folder's old files. Preflight failures before the run
  (missing credentials, the CLI install, `claude --version`, unzipping the
  fixture, a rejected `E2E_LLM_ARTIFACT_ROOT`) exit early, and the EXIT trap
  then writes a minimal `{"schema_version": 2, "pass": false, "error": "<step>
  failed before the run"}` -- so `results.json` always exists.
- `results.json` -- the one merged row (`schema_version`, `timestamp`,
  `git_sha`, `mode`, `fixture`, `model`, `job_id`, `db`, `cost_usd`,
  `num_turns`, `tokens_in`/`tokens_out`, `duration_s`, `checks: {transcript,
  contracts, engineering, pdf, ui, judge}` (`ui` is `null` for `chat`; schema
  version 2 renamed the `html` check to `engineering` when the HTML reports
  were retired, #987),
  `transcript_error` (`null` unless check-transcript failed; cost, turns and
  tokens are reported either way), `judge: {mean, scores}`, `pass`). Plan 3b uploads this to the results store, so it's
  kept flat and JSON-shaped on purpose.

**Env vars**: `CLAUDE_BIN` (default `claude`), `CLAUDE_CODE_VERSION` (npm
version to install when `$CLAUDE_BIN` is missing, default pinned to
`2.1.288`, the published version when this was written -- bump it
deliberately), `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY` *or*
`CLAUDE_CODE_USE_BEDROCK` + `AWS_REGION`, `E2E_LLM_TIMEOUT` (seconds, default
3600), `E2E_LLM_MAX_TURNS` (default 200) and `E2E_LLM_MAX_BUDGET_USD`
(default 50: a discourse run costs about $18-20, plus $3-5 when the DynamoDB merge needs a fix round; scoped to `dynamodb,aurora_postgresql` about $56), each only passed if the CLI's own `--help` mentions the flag, `E2E_LLM_TARGET_DATABASES` (a scoped run, see above),
`E2E_LLM_JOB_BUDGET` (seconds, default 4800, see `steps.json` above),
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
configured outside `--mcp-config`), `--verbose` and `--include-hook-events` (hook lifecycle
events in the stream, #747) if the help text mentions
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

**Cost and time**: the first internal-pipeline run (chat/wordpress) cost
$4.06 and ran 11 `result` messages (one per background subagent plus the
orchestrator) across 493 transcript lines; see `ci/llm/run.py`'s module
docstring for the confirmed stream-json shapes (aggregation across all
`result` messages, `permission_denials` entries, nested `usage` fields) and
`tests/unit/ci/fixtures/real-chat-wordpress-failed.jsonl` for a trimmed copy
of that transcript.

**Denials**: `check-transcript` splits each denied Bash command on pipes, `&&`, `||`, `;` and subshells. A denial of one of the pipeline's own allowlisted commands on its own means the allowlist is broken and fails the run. A compound command that contains an allowlisted part but was denied for the rest (for example `search_artifacts.py ... | uv run python -c ...`) is a tool-use rule violation by the model: it also fails the run, because the commands forbid chaining and post-processing, but it is reported as `rule_violations` (count and commands) in `summary.json` and `results.json` and the error names the violation, not the allowlist. Any other denial is the model's own exploratory tool use and only warns (`denials`). Every entry is named `<Tool>: <target>` (Bash: the command, Skill: the skill name, Read/Write/Edit: the path, WebFetch: the URL, other tools: the tool name and its compact input). A denied non-Bash call that matches an allow rule and no deny rule counts as a broken allowlist; one outside the allowlist (for example a `Skill` that is not listed) only warns and is recorded as `unlisted_tool_denials`, so you can tell an allowlist gap from a model misstep. File paths are matched the way Claude Code does: tool inputs carry absolute paths, relative rules such as `Write(artifacts/**)` resolve against the session's cwd (from the transcript's `init` record), `..` is normalised first, and a path a deny rule blocks (for example `artifacts/.local-ui/`) is never counted as allowlisted.

### `ci/llm/aggregate_notes.py`

Finds the judge notes that keep coming back. The judge's scores are noisy
(#513), but a note that repeats across runs is a steadier signal and usually
worth an issue. It reads saved `judge.json` files (default
`test-results/**/judge.json`; pass files, directories or globs to override),
splits each note into sentences, and groups similar ones with a deterministic
token similarity (every number becomes one `#` token, stopwords dropped,
negations such as "not" kept and required to match, Jaccard at least
`--threshold`, default 0.5). Sentences are sorted before grouping, so the
result does not depend on input order or run names; paraphrases below the
threshold do not merge, so counts are a lower bound. No model is called. It
prints per-criterion score mean and spread, then the notes that recur in at
least `--min-runs` distinct runs (default 3), with the runs they came from. A
path argument that matches nothing is an error.

```bash
uv run python ci/llm/aggregate_notes.py                      # test-results/**/judge.json
uv run python ci/llm/aggregate_notes.py runs/*/test-results --min-runs 5 --concerns-only
uv run python ci/llm/aggregate_notes.py --json > recurring.json
```

`--concerns-only` is a lossy convenience: it keeps only sentences containing
hedge or gap wording (could, soft, somewhat, repeats, ambiguous, generic, ...),
so recurring praise does not drown them out. Run without it when in doubt.

A `judge.json` written since #952 also has a structured `defects[]` list. Those entries are grouped per criterion and kind by the same similarity on the quoted text and listed after the notes (`recurring_defects` in `--json`), so a defect that comes back in many runs reads as a worklist item. Older files have no defects and add nothing there.

## Release notes: `../scripts/release_notes.py` (#1184)

Not a CI gate: the release workflow (`.github/workflows/release.yml`) runs it when a `v*` tag is pushed, and anyone can run it to preview a release. `uv run python scripts/release_notes.py --since <previous tag> [--until <ref>] [--output <file>] [--body [--max-rows N]]` reads committed files at the two refs with `git show` (no pipeline run, no network, needs the tags and history locally) and writes one Markdown section: the routing-snapshot diff (`tests/e2e/baselines/routing_snapshot.json`; one row per query whose final engine, score on that engine or last Move's reason code moved, plus added and removed queries; engine changes first, then reason-code-only changes, then score-only drift, each by sample and query), and one changelog line per rule change (a `src/catalog/**/*.toml` file added, changed or removed, with the subjects of the commits that touched it) and per contract change (a version move, addition or removal in `tests/contract/schema_snapshot.json` and in the public contract's `tests/contract/assess_released_snapshot.json`, with the field changes `scripts/contract_schema_snapshot.py` classifies). When `--until` is a tag the public contract's released snapshot was not recorded for, it adds a note naming the `--write-released` command, which records that tag's own shape. `--body` writes the release body's copy: at most 50 engine and reason-code rows, score-only drift as one summary line (counts per sample and per score change), 40 changelog lines of at most 400 characters, and under 100,000 characters overall (GitHub's limit is 125,000), each cut pointing at the attachment. `--previous-tag <tag>` prints the tag a release is compared with: the highest version tag below it, by exact name and PEP 440 order (so a final release follows its own pre-releases and a hotfix follows its own line). The workflow uses it for every range, puts the `--body` copy in the release body and attaches the full file as `release-changes.md`; the notes step is non-blocking (`continue-on-error`), and a failure leaves a warning line in the body. Covered by `tests/unit/scripts/test_release_notes.py` (two small synthetic snapshots, and a throwaway git repository with two tags).

## `../scripts/hooks/` (Claude Code hooks, #731)

Eight small scripts that refuse a guardrail bypass at the tool call instead of after the fact. Each reads the hook event JSON on stdin and exits 0 (allow) or 2 (block, one-line reason on stderr shown to the model); an unreadable event allows. They run as `uv run --no-project python -I "$CLAUDE_PROJECT_DIR/scripts/hooks/<name>.py"`, outside the project environment, and use the standard library only, with one exception: `block_contract_version_edit.py` imports `scripts/resolve_contract_versions.py` (itself stdlib-only) when an edit changes a version line. Unit tests: `tests/unit/hooks/`.

| Script | Event / matcher | Where | Rule |
|---|---|---|---|
| `block_commit_bypass.py` | PreToolUse `Bash` | `settings.json`, `settings.ci.json` | Blocks `git commit --no-verify` (and abbreviations), `-n` alone or in a cluster such as `-an` (not `-uno`), pre-commit's `SKIP=<hook-id>` (inline, via `env` or `export`), and `git -c core.hooksPath=... commit`, also when `git` is reached through `command`, `sudo`, `env`, `nice`, `nohup`, `time` or `exec`. Out of reach: a bypass hidden in `bash -c "..."` or built from variables, and the persistent `git config core.hooksPath` form; the commit-message hook and `ci/check_commits.sh` remain the backstops. |
| `block_credential_output.py` | PreToolUse `Bash` | `settings.json`, `settings.ci.json` | Blocks a call that prints a cloud credential helper's output, because a Bash call's output is kept in the session log (#1165). Helpers: `ada credentials print`, `aws configure export-credentials`, `aws sts get-session-token`, `assume-role`, `assume-role-with-saml`, `assume-role-with-web-identity` and `get-federation-token`, `aws ecr get-login-password` and `get-authorization-token`, `aws codeartifact get-authorization-token`, `aws eks get-token`, `gcloud auth print-access-token`, `print-identity-token` and `application-default print-access-token`, `az account get-access-token`. Allowed: the output captured by an assignment (`TOKEN=$(...)`, `export TOKEN="$(...)"`, backticks), passed to `eval $(...)` or `source <(...)`, piped into a registry login that reads stdin (`docker`, `podman`, `helm registry`, `buildah`, `skopeo` `login --password-stdin`), or redirected to a path that is not shown (`> f`, `>> f`, `1> f`, `&> f`, `> /dev/null`; not `2>&1`). `> /dev/stdout`, `/dev/stderr`, `/dev/tty`, `/dev/fd/*` and a `tee` of one of them print, so they are blocked. A pipe into anything else is not a way out (`... \| jq .SecretAccessKey` prints). The program may be written with a path (`/usr/local/bin/aws`) and a word quoted or escaped; those are normalised first. The message never suggests writing credentials to a file. The match is linear in the command length (a unit test checks the growth), so a long command cannot reach the 10 s hook timeout, which would fail open. Only unquoted text counts, so a commit message or an issue comment that names a helper passes, while a `$(...)` inside double quotes is still looked at. `aws sts get-caller-identity` and other calls that print no secret pass. Out of reach: a helper hidden in `bash -c "..."`, a variable or a script, and a captured value printed afterwards (`echo $TOKEN`). |
| `block_fork_pr_push.py` | PreToolUse `Bash` | `settings.json`, `settings.ci.json` | In a worktree checked out for a contributor's fork PR, refuses a `git push` to anything but the fork (#1165). `gh pr checkout` leaves the branch's `remote` and `pushRemote` set to the fork's URL while `origin` stays upstream; the hook reads that from the repository's git config (the event's `cwd`, followed through an earlier `cd <dir>` or `pushd <dir>` in the same command and `git -C <dir>`, `--git-dir` and `--work-tree`). A plain `git push` (it goes to the fork), a push to the fork's URL and a push to a remote that points at it pass; `git push origin HEAD`, `git push -u origin <new-branch>` and a push to any other URL are refused, naming the branch and the fork. It does not check whether the branch already exists upstream: no push to another repository belongs in a fork PR's worktree. An ordinary worktree, a detached HEAD, a directory that is not a repository, or a git that does not answer are never blocked. A command substitution on the push line (`git push $(echo origin) HEAD`) counts as an unknown remote and is refused. Out of reach: a worktree not made with `gh pr checkout` (a branch fetched from `pull/N/head` has no fork URL in its config), a push hidden in `bash -c "..."` or a script, and a git alias. |
| `block_baseline_edit.py` | PreToolUse `Edit\|Write\|MultiEdit` | `settings.json`, `settings.ci.json` | Blocks hand edits of `tests/e2e/baselines/routing_snapshot.json`, `tests/e2e/baselines/routing_baseline.json`, `tests/contract/schema_snapshot.json`, `tests/contract/value_set_snapshot.json`, the `assess()` contract's released snapshot `tests/contract/assess_released_snapshot.json` and the `assess()` JSON Schemas in `docs/contracts/schemas/`, naming the `--write` regeneration command. |
| `block_contract_version_edit.py` | PreToolUse `Edit\|Write\|MultiEdit` | `settings.json`, `settings.ci.json` | Blocks an Edit, MultiEdit or Write of `src/contracts/<module>.py` that changes the `contract_version` default of a model the file already has, or a Version History line (an entry or its continuation), naming `scripts/bump_contract.py` (declare the bump) and `scripts/resolve_contract_versions.py` (writes the number and the entry after the last rebase; it runs as a script, so it is not affected) (#1408). It works out the file's text after the call and compares only those lines. Allowed: every other change; adding or removing a model with its own `contract_version` (a new model starts at its own version, and `bump_contract.py` cannot declare a model the snapshot does not list yet); a contract `bump_contract.py` sends to a hand bump because the resolver cannot edit it (today `aurora_mysql_model_output`, `aurora_postgresql_model_output`, `assignment_models.ValidationResult` and the `model_flag` module; `aurora_design_delta` keeps its version only in `DELTA_VERSION`, which the hook does not watch), whose default may then be edited, and so may any Version History line in the same module (module-wide: in `assignment_models`, which has `ValidationResult`, a history edit passes even next to the resolvable `Assignment`); the set comes from `resolve_contract_versions.hand_bumped_contracts`, the dry run `bump_contract.py` uses, never a second list, and only a call that moves a version line loads that code; a new contract file; a file with merge conflict markers; and an edit whose resulting text does not parse as Python (the hook fails open on it; a contract file that does not parse fails the tests and the contract checks anyway). A Version History conflict on a rebase: resolve it with git by taking main's side of the version hunks (or by editing out the markers, which the hook allows while they are in the file), keep the branch's pending files, then re-run `uv run python scripts/resolve_contract_versions.py` after the last rebase. Its patterns are copies of `scripts/contract_bumps.py`'s; a test asserts they agree. |
| `remind_llm_run.py` | Stop | `settings.json` only | Prints what `ci/needs_llm_run.py` says about the files this branch changed since its merge-base with `origin/main` (committed, uncommitted and untracked) when `e2e-llm` or `bedrock-smoke` is required. Shown once per session and again only when the verdict changes. A reminder: never blocks, and is silent in headless runs (`MODERNIZER_CI_SANDBOX=1`). |
| `enforce_tool_use_rule.py` | PreToolUse `Bash` | `settings.ci.json` only | Enforces the shared tool-use rule: a Bash call must be exactly one simple command covered by a `Bash(...)` allow rule in `settings.ci.json` (read at call time, so the allowlist stays the single source). Pipes, chains, subshells, `$(...)`, heredocs and `cat`/`jq`/`python3 -c`/`sed`/`ls`/`cd`/`grep` are refused with the rule's text. So is any redirection to or from a file: the scripts write their own artifacts and file writes go through Edit/Write, whose allow rules (`artifacts/**`) bound them, so a shell `> file` would sidestep them. Only fd duplication (`2>&1`) and `/dev/null` pass. |
| `require_gate_shown.py` | PreToolUse `Bash\|Agent\|Task` | `settings.json`, `settings.ci.json` | Holds schema design in a `/modernize` run until the approval gate ran on its own (#1401). The gate script's output is what the user sees (maintainer decision on #1401), so only the order is enforced: a `scripts/run_schema_design.py` call (not `--help`) or `/design-schema-*` dispatch for a job is refused unless a successful `print_summary.py --gate` for that job ran in an earlier assistant message (not the same one as the call). Only real invocations count: one simple command `uv run python scripts/<name>.py ...` with a `--job-id` (never a chain, a heredoc, quoted text or another program such as `gh`, `git` or `ruff`), and a dispatch whose prompt starts `Run /design-schema-` or `Follow /design-schema-` with `job_id=`. A real gate call for a job holds that job, and keeps holding it once its result is recorded only if the block has a `Summary page` line (chat mode is not held). A real `run_assessment.py --resume-reality-check` call holds the job only when the session's latest slash command is `/modernize`, so skipping the gate is refused while `/reality-check` then `/design-schema-<engine>` by hand is not. Messages are told apart by their API message id (else by the tool results between them); the call is looked up by the event's `tool_use_id`, re-reading the streamed session transcript every quarter second for up to two seconds until it is recorded, and while it is not, a later assistant message after the gate's result stands for it. Subagent records never count; a call whose event carries `agent_id` is not held, and without one a subagent call is judged against the session, which is safe because its dispatch passed only after the gate. A missing transcript allows. `check-transcript` credits the gate output's `Summary page` URL as shown (as well as orchestrator text) when the gate ran before schema design started and not in the same message as the first schema-design call, and likewise the `Results page` URL in `print_summary.py --final`'s output when it ran after the `/synthesize` dispatch reported back (its hand-back result, or a completed task notification); and it does not count a Phase 6 call as that start when its result is an error whose text starts with `PreToolUse:<Tool> hook error:` and carries this hook's reason. |

Headless runs load both files (`--setting-sources project` plus `--settings`), so the hooks listed for both (`block_commit_bypass.py`, `block_credential_output.py`, `block_fork_pr_push.py`, `block_baseline_edit.py`, `block_contract_version_edit.py` and `require_gate_shown.py`) are in each; the identical command runs once. `remind_llm_run.py` is interactive only and `enforce_tool_use_rule.py` headless only. `hook_common.split_compound_command` is a tested copy of `_split_compound_command` in `ci/llm/run.py` (that module imports third-party packages, which a hook must not); a test asserts the two agree. The two hooks of #1165 add no `permissions.allow` rule, so `settings.ci.json`'s allowlist is unchanged; the headless run also denies `Bash(git push *)`, so `block_fork_pr_push.py` matters in interactive sessions.

A call a hook refuses is shown to the model as `PreToolUse:<Tool> hook error: Blocked ...` and also lands in `permission_denials`. `check-transcript` matches that `tool_result` (by `tool_use_id`) to our hook messages and reports the call as `prevented_by_hook` (count and commands in `summary.json` and `results.json`): it warns and never fails the run, because the call was refused before it ran. A violation that was not refused keeps failing as described under Denials. Replaying 105 saved headless transcripts (2679 Bash calls) through `enforce_tool_use_rule.py` refuses 26 calls, all genuine violations (pipes into `head`/`tail`/`cut`/`grep`, `ls`, `find`, a `VAR=...;` chain, `echo`), and none of the documented pipeline commands. A `PreToolUse` hook that ends in a non-blocking error (a crash, a timeout, `uv` missing: any exit but 0 and 2) leaves enforcement off for that call, because the hooks fail open by design. `check-transcript` reports it as `hook_errors` (count and hooks in `summary.json` and `results.json`) and warns; it fails the run only when the record names `require_gate_shown.py` (whose absence changes the run order), which the session log's `attachment` does through its `command`. The stream's `system`/`hook_response` record (`outcome` `error` or `cancelled`; fields per the Agent SDK reference) is what a headless run sees, since `ci/e2e-llm.sh` passes `--include-hook-events` when the CLI lists it; it carries no `command`, so it warns only. A timeout shows as `outcome` `cancelled` in the stream and as an `attachment` of type `hook_cancelled` with `timedOut` true in the session log. Still to confirm on an `e2e-llm` run: what `hook_name` holds in a real stream, and that a timed-out PreToolUse hook arrives as `cancelled` (#747). A denial found only in `tool_result` text (no `permission_denials` on any result line) is labelled `<Tool>: <text>`, the tool taken from the `tool_use` it answers, so `denials.commands` has one format. Changing `settings.ci.json` or the hooks needs an `e2e-llm` run.

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
  always does, today) — keeping both avoids relying on that. The `*` form
  also covers `--page summary|results --job-id <id>`, which starts nothing
  and prints the job page's `ui_url` the command quotes (#1233).
- `Bash(uv run python scripts/print_summary.py *)`: prints the approval-gate
  block (`--gate`) and the completion summary (`--final`) the command quotes
  verbatim (#1307); with `--mode ui|both` they carry the job pages' URLs, so
  `/modernize` runs no `start_local_ui.py --page` call for them. It only reads the job's artifacts and prints Markdown, so
  the figures come from the facts layer, not from the model; the sandbox
  refuses its `--artifact-root` outside the repo and a `--db`/`--job-id` that
  is not a plain name, like the other scripts.
- `Bash(uv run python scripts/search_artifacts.py *)`: the headless session
  may have no Grep tool (Claude Code 2.1.288 in the internal validation
  pipeline does not) and `grep` through Bash is not allowed, so the shared
  tool-use rule in the commands points at this read-only search script. It
  refuses paths that resolve outside the repo, never opens `.env`/key/
  credential-like files, and caps its output (see its module docstring).
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
- `Bash(uv run python scripts/run_pe_review.py *)` and
  `Bash(uv run python scripts/run_post_schema_routing.py *)` (#1059): the
  headless schema-design PE review and post-schema reroute, the steps Bedrock
  mode runs inside schema design. `run_pe_review.py` writes one review request
  per design (per group for DynamoDB) under `artifacts/` and, with
  `--finalize`, records the review subagent's answer in the design trace;
  `run_post_schema_routing.py` runs the shared router one pass per call
  (`--rollback` after a failed redesign). Both only read and write the job's
  own artifacts and print one JSON line.
- `Skill(review-schema)`: the PE review subagent `/modernize` Phase 6c
  dispatches, one per design (#1059), allowed by exact name like the others
  below.
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
