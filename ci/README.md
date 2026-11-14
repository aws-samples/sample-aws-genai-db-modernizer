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

Lint and type-check, with the same commands and arguments as the pre-commit hooks and the `lint` CI job, so a clean `pre-commit run --all-files` and a clean `ci/lint.sh` agree. It runs ruff, black and isort over the tracked Python files the hooks of the same names select, which `ci/python_files.py` reads from their patterns, so both check the same files on any checkout (#1844) <!-- claim: tests/unit/ci/test_security_parity.py::test_lint_sh_and_the_precommit_hook_cover_the_same_python_files, tests/unit/ci/test_security_parity.py::test_a_checkout_with_extra_files_keeps_lint_and_hooks_in_step --> and mypy (below); the license headers (`scripts/license_headers.py --check`, #1722: every source file starts with the two-line SPDX header; `--fix` adds it); the Claude Code command validator (`scripts/validate_skills.py`); the secret scan of the compressed replay fixture files (`ci/llm/private_scan.py tests/fixtures/real_runs`, #1847, see `replay.sh`); the claim markers (`ci/claims.py check`, below); this page's length budget (`ci/readme_budget.py`, below); the generated files: the rule catalog (`scripts/render_catalog.py --check`), the cookbook's graph query section (`scripts/render_graph_catalog.py --check`, #307), the `assess()` JSON Schemas (`scripts/render_contract_schemas.py --check`, #1183) and the AWS Transform image's pinned requirements (`scripts/render_atx_requirements.py --check`, #1725).

mypy is plain `uv run mypy` in the project environment, and the pre-commit hook runs the same command (`language: system`), so the two cannot disagree (#2245). Both read the paths (`src`, `tests`, `scripts`, `ci`, `benchmarks`, `docs`, `pipeline`) and the per-tree rules from `[tool.mypy]` in `pyproject.toml`: every tree is checked with strict Optional except `tests/` (#2255). <!-- claim: tests/unit/ci/test_mypy_parity.py::test_the_hook_runs_the_project_mypy_with_no_arguments_of_its_own, tests/unit/ci/test_mypy_parity.py::test_lint_sh_runs_the_same_command_with_no_arguments_of_its_own, tests/unit/ci/test_mypy_parity.py::test_the_configured_files_are_every_tracked_python_tree_but_contrib -->

It also checks two tables' age, and both checks warn and never fail: the price table (`scripts/refresh_prices.py --check-age`, #1196) and the Aurora PostgreSQL extension lists (`scripts/check_extension_list_age.py`, #1078: `src/catalog/aurora_postgresql_extensions.toml` retrieved more than 90 days ago; update it by hand from the public extension versions page). <!-- claim: tests/unit/scripts/test_refresh_prices.py::TestAgeCheck::test_check_age_only_warns, tests/unit/shared/test_gap_catalog.py::test_the_extension_list_age_warns_after_the_limit_and_never_fails, tests/unit/shared/test_gap_catalog.py::test_the_extension_list_age_check_exits_zero_when_stale, tests/unit/shared/test_gap_catalog.py::test_the_extension_list_age_limit_is_90_days --> The monthly price-refresh workflow and its 45-day limit are in `scripts/refresh_prices.py`'s docstring. No env vars. No prerequisites beyond `uv sync`.

## `claims.py` and `readme_budget.py` (#1851)

A behaviour sentence in a page carries a claim marker, `<!-- claim: tests/path/test_x.py::test_name -->`, naming the test that proves it (format and rules: CONTRIBUTING.md, "Claims name their test"). `uv run python ci/claims.py check` fails a marker whose test pytest would not collect and run, read from the AST: a missing file, class or function, a file outside `testpaths`/`python_files`, a test marked `skip`, `skipif` or `xfail`, or a parametrize id it cannot check. It does not run the tests unless you pass `--run`. <!-- claim: tests/unit/ci/test_claims.py::test_check_names_the_page_line_and_reason, tests/unit/ci/test_claims.py::test_resolve_rejects_what_names_no_test --> A misspelt marker (`<!-- Claim:`, `<!-- claims:`) fails too. <!-- claim: tests/unit/ci/test_claims.py::test_check_fails_on_a_misspelt_marker --> A marker in a code block or code span is an example and is not read. <!-- claim: tests/unit/ci/test_claims.py::test_markers_in_code_are_examples_and_are_not_read --> `uv run python ci/claims.py list` prints every marker. What the check can and cannot see: its docstring. Tests: `tests/unit/ci/test_claims.py`.

`uv run python ci/readme_budget.py` holds this page to a length budget: 4,000 characters a section and 1,200 a block (paragraph, list item or table row). A section that was over the budget when it landed has an allowance in `ci/readme_budget.toml`, and the check fails an allowance that is higher than the base ref's copy (`--base`, else `$README_BUDGET_BASE`, else `origin/main`) or that the base does not have, so a new or renamed section gets the default budget. <!-- claim: tests/unit/ci/test_readme_budget.py::test_a_section_over_the_budget_fails, tests/unit/ci/test_readme_budget.py::test_an_allowance_only_goes_down_against_the_base, tests/unit/ci/test_readme_budget.py::test_a_new_or_renamed_heading_gets_the_default_budget, tests/unit/ci/test_readme_budget.py::test_the_base_ref_can_be_set_by_env_and_flag --> A clone without the base ref gets a warning and no comparison; the GitHub `lint` job sets `README_BUDGET_REQUIRE_BASE=1`, which makes that an error. <!-- claim: tests/unit/ci/test_readme_budget.py::test_a_missing_base_ref_warns_and_skips, tests/unit/ci/test_readme_budget.py::test_a_missing_base_ref_is_an_error_when_the_comparison_is_required, tests/unit/ci/test_readme_budget.py::test_the_github_lint_job_requires_the_comparison --> How a script works belongs in its docstring; this page says what it is for, how to run it and where its tests are. Tests: `tests/unit/ci/test_readme_budget.py`.

## `security.sh` (#1844)

The one security scan: semgrep, gitleaks and bandit. The `security` pre-commit hook runs it on the staged files, the GitHub `security` job on every tracked file, and `pre_merge.sh` on the branch's changed files with `--quick` and on every tracked file without it. <!-- claim: tests/unit/ci/test_security_parity.py::test_the_precommit_security_hook_runs_the_script_on_every_staged_file --> <!-- claim: tests/unit/ci/test_security_parity.py::test_the_github_security_job_runs_the_script_on_the_whole_tree --> <!-- claim: tests/unit/ci/test_security_parity.py::test_pre_merge_runs_the_script_quick_on_the_branch_and_full_otherwise --> No other hook or workflow step runs a scanner, and nothing outside `ci/security.toml` names a semgrep ruleset. <!-- claim: tests/unit/ci/test_security_parity.py::test_no_other_precommit_hook_runs_a_scanner --> <!-- claim: tests/unit/ci/test_security_parity.py::test_no_workflow_step_runs_a_scanner_any_other_way --> <!-- claim: tests/unit/ci/test_security_parity.py::test_nothing_else_names_a_floating_semgrep_ruleset -->

- `./ci/security.sh`: every tracked file (`git ls-files`).
- `./ci/security.sh --quick [--base REF]`: the files changed since the merge base with `REF` (default `origin/main`), plus uncommitted and untracked ones; deleted files are skipped. <!-- claim: tests/unit/ci/test_security.py::test_changed_files_cover_the_branch_uncommitted_and_untracked_not_deleted -->
- `./ci/security.sh FILE...`: those files, repository files only. `--only semgrep|gitleaks|bandit` runs one scanner. <!-- claim: tests/unit/ci/test_security.py::test_existing_files_drop_paths_outside_the_repository --> <!-- claim: tests/unit/ci/test_security.py::test_main_only_runs_the_named_scanner -->

Each scanner applies its own ignores. Semgrep skips what `.semgrepignore` names; `ci/security.py` filters, since semgrep scans explicit targets regardless. <!-- claim: tests/unit/ci/test_security.py::test_semgrep_targets_apply_the_repositorys_semgrepignore --> Bandit reads the `.py` files and applies `[tool.bandit]`. Gitleaks scans a copy of exactly those files with `.gitleaks.toml`, so `.venv` is never scanned. Exit 0 when clean, 1 on a finding, 2 when a scanner could not run: a fetch or checksum failure, a missing `uv`, a scanner that can't start, or one that exits with an error rather than findings. Semgrep and bandit findings are read from their JSON reports, so a crash that exits 1 counts as could not run; gitleaks reports findings with exit 3. <!-- claim: tests/unit/ci/test_security.py::test_main_exit_code --> <!-- claim: tests/unit/ci/test_security.py::test_a_missing_uv_exits_2_with_one_line --> <!-- claim: tests/unit/ci/test_security.py::test_a_scanner_that_cannot_start_exits_2 --> <!-- claim: tests/unit/ci/test_security.py::test_exit_1_with_no_results_is_a_crash, tests/unit/ci/test_security.py::test_a_bandit_crash_is_could_not_run -->

The pins live in `ci/security.toml`. Semgrep and every dependency are pinned with hashes in `ci/semgrep-requirements.txt`. <!-- claim: tests/unit/ci/test_security_parity.py::test_semgrep_and_every_dependency_are_pinned_with_hashes --> Each run reinstalls that dedicated environment with `uv pip sync --reinstall --require-hashes --no-build`, so every installed file, the `semgrep` entry point included, is rewritten from an archive whose hash the lock lists (9 to 14 s measured). <!-- claim: tests/unit/ci/test_security.py::test_the_semgrep_environment_is_synced_to_the_hashed_lock_on_every_run, tests/unit/ci/test_security.py::test_run_semgrep_reinstalls_unless_told_to_skip --> The pre-commit hook passes `--skip-env-reinstall`, which syncs without `--reinstall`: that checks the installed versions, not the files. <!-- claim: tests/unit/ci/test_security.py::test_the_hook_syncs_the_semgrep_environment_without_reinstalling --> Bandit comes from `uv.lock`. Gitleaks' release archive is checked against its SHA-256 on every run before the binary is unpacked, and a mismatched cached archive is downloaded again. <!-- claim: tests/unit/ci/test_security.py::test_a_swapped_cached_archive_is_downloaded_again --> <!-- claim: tests/unit/ci/test_security.py::test_a_swapped_cached_archive_with_no_network_fails -->

Semgrep's rules are pinned by commit of [semgrep/semgrep-rules](https://github.com/semgrep/semgrep-rules) and [trailofbits/semgrep-rules](https://github.com/trailofbits/semgrep-rules). A registry config (`p/python`, `--config=auto`) changes under a fixed tree (#1708). Vendoring isn't possible: the Semgrep Rules License forbids distributing the rules. The list is the files behind `p/python`, `p/security-audit` and `--config=auto` for this repository, as of 2026-11-04. <!-- claim: tests/unit/ci/test_security_parity.py::test_the_pinned_rules_keep_both_old_rulesets --> Listed paths are matched with exact case against the commit's git tree, so a case-insensitive filesystem can't hide a wrong name. <!-- claim: tests/unit/ci/test_security.py::test_rule_files_are_matched_with_exact_case_from_the_git_tree -->

Each source is fetched into `~/.cache/db-modernizer-security` (`SECURITY_CACHE_DIR`). Every run hashes the listed rule files' bytes and compares the result with `rules_sha256` in `ci/security.toml`, so an edit that `git status` can't see (`--skip-worktree`, `git replace`) is caught too. On a mismatch the sources are fetched again once, then the run exits 2. <!-- claim: tests/unit/ci/test_security.py::test_a_tamper_git_status_cannot_see_is_caught_by_the_rules_pin, tests/unit/ci/test_security.py::test_a_tampered_checkout_that_refetches_wrong_fails, tests/unit/ci/test_security.py::test_a_tampered_checkout_is_fetched_again --> The merged rules file is rebuilt every run, each rule under its registry id (path part lowercased), so `nosemgrep: <rule id>` comments match. <!-- claim: tests/unit/ci/test_security.py::test_the_merged_rules_are_rebuilt_on_every_run --> <!-- claim: tests/unit/ci/test_security.py::test_rule_id_lowercases_the_path_but_not_the_rules_own_id --> It is written to a git-ignored `.security-semgrep-rules.<pid>.yaml` at the repository root for the run. Semgrep prefixes a rule file's ids with its folder otherwise, and runs many times slower from outside the repository.

Semgrep runs with `--timeout 0`, so a slow machine never skips a rule. <!-- claim: tests/unit/ci/test_security.py::test_semgrep_runs_with_no_per_rule_timeout --> To move a rule pin, change the commit and write the hash `./ci/security.sh --print-rules-sha256` prints (a fresh fetch) into `rules_sha256`; <!-- claim: tests/unit/ci/test_security.py::test_the_pin_helper_prints_the_hash_of_a_fresh_fetch --> to move semgrep, regenerate the requirements as the file's header says. Then run `./ci/security.sh`, and fix or justify every new finding in the same pull request. `detect-secrets` stays a separate pre-commit hook. <!-- claim: tests/unit/ci/test_security_parity.py::test_detect_secrets_stays_a_separate_precommit_hook --> `lint.sh` and the black, isort and ruff hooks cover the same Python files. <!-- claim: tests/unit/ci/test_security_parity.py::test_lint_sh_and_the_precommit_hook_cover_the_same_python_files -->

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

Sharding (#956): `TEST_SHARD=N/M ./ci/test.sh` runs only shard N of M, split by [pytest-split](https://github.com/jerry-git/pytest-split) (`--splits M --group N --splitting-algorithm least_duration`) from the per-test durations recorded in `.test_durations` at the repo root. The M shards together run exactly the tests an unsharded run does; a test missing from the file counts as the average duration, so new tests only make the balance a little worse, never drop a test. The GitHub `test-shard (N/6)` jobs run it with six shards, and the required `test` job passes only when all six did. Coverage (`--cov=src`) runs on pushes to main and in the nightly scheduled run, not on pull requests: each shard uploads its `.coverage` data and the `test` job combines them and applies the same 65% gate to the whole suite. With `--cov`, `ci/test.sh` sets `COVERAGE_CORE=sysmon` unless it is already set (#1718): Python 3.12's `sys.monitoring` tracer gives the same line coverage (95% on the full suite with either tracer) in 289 s, against 634 s for the default tracer and 260 s without coverage. Branch coverage would need the default tracer on 3.12, and this repo measures lines only.

Per-test time limit (#1835): `ci/test.sh` passes `--timeout=300 --timeout-method=thread` ([pytest-timeout](https://github.com/pytest-dev/pytest-timeout), in the `dev` extra), so a test that hangs fails under its own name after five minutes instead of stalling its shard until the CI job limit. The thread method also stops a test blocked in native code or a system call, prints every thread's stack, and ends that xdist worker; xdist replaces it and reports the test it was running. Five minutes is five times the slowest test in `.test_durations` (about a minute), and `tests/unit/ci/test_test_sh_sharding.py` keeps it at least that. A caller's own `--timeout` comes later on the command line and wins: `./ci/test.sh --timeout=0` turns the limit off, for example under a debugger.

Refresh the durations after adding, renaming or speeding up many tests (on any machine; only the relative times matter): `./ci/test.sh --store-durations --clean-durations`, then commit `.test_durations`. Record them with the variable unset (the full `default` Hypothesis profile, as main and nightly run); pull requests run the lighter `ci` profile, which shortens the property tests in every shard alike, so the balance holds. `--clean-durations` drops entries for tests that no longer exist. The file is about 6 MB, so it is excluded from the pre-commit large-file check, and from the end-of-file fixer because pytest-split writes it without a trailing newline.

The public `assess()` contract's consumer tests run here too (#1184): `tests/contract/test_assess_consumer_contract.py` runs `assess()` on the fixtures in `tests/contract/fixtures/assess_consumer_fixtures.json` for every supported `run_through` (model entry points patched) and checks each fixture's guarded fields against the published result schema, and `tests/contract/test_assess_released_contract.py` fails a breaking change since the last release, from contract 1.3 on (`tests/contract/assess_released_since_1_3.json`), without a MAJOR bump; `tests/contract/assess_released_snapshot.json` is the historical record of the last release recorded before 1.3 (v0.4.0-beta.1's 1.1; v0.4.0-beta.2's 1.2 was never recorded), never re-recorded, whose versions the test asserts are now refused (#1641). The same comparison against main's copy of that file runs in `scripts/resolve_contract_versions.py --check --against` (the `validate-contracts` job and `ci/pre_merge.sh`), so a branch cannot pass by re-recording it. See CONTRIBUTING.md's "The public `assess()` contract".

The raw-query large-server memory regression runs without a platform skip on
Linux, macOS and Windows wherever this test command is run. The GitHub CI
workflow currently gates on Ubuntu only. The worker measures current RSS with
psutil. On Windows, run `ci/test.sh` from Git Bash or WSL, or run
`uv run pytest tests/ --ignore=tests/e2e -m "not integration and not e2e" --fail-on-skip`
directly.

The safety filters' benign-input corpus (#1161) runs here as part of the unit tests: `tests/unit/safety_filters/test_benign_corpus.py` reads `tests/fixtures/safety_filter_corpus/corpus.json` and checks that ordinary SQL, shell and prose pass unchanged through `src/api/secret_guard.py` and `src/shared/prose_style.py`, and that a small set of known-bad strings is still caught. The `safety-filter-corpus` pre-commit hook runs it whenever a commit touches either module or the corpus. A reported false positive or miss goes into the corpus with its fix.

## `../scripts/regenerate_baselines.py` (#1163)

One command for every generated baseline: the routing baseline and snapshot, the contract schema and value-set snapshots, the `assess()` JSON Schemas and the rendered rule catalog. `--check` writes nothing and exits 1 when any is stale, naming each with its own command; without it, the script regenerates only the stale ones with those commands. `--reason "..."` is required when a routing baseline is stale, and `--only <name>` (repeatable) limits the run. The release snapshots are recorded at a release (`--write-released <tag>`), never here. What counts as stale, timings and what is left out: the script's docstring. Tests: `tests/unit/scripts/test_regenerate_baselines.py`.

## `pre_merge.sh`

A cheap failsafe to run before pushing or asking for a merge: `./ci/pre_merge.sh [--quick] [--strict-main-tests | --no-main-tests] [--mutation]` (`--quick` skips the full test suite and limits the version-pin check to what the branch touched). In order, each step prints one status line: fetch the public repo's main (`upstream` or `origin`, whichever points at it, else `origin`); check that a commit runs the repository's hooks (#1843, `pre_merge.py hooks`: pre-commit's `pre-commit` and `commit-msg` hooks, or a `core.hooksPath` that runs them; the rules are in `ci/pre_merge.py`'s docstring); count commits behind main; check every commit's author (`scripts/check_commit_authors.py --range <remote>/main..HEAD`, see `check_commits.sh`); check for merge conflicts with `git merge-tree --write-tree` (no worktree, index or ref change); check contract versions (ADR-008 Decision 8; the rules: `ci/pre_merge.py`'s `contracts` docstring and CONTRIBUTING.md's Contract Changes): a changed contract needs a pending bump or a version above main's, and `scripts/resolve_contract_versions.py --check --against <merge base>` fails a shape or value change that has neither. On a branch stacked on another open pull request (#1846), the three contract checks compare with that pull request's head instead of main and print `stacked on #N: re-check after #N merges` (the details: `pre_merge.py stacked`'s docstring); the merge queue still compares with main. <!-- claim: tests/unit/ci/test_stacked_contract_check_scratch_repo.py::test_the_merge_queue_always_compares_with_main --> <!-- claim: tests/unit/ci/test_pre_merge.py::test_pre_merge_sh_compares_the_contract_checks_with_the_stack_base, tests/unit/ci/test_pre_merge.py::test_check_contract_on_a_stacked_branch_compares_with_the_stack_base, tests/unit/ci/test_pre_merge.py::test_a_branch_carrying_another_pr_head_is_stacked_on_it, tests/unit/ci/test_pre_merge.py::test_stack_notice_names_the_pull_request_twice --> Pending bumps are listed (a pending MAJOR as a warning with its command, see `resolve_contract_versions.py` below); warn when a generated file differs from main but its generator check fails; check the ADR touch rule (#1154, `pre_merge.py adr`): a `.py` change under `src/agents/referee/` or `src/shared/` needs an edited ADR, or a `No ADR change: <reason>` line in a commit message or the PR body; check the docs-impact map (#1162, below); run the branch's new and changed tests against main's code and list the ones that pass there (see below); with `--mutation`, `ci/hunk_revert.py` (below); run `ci/lint.sh` and `ci/security.sh`; run `ci/replay.sh` (below); run `scripts/check_version_pins.py` (see below; with `--quick`, `--base <remote>/main`); without `--quick`, run `ci/test.sh -q`; print the `needs_llm_run.sh` verdict against main; check the PR body's Evidence; and, last, read the PR's last CI run with `ci/rerun_failed.py status` (#1849, below), printing `./ci/rerun_failed.sh <PR>` as a warning when it was red (never a failure). It exits non-zero when a step fails (a warning never does; with `--strict-main-tests` a new or changed test that passes on main fails too) and ends with a summary. The parsing and verdict logic lives in `ci/pre_merge.py`, covered by `tests/unit/ci/test_pre_merge.py`. No env vars. It needs network for the fetch and, through `gh` when it is installed and logged in (`gh auth status`, `gh pr view`, `gh run list`), for the author check, the PR body and the last CI run; without it those steps fall back or skip.

`scripts/check_version_pins.py` (#1226) bumps every contract the resolver can edit by one MINOR in a temporary worktree of HEAD and runs every test file that mentions a contract version: a test that fails only under the bump compares with a literal version and would fail on the resolved branch (exit 1); compare with `current_version(Model)` from `src/shared/contract_versioning.py` instead. With `--base REF` (#1845, what `pre_merge.sh --quick` passes) it runs only the test files the branch's changes can reach; the full run is in the `validate-contracts` job on every pull request. Which changes it can place, and what the narrowed run can miss: its docstring. Covered by `tests/unit/scripts/test_check_version_pins.py`.

The docs-impact step (#1162) reads `ci/docs_impact_map.toml`: `[[entry]]` tables with a `name`, `paths` (globs against repo-relative paths; `*` stays in one folder, `**` crosses folders), `docs` (the doc pages, help-site pages and command files that describe those paths) and `why` (what drifted). For each entry whose paths the branch changed (`git diff <remote>/main...HEAD`), it looks for a changed file among that entry's pages. An entry with none is unreviewed: the step warns and lists the entry, the first changed path and the pages to review. It fails only when some entry is unreviewed and neither the pull request body nor a commit message has a `Docs: <reason>` line (case-sensitive, a `-` or `*` bullet is fine, a real reason: the template's `<reason>` placeholder or an empty one does not count). The body is read as the Evidence check reads it (`ci/pr_evidence.py`'s `_visible`: a line in a comment, code span, fenced or indented code does not count); in a commit message only the body or a trailer counts, never the subject, so a Conventional Commits `docs: ...` subject is not a reason. The template's Docs field is where it goes. It needs no network and no test run, so it runs with `--quick`; without a pull request body (no `gh`, or no PR yet) only a `Docs:` commit trailer counts, and the error says so. The map is seeded where pages drifted before and is kept small on purpose: add an entry when a page went stale after a code change, never one per module. Each test file a claim marker cites (`ci/claims.py`, #1851) is an entry too, derived when the step runs: a branch that changes the test reviews the pages that cite it. <!-- claim: tests/unit/ci/test_pre_merge.py::test_docs_impact_reviews_the_pages_a_changed_claim_test_proves --> `tests/unit/docs/test_docs_impact_map.py` fails an entry whose page does not exist or whose path matches no tracked file, and `tests/unit/docs/test_doc_enum_claims.py` is the companion for lists: each engine list a page states outright (the high-level design, the load-test guide, the engine ids on the use-Claude-Code page, the contributing file tree) must equal the engine catalog's active, planned or legacy set or the `src/agents/load_test/` folders, and AGENTS.md's `--llm-mode` table and the add-skill command's mode list must equal the `choices` of the scripts that take the flag (read from the AST, as `test_doc_script_references.py` does for flags). Unit tests for the step: `tests/unit/ci/test_pre_merge.py`.

The main-tests step (#1155) checks AGENTS.md self-review item 1, "every new test fails on main". `ci/main_tests.py changed` lists the test functions the branch adds or changes in committed `tests/**/test_*.py` files (uncommitted edits are not seen): each is compared with the merge base, decorators included, so a new parametrize case counts; a renamed file is compared with its old path; nested `Test*` classes are included; a test whose helper changed but whose own code did not is not listed. The step prunes stale worktree entries, makes a temporary detached worktree of main, checks out the branch's `tests/` tree into it, and runs those tests there with this worktree's Python and `PYTHONPATH` on the temporary worktree (it checks that `src` imports from there first, and warns instead if it does not). It runs one pytest per file, up to four at a time, so a file that fails to import on main does not stop the others, and removes the worktree on exit. The status line counts the tests that fail on main, those that fail only because their file does not import on main (a new test that imports a new function lands here: it is never actually run against main's code, so check it by hand), those skipped on main, and those whose every case passes on main; the step then lists each test by node id and outcome. A test that passes on main either misses the changed path or guards behaviour main already has. Mark the second kind with a `@pytest.mark.passes_on_main` decorator on the test or an enclosing class. The step does not run it, and lists it by name as marked. A module-level `pytestmark` with that marker is not honoured: the file's tests run as usual and the file is reported. `tests/e2e/` and `tests/integration/` are not run (they need the whole pipeline or a live stack). By default the step only warns; `--strict-main-tests` makes a passing test a failure (pre_merge exits non-zero), and `--no-main-tests` skips the step. It runs with `--quick` and costs a few seconds for a handful of files. Its logic is covered by `tests/unit/ci/test_main_tests.py`.

## `hunk_revert.py` (#1852)

`uv run python ci/hunk_revert.py [--base REF] [--max-hunks N] [--timeout S] [--jobs N] [--strict]` checks AGENTS.md self-review item 2, "revert each part of the fix and check that some test fails", one hunk at a time. `./ci/pre_merge.sh --mutation` runs it, because it takes minutes. In disposable worktrees of HEAD it reverts each committed `src/` and `scripts/` hunk to the merge base's lines (lines split on `\n` only, as git counts them), runs the branch's new and changed tests (the main-tests step's list), and reports each hunk as `noticed`, `unnoticed` (no test failed) or `timeout`. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_the_report_names_exactly_the_unguarded_hunk, tests/unit/ci/test_hunk_revert.py::test_revert_leaves_the_other_hunks_in_place, tests/unit/ci/test_hunk_revert.py::test_an_odd_line_break_before_a_hunk_does_not_hide_it --> The runs pass `--fail-on-skip` when the repository's conftest defines it, as `ci/test.sh` does. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_fail_on_skip_is_passed_when_the_conftest_defines_it -->

It never writes to your working tree, and it removes its worktrees on exit. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_the_users_working_tree_is_untouched_and_the_worktrees_are_removed --> On SIGINT, SIGTERM or SIGHUP it also kills its pytest runs, and a second Ctrl-C to the whole `uv run ... | tee` group does not cut the cleanup short. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_ctrl_c_removes_the_worktrees, tests/unit/ci/test_hunk_revert.py::test_a_second_ctrl_c_through_uv_and_tee_does_not_cut_the_cleanup_short, tests/unit/ci/test_hunk_revert.py::test_the_handler_ignores_further_signals_before_it_raises, tests/unit/ci/test_hunk_revert.py::test_signals_are_ignored_during_a_normal_cleanup --> After a SIGKILL, the next run sweeps up the worktrees, the folder and the pytest runs left behind, and leaves alone those of a run that is still going. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_a_killed_run_is_swept_by_the_next_one, tests/unit/ci/test_hunk_revert.py::test_the_sweep_leaves_a_live_run_and_young_folders_alone, tests/unit/ci/test_hunk_revert.py::test_the_sweep_removes_a_locked_registration_whose_folder_is_gone, tests/unit/ci/test_hunk_revert.py::test_a_zombie_or_a_reused_pid_is_not_the_recorded_process -->

Skipped, each listed with the reason: a Python hunk that changes only comments, formatting or whitespace (the AST, docstrings included, is unchanged), UI code (its tests are Jest), binary files and files that are not UTF-8. Nothing else is skipped. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_python_comment_and_whitespace_hunks_are_skipped_and_reported, tests/unit/ci/test_hunk_revert.py::test_skip_reason_reads_python_code_not_text, tests/unit/ci/test_hunk_revert.py::test_skip_reason_leaves_ui_code_to_its_own_tests, tests/unit/ci/test_hunk_revert.py::test_binary_and_non_utf8_files_are_skipped_and_reported --> Docstring hunks run, because a docstring is behaviour here (contract schemas, tool specs, `--help`). Hunks in other file types run too, because a `#` line or a blank line can be data there (YAML block scalars, TOML strings, heredocs, prompts). <!-- claim: tests/unit/ci/test_hunk_revert.py::test_no_docstring_hunk_is_skipped, tests/unit/ci/test_hunk_revert.py::test_other_files_are_never_skipped -->

Bounds: `--max-hunks` hunks run (default 40, the rest are listed as `capped`), each run gets `--timeout` seconds (default 300) and is then killed and reported as `timeout`, never `unnoticed`, and `--jobs` worktrees run in parallel (default 1). It prints one progress line per run. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_the_hunk_cap_leaves_the_rest_unchecked_and_says_so, tests/unit/ci/test_hunk_revert.py::test_a_run_over_the_timeout_is_reported_as_timed_out_not_unnoticed, tests/unit/ci/test_hunk_revert.py::test_parallel_jobs_give_the_same_report, tests/unit/ci/test_hunk_revert.py::test_progress_lines_count_the_runs, tests/unit/ci/test_hunk_revert.py::test_the_defaults --> It exits 0 with the report; `--strict` exits 1 when a hunk is unnoticed; 2 means it could not judge, for instance because the tests fail on HEAD itself. With no new or changed test, every hunk is unnoticed. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_strict_fails_when_a_hunk_goes_unnoticed, tests/unit/ci/test_hunk_revert.py::test_strict_passes_when_every_hunk_is_noticed, tests/unit/ci/test_hunk_revert.py::test_a_baseline_that_fails_on_the_branch_stops_with_exit_2, tests/unit/ci/test_hunk_revert.py::test_no_changed_test_means_every_hunk_goes_unnoticed_without_a_run --> `pre_merge.sh --mutation` only warns. <!-- claim: tests/unit/ci/test_hunk_revert.py::test_pre_merge_mutation_step_is_opt_in_and_never_fails --> What it cannot see (uncommitted edits, a hunk only an unchanged test notices): its docstring. Tests: `tests/unit/ci/test_hunk_revert.py`, on scratch repositories.

## `rerun_failed.sh` (#1849)

`./ci/rerun_failed.sh [--list] [PR]` re-runs, locally, the tests that failed in a pull request's last CI run, so the same failures are not pushed again. `ci/rerun_failed.py` (standard library and `gh`) reads the failed test ids from the newest completed `CI` run of the pull request's own commits. A failure with no test id (lint, a contract check) re-runs nothing and names the failed jobs. The unit, contract and property ids run with a pull request's `ci/test.sh` settings; `tests/e2e` ids are printed, not run. `--list` prints the ids only. It needs network and a `gh` login with read access, and re-runs the ids on your working tree, not the run's commit. How it picks the run, reads the logs and exits: its docstring. Covered by `tests/unit/ci/test_rerun_failed.py`, against a fake `gh`.

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

The help site's sample report is not a pipeline run: `scripts/build_docs_sample.py` renders the recorded WordPress model run committed under `docs/examples/wordpress/sample-run/` (its README names the release) with the current renderers, so no model is called. Refresh that run once per release (the release steps in CONTRIBUTING.md, "Releases"): run `/modernize` on the WordPress sample, then `uv run python scripts/build_docs_sample.py --refresh-from artifacts/wordpress/<job>`, update the release named in the README and in `docs/sample.md`, and have a person read the refreshed `report.json`'s diff before it merges. `--deterministic` renders a fresh deterministic run instead.

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
is never relaxed. The watchdog's own timer gets none of the caller's stdio, so a timer that outlives it cannot hold the caller's output open for the whole limit (#1835). In CI (`CI=true`, set automatically by GitHub Actions) `playwright_ensure_deps`
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

A zero-token replay of the saved real-model runs (#1409): `./ci/replay.sh`, no model call and no browser; it needs only the `dev` extra (with the `e2e` extra it also gives pytest-playwright its own `--output` folder). A real-model run costs about 4 USD on wordpress and 40 USD on discourse, but most of its checks are deterministic once the model has answered, so one passing run per sample is kept in `tests/fixtures/real_runs/` (what each holds and how to refresh it: [its README](../tests/fixtures/real_runs/README.md)) and replayed with the current code: `ci/llm/run.py check-transcript` over its transcript against the current allowlist, then the deliverables re-rendered from its `report.json` by the current renderers and the external-mode e2e checks over them (`tests/e2e`, the tests parametrized over the `external` job: the customer-text guard, the PDF and engineering report checks, the judge facts' agreement with the report). Both `ci/pre_merge.sh` (with `--quick` too) and the `replay` e2e shard run it.

It replays `wordpress-both`, then `discourse-both` (#1847), each into `$E2E_OUTPUT/replay/<fixture>/`. A fixture not saved yet (no `run.json`) is skipped with a line saying so, a failed one does not stop the next, and the last line sums up; it fails when a fixture failed or none ran. <!-- claim: tests/unit/ci/test_replay.py::test_replay_sh_replays_both_samples_by_default, tests/unit/ci/test_replay.py::test_replay_sh_skips_a_fixture_not_saved_yet_and_fails_when_none_ran, tests/unit/ci/test_replay.py::test_replay_sh_keeps_going_after_a_failed_fixture_and_fails_at_the_end --> `REPLAY_FIXTURE` replays one fixture, which must exist; `REPLAY_FIXTURES` replaces the list. <!-- claim: tests/unit/ci/test_replay.py::test_an_explicit_replay_fixture_must_exist -->

What it does not re-run: synthesis and the facts layer (`src/shared/report_facts.py`). The saved `report.json`, its `facts` included, is the one that run wrote; only the renderers and the checks are current. So a change to synthesis or to how a fact is computed is not covered here (the deterministic shards and e2e-llm cover it), and the fixture ages. `@pytest.mark.replay_stale` (registered in `pyproject.toml`) marks an external-mode check of what synthesis writes that a saved `report.json` predates, for example #1510's check that no mitigation strategy restates a risk's mitigation: e2e-llm runs it, the replay deselects it. Give the reason in the test's docstring, and drop the marker when the fixtures are refreshed. `tests/unit/ci/test_replay_stale_marker.py` checks that only `ci/replay.sh` deselects it. **The wordpress fixture is mixed** (#1463): its `job/` and synthesis files come from a research run and its transcript and `run.json` figures from an e2e-llm run (`run.json`: `job_source`, `job_relabelled_from`), so a green replay there means the current renderers and checks over that job, not over a passing e2e-llm run.

A fixture file over 900 KB is stored gzip-compressed as `<name>.gz` (#1847), with fixed header fields, so saving the same run again writes the same bytes; `prepare` unpacks every one before the replay runs. <!-- claim: tests/unit/ci/test_replay.py::test_save_stores_a_file_over_the_cap_gzip_compressed, tests/unit/ci/test_replay.py::test_a_resave_writes_the_same_compressed_bytes, tests/unit/ci/test_replay.py::test_prepare_unpacks_every_compressed_file --> `save` refuses a file still over 1,000,000 bytes compressed, so `check-added-large-files` (1000 KB) passes on every fixture file. <!-- claim: tests/unit/ci/test_replay.py::test_save_refuses_a_file_too_large_even_compressed, tests/unit/ci/test_replay.py::test_every_compressed_fixture_file_stays_under_the_repo_large_file_limit --> The detect-secrets hook skips a compressed file unread, so the `scan-compressed-fixtures` hook and `ci/lint.sh` run `ci/llm/private_scan.py` over its decompressed text. <!-- claim: tests/unit/ci/test_private_scan.py::test_detect_secrets_own_hook_reads_nothing_in_a_compressed_file, tests/unit/ci/test_private_scan.py::test_a_planted_secret_inside_a_compressed_fixture_file_is_caught, tests/unit/ci/test_private_scan.py::test_the_hook_and_lint_scan_every_compressed_fixture_file --> Covered by `tests/unit/ci/test_replay.py` and `tests/unit/ci/test_private_scan.py`.

## `needs_llm_run.sh`

`./ci/needs_llm_run.sh [BASE]` (default `origin/main`) diffs `BASE...HEAD` (committed changes only; both sides of a rename count) and prints, per run, `required`, `nightly` or `not required` plus the matching paths: `e2e-llm` (the headless Claude Code run: session inputs, the builders and validators of its `llm_input`/request files, and the graph query catalog and its runner, whose rows the `/explore` step gives to a model) and `bedrock-smoke` (Bedrock-only call sites the headless run never reaches). A file can be in both runs. Any git error (bad base, shallow clone, no merge base) exits 2 with a message and no verdict. <!-- claim: tests/unit/ci/test_needs_llm_run.py::test_main_prints_verdict_per_run, tests/unit/ci/test_needs_llm_run.py::test_cli_fails_closed_on_bad_base -->

Each run has two tiers of paths (#1965), `*_PROMPT` and `*_NIGHTLY` in `ci/needs_llm_run.py`:

- **Prompt tier, `required`:** files that write, word, frame or tag text sent to a model: the skills and commands (`src/skills/`, `.claude/`, AGENTS.md, CLAUDE.md), `ci/e2e-llm.sh` and the judge's prompt, the sample collections the e2e-llm fixtures unzip (`docs/examples/wordpress/wordpress.zip`, `docs/examples/discourse/discourse.zip`), the prompt and request builders (every `prepare_*_llm_input` home, `design_view.py`, the schema-design input builders, `scripts/run_analysis.py` and `scripts/run_schema_design.py`), the framing (`prompt_framing.py`, `bedrock_guardrail.py`), prose that reaches a model word for word (the write-intent catalog, the migration-wave rationale, the correction-turn messages, the AWS Transform tool descriptions), the model-output contracts, and the model settings (`src/shared/models.py`). The run's result line is needed before the pull request merges. <!-- claim: tests/unit/ci/test_needs_llm_run.py::test_e2e_llm_prompt_tier_is_required, tests/unit/ci/test_needs_llm_run.py::test_bedrock_smoke_prompt_tier_is_required, tests/unit/ci/test_needs_llm_run.py::test_shared_prompt_file_requires_both, tests/unit/ci/test_needs_llm_run.py::test_every_e2e_llm_sample_collection_is_prompt_tier -->
- **Nightly tier, `nightly`:** pipeline logic whose output a model only reads as data (routing, scoring, pattern catalogs, collectors and analysis values, synthesis math), the model-call plumbing (`role_bedrock_model.py`, the AWS Transform runtime) and the runner scripts that only read and write paths. Unit tests cover them on every pull request, and the deterministic e2e covers the pipeline ones; neither reaches the Bedrock plumbing or a real model, which the nightly run covers. PR Evidence accepts the `nightly` line with no result. <!-- claim: tests/unit/ci/test_needs_llm_run.py::test_e2e_llm_nightly_tier_runs_nightly, tests/unit/ci/test_needs_llm_run.py::test_bedrock_smoke_nightly_tier_runs_nightly, tests/unit/ci/test_pr_evidence.py::test_nightly_runs_need_no_result_line_even_in_the_queue --> The maintainers run e2e-llm on wordpress and the Bedrock smoke on `main` each night, covering the day's merges; a red nightly run is bisected over that day's merges and fixed forward (a maintainer process: no check in this repository runs or enforces it).
- **Not listed, `not required`.** <!-- claim: tests/unit/ci/test_needs_llm_run.py::test_deterministic_paths_need_neither, tests/unit/ci/test_needs_llm_run.py::test_empty_diff_needs_neither -->

The prompt tier wins: one prompt-tier path makes the run `required`, and the nightly-tier paths are then listed with `(nightly)`. <!-- claim: tests/unit/ci/test_needs_llm_run.py::test_prompt_tier_wins_in_a_mixed_diff --> The rule is applied conservatively: when a path's tier is unclear, it is prompt tier if a change to it alters the wording, framing, tags or field set of what a model receives, prose that reaches it word for word, the shape it is asked to return, or the model settings. Values computed upstream and read as data are nightly. Before a release the full run is due whatever the tiers say (CONTRIBUTING.md, Releases).

A unit test scans `src/` and `scripts/` for model imports, model-id env reads and `llm_input`/request builders, and fails if a hit is not listed. <!-- claim: tests/unit/ci/test_needs_llm_run.py::test_every_model_call_site_is_listed --> Another scans `src/`, `scripts/` and `ci/llm/` for files that build a prompt (a system prompt, `frame_untrusted` or `with_untrusted_notice` text, an `llm_input` or request builder) and fails when a file the scan finds is in no list, or is not in the prompt tier of a run that lists it (the test names the reviewed exceptions). <!-- claim: tests/unit/ci/test_needs_llm_run.py::test_prompt_builders_are_listed, tests/unit/ci/test_needs_llm_run.py::test_prompt_builders_are_in_the_prompt_tier --> A third follows the imports of the designer-input builders (`prepare_schema_design_input` and the analysis agents) and fails if a module feeding that input is neither listed nor reviewed in the test's `REVIEWED_DETERMINISTIC_IMPORTS`. No model call, no secrets, not wired into a workflow: the real-model runs themselves are done by the maintainers.

## `bedrock_smoke.py` (#2085)

`uv run python ci/bedrock_smoke.py smoke/status.jsonl` is the Bedrock smoke's pass check: it reads the status lines `scripts/run_assessment.py --all -y --llm-mode bedrock` printed and passes when every phase ended `complete` or `unchanged`, `schema_design`, `synthesis` and `report` are there, and the report lists no errors. `wave_redesign` also passes on each normal ending of its loop (`LOOP_ENDINGS` in `src/agents/schema_design/wave_redesign.py`: `complete`, `exhausted` when rounds ran and a wave stays held, `stopped` by a run ceiling) and on `skipped` with a reason; `failed`, `error` or any other status fails it. <!-- claim: tests/unit/ci/test_bedrock_smoke.py::test_each_normal_loop_ending_passes, tests/unit/ci/test_bedrock_smoke.py::test_the_accepted_endings_are_the_loops_own, tests/unit/ci/test_bedrock_smoke.py::test_a_skip_passes_only_with_its_reason, tests/unit/ci/test_bedrock_smoke.py::test_a_design_that_raised_fails_the_smoke, tests/unit/ci/test_bedrock_smoke.py::test_a_real_failure_still_fails, tests/unit/ci/test_bedrock_smoke.py::test_a_missing_phase_fails --> It exits 0 on a pass, 1 on a failure (the reason on stderr) and 2 when it cannot read the file. <!-- claim: tests/unit/ci/test_bedrock_smoke.py::test_the_file_the_ci_job_reads -->

## `pr_evidence.py` (#1153)

`uv run python ci/pr_evidence.py render [--base REF] [--main-tests-line TEXT]` (default `origin/main`) prints the PR template's Evidence section from the head commit: the `needs_llm_run` verdict lines, a comment per required run saying to add its result line when the run finishes (per nightly run, one saying none is needed), the new and changed tests with their node ids, and a routing drift line, which says `none` only when no file in the routing set changed. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_render_agrees_with_check, tests/unit/ci/test_pr_evidence.py::test_render_writes_the_nightly_lines_and_check_accepts_them, tests/unit/ci/test_pr_evidence.py::test_drift_none_only_when_no_routing_file_changed, tests/unit/ci/test_pr_evidence.py::test_drift_is_unknown_when_the_set_cannot_be_computed --> It exits 2 when `ci/main_tests.py` fails. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_render_fails_loudly_when_main_tests_fails -->

`python3 ci/pr_evidence.py check (--body-file FILE | --body-env VAR | --queue-ref-env VAR) [--base REF] [--author-env VAR] [--require-results]` (default base `HEAD^1`, standard library only) compares the PR body with the diff. Comments, fenced and indented code are never read; code spans are read only by the Claims table and by the near-miss result-line check below. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_crlf_and_cr_bodies_parse_like_lf, tests/unit/ci/test_pr_evidence.py::test_html_comments_are_ignored, tests/unit/ci/test_pr_evidence.py::test_lines_in_an_unclosed_comment_or_a_fence_do_not_count, tests/unit/ci/test_pr_evidence.py::test_indented_code_blocks_do_not_count --> What it enforces (the exact rules: its docstring):

- One verdict line per run, exactly as `ci/needs_llm_run.sh` prints it; a missing, contradicting or conflicting one fails. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_missing_verdict_fails, tests/unit/ci/test_pr_evidence.py::test_contradicting_verdict_fails, tests/unit/ci/test_pr_evidence.py::test_conflicting_lines_for_one_run_fail -->
- A `nightly` run needs no result line, also with `--require-results`, but a result line it has must be a pass; a `required` one still needs its result. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_nightly_runs_need_no_result_line_even_in_the_queue, tests/unit/ci/test_pr_evidence.py::test_a_required_run_beside_a_nightly_one_still_needs_its_result, tests/unit/ci/test_pr_evidence.py::test_a_nightly_verdict_must_match_the_diff, tests/unit/ci/test_pr_evidence.py::test_a_failing_result_line_on_a_nightly_run_fails -->
- For each required run, a `<run> result: ...` line that begins with `pass` or is a `MODERNIZE_RESULT: complete` line. A negative, tentative, empty or placeholder result fails, and so does a near miss (other casing, bold, a code span). <!-- claim: tests/unit/ci/test_pr_evidence.py::test_required_result_must_be_a_strict_pass, tests/unit/ci/test_pr_evidence.py::test_placeholder_or_empty_result_is_an_error_not_pending, tests/unit/ci/test_pr_evidence.py::test_a_near_miss_result_line_is_an_error_never_pending --> With no result line yet the run is pending: a warning, exit 0, unless `--require-results` is passed (#1850). <!-- claim: tests/unit/ci/test_pr_evidence.py::test_required_run_without_a_result_line_is_pending_not_an_error, tests/unit/ci/test_pr_evidence.py::test_require_results_turns_pending_into_an_error -->
- The Claims table (#1851), under any heading that starts with `Claims`: each row names tests `ci/claims.py` accepts, resolved in the working directory, or says `removed`. A PR that changes a claim page (`CLAIM_PAGES`: `docs/`, the command and subagent files, the skills, every README, AGENTS.md, CONTRIBUTING.md) needs a row or a `No claims: <reason>` line. Code spans keep their text here, and a row is the header only when a separator row follows it. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_a_docs_change_without_a_claims_table_fails, tests/unit/ci/test_pr_evidence.py::test_rows_that_name_no_test_fail, tests/unit/ci/test_pr_evidence.py::test_rows_naming_real_tests_or_removed_pass, tests/unit/ci/test_pr_evidence.py::test_no_claims_with_a_reason_passes_and_the_placeholder_does_not, tests/unit/ci/test_pr_evidence.py::test_every_claims_heading_form_is_read, tests/unit/ci/test_pr_evidence.py::test_a_header_without_a_separator_is_a_row, tests/unit/ci/test_pr_evidence.py::test_every_claim_page_kind_needs_the_table, tests/unit/ci/test_pr_evidence.py::test_the_cli_resolves_claims_against_the_working_directory --> It checks that the test exists, not that it proves the sentence.
- A PR is skipped only when its author is `dependabot[bot]` and every commit since the base is the bot's own verified work as the commits API reports it, with no merge commit. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_cli_skips_only_dependabot_with_only_dependabot_commits, tests/unit/ci/test_pr_evidence.py::test_cli_checks_a_dependabot_pr_with_a_human_commit_on_top, tests/unit/ci/test_pr_evidence.py::test_a_merge_commit_in_a_dependabot_pr_disables_the_skip -->

Failures print `::error::` lines and exit 1; exit 2 means the diff or the API could not be read. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_cli_exit_2_when_the_diff_cannot_be_read, tests/unit/ci/test_pr_evidence.py::test_cli_queue_gate_exits_2_when_the_api_fails -->

The workflow (`.github/workflows/pr-evidence.yml`) has two jobs, both with read-only tokens and no secrets. `PR Evidence` runs `check` on pull request events, a body edit included, with the body in an env var, and passes as a no-op on `merge_group`. `PR Evidence results` passes on a pull request; in the merge queue it reads the queued PR's body through the API and runs `check --require-results`, Claims included, with `ci/pr_evidence.py`, `ci/needs_llm_run.py` and `ci/claims.py` taken from the group's base, so a PR cannot rewrite its own gate. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_workflow_is_hardened_reads_the_body_from_env_and_reports_in_the_queue, tests/unit/ci/test_pr_evidence.py::test_every_workflow_step_but_the_noop_is_limited_to_pull_request, tests/unit/ci/test_pr_evidence.py::test_results_gate_is_a_separate_job_that_only_enforces_in_the_merge_queue, tests/unit/ci/test_pr_evidence.py::test_the_queue_gate_runs_the_bases_scripts_not_the_pull_requests, tests/unit/ci/test_pr_evidence.py::test_cli_queue_gate_checks_the_claims_table_of_the_entrys_own_diff, tests/unit/ci/test_pr_evidence.py::test_the_gate_copies_run_from_another_folder_and_resolve_against_the_checkout --> `--base` must be the sha in the queue ref, so the diff is the entry's own commits; that relies on the ruleset's `ALLGREEN` grouping and `REBASE` merge method, which no code checks. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_cli_queue_gate_exits_2_when_the_base_is_not_the_queue_refs_sha --> `PR Evidence results` is a required check; `PR Evidence` stays advisory. The workflow file is read from the merge group's ref, so it is protected only by who has write access (the details: `ci/pr_evidence.py`'s docstring). `ci/pre_merge.sh` runs `check` (without `--require-results`) on the body it fetched with `gh pr view`. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_pre_merge_runs_the_check_on_the_body_it_already_fetched --> Unit tests: `tests/unit/ci/test_pr_evidence.py`.

## `contract_major_gate.py` (#1648)

The `contract-major-gate` required check (`.github/workflows/contract-major-gate.yml`). It fails a pull request that moves the `assess()` contract version, of the request or the result, without a maintainer's label: a MAJOR needs `contract-major` (`Contract Major Approved` still counts), a MINOR needs `contract-minor` (#2176). A label counts only when the user who last applied it has the maintain or admin role and applied it after the PR's latest push; the workflow runs the base's copy of the script. A PATCH move, or none, passes without a GitHub call; internal contracts need no label. <!-- claim: tests/unit/ci/test_contract_major_gate.py::test_moved_without_the_label_fails, tests/unit/ci/test_contract_major_gate.py::test_moved_with_the_label_passes, tests/unit/ci/test_contract_major_gate.py::test_a_major_move_of_the_result_alone_fails_without_the_label, tests/unit/ci/test_contract_major_gate.py::test_unmoved_passes_without_asking_github, tests/unit/ci/test_contract_major_gate.py::test_an_assess_minor_move_needs_the_minor_label, tests/unit/ci/test_contract_major_gate.py::test_a_label_a_non_maintainer_applied_fails, tests/unit/ci/test_contract_major_gate.py::test_a_patch_passes_without_asking_github, tests/unit/ci/test_contract_major_gate.py::test_an_internal_contract_bump_needs_no_label --> How it reads the versions, the labels and the merge queue, and its exit codes: its docstring. Locally: `uv run python ci/contract_major_gate.py --base origin/main --head HEAD`. Tests: `tests/unit/ci/test_contract_major_gate.py`.

## `resolve_contract_versions.py` (#1158, #2176)

Contract version bumps are declared in every pull request and resolved once, at release (ADR-008 Decision 8). A pull request runs `uv run python scripts/bump_contract.py <contract> minor|major --reason "..." --issue N`, which writes one file under `src/contracts/pending/`, and keeps the version, the Version History and both contract snapshots at main's.

- `--check [--against REF] [--refuse-pending-major] [--require-resolved]` is the `validate-contracts` step (`Check pending contract bumps`) and part of `pre_merge.sh`. `--against` compares the contracts with REF's committed snapshots and with the snapshots REF's own code builds (counting only the pending files the change adds or changes), and refuses a `contract_version`, Version History line or snapshot entry written by hand. `--refuse-pending-major` (every event but a pull request) refuses a pending MAJOR; a pending MINOR reaches main and waits there. `--require-resolved` fails on any pending file: the release workflow runs it before publishing a tag. <!-- claim: tests/unit/scripts/test_contract_bumps.py::test_the_merge_queue_refuses_a_pending_major_and_accepts_a_pending_minor, tests/unit/ci/test_release_resolve_scratch_repo.py::test_the_merge_queue_accepts_a_pending_minor_and_refuses_a_pending_major, tests/unit/ci/test_release_resolve_scratch_repo.py::test_a_further_change_without_its_own_pending_file_fails, tests/unit/ci/test_release_resolve_scratch_repo.py::test_a_version_resolved_in_a_pull_request_is_refused -->
- With no flag it is the per-PR resolve of a MAJOR, after the last rebase: it resolves the contracts that have a pending MAJOR file and leaves every MINOR to the release.
- `--release --issue N [--date YYYY-MM-DD] [--no-commit]` is the release step (CONTRIBUTING.md's Releases, step 2): it resolves every pending file in this tree once, each contract by the strongest kind declared for it, writes the versions and the Version History entries, deletes the pending files, regenerates both snapshots and commits all of it as one commit ending `Refs #N`. It refuses uncommitted changes under `src/contracts/` or `tests/contract/` and writes nothing when a contract cannot be resolved. <!-- claim: tests/unit/ci/test_release_resolve_scratch_repo.py::test_the_release_resolves_every_pending_file_in_one_commit, tests/unit/scripts/test_contract_release_resolve.py::test_the_release_writes_nothing_when_a_contract_cannot_be_resolved -->

The rules and messages: the script's docstring. Tests: `tests/unit/scripts/test_contract_bumps.py`, `tests/unit/scripts/test_contract_release_resolve.py`, `tests/unit/ci/test_release_resolve_scratch_repo.py`.

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

`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default `1800000`; a set value is kept): how long `claude -p` keeps background subagents after the main turn ends (its default is 10 minutes; #2175).

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

**The `/explore` step** (#307): right after the judge, so every model call ends before the browser steps (#1977). <!-- claim: tests/unit/ci/test_explore_check.py::test_the_explore_step_runs_right_after_the_judge --> The script asks `/explore` one fixed question about the job, `How many queries does DynamoDB serve, and from which source tables?`, in its own `claude -p "/explore --job-id <job> ..."` session. The question is not the catalog's wording and needs a parameter: it is answered by the entry `engine_destinations` with `engine=dynamodb`, whose one row for the engine holds the count (`queries`) and the source tables; the per-design-object `target_counts` in that row are not the question's figure, and an answer that cites only them fails. The session runs with the same CLI, settings, permissions and sandbox as the `/modernize` run, under the job budget (`run_before_deadline`), with a wall-clock timeout of its own (`E2E_LLM_EXPLORE_TIMEOUT`, default 600 seconds) and its own caps in place of the run's: `--max-turns` `E2E_LLM_EXPLORE_MAX_TURNS` (default 15) and `--max-budget-usd` `E2E_LLM_EXPLORE_MAX_BUDGET_USD` (default 2), each only when the CLI lists the flag. The script first runs the same entry itself (`scripts/graph_query.py run engine_destinations --param engine=dynamodb`, no model) into `explore-expected.json`. `ci/llm/run.py check-explore` then passes when the session ended without an error, no `graph_query.py` call was denied (a denial means the allowlist is broken), the session ran `graph_query.py run engine_destinations` on the job with `engine=dynamodb` (in any argument order the CLI's own parser accepts), and its final answer ties the engine's count (the row's `queries`) to it: one line (a table row), sentence or clause names that destination or its source table (whole names only, the schema optional), names no other row, and holds the count as a number on its own. The entry's ref is never read as a figure, and an engine in an `all`-rows check counts by its id or its AWS service name (`display_engine`). The verdict, with the answer, goes to `explore.json`, and `results.json` records it as `checks.explore` (required once the step ran) and an `explore` section; `total_cost_usd` is the run's `cost_usd` plus the session's. A dry run skips the step unless `E2E_LLM_EXPLORE_TRANSCRIPT` names a saved `/explore` session, and so does a run whose engine scope (`E2E_LLM_TARGET_DATABASES`) leaves out DynamoDB; `checks.explore` is then `null` and not required.

**Merge rule after a failed transcript check** (#1408; a process rule, not enforced): a pull request head that failed an `e2e-llm` transcript check (`transcript_error` in `results.json`) is not covered by a later batch run that passes on the same head, nor by any run on another head that includes it: that pass can be the model taking another path. It stays unmerged until a fix is pushed and an `e2e-llm` run on the fixed head passes the same check. A head that failed the same check twice and then passed once merged in #1358 and had to be reopened as #1401.

**Outputs**, all under `$E2E_OUTPUT/llm-<mode>-<fixture>/` (default
`test-results/llm-<mode>-<fixture>/`):

- `claude-version.txt`, `claude-flags.txt` -- the installed CLI's version and
  the exact flags this run chose (see "CLI flag feature-detection" below).
- `input/` -- the sample fixture, unzipped here rather than into the repo
  root.
- `transcript.jsonl` -- the full stream-json transcript.
- `claude-exit.txt`, `summary.json` -- the claude process's exit code/duration,
  and `ci/llm/run.py check-transcript`'s parsed summary (or `{"error": ...}`).
- `explore-transcript.jsonl`, `explore-expected.json`, `explore.json` -- the `/explore`
  session's stream-json transcript, `graph_query.py run`'s own result for the entry,
  and `check-explore`'s verdict (see "The `/explore` step" above).
- `model.json` -- `ci/llm/run.py check-model`'s `{"model", "expected", "match"}` (not in a dry run).
- `reports-junit.xml`, `ui-junit.xml` (ui/both only), `pw-reports/`, `pw-ui/` --
  same shapes as `e2e.sh`'s outputs, produced by the same pytest invocations.
- `judge.json` -- `ci/llm/judge.py`'s rubric scores. The judge grades the same prompt `JUDGE_SAMPLES` times (default 3), one after another, with a pinned model (`JUDGE_MODEL` overrides it), and `scores` are the per-criterion medians the pass rule is applied to. Measured on saved main jobs (2026-10-18, Bedrock): wordpress (119K-char prompt) $0.29 and 96 s for three samples, discourse (145K) $0.32 and 95 s; the first sample costs about $0.20, later ones about $0.05, each about 30 s. It also records `model` (the model that answered, read from the CLI record's `modelUsage`; every `judge.json` before #952 has `null` there), `model_requested`, `model_matches_request` (false when a managed or user CLI setting answered with another model; such scores are not comparable), `rubric_version` and `rubric_sha256`, `samples` (each sample's scores, model, cost and `duration_s`), `spread` (max - min per criterion), `samples_failed`, `notes` (from the sample closest to the medians, `notes_from_sample`) and `defects` (one entry per defect the samples named, with criterion, kind, where, quote and how many samples named it). With `E2E_LLM_JUDGE_BASELINE` set to a saved main `judge.json`, `baseline` holds the per-criterion deltas when both were graded under the same `rubric_version` (otherwise `compared: false` with a note, never a failure), and the judge step fails when one criterion's median drops by 2 or more, or two or more criteria drop by 1: compare criteria, not the mean, because the mean hides moves that cancel; a single one-point drop is within noise. The judge measures the deliverables, not routing: routing quality is the answer-key benchmark's gate (`benchmarks/README.md`). Rubric version 5 (#513) anchors five criteria on counts: each says what one defect is, what is not one, and what 5, 4, 3, 2 and 1 defects score, and the multi-engine and ownership rules are applied before scoring. Re-judging three saved wordpress deliverables 6 to 9 times each (Bedrock, Sonnet 5.5, $1.67 under version 4, $1.54 under version 5): the mean standard deviation of one sample's score per criterion fell from 0.28 to 0.06, and a median of three taken from those samples differed from the all-sample median in 17% of draws (146 of 868) under version 4 and 1% (4 of 420) under version 5. Scores rose (mean of medians 4.02 to 4.48) because a single defect now scores 4, where the old '3: at least one' anchor covered one to many; the pass thresholds are unchanged, and a headline defect (engine set, total, risk level, headline count, a table sequenced into an unselected engine) limits any criterion to 2. A version 5 baseline does not exist yet: the maintainers' ops run saves one from the first passing e2e-llm run on main after #1706 merges, and until then the baseline regression gate is off (`compared: false`).
- The judge's prompt budget (#2146): 250K characters (was 150K). Wordpress (163,469 whole) fits; discourse is cut (estimated +$0.07 per run), keeping the Migration map's multi-engine rows (199/276 lines). Over it the deliverables keep at least half the room, a 240-char floor per cut section, rubric sections last; `judge_input` fails the judge (exit 2, no model call) when a section would reach it empty or PDF extraction fails. <!-- claim: tests/unit/ci/test_judge_input_budget.py::test_every_section_of_a_discourse_sized_job_reaches_the_judge, tests/unit/ci/test_judge_input_budget.py::test_an_empty_engineering_report_fails_without_a_model_call, tests/unit/ci/test_judge_input_budget.py::test_a_pdf_whose_text_fails_to_extract_fails_the_judge_input, tests/unit/ci/test_judge_input_budget.py::test_a_cut_migration_map_keeps_the_multi_engine_rows_first -->
- `token_usage` in `results.json` (and `job-logs/token_usage.json`) -- `ci/llm/token_usage.py` sums the session's per-turn input-side usage (input, cache-read, cache-write tokens, calls; subagent turns included) per pipeline phase (`phases`) and per agent (`agents`), in the same key names as the Bedrock `token_usage` in `design_trace.json`. It runs after the `/explore` step, so that session is a row of the same table. Attribution (#745): a turn that runs `scripts/run_<phase>.py`, or calls a design-schema, review-schema, reality-check or synthesize skill, sets its agent's phase until the agent shows another; a subagent starts on the phase its dispatch names (the Agent call's prompt: its slash command, else the first phase script it mentions), else on the first phase it runs, so the turns in which it reads its command, skills and input count for that phase; the top-level session's own turns before any phase are `orchestrator`; every `/explore` turn is `explore`. Only a subagent turn with no matching dispatch and no phase of its own is `unattributed`, counted in `unattributed_calls`: 0 of 83 calls on the replay fixture (24% before), 0 of 93 on two recent wordpress runs (23% and 28% before) and 0 of 675 and 944 on two discourse runs (41% and 37% before). `agents` (#355) has one row per agent -- `orchestrator`, each subagent labelled by its dispatch's description in the order it first appears, then `explore` -- with its phases, calls, input, cache-read and cache-write tokens, `largest_context` (its largest single turn's input + cache read + cache write), `models` (the models its turns ran on, so a subagent definition's `model:` change shows) and `input_cost_usd`; `phases` and `total` carry `input_cost_usd` too, and both tables sum to `total`. Per-row `outputTokens` is `null`: the transcript's assistant records only carry the first-chunk output count, and neither the Agent tool's result nor `task_notification` holds an agent's output total, so `total.outputTokens` is the sessions' total from the final result records' `modelUsage`, and `session_model_usage` shows the session figures to compare against. Costs come in two figures, kept apart. The **list price** is from the repo's one model price table, `src/catalog/model_prices.toml` (public Bedrock list prices, dated there), read with `scripts/usage_cost.py`'s loader by Bedrock model id including its inference profile: a turn's bare model (`claude-sonnet-5-5`) is priced as the one profile id of that model the session's `modelUsage` names (`global.` or `us.`, which is 10% dearer). A 1-hour cache write, which the table has no row for, is priced at 2x input, the rows and `cost.models` alike. Every row's `input_cost_usd` is the list price of its input side; output is priced per session (`output_cost_usd`); `rows_input_cost_usd + output_cost_usd + unmatched_cost_usd = list_price_cost_usd`, where `list_price_cost_usd` prices each model's `modelUsage` totals and `unmatched_cost_usd` is what the turns do not show. The **CLI's figure** is `cli_cost_usd`: the sessions' own `total_cost_usd`, the run plus the `/explore` session. `results.json` keeps reporting it as `total_cost_usd`, and the soft budgets compare it, because every saved run, the budgets and the existing readers use it; `results.json` also gets `list_price_cost_usd`. The CLI estimates with its own prices, so the two differ: on the Sonnet 5.5 runs the CLI figure is about 12% higher ($3.58 against $3.20 on the replay fixture) because it charges cache reads at $0.20/M where the Bedrock list price is $0.10/M. `cli_minus_list_price_usd` and `cost.models` (`cli_cost_usd` and `list_price_cost_usd` per model) report that as information, not a warning. A model with no catalog price leaves its rows' cost `null`, counts in `unpriced_calls` and is named in `unpriced_models` (no guessed price); `tests/unit/ci/test_token_usage.py` fails when a model in a saved real transcript has none. `results.json` gets a `warnings` entry only when `cost.cli_cost_usd` and `total_cost_usd` (the same figure, read twice) differ by more than $0.01. When the transcript cannot be read or has no assistant turn, the file holds `{"source": "transcript", "error": ...}`, the step fails, and `results.json` has `token_usage: null` with the reason in `token_usage_error` (also when the file is missing or not JSON; `null` when the step ran fine or did not run). Lines that are not JSON are skipped and counted in `skipped_lines`. Grouped runs also carry a `token_usage` run total in the consolidated `design_trace.json`.
- `budget`, `warnings` in `results.json` -- the soft cost budget from `ci/llm/budgets.toml` (#355): the `<fixture>.<mode>` entry's `max_total_cost_usd` (mode `chat`, `ui` or `both`), else the fixture's own. `budget: {key, field, limit_usd, value_usd, exceeded}` names the entry used and the field compared (`total_cost_usd`, the CLI's figure); it is `null` for a fixture with no budget, and over the limit adds one `warnings` entry. A warning never fails the run (`pass` ignores it). The budgets are set just above the measured both-mode runs on Claude Sonnet 5.5: wordpress $4.50 (24 runs cost $3.58-3.91), discourse $50 (about $41, about $60 with a DynamoDB merge fix round). Chat and ui runs use the fixture's figure until they are measured; a scoped run is held to the same figure.
- `job.tar.gz` -- the job's replay artifacts (`<db>/<job>/...`), written by the EXIT trap with `ci/llm/run.py collect-job` (#1463) so `ci/llm/replay.py save` can keep them. Allowlisted: `collector/output.json`, `referee-triage/triage.json`, `analysis-*/analysis.json`, `assignment/v*/assignment.json` and `access_patterns.json`, `reality-check/output.json` (and `reality-check/v*/`), `schema-*/v*/schema_output.json` and `synthesis/v*/report.json` (a file over 30 MB goes under `too_large`). The same step gives those files and every other uploaded text file (transcripts, logs, deliverables, `results.json`) every check of `ci/llm/private_scan.py`, which allows only the example values `src/shared/example_values.py` defines (#2053), and fails closed. A value on one line becomes `[REDACTED:<kind>]`. Withheld instead: a file with a private key, key material or a cloud credential (then the archive too), one it cannot clean, one that is not UTF-8 and, on a run with any finding, the binary uploads (PDF, `trace.zip`, screenshots). Any finding writes `job.findings.json` (file, line, kind, the value's field, JSON path and shape, not the value), sets `pass: false` and fails the job; a scan error withholds every upload. `save` keeps a flagged run only with `--allow-redacted`. <!-- claim: tests/unit/ci/test_llm_run.py::test_collect_job_redacts_a_secret_and_keeps_the_archive, tests/unit/ci/test_llm_run.py::test_a_key_split_across_two_files_is_withheld_whole, tests/unit/ci/test_llm_run.py::test_a_file_that_is_not_utf8_is_withheld, tests/unit/ci/test_llm_run.py::test_collect_job_redacts_the_transcripts_and_every_other_uploaded_text_file, tests/unit/ci/test_llm_run.py::test_a_scan_crash_withholds_every_upload_and_fails_the_run, tests/unit/ci/test_replay_redacted.py::test_save_refuses_a_redacted_archive, tests/unit/ci/test_llm_run.py::test_no_value_piece_reaches_the_manifest_or_stdout --> The scan cannot recognise a customer's own hostnames or an unusual key, so the allowlist is the first line of defence, the scan the second, and `save`'s checks and `tests/unit/ci/test_private_scan.py` a third. Its JSON (counts only) goes to `job-logs/collect-job.json` and the console. A WordPress job is about 0.3 MB.
- `job-logs/` -- the run's own logs, copied by the EXIT trap (`ci/llm/run.py
  collect-logs`) because only this directory is uploaded: `run_assessment.log`
  from `artifacts/<db>/<job>/_logs/` and the local UI's `local-ui-api.log` /
  `local-ui-serve.log` (ui/both), each capped at its last 1 MB.
- `steps.json` -- exit code of every step (`build-ui` (ui/both, run *before*
  the claude call so `scripts/start_local_ui.py` finds `src/ui/build/` and
  never builds inside a Bash tool call), `claude`, `check-model` (not in a dry run), `check-transcript`,
  `render`, `judge`, `explore-expected`, `explore`, `check-explore` (the three skipped
  in a dry run without `E2E_LLM_EXPLORE_TRANSCRIPT` and in a scope without DynamoDB),
  `install-browsers`, `report-tests`, `ui-tests`, `token-usage`, `results`); a step failing never stops the script early. The judge runs before
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
  contracts, engineering, pdf, ui, judge, judge_input, explore}` (`ui` is `null` for `chat`,
  `explore` when the step did not run, `judge_input` when `judge.json` has none; schema
  version 2 renamed the `html` check to `engineering` when the HTML reports
  were retired, #987),
  `transcript_error` (`null` unless check-transcript failed; cost, turns and
  tokens are reported either way), `judge: {mean, scores}`, `judge_input`, `explore: {entry,
  ran_entry, missing_figures, cost_usd, num_turns, error}` (`null` when the step did
  not run; its cost is not in the run's `cost_usd`), `total_cost_usd` (the run's
  `cost_usd` plus the explore session's), `token_usage` and `token_usage_error`,
  `list_price_cost_usd`, `budget` and `warnings` (see above), `pass`, and `fail_reasons`
  when it is false, one line per reason, #2175). Plan 3b uploads it to the results store, so it is flat JSON.

**Env vars**: `CLAUDE_BIN` (default `claude`), `CLAUDE_CODE_VERSION` (npm
version to install when `$CLAUDE_BIN` is missing, default pinned to
`2.1.288`, the published version when this was written -- bump it
deliberately), `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY` *or*
`CLAUDE_CODE_USE_BEDROCK` + `AWS_REGION`, `E2E_LLM_TIMEOUT` (seconds, default
3600), `E2E_LLM_MAX_TURNS` (default 200) and `E2E_LLM_MAX_BUDGET_USD`
(default 50: a discourse run costs about $18-20, plus $3-5 when the DynamoDB merge needs a fix round; scoped to `dynamodb,aurora_postgresql` about $56), each only passed if the CLI's own `--help` mentions the flag, `E2E_LLM_TARGET_DATABASES` (a scoped run, see above),
`E2E_LLM_JOB_BUDGET` (seconds, default 4800, see `steps.json` above),
`E2E_LLM_ARTIFACT_ROOT` (dry-run only, default `$REPO_ROOT/artifacts`, see below),
`E2E_LLM_EXPLORE_TIMEOUT` (seconds, default 600), `E2E_LLM_EXPLORE_MAX_TURNS` (default 15),
`E2E_LLM_EXPLORE_MAX_BUDGET_USD` (default 2) and `E2E_LLM_EXPLORE_TRANSCRIPT`
(dry-run only, a saved `/explore` session; unset, the explore step is skipped),
`E2E_OUTPUT` (as in `e2e.sh`).

**`E2E_LLM_ARTIFACT_ROOT`** is honoured only with `E2E_LLM_DRY_RUN=1`. A
real run always uses `$REPO_ROOT/artifacts` and exits 2 if the variable names
anything else: the headless run writes `./artifacts`, the allowlist permits
writes only under `artifacts/**` and the sandbox (below) refuses roots outside
the repo, so another root could only point the checks at another job.

**Sandbox**: the claude process and its tool calls (only those) run with
`MODERNIZER_CI_SANDBOX=1`. Every
allowlisted script then refuses `--file`/`--artifact-root`/`--assumptions`
values outside the repo and `--db`/`--job-id` values that aren't a single
`[A-Za-z0-9_.-]+` path component (`scripts/_sandbox.py`), exiting with its
normal JSON error. This covers what permission rules can't: a `Read` deny
rule doesn't stop an allowlisted Python script from opening a path itself.
The claude call's stdin is `/dev/null`, so it never reads the runner's stdin.

**CLI flag feature-detection**: CLI builds differ (an internal v2.1.288 build does not list
`--max-turns`; the public `@anthropic-ai/claude-code` package CI installs does, and needs
`--verbose` with `--output-format stream-json`), so the script reads `"$CLAUDE_BIN" --help`
once and adds `--max-turns`, `--max-budget-usd`, `--setting-sources project` (only the
project's settings files, plus `--settings`), `--strict-mcp-config` (ignore MCP servers
outside `--mcp-config`), `--verbose` and `--include-hook-events` (#747) only
when it lists them, logs the chosen flags and `claude --version` to the output directory,
and warns when `--max-turns` is missing. `ci/llm/judge.py` detects
`--setting-sources`/`--strict-mcp-config` the same way.

**Dry-run mode** (`E2E_LLM_DRY_RUN=1`, plus `E2E_LLM_TRANSCRIPT=<path>`):
skips the claude call entirely and copies the given transcript `.jsonl` in
its place, so the rest of the script -- `check-transcript`, the deliverable
checks, the judge (still a real subprocess call, typically pointed at a stub
via `CLAUDE_BIN`), and `results.json` -- can be exercised locally with no
model access. With `E2E_LLM_EXPLORE_TRANSCRIPT=<path>` the `/explore` step checks
that saved session the same way; without it the step is skipped. The transcript's `MODERNIZE_RESULT` line must name a job/db
that actually exists under `E2E_LLM_ARTIFACT_ROOT` and must match the
`<mode> <fixture>` arguments given on the command line.

**Cost and time**: the first internal-pipeline run (chat/wordpress) cost
$4.06 and ran 11 `result` messages (one per background subagent plus the
orchestrator) across 493 transcript lines; see `ci/llm/run.py`'s module
docstring for the confirmed stream-json shapes and
`tests/unit/ci/fixtures/real-chat-wordpress-failed.jsonl` for a trimmed copy
of that transcript.

Since #2081 a run reviews each Aurora and ElastiCache design too: two or three
more review calls per run, not measured yet.

**Denials**: `check-transcript` splits each denied Bash command on pipes, `&&`, `||`, `;` and subshells. A denial of one of the pipeline's own allowlisted commands on its own means the allowlist is broken and fails the run. A compound command that contains an allowlisted part but was denied for the rest is a tool-use rule violation by the model: it also fails the run, because the commands forbid chaining and post-processing, but it is reported as `rule_violations` (count and commands) in `summary.json` and `results.json` and the error names the violation, not the allowlist. A denied allowlisted command the shell would expand (`$name`) warns (`expansion_denials`), as does any other (`denials`, the model exploring). <!-- claim: tests/unit/ci/test_llm_run.py::test_shell_expansion_denial_warns_and_does_not_fail --> Every entry is named `<Tool>: <target>` (Bash: the command, Skill: the skill name, Read/Write/Edit: the path, WebFetch: the URL, other tools: the tool name and its compact input). A denied non-Bash call that matches an allow rule and no deny rule counts as a broken allowlist; one outside the allowlist (for example a `Skill` that is not listed) only warns and is recorded as `unlisted_tool_denials`, so you can tell an allowlist gap from a model misstep. File paths are matched the way Claude Code does: tool inputs carry absolute paths, relative rules such as `Write(artifacts/**)` resolve against the session's cwd (from the transcript's `init` record), `..` is normalised first, and a path a deny rule blocks (for example `artifacts/.local-ui/`) is never counted as allowlisted.

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

### `ci/llm/compare_costs.py`

Compares e2e-llm runs (#763): the tool for measuring a cost lever against a baseline. It reads saved `results.json` files (or the run folders holding them, optionally named `LABEL=PATH`) and prints a Markdown table with one column per run: the CLI's total (`total_cost_usd`) and the list-price cost, run, `/explore`, output and unmatched cost, wall time, judge mean, turns, calls, the token totals, the largest single context and unattributed calls, then a block per phase (`--by phase`, the default), per agent group (`--by agent`) or both: list-price input-side cost, cache writes, cache reads and calls.

An agent group is the orchestrator, the `/explore` session, or the subagents whose description matches once "part N of M" is dropped (the DynamoDB group drafts are one group, with the number of agents); use it for levers on the orchestrator, whose later turns count toward the phase it ran last. With `--baseline` (repeatable) the baseline runs come first, followed by a `baseline mean` column, the other runs, a `candidate mean` column and the `change` of the candidate mean against the baseline mean. A mean over fewer runs than its group has says so (`$3.70 (1/2)`), and a field an older `results.json` lacks shows `-`.

Runs of different fixtures or modes are refused (exit 2) unless `--allow-mixed` is given, which prints a `WARNING:` line above the table. `--json` prints the same data as JSON. Read-only, no model call.

```bash
uv run python ci/llm/compare_costs.py --baseline base1/llm-both-wordpress --baseline base2/llm-both-wordpress \
    lever1/llm-both-wordpress lever2/llm-both-wordpress
uv run python ci/llm/compare_costs.py --by agent ttl-1h=runs/a/results.json runs/b/results.json --json
```

The judge mean moves by up to a point between runs of the same commit (3.29 to 4.57 over 24 wordpress runs), so read a lever's quality from the deterministic checks and two or more runs, never one judge score; cost is steadier (those runs cost $3.58-3.91).

## Release notes: `../scripts/release_notes.py` (#1184)

Not a CI gate: the release workflow (`.github/workflows/release.yml`) runs it when a `v*` tag is pushed, and anyone can run it to preview a release. `uv run python scripts/release_notes.py --since <previous tag> [--until <ref>] [--output <file>] [--body [--max-rows N]]` reads committed files at the two refs with `git show` (no pipeline run, no network) and writes the routing-snapshot diff, one changelog line per rule and contract change, and the catalog values that moved. `--body` writes the release body's capped copy, and `--previous-tag <tag>` prints the tag a release is compared with. The sections, the caps and the workflow's use of it: the script's docstring. Tests: `tests/unit/scripts/test_release_notes.py`.

## `../scripts/hooks/` (Claude Code hooks, #731)

Ten small scripts that refuse a guardrail bypass at the tool call. Each reads the hook event JSON on stdin and exits 0 (allow) or 2 (block, one-line reason on stderr shown to the model); an unreadable event allows. They run as `uv run --no-project python -I "$CLAUDE_PROJECT_DIR/scripts/hooks/<name>.py"`, outside the project environment, and use the standard library only, except that `block_contract_version_edit.py` imports `scripts/resolve_contract_versions.py` (itself stdlib-only) when an edit changes a version line. What each catches, and what it can't: its docstring. Unit tests: `tests/unit/hooks/`.

| Script | Event / matcher | Where | Rule |
|---|---|---|---|
| `block_commit_bypass.py` | PreToolUse `Bash` | both | Blocks `git commit --no-verify` (or `-n`), the pre-commit skip variable and `git -c core.hooksPath=... commit`. |
| `block_credential_output.py` | PreToolUse `Bash` | both | Blocks a call that prints a cloud credential helper's output, which would stay in the session log (#1165); capturing, piping or redirecting it passes. |
| `block_fork_pr_push.py` | PreToolUse `Bash` | both | In a worktree checked out for a contributor's fork PR, refuses a `git push` to anything but the fork (#1165). |
| `block_baseline_edit.py` | PreToolUse `Edit\|Write\|MultiEdit` | both | Blocks hand edits of generated baselines and snapshots, naming each one's regenerating command. |
| `block_contract_version_edit.py` | PreToolUse `Edit\|Write\|MultiEdit` | both | Blocks a hand edit of a contract's `contract_version` default or Version History, naming `scripts/bump_contract.py` (#1408). |
| `remind_llm_run.py` | Stop | `settings.json` only | Says when the branch's changes need a real-model run (`ci/needs_llm_run.py` says `required`). Never blocks. |
| `enforce_tool_use_rule.py` | PreToolUse `Bash` | `settings.ci.json` only | Refuses a Bash call that is not exactly one simple command covered by a `Bash(...)` allow rule in `settings.ci.json` (the shared tool-use rule). |
| `require_gate_shown.py` | PreToolUse `Bash\|Agent\|Task` | both | Holds schema design in a `/modernize` run until the approval gate ran on its own and its block is in the orchestrator's own chat text (#1401, #1626). |
| `remind_quote_printed_summary.py` | PostToolUse `Bash` | both | After `print_summary.py --gate` or `--final`, tells the model to paste the printed block verbatim (#1605). Never blocks. |
| `require_modernize_result.py` | Stop | `settings.ci.json` only | `/modernize --auto`/`-y` only (`/wave-edit`: text only): refuses a stop with no `MODERNIZE_RESULT` line and nothing (#1574) or a subagent (#2175) running. |
| `record_dispatch.py`, `record_report.py` | PostToolUse `Agent\|Task\|SendMessage`, SubagentStop | both | Record dispatches and reports in the job's ledger (#2215). |

"both" means `settings.json` and `settings.ci.json`. Headless runs load both files (`--setting-sources project` plus `--settings`), and a command listed in both runs once. `hook_common.split_compound_command` is a tested copy of `ci/llm/run.py`'s splitter, which a hook cannot import. The headless run also denies `Bash(git push *)`, so `block_fork_pr_push.py` matters in interactive sessions.

A call a hook refuses is shown to the model as `PreToolUse:<Tool> hook error: Blocked ...` and also lands in `permission_denials`; `check-transcript` reports it as `prevented_by_hook` and warns, never fails, because the call was refused before it ran.

The hooks fail open by design: a `PreToolUse` hook that ends in a non-blocking error (a crash, a timeout, `uv` missing: any exit but 0 and 2) leaves enforcement off for that call. `check-transcript` reports it as `hook_errors` and warns; it fails the run only when the record names `require_gate_shown.py`, whose absence changes the run order. Still to confirm on an `e2e-llm` run: what `hook_name` holds in a real stream, and that a timed-out PreToolUse hook arrives as `cancelled` (#747). Changing `settings.ci.json` or the hooks needs an `e2e-llm` run.

## `../.claude/settings.ci.json`

The permission allowlist for headless `claude -p "/modernize ..." --permission-mode
dontAsk` runs. JSON has no comment syntax, so the rationale for its less
obvious entries lives here instead:

- `Bash(uv run python scripts/start_local_ui.py)` and `...py *)` (both forms):
  the only command `/modernize --mode ui|both` runs to start the local API +
  UI (its module docstring says why). Both forms are listed so a run never
  relies on whether the script is called with no arguments. The `*` form
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
- `Bash(uv run python scripts/graph_query.py *)` (#307): the only command `/explore`
  runs (`list`, `params <id>`, `run <id> --param k=v --job-id <id>`). It takes catalog
  entry ids and parameter values, never Cypher: each entry is a fixed, read-only,
  bounded statement, the values are bound as parameters, and `run` refuses a value
  that is not one of its parameter's choices on the job's graph. Under
  `MODERNIZER_CI_SANDBOX=1` the artifact root must be the pipeline's own,
  `<repo>/artifacts` (or `$ARTIFACT_DIR` when set; the model cannot set it, since an
  allow rule matches only a command that starts with the script's name): any other
  folder, inside the repo or not, is refused before
  the graph is opened, with or without `--clean`, as is a `--db`/`--job-id` that is not
  a plain name (`scripts/_sandbox.py` `pipeline_artifact_root_violation`, tested in
  `tests/graph/test_graph_query_cli.py`). It prints one JSON line.
- `Bash(uv run python scripts/run_transformation_contract.py *)`, `...run_data_movement_plan.py *)`,
  `...render_dms_step.py *)`, `...run_dms_step.py *)` (#1886): `/move`; the sandbox refuses
  AWS writes (`tests/unit/scripts/test_run_dms_step.py`).
- `TaskStop` (#2175): stops a subagent the Stop hook names as hung (30 minutes, no
  report) before the `failed` line, so the session ends at once.
- `Bash(rm *)`, `Bash(sudo *)`, `Bash(wget *)`, `Bash(nc *)` in `deny`: nothing
  needs them. Explicit denies are defense in depth against an injected or
  hallucinated command: a deny always wins, so a future broad allow can't
  reopen them.
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

### The schema design scripts in `settings.ci.json`

- `Bash(uv run python scripts/run_pe_review.py *)` and
  `Bash(uv run python scripts/run_post_schema_routing.py *)` (#1059): the
  headless schema-design PE review and post-schema reroute, the steps Bedrock
  mode runs inside schema design. `run_pe_review.py` writes one review request
  per design (per group for DynamoDB) under `artifacts/` and, with
  `--finalize`, records the review subagent's answer in the design trace;
  `run_post_schema_routing.py` runs the shared router one pass per call
  (`--rollback` after a failed redesign). Both only read and write the job's
  own artifacts and print one JSON line.
- `Bash(uv run python scripts/run_wave_redesign.py *)` (#1992): Phase 7b. It
  moves the job's design files (for DynamoDB in parts, only the parts the round
  redesigns, #2119) to `wave-redesign/` and back (`--rollback`, and `--stop`,
  which closes a round the run did not run, #2122), writes the round's checks
  and log, and prints one JSON line.
- `Bash(uv run python scripts/run_wave_edit.py *)` (#2067): `/wave-edit`. It
  shows one wave, records the customer's decision on its review warnings under
  `decisions/wave-edit/`, moves that wave's design files to `wave-redesign/` the
  same way, and prints one JSON line. It has no `--rollback`: a failed round
  is rolled back with `run_wave_redesign.py --rollback`.
- `Bash(uv run python scripts/run_schema_design.py *)` also covers
  `--mark-dispatched <id> ...` and `--mark-reported <id> ...` (#2215): the
  headless orchestrator's dispatch ledger. They write one file,
  `<job>/_logs/dispatch_ledger.json` (atomically), take only subagent ids
  (letters, digits, `_.:-`) and print one JSON line; `--wait-for-report` reads
  that ledger and returns `nothing_pending` only when it lists no outstanding
  subagent. No new allow rule is needed.
