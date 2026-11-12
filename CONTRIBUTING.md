# Contributing to Database Modernizer Assessment

Thank you for your interest in contributing to Database Modernizer Assessment! This document provides guidelines for contributing to the project.

Coding agents and power users: [AGENTS.md](AGENTS.md) is the short working guide (repo map, test tiers, headless rules).

## Table of Contents

1. [Code of Conduct](#code-of-conduct)
2. [Reporting Bugs/Feature Requests](#reporting-bugsfeature-requests)
3. [Getting Started](#getting-started)
4. [Development Workflow](#development-workflow)
5. [Branching Strategy](#branching-strategy)
6. [Commit Message Format](#commit-message-format)
7. [Pull Request Process](#pull-request-process)
8. [Code Standards](#code-standards)
9. [Testing Requirements](#testing-requirements)
10. [Documentation](#documentation)
11. [Security](#security)
12. [Licensing](#licensing)

---

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
<opensource-codeofconduct@amazon.com> with any additional questions or comments.

---

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already reported the issue. Please try to include as much information as you can:

- A reproducible test case or series of steps
- The version of our code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

---

## Getting Started

### Prerequisites

- Python 3.12+
- Git
- uv (Python package manager)
- Node.js 22 (for the local UI and UI development)
- AWS CLI configured with credentials (for Bedrock, live-mode collection, or the AWS Transform integration)

### Quick Setup

```bash
# Clone repository
git clone https://github.com/aws-samples/sample-aws-genai-db-modernizer.git
cd sample-aws-genai-db-modernizer

# Run setup script (installs deps, pre-commit hooks, validates environment)
./scripts/setup_dev.sh

# Verify installation
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json
```

The setup script will:

1. Check Python 3.12+ is installed
2. Install uv if not present
3. Create virtual environment and install all dependencies
4. Install pre-commit hooks (formatting, linting, type checking, security scanning)
5. Run initial validation

### Pre-commit Hooks

Pre-commit hooks run automatically on every commit to ensure code quality:

- **Black** - format Python code
- **isort** - sort imports
- **Ruff** - lint Python code
- **mypy** - type checking
- **Security scan** - `ci/security.sh` on the staged files: semgrep with the rules `ci/security.toml` pins, gitleaks and Bandit (the same script the CI `security` job and `./ci/pre_merge.sh` run) <!-- claim: tests/unit/ci/test_security_parity.py::test_the_precommit_security_hook_runs_the_script_on_every_staged_file, tests/unit/ci/test_security_parity.py::test_the_github_security_job_runs_the_script_on_the_whole_tree, tests/unit/ci/test_security_parity.py::test_pre_merge_runs_the_script_quick_on_the_branch_and_full_otherwise -->
- **detect-secrets** - secrets, against `.secrets.baseline` <!-- claim: tests/unit/ci/test_security_parity.py::test_detect_secrets_stays_a_separate_precommit_hook -->
- **markdownlint** - lint Markdown files
- **Conventional Commits** - validate commit message format
- **License headers** - every new source file carries the SPDX license header (see [Licensing](#licensing))
- General checks: YAML/JSON syntax, large files, private keys, merge conflicts

If hooks don't run after cloning, install manually:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

If `core.hooksPath` is set (some tooling sets a global one whose hooks chain to the clone's own), pre-commit refuses to install. Blank it for that one command, which writes the hooks to the clone's hooks folder and leaves your config unchanged. `./scripts/setup_dev.sh` does this for you, and never removes a system or global hooks path, so tooling that relies on one keeps working:

```bash
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0= uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

`./ci/pre_merge.sh` (also with `--quick`) fails when the hooks a commit runs are not the repository's (#1843): the clone's `pre-commit` and `commit-msg` hooks must be pre-commit's, or, with `core.hooksPath` set, that folder must have a hook of each type and the clone's pre-commit hooks must be installed for it to chain to. It also fails when `core.hooksPath` is set to an empty value, under which git runs no hooks at all, and warns instead of passing when git is older than 2.26 and cannot report the hooks path's scope. It cannot tell whether a hooks path's hook really chains.

> **Note:** If your PR shows failing CI scans (lint, type check, security), it most likely means you haven't installed the pre-commit hooks locally. Install them with the commands above (or run `./scripts/setup_dev.sh`), fix any issues, and re-push your changes.

---

## Development Workflow

### 1. Create an Issue

Before starting work, create an issue describing:

- What you want to build/fix
- Why it's needed
- Proposed approach (for larger changes)

Ask on the issue to be assigned before you open a pull request. The
`issue-assignment-gate` check (see [Issue Assignment Gate](#issue-assignment-gate))
fails any pull request that doesn't say `Closes #N` for an issue assigned to
its author, so getting assigned first saves a round trip.

### 2. Fork and Branch

```bash
# Fork the repo on GitHub, then clone your fork
git clone https://github.com/YOUR_USERNAME/sample-aws-genai-db-modernizer.git
cd sample-aws-genai-db-modernizer

# Add upstream remote
git remote add upstream https://github.com/aws-samples/sample-aws-genai-db-modernizer.git

# Create feature branch
git checkout -b feat/your-feature-name
```

### 3. Make Changes

- Write code following our [Code Standards](#code-standards)
- Add tests for new functionality
- Update documentation as needed
- Run tests locally before committing

### 4. Commit Changes

Follow our [Commit Message Format](#commit-message-format). Set up the commit template to guide your messages:

```bash
git config commit.template .gitmessage
```

Then commit as usual:

```bash
git add .
git commit -m "feat(collector): add CloudWatch metrics collection"
```

### 5. Push and Create Pull Request

```bash
git push origin feat/your-feature-name
```

Then open a Pull Request on GitHub against `main`.

---

## Branching Strategy

We use a **trunk-based** strategy with short-lived feature branches:

### Branch Types

| Branch Type | Naming Convention | Purpose | Base Branch |
|---|---|---|---|
| **main** | `main` | Production-ready code | - |
| **feature** | `feat/<description>` | New features | main |
| **fix** | `fix/<description>` | Bug fixes | main |
| **hotfix** | `hotfix/<description>` | Urgent production fixes | main |
| **docs** | `docs/<description>` | Documentation updates | main |

---

## Commit Message Format

We follow [**Conventional Commits**](https://www.conventionalcommits.org/en/v1.0.0/#summary) specification:

### Format

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

### Types

| Type | Description | Example |
|------|-------------|---------|
| **feat** | New feature | `feat(collector): add MySQL collector agent` |
| **fix** | Bug fix | `fix(api): handle connection timeout errors` |
| **docs** | Documentation only | `docs(readme): update installation instructions` |
| **refactor** | Code refactoring | `refactor(agent): simplify error handling` |
| **perf** | Performance improvements | `perf(collector): optimize schema collection` |
| **test** | Adding or updating tests | `test(collector): add contract validation tests` |
| **chore** | Maintenance tasks | `chore(deps): update boto3 to 1.29.7` |
| **ci** | CI/CD changes | `ci(github): add contract validation workflow` |

### Scopes

Common scopes in this project:

- `collector` - Collector agents
- `analysis` - Analysis agents
- `referee` - Referee agent
- `schema-design` - Schema design agents
- `api` - API server
- `ui` - Frontend/UI
- `contracts` - Contract models (Pydantic)
- `infra` - Infrastructure/deployment

### Example

A message that passes the check (copy it and change the words):

```text
fix(api): handle timeout errors in job status

The job status endpoint returned a 500 when the store timed out.
Catch the timeout and return a 503 with a retry hint.

Closes #123
```

A follow-up commit that leaves the issue open ends with `Refs #123` instead of `Closes #123`. When the check fails, it prints the failed rules and this same example.

### Rules

1. Use imperative mood: "add" not "added" or "adds"
2. Don't end subject with period
3. Aim for a 50-character subject line; 72 characters is the hard limit the hook below enforces.
4. Separate subject from body with blank line
5. Keep the body to 2-4 non-blank lines: a line or two on the problem, a line or two on the fix. The full explanation, review notes and test results go in the issue and the PR, not the commit.
6. Reference exactly one issue at the end of the body: "Closes #123" when the commit finishes the issue, "Refs #123" for a follow-up. One commit per issue (see the Conventions in AGENTS.md), so "Closes #1, #2" (or any other multi-issue trailer) is rejected -- split multi-issue work into separate commits instead. Other git trailers (e.g. "Signed-off-by:") are recognized as trailers, not counted toward the 2-4 body lines in rule 5.
7. A `check-commit-message` pre-commit hook (`commit-msg` stage) and `ci/check_commits.sh` enforce rules 3, 5 and 6 (subject at most 72 characters, 2-4 non-blank body lines, a trailing `Closes #N`/`Refs #N` trailer) plus no `Co-authored-by` trailer. Both ignore everything from a `git commit -v` scissors line down (the diff it appends for reference is never part of the message). Merge (including in a linked worktree), fixup/squash/amend, and a default `git revert` message (`Revert "..."` plus only `This reverts commit <sha>.` lines -- a hand-edited revert message is checked normally) are skipped. <!-- claim: tests/unit/scripts/test_check_commit_message.py::test_merge_message_in_a_worktree_merge_is_skipped, tests/unit/scripts/test_conventional_commit_hook.py::test_merge_in_progress_in_a_worktree_skips_the_conventional_check -->
8. `ci/check_commits.sh` also checks the author of every commit your pull request adds: a name that is not a placeholder (such as `x`, `root` or `Your Name`; a name of one ASCII character is refused, while one non-ASCII letter, such as a one-character CJK name, is fine) and a real email address (not `you@example.com` or a `.local` host). Set `git config user.name` and `user.email` before your first commit; see ci/README.md, `check_commits.sh`.

---

## Pull Request Process

### Before Creating a PR

1. **Update your branch**

   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Run all tests**

   ```bash
   ./ci/pre_merge.sh --quick   # fetch, conflicts, contract versions, lint
   uv run pytest <paths> -n 4  # targeted tests for what you touched
   make e2e         # deterministic end-to-end (needs the `e2e` extra, Node 22,
                     # Playwright browsers, and a UI build -- ci/e2e.sh handles
                     # all of that); or: ./ci/e2e.sh
   make e2e-llm     # headless /modernize run against a real model -- needs
                     # model access (CLAUDE_CODE_USE_BEDROCK+AWS_REGION or
                     # ANTHROPIC_API_KEY); see ci/README.md. Needed before
                     # merging only when ./ci/needs_llm_run.sh prints
                     # "e2e-llm: required"; "nightly" needs no run from you.
   ```

   While iterating, run targeted tests with at most 4 workers. Before asking
   for review, run the full unit, contract and property suites (`make test`, or
   `./ci/test.sh -q`, which `./ci/pre_merge.sh` also runs) and make them green;
   they are not CI-only. Only the e2e and real-model tiers are left to CI unless
   your change needs them. The suites include the `atx_orchestrator` tests, which
   need the AWS Transform SDK: run
   `uv pip install -r src/atx_orchestrator/requirements-sdk.txt` (see
   `tests/unit/atx_orchestrator/conftest.py`; first-time `./scripts/setup_dev.sh`
   does it too). `uv sync` removes it, so repeat the install after any `uv sync`.
   Without it `ci/test.sh` stops before pytest with a one-line message.

3. **Verify linters pass** (pre-commit runs these automatically on commit, but you can run manually):

   ```bash
   make lint        # or: uv run pre-commit run --all-files
   ```

4. **Update documentation** if needed

5. **Fill in "Maintainer decision this implements"** in the PR template: link the issue comment where the decision was recorded, or write "None"
6. **Self-review** against the [Before asking for review](AGENTS.md#before-asking-for-review) checklist in AGENTS.md, and run `./ci/pre_merge.sh`

### PR Title Format

Use the same format as commit messages:

```
feat(collector): add MySQL collector agent with RDS support
fix(api): handle timeout errors in job status endpoint
```

### PR Size Guidelines

- **Small PR**: < 200 lines changed (preferred)
- **Medium PR**: 200-500 lines changed
- **Large PR**: > 500 lines changed (consider splitting)

Smaller PRs are reviewed faster and merged more easily.

### Labels

- **`good first issue`**: small, standalone fixes with a suggested fix, good for a first contribution. They are never part of an epic or a feature, never change a contract, and nothing else waits on them. Before opening the PR, run `make lint` (or `./ci/lint.sh`) and the full `./ci/test.sh -q`: a PR can't merge until the suites pass on CI.
- **`help wanted`**: well-scoped issues that need more context in the codebase; contributions welcome.
- **`Maintainer Decision`**: the issue or PR is waiting for a maintainer to decide. While a PR carries this label, the `maintainer-decision-gate` check fails and the PR can't be merged. A maintainer removes the label once the decision is made, and the check passes on the next run. A PR opened before this check existed has no result for it until it gets a new push, a label change, or is closed and reopened.
- **`contract-major`** and **`contract-minor`**: a maintainer approved moving the `assess()` contract MAJOR or MINOR on this PR. The `contract-major-gate` check fails an `assess()` version move unless the PR carries the matching label, applied by a user with the maintain or admin role after the PR's latest push, when the check runs; a PATCH needs neither, and internal contracts (`src/contracts/`) need no label. A new push resets the approval: a maintainer re-applies the label. `Contract Major Approved`, the old name of `contract-major`, still counts. Agents may apply them, and never remove them without the maintainer's explicit approval. See [The public `assess()` contract](#the-public-assess-contract). <!-- claim: tests/unit/ci/test_contract_major_gate.py::test_an_assess_minor_move_needs_the_minor_label, tests/unit/ci/test_contract_major_gate.py::test_a_label_a_non_maintainer_applied_fails, tests/unit/ci/test_contract_major_gate.py::test_the_legacy_label_still_approves_an_assess_major, tests/unit/ci/test_contract_major_gate.py::test_a_patch_passes_without_asking_github, tests/unit/ci/test_contract_major_gate.py::test_an_internal_contract_bump_needs_no_label -->
- **`scope: internal`**: exempts a pull request from the `issue-assignment-gate` check below (maintenance/internal work that isn't tied to a contributor-facing issue).

### Issue Assignment Gate

Every pull request must say `Closes #N` (or `Fixes`/`Resolves #N`, case-insensitive) for at least one issue, and that issue must be assigned to the pull request's author -- the `issue-assignment-gate` check fails otherwise, with a message explaining what's missing. Get the issue assigned to you before opening the PR (see [Create an Issue](#1-create-an-issue)). Issues linked under the pull request's "Development" sidebar section count the same as one written in the description -- both come from the same GitHub data -- but linking one there doesn't by itself re-run the check either: like getting assigned after the fact, it still needs an edit to the description or a new commit before the check sees it. A closed issue counts too, as long as it's still assigned to you. If the check fails, assigning the issue to yourself does not by itself make the check pass: edit the pull request description or push a commit once you're assigned (either re-runs the check), or ask a maintainer to re-run it -- re-running a check directly needs write access, and for outside contributors every workflow run (not just the first) already waits for a maintainer's approval before it starts. Maintainers (admin or write access -- including the built-in maintain role, or a custom role built on write access), Dependabot, and pull requests labelled `scope: internal` are exempt.

Changes to `.github/workflows/issue-assignment-gate.yml` itself (or any other workflow) are requested for maintainer review by `.github/CODEOWNERS` -- the branch ruleset doesn't require that review, which is a separate maintainer decision. Checking out the base commit before running the gate's script stops a pull request from changing *what the script does*, but GitHub still reads the *workflow file* from the pull request's own merge ref; what actually keeps a pull request from changing what the workflow itself is allowed to do is that outside contributors' workflow runs always wait for a maintainer's approval, on every run and not just the first, so a workflow edit is seen before it ever runs -- plus the one approving review merging already needs regardless.

### Reviewing a PR

Reviewers using Claude Code can run the same checklist by hand -- routing bias and drift, deliverables agreeing, customer wording and escaping, contract bumps, whether new tests fail on main, evidence for behavior claims, public wording -- with the `pr-reviewer` subagent (`.claude/agents/pr-reviewer.md`): ask Claude Code to review the PR with the `pr-reviewer` agent, and it checks out the PR in its own disposable worktree, runs the hooks and guard tests, and posts one ranked comment.

When a review round proposes replacing patterns with an allowlist or a parser, the next round does that rather than adding more patterns. Each added pattern answers one hostile input and the next round finds another; the allowlist or parser answers the whole class. This applies to people and to coding agents briefed to address review findings.

A text or SQL heuristic (a detector in the declared fact modules, a prose rewriter or a guard) runs over the shared adversarial corpus in `tests/corpus/`: quoted keywords, comments, dollar-quoted strings, quoted identifiers, negation, ranges, code fences and very long input, in each dialect an entry applies to. A new one is registered in `tests/corpus/detectors.py` with its expected answers, or `tests/unit/corpus/test_adversarial_corpus.py` fails. A hostile input a review finds by hand goes into the corpus, so every heuristic is checked against it; a wrong answer the PR does not fix is filed as a bug and marked `xfail(strict=True)` with that issue's number.

---

## Code Standards

### Python Code Style

We follow **PEP 8** enforced via pre-commit hooks. On every commit, the following run automatically:

- **Black** - code formatting
- **isort** - import sorting
- **Ruff** - linting
- **mypy** - type checking
- **Security scan** - `ci/security.sh` (semgrep, gitleaks, Bandit) <!-- claim: tests/unit/ci/test_security_parity.py::test_the_precommit_security_hook_runs_the_script_on_every_staged_file -->

If you need to run them manually:

```bash
uv run pre-commit run --all-files
./ci/security.sh            # the security scan over every tracked file, as CI runs it
./ci/security.sh --quick    # only the files changed since the merge base with origin/main
```

It keeps the pinned semgrep rules, a semgrep environment and the gitleaks release in `~/.cache/db-modernizer-security`, so the first run needs network. Each run checks the rule files against the hash in `ci/security.toml` and the gitleaks archive against its SHA-256, fetching either again on a mismatch, and reinstalls the semgrep environment from the hashed lock. The pre-commit hook skips that reinstall and checks only the installed versions (see `ci/README.md`). <!-- claim: tests/unit/ci/test_security.py::test_a_tamper_git_status_cannot_see_is_caught_by_the_rules_pin, tests/unit/ci/test_security.py::test_a_swapped_cached_archive_is_downloaded_again, tests/unit/ci/test_security.py::test_the_semgrep_environment_is_synced_to_the_hashed_lock_on_every_run, tests/unit/ci/test_security.py::test_the_hook_syncs_the_semgrep_environment_without_reinstalling -->

### Code Quality Rules

1. **Type Hints**: Use type hints for all function signatures
2. **Docstrings**: Use Google-style docstrings for public functions
3. **Error Handling**: Handle errors gracefully with specific exception types
4. **Logging**: Use structured logging via `structlog`
5. **Constants**: Use uppercase for module-level constants

### File Organization

```
src/
├── agents/          # Pipeline agents (collector, referee, schema_design, load_test)
├── api/             # FastAPI application and routes
├── contracts/       # Pydantic models for agent I/O
├── orchestrator/    # Pipeline orchestration (LocalOrchestrator)
├── skills/          # Markdown prompts for LLM behavior
├── storage/         # Artifact storage abstraction
├── tools/           # Deterministic tools
│   ├── analysis/    #   Workload analysis (DynamoDB, DocumentDB, OpenSearch, ElastiCache, Aurora PostgreSQL, Aurora MySQL)
│   ├── aws/         #   AWS service integrations (RDS, S3, SSM)
│   └── database/    #   Database-specific tooling (MySQL, PostgreSQL)
└── ui/              # React frontend
```

---

## Testing Requirements

### Test Types

1. **Unit Tests** (`tests/unit/`) - Test individual functions/tools
2. **Contract Tests** (`tests/contract/`) - Validate Pydantic model conformance
3. **Integration Tests** (`tests/integration/`) - Test agent workflows end-to-end

### Running Tests

```bash
# Run all tests (unit, contract, property, graph; no integration or e2e)
make test                              # or: ./ci/test.sh --cov=src --cov-report=term

# Deterministic end-to-end (pipeline + rendered HTML/PDF + UI smoke)
make e2e                               # or: ./ci/e2e.sh

# Headless /modernize against a real model, same deliverable checks + a
# rubric-based quality judge (needs model access -- see ci/README.md)
make e2e-llm                           # or: ./ci/e2e-llm.sh both wordpress

# Run only contract tests
uv run pytest tests/contract/ -v
```

The disposable-engine tests (`tests/unit/tools/database/test_disposable_engine.py`)
run once for every PostgreSQL and MySQL major they find. `pgserver` (PostgreSQL 16)
comes with the `dev` extra, and its binaries also cover the `initdb`/`postgres`
path. Any other major comes from conda-forge: `conda install -c conda-forge
postgresql=<major>` and `conda install -c conda-forge mysql-server=<major>` (or
`micromamba`, one environment per major). Point `MODERNIZER_PG_BIN_<major>` (for
example `MODERNIZER_PG_BIN_14`) at the environment's `bin` folder, and
`MODERNIZER_MYSQLD_<major>` (for example `MODERNIZER_MYSQLD_8_0`) at its `mysqld`;
a `postgres` or `mysqld` on `PATH` is found too, and an explicit argument wins over
both. The MySQL tests are marked `integration`, so the gated run deselects them instead
of skipping them. <!-- claim: tests/unit/tools/database/test_disposable_engine.py::test_the_mysql_engine_tests_are_deselected_not_skipped_in_a_gated_run -->
Run them as an ordinary user: a disposable engine refuses to start as root (an
`EngineStartError`, not a missing engine), because the private socket folder cannot
stay 0700 there. <!-- claim: tests/unit/tools/database/test_disposable_engine.py::test_running_as_root_is_refused -->
`MODERNIZER_DISPOSABLE_ENGINE_PASSWORD` sets the password of MySQL's `root`; the
role that runs the routines always gets its own generated password. <!-- claim: tests/unit/tools/database/test_disposable_engine.py::test_root_and_the_runner_never_share_a_password -->
An engine that a crashed run left behind, with its folder, is stopped and removed by
the next start once its owning process is gone. <!-- claim: tests/unit/tools/database/test_disposable_engine.py::test_a_dead_owners_folder_is_swept_and_a_live_one_is_left -->

`make test` needs the AWS Transform SDK for the `atx_orchestrator` tests
(`uv pip install -r src/atx_orchestrator/requirements-sdk.txt`; see
`tests/unit/atx_orchestrator/conftest.py`) and `make e2e` needs the `e2e`
extra (`uv sync --extra e2e`), the `docs` extra (`uv sync --extra docs`,
needed to build the help site this suite also checks), Node 22, and
Playwright's browsers (`ci/e2e.sh` installs the browsers and builds the UI
and the help site itself). `make e2e-llm` needs all of that plus model
access and costs real tokens (cost and time per run: AGENTS.md's test-tier
table and `ci/README.md`'s `e2e-llm.sh` section). Beyond the per-PR runs the
prompt tier needs, the maintainers run it nightly on `main`.

### Local Pipeline Testing

For end-to-end testing of the full analysis pipeline:

```bash
# Run assessment (collect through reality-check):
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json

# Resume from existing job:
uv run python scripts/run_assessment.py --job-id <id> --db <name>

# Finalize reality check after LLM response:
uv run python scripts/run_assessment.py --job-id <id> --db <name> --resume-reality-check
```

This runs the deterministic pipeline (Collect, Triage, Analyze, Assign, Reality Check) and stops. Schema design and synthesis require LLM reasoning and are handled separately via Claude Code slash commands or `--all --llm-mode bedrock`.

In a terminal the script prints its full progress. When stdout is not a terminal (a pipe, CI, or a headless Claude Code session), stdout has only the `{"phase": ...}` status lines and a final `{"log": ...}` line; the progress goes to `artifacts/<db>/<job>/_logs/run_assessment.log`. Add `--verbose` to get the full progress on stdout anyway.

### Routing regression guards

A routing change (a referee/resolver fix, a refactor that must not move anything, the catalog work in #489) is checked two ways, both in `tests/e2e/` and both part of `make e2e`. Neither is an identity gate: a routing refactor is expected to change which engine some queries land on, and that is fine. Their job is to show and explain every change, not to forbid one.

`test_routing_baseline.py` (`baselines/routing_baseline.json`) checks coarse shape: owner shares, wave structure, cache overlay -- within a tolerance, so it tolerates noise but can miss moves that cancel out between engines.

`test_routing_snapshot.py` (`baselines/routing_snapshot.json`) reports every per-query change, grouped by which step produced it: each candidate engine's score through the resolver's base fit / anti-pattern penalty / hard-exclusion-or-write-gate steps (the score `resolve()` actually used, not a recompute -- `base`/`penalty` are kept only as diagnostics, with any gap recorded rather than raised), the resolver's first pick (plus its co-dependency group, signal, or tie-break rule, when one applies), the reality check's final pick and parsed fit score, the cache overlay engine, and the consolidation records (keyed by from/to/action, including which queries were retained and why). The failure message groups by step and shows a short excerpt of each changed query's text. The snapshot file itself is written one query per line with sorted keys, so a `git diff` or GitHub's file view lists exactly which queries moved instead of one unreadable multi-hundred-KB line.

For a fixture not bundled in the repo (e.g. AdventureWorks later), point `ROUTING_SNAPSHOT_EXTRA` at a local `collection.json`; `ROUTING_SNAPSHOT_EXTRA_DB` names the database/engine family passed to the pipeline (default `extra`). Setting `ROUTING_SNAPSHOT_EXTRA` switches `--write` entirely to writing `routing_snapshot_<db>.json` next to that input file -- never committed, and never touching the committed `baselines/routing_snapshot.json`; unset it to go back to writing the committed baseline.

A PR that changes routing regenerates whichever snapshot(s) moved, with a reason, and pastes the printed diff into the PR description so a reviewer sees exactly what moved without re-deriving it themselves:

```bash
uv run python -m tests.e2e.routing_baseline --write --reason "why it moved (#NNN)"
uv run python -m tests.e2e.routing_snapshot --write --reason "why it moved (#NNN)"
```

After a rebase, or when CI reports a stale baseline, one command covers every generated baseline (the routing baseline and snapshot, the contract schema and value-set snapshots, the `assess()` JSON Schemas, the rendered rule catalog). `uv run python scripts/regenerate_baselines.py --check` writes nothing, names each stale baseline with its own command and exits 1 if any is stale; `uv run python scripts/regenerate_baselines.py --reason "why it moved (#NNN)"` regenerates the stale ones by calling those same commands (`--only <name>` limits it). A baseline that is already fresh is left alone, so a clean tree changes nothing. Never hand-merge or hand-edit them.

---

## Documentation

All contributions should include appropriate documentation:

1. **Docstrings** - All public functions must have docstrings
2. **README updates** - Update relevant README files for new features
3. **Guide updates** - Update implementation guides in `docs/guides/` if behavior changes
4. **Docs-impact map** - A change to a path in `ci/docs_impact_map.toml` needs an edit to one of its pages, or a `Docs: <reason>` line in the PR body; see [ci/README.md](ci/README.md).
5. **Claims name their test** - see below.

### Claims name their test

A behaviour sentence says what the code does: "the check fails a verdict that contradicts the script", "a tie goes to the source-compatible engine". Review's most common finding is one that says more than the code does, so every behaviour sentence a PR adds or changes in `docs/` (ADRs included, planning notes under `docs/superpowers/` aside), a command or subagent file (`.claude/commands/`, `.claude/agents/`), a skill (`src/skills/`), any README (fixtures aside), AGENTS.md or CONTRIBUTING.md names the test that proves it, in two places:

- **In the page**, a claim marker right after the sentence: an HTML comment that renders as nothing, holding the test's pytest node id. Several ids are separated by commas or spaces. A UI (Jest) test has no node id, so its marker names the test file. Name a test that runs: a parametrize id (`[...]`) and a test marked `skip`, `skipif` or `xfail` are refused.

  ```markdown
  A missing verdict line fails the check. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_missing_verdict_fails -->
  ```

- **In the PR body**, a row of the template's Claims table: the sentence, and the same test, or `removed` when the PR deletes the sentence. A PR that changes those pages but no behaviour sentence writes `No claims: <reason>` instead.

A sentence no test proves is cut, or reworded to what the code does. It is never listed as proven, and "covered by existing tests" names the tests. Describing a rule (what a contributor should do) is not a behaviour sentence and needs no marker. An ADR's numbered decisions keep their `## Enforcement` table (`tests/unit/docs/test_adr_enforcement.py`); a marker is for the other sentences. Markers live in Markdown only: a sentence in a docstring or a code comment is out of their reach and out of the docs-impact map, so keep behaviour claims a reader relies on in a page, and a docstring to how the code works.

What checks it:

- `uv run python ci/claims.py check` (in `ci/lint.sh` and the `claim-markers` pre-commit hook) fails a marker whose test pytest would not collect and run (the file under `testpaths` and matching `python_files`, the class and the function, no skip or xfail mark), read from the AST, and a misspelt marker (`<!-- Claim:`). `--run` also runs the marked tests. A marker in a code block or code span, like the example above, is not read. <!-- claim: tests/unit/ci/test_claims.py::test_check_names_the_page_line_and_reason, tests/unit/ci/test_claims.py::test_resolve_rejects_what_names_no_test, tests/unit/ci/test_claims.py::test_markers_in_code_are_examples_and_are_not_read, tests/unit/ci/test_claims.py::test_check_fails_on_a_misspelt_marker -->
- The `PR Evidence` check (`ci/pr_evidence.py`, also run by `ci/pre_merge.sh`) fails a Claims row whose test does not exist, and a PR that changes those pages with neither a row nor a `No claims:` line. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_rows_that_name_no_test_fail, tests/unit/ci/test_pr_evidence.py::test_a_docs_change_without_a_claims_table_fails, tests/unit/ci/test_pr_evidence.py::test_no_claims_with_a_reason_passes_and_the_placeholder_does_not -->
- `ci/pre_merge.sh`'s docs-impact step lists the pages whose markers cite a test file the branch changed. <!-- claim: tests/unit/ci/test_pre_merge.py::test_docs_impact_reviews_the_pages_a_changed_claim_test_proves -->

None of them reads whether the test proves the sentence. The reviewer does, from the Claims table.

An open pull request opened before the Claims section existed has none in its body, so its PR Evidence check goes red on its next push or body edit when it changes a claim page. Add the section (the template's table, or a `No claims: <reason>` line); nothing else needs to change.

---

## Security

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

---

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.

By contributing, you agree that your contributions will be licensed under the MIT-0 License.

Every source file we wrote (`.py`, `.sh`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.sql`) starts with this two-line header, in the file's comment syntax, after any byte order mark, shebang line, Python encoding line or leading JS pragma docblock (`/** @jest-environment node */`, `@flow`, `@jsx`, which their tools read only at the very top) and before a JS `'use strict'` directive, with one blank line before the code:

```text
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
```

You don't have to type it: `uv run python scripts/license_headers.py --fix` adds it to every file without it, and running it again changes nothing. The `license-headers` pre-commit hook and `./ci/lint.sh` run `--check`, which lists the files that lack it and fails. Only `SPDX-License-Identifier: MIT-0` in the file's leading comment block counts: the text inside a docstring or string does not, and a file under another identifier fails the check. `contrib/` keeps its own provenance and is skipped (`[tool.license-headers]` in `pyproject.toml`), as are vendored code and build outputs. A generated source file gets the header from its generator (for example `scripts/render_catalog.py` for `engineCatalog.generated.js`), never from a hand edit.

---

## Finding Contributions to Work On

Looking at the existing issues is a great way to find something to contribute on. Issues labeled `good first issue` or `help wanted` are great places to start.

---

## Contract Changes

Breaking changes to contracts (`src/contracts/`) require special process:

1. Create a proposal issue with rationale and impact analysis
2. Increment the contract version
3. Provide a migration guide
4. Update all affected agents

See `docs/contracts/agent-contracts-spec.md` for details.

**Any** contract change (breaking or not) needs a `contract_version` bump:
MINOR for an additive optional field or a new enum value, MAJOR for a
removal, a type change, a newly required field, a removed enum value, or a
tightened constraint (`ge`/`le`/`gt`/`lt`/`min_length`/`max_length`/
`pattern`); loosening a constraint is MINOR (the full table is in
`docs/contracts/agent-contracts-spec.md`'s Versioning Strategy section).
One exception: `contract_version` itself adopting exactly `^\d+\.\d+$` (from
an unpatterned string or a MAJOR.MINOR `Literal`) is MINOR, since every
published version already matches it (#1006).
`tests/contract/test_schema_snapshot.py` enforces this against a committed
JSON Schema snapshot of every versioned contract, keyed by model, in
`tests/contract/schema_snapshot.json` -- it fails if a schema changes
without its version, or if the version changes without the snapshot being
regenerated, naming the exact nested path that changed (e.g.
`agent_metadata.retries`, not just the top-level field) and the bump it
requires. Regenerate it deliberately and review the diff like the routing
snapshot (`tests/e2e/routing_snapshot.py`):

```bash
uv run python scripts/contract_schema_snapshot.py --write
```

`tests/contract/test_value_set_snapshot.py` applies the same rule to the
values contracts publish: enum and literal members (reason codes, flag
reasons, phases), engine ids, capability ids and access-pattern id prefixes,
in `tests/contract/value_set_snapshot.json`. Adding a value is a MINOR bump of
the publishing contract, removing or renaming one is MAJOR. Regenerate with
`uv run python scripts/contract_value_snapshot.py --write`.

Do not pick the new number yourself: parallel pull requests used to claim the same one and renumber it on every rebase (#1158, ADR-008 Decision 8). Declare the bump kind instead, which writes one file under `src/contracts/pending/` and nothing else:

```bash
uv run python scripts/bump_contract.py synthesis_output minor --reason "Added optional ``foo`` (what and why)." --issue 1234
```

Leave the version, the Version History and both snapshots at main's, and merge the pull request with its pending file: the number is assigned at release, not in your pull request (#2176). The snapshot tests accept a change the pending files cover (yours and the ones already on main), and do not compare the version a pending contract's entry records, so a value set you added keeps passing when another pull request's bump of the same module lands first. `validate-contracts` and `ci/pre_merge.sh` (`scripts/resolve_contract_versions.py --check --against <main>`) fail a contract change with no pending file of your own, even when main already holds one for that contract, and refuse a `contract_version`, a Version History line or a snapshot entry you wrote by hand, and an edit of a pending file main already holds (declare your own). <!-- claim: tests/unit/scripts/test_contract_release_resolve.py::test_a_change_needs_its_own_pending_file_even_when_main_holds_one, tests/unit/scripts/test_contract_release_resolve.py::test_a_version_moved_by_hand_is_refused, tests/unit/scripts/test_contract_release_resolve.py::test_a_snapshot_entry_of_a_pending_contract_written_in_a_pull_request_is_refused --> The one exception is a MAJOR: the merge queue refuses a pending MAJOR, because artifacts of the new shape would carry the old MAJOR. After your last rebase onto main, resolve it in the pull request (with its reader adapter), then amend and push: <!-- claim: tests/unit/scripts/test_contract_release_resolve.py::test_the_merge_queue_refuses_only_a_pending_major, tests/unit/scripts/test_contract_release_resolve.py::test_a_major_resolved_in_its_own_pull_request_passes -->

```bash
uv run python scripts/resolve_contract_versions.py
```

It resolves only the contracts that have a pending MAJOR file, with their other pending files, and leaves every MINOR to the release ([Releases](#releases)).

In tests, compare a written contract's version with `current_version(Model)` (`src/shared/contract_versioning.py`), never a literal such as `"1.44"`: the resolver moves the number after review, and `validate-contracts` runs `scripts/check_version_pins.py`, which fails a test that pins one (#1226). The merge queue refuses a pull request that still has a pending MAJOR, and `validate-contracts` fails a contract change since main with neither a version move nor a pending bump, even if you regenerated the snapshot. A contract the resolver cannot edit is refused by `bump_contract.py`, which says how to bump it by hand. If your branch is stacked on another that has since merged, delete that branch's pending file after rebasing; the resolver refuses it. While the branch is stacked on another open pull request (open, not a draft, from this repository, with its head commit among your branch's commits; one whose head has moved on since you took it does not count, so rebase onto its new head), `ci/pre_merge.sh` and the `validate-contracts` job print `stacked on #N: re-check after #N merges` and compare the contract versions with its head commit instead of main, so its resolved version is not read as a hand bump (#1846). <!-- claim: tests/unit/ci/test_pre_merge.py::test_a_branch_carrying_another_pr_head_is_stacked_on_it, tests/unit/ci/test_pre_merge.py::test_a_draft_or_fork_pr_is_never_the_stack_base, tests/unit/ci/test_pre_merge.py::test_a_stacked_on_pr_that_moved_on_is_not_a_stack, tests/unit/ci/test_pre_merge.py::test_pre_merge_sh_compares_the_contract_checks_with_the_stack_base, tests/unit/ci/test_pre_merge.py::test_the_ci_contract_step_looks_for_a_stack_without_the_pull_request_payload --> The comparison enforces what it does against main (a pending file next to a hand-written version fails; a shape change with no version move and no pending bump fails; a plain hand bump passes `ci/pre_merge.py contracts`, and `--check --against` refuses it for a contract the resolver can edit unless it is that contract's next MAJOR) <!-- claim: tests/unit/ci/test_pre_merge.py::test_check_contract_on_a_stacked_branch_compares_with_the_stack_base, tests/unit/ci/test_pre_merge.py::test_a_plain_hand_bump_is_allowed_against_main_and_against_a_stack_base, tests/unit/scripts/test_contract_release_resolve.py::test_a_version_moved_by_hand_is_refused --> and does not see a problem inherited from #N, which shows on #N's own check and in the merge queue; the merge queue always compares with main. Resolve your own MAJOR against that head too (`--base <its head commit>`), and again against main after #N merges and you rebase.

A contract written to disk declares its own version,
`contract_version: str = Field(default="X.Y", pattern=r"^\d+\.\d+$", ...)`,
and a writer that builds a new version of a file from an earlier one stamps
the model's default rather than copying the old value forward.
`tests/contract/test_written_contracts_versioned.py` finds every
`write_json(..., x.model_dump())` call and fails when the model it writes has
no such field (ADR-008 Decision 1).

If two in-flight PRs both touch a contract, neither regenerates its
snapshot entry: both keep their pending files, and the release resolve
writes the entry once.

### Renaming an id or an enum value

A saved job keeps the spelling it was written with, and the contract version stamp does not cover it. A renamed or removed id or enum value needs a legacy-map entry in the same PR (a `status = "legacy"` engine file, a `penalty_aliases` entry, a kept `ReasonCode` member, `LEGACY_CAPABILITY_IDS`, or a MAJOR adapter). `tests/fixtures/old_jobs/<tag>/` holds a real job an older release wrote; `tests/contract/test_old_job_loads.py` loads it through the real read paths and fails when a stored engine id, access class, pattern key, catalog pattern id, capability or move reason code no longer resolves (it reads every JSON value by key name, so a new file in the fixture is covered). Other vocabularies are not checked. Convention: keep the fixture as the release wrote it; fix the code with a legacy map instead.

### The public `assess()` contract

The library entry point's request and result (`src/assessment/models.py`, [docs/contracts/assess.md](docs/contracts/assess.md)) are versioned on their own (`CONTRACT_VERSION`) and promised to callers, so two guards in `tests/contract/` (run by `make test` and `ci/test.sh`) check them as a caller would:

- `test_assess_consumer_contract.py` runs `assess()` on the fixtures in `tests/contract/fixtures/assess_consumer_fixtures.json` (WordPress, the SQL Server sample `trailgear`, and WordPress with an engine scope), for every `run_through` in `SUPPORTED_RUN_THROUGH`, with the model entry points patched. Each fixture lists the result fields it guards; every guarded field must be present with the type the published JSON Schema gives it, every result field must be guarded by some fixture, and the result must validate against `docs/contracts/schemas/assess_result.v1.schema.json`. A new result field is added to a fixture's `guards`; a phase that calls a model adds its entry point to `MODEL_ENTRY_POINTS` there.
- `test_assess_released_contract.py` compares the current shape with the one the last release shipped, from contract `1.3` on, `tests/contract/assess_released_since_1_3.json`, using the same field classification as the internal snapshot (`"released": null` until the first release that ships `1.3` records its tag). `tests/contract/assess_released_snapshot.json` is the historical record of the last release recorded before `1.3` (v0.4.0-beta.1's contract `1.1`; v0.4.0-beta.2 shipped `1.2`, but its shape was never recorded): it is kept as it was, never re-recorded, and the test asserts that every version it records is now refused (this release supports `1.3` only, #1641). Removing or renaming a field, changing its type, or removing or changing an enum value fails unless `CONTRACT_VERSION`'s MAJOR number rises (and a MAJOR adds the next schema file, `docs/contracts/schemas/README.md`). Once a release is recorded, an additive change needs a MINOR rise; before the first release, additions keep `1.0`. A new value of a result enum (a reason code, a role) counts as additive. The file changes only in the release step below, never by hand: the `validate-contracts` job and `ci/pre_merge.sh` (`scripts/resolve_contract_versions.py --check --against <main>`) compare with main's copy of it, and accept a change to it only when it records an existing release tag with exactly that tag's shape, so a pull request cannot pass by re-recording it.
- `test_assess_saved_results.py` reads the saved `1.3` results in `tests/fixtures/assess_results_v1_3/` back through `AssessmentResult` and checks that every saved value is unchanged (see [Releases](#releases)).

Moving the MAJOR is itself an approval: it breaks callers. The `contract-major-gate` check (`.github/workflows/contract-major-gate.yml`, `ci/contract_major_gate.py`) compares the request's and the result's version (`CONTRACT_VERSION` and each JSON Schema's `x-contract-version`) with the base's, on every push, label change and merge group, and fails a MAJOR move unless the PR carries the `contract-major` label (or its old name, `Contract Major Approved`), and a MINOR move unless it carries `contract-minor`, applied by a maintainer, when the check runs (the labels, who applied them and their role are read from the API). A label removed earlier never counts. A PATCH move, or none, passes. In the merge queue only the entry's own commits count. A label counts only while it is on the PR, the user who last applied it has the maintain or admin role, and it was applied after the PR's latest push (an approval resets on every push); <!-- claim: tests/unit/ci/test_contract_major_gate.py::test_a_label_applied_before_the_latest_push_no_longer_counts, tests/unit/ci/test_contract_major_gate.py::test_a_label_re_applied_after_the_push_counts_again --> the workflow runs the base branch's copy of the gate script, never the PR's; <!-- claim: tests/unit/ci/test_contract_major_gate.py::test_the_workflow_runs_the_base_branchs_copy_of_the_gate --> a MAJOR and a MINOR in one PR need both labels, and `contract-minor` never approves a MAJOR. <!-- claim: tests/unit/ci/test_contract_major_gate.py::test_an_assess_major_needs_the_major_label_and_the_minor_label_is_not_enough, tests/unit/ci/test_contract_major_gate.py::test_an_assess_major_and_minor_need_both_labels, tests/unit/ci/test_contract_major_gate.py::test_a_label_removed_after_approval_drops_it, tests/unit/ci/test_contract_major_gate.py::test_the_latest_labeled_event_decides_who_applied_it -->

## Releases

This section owns the release procedure. Every release is a tag on `main`: there are no release or maintenance branches (a `hotfix/` branch is a short-lived branch off `main` like any other), and a fix for a released version ships as the next tag from `main`. Nothing is built or deployed from a release; the tag publishes a GitHub release.

### Version numbers

The package version is never edited by hand (#391): `pyproject.toml` declares `dynamic = ["version"]`, and `hatch-vcs` derives it from the tag (`v0.4.0` becomes `0.4.0`; a prerelease tag such as `v0.4.0-beta.1` becomes `0.4.0b1`, PEP 440). There is no version field to bump and no release-prep PR for the version. A checkout without the matching tag reports a version that is obviously not a release, and a copy with no `.git` falls back to `0.0.0`.

Tags follow `vMAJOR.MINOR.PATCH`, with a `-beta.N` suffix while the project is in beta (`v0.1.0-beta.2` to `v0.4.0-beta.2` so far). MAJOR is a breaking change for users, MINOR a backward-compatible feature, PATCH a fix. Contract versions are separate and internal (ADR-008); the user-facing number is the tag.

From contract `1.3` on, a release must read the results earlier releases wrote: `tests/fixtures/assess_results_v1_3/` holds `1.3` `assess()` results written by the code released as v0.4.0-beta.3 (WordPress at `analysis` and `reality_check`, trailgear at `analysis`), and `tests/contract/test_assess_saved_results.py` reads them back with every saved value. They are the read-back guarantee from `1.3`: never regenerate or edit them (the edit hook blocks it), and when that test fails, fix the code, not the files.

### Steps

1. **Refresh the help site's sample report.** The site renders a recorded model run committed under `docs/examples/wordpress/sample-run/`, and that run should come from the release being prepared. Run `/modernize` on the WordPress sample in Claude Code (`docs/examples/wordpress/wordpress.zip`) on the release candidate, then copy the run in and open a PR with it. Only ever refresh from a synthetic or public sample such as WordPress, never from a customer's database:

   ```bash
   uv run python scripts/build_docs_sample.py --refresh-from artifacts/wordpress/<job>
   # Update the release and synthesis contract version in
   # docs/examples/wordpress/sample-run/README.md and the release in docs/sample.md
   uv run pytest tests/unit/scripts/test_build_docs_sample.py -q
   ```

   The refresh copies the synthesis `report.json`, the collected names and the routing records the detailed engineering report reads (the effective assignment's lineage `assignment/v<N>/assignment.json`, its `access_patterns.json` and each engine's `analysis-<engine>/analysis.json`), and refuses the job when either carries one of these (`private_markers` in `scripts/build_docs_sample.py`): a local path, an e-mail address, a non-public URL, an internal hostname, an IP address, an AWS account id, an AWS ARN, an AWS access key id, an API token, a private key, or this machine's user name. It then regenerates the folder's entries in `.secrets.baseline`, so detect-secrets still scans the sample at commit (and gitleaks in CI) and flags anything new in it.

   A pattern list cannot judge free text: the report carries model-written prose (the summary, the reality-check review, the risks and trade-offs) where a customer name or an internal reference would not match any pattern. Before the refresh PR merges, a person reads the diff of the refreshed `report.json`'s prose fields.

2. **Resolve the pending contract bumps.** Every pending file under `src/contracts/pending/` waits on `main` until here (ADR-008 Decision 8, #2176). On a branch from the latest `main`, resolve them all in one commit, with the release's tracking issue as its `Refs` trailer, then open a pull request with it and merge it before going on:

   ```bash
   git checkout -b release/resolve-contracts origin/main
   uv run python scripts/resolve_contract_versions.py --release --issue <tracking issue>
   ```

   It moves each contract by the strongest kind declared for it (several pending files for one contract make one bump), writes the `contract_version` defaults and the Version History entries, deletes the pending files and regenerates `tests/contract/schema_snapshot.json` and `tests/contract/value_set_snapshot.json`. It refuses uncommitted changes under `src/contracts/` or `tests/contract/`, and writes nothing when a contract cannot be resolved. `validate-contracts` reads that commit as the release resolve, not as a hand bump, as long as it changes no contract shape beyond what the resolved pending files already cover on main. With no pending file it does nothing. The release workflow refuses a tag on a commit that still holds a pending file (`resolve_contract_versions.py --check --require-resolved` in `.github/workflows/release.yml`), so skipping this step stops the release. <!-- claim: tests/unit/scripts/test_contract_release_resolve.py::test_the_release_workflow_refuses_a_tag_with_pending_bumps, tests/unit/ci/test_release_resolve_scratch_repo.py::test_an_unbumped_change_cannot_ride_on_a_release_resolve --> <!-- claim: tests/unit/ci/test_release_resolve_scratch_repo.py::test_the_release_resolves_every_pending_file_in_one_commit, tests/unit/ci/test_release_resolve_scratch_repo.py::test_the_release_refuses_a_dirty_contract_tree, tests/unit/scripts/test_contract_release_resolve.py::test_the_release_writes_nothing_when_a_contract_cannot_be_resolved -->

3. **Preview the release.** The routing and contract changes it will carry:

   ```bash
   uv run python scripts/release_notes.py --since <previous tag>
   ```

4. **Run the full pre-release check** on the `main` commit you will tag. The per-PR real-model runs cover only prompt-tier changes and the nightly run only wordpress (`ci/needs_llm_run.sh` in ci/README.md), so a release gets every tier, whatever the verdict says: all checks green (`./ci/pre_merge.sh` on `main`, which with an empty diff still runs lint and the full test suite, and `make e2e`), e2e-llm on both samples, and the Bedrock smoke run, which the maintainers start. Record each run's result line (`e2e-llm result: ...` per sample, `bedrock-smoke result: ...`) where the release is tracked, and tag only when every one passes:

   ```bash
   ./ci/pre_merge.sh
   make e2e
   make e2e-llm E2E_LLM_FIXTURE=wordpress
   make e2e-llm E2E_LLM_FIXTURE=discourse
   ```

5. **Tag `main`.** `main` is protected, so the tag is the only step that publishes a release; nothing commits a version bump back to `main` (the contract versions landed in step 2's pull request).

   ```bash
   git checkout main && git pull
   git tag -a v0.4.0 -m "Release v0.4.0: <one line>"
   git push origin v0.4.0
   ```

6. **Record the public contract's shipped shape**, so the next release is compared with it. `--write-released` refuses a tag that does not exist and reads the models at the tag itself (`git archive`), never the working tree, so run it after pushing the tag, in a pull request to main (a fix release's tag too):

   ```bash
   git fetch --tags
   uv run python scripts/contract_schema_snapshot.py --write-released v0.4.0
   ```

   In the same pull request, record the rule catalog's shipped format and files (#1647). It reads `src/catalog/` at the tag (`git archive`), writes `tests/contract/catalog_format_snapshot.json` with `released` set to the tag (a draft holds `released: null` until then), and writes the tag's catalog files, as raw TOML, to `tests/contract/fixtures/catalog_releases/<tag>/`, which `tests/contract/test_catalog_old_releases_load.py` loads with the current loader from then on:

   ```bash
   uv run python scripts/catalog_format_snapshot.py --write-released v0.4.0
   ```

7. **Check the release.** The tag runs `.github/workflows/release.yml`, which checks out full history, verifies that the resolved `hatch-vcs` version matches the tag (the workflow fails if not) and publishes the GitHub release. The release body is built by `scripts/release_notes.py`: it picks the previous release with `scripts/release_notes.py --previous-tag <tag>` (the highest version tag below it: exact names, PEP 440 order, so `v0.3.0` follows `v0.3.0-beta.2` and `v0.4.1` follows `v0.4.0`), then runs `scripts/release_notes.py --since <previous tag> --until <tag>` and attaches its output to the release: the routing-snapshot diff since the previous release (from `tests/e2e/baselines/routing_snapshot.json`: engine changes first, then reason-code-only changes, then queries whose score alone moved), and one changelog line per rule change (a file under `src/catalog/`, with the commits that touched it) and per contract change (an internal contract's version move from `tests/contract/schema_snapshot.json`, and the public contract's from `tests/contract/assess_released_since_1_3.json`, or at a release before `1.3`, `tests/contract/assess_released_snapshot.json`), and, per catalog file, every value that moved since the previous release ("Catalog values changed since <previous tag>"). The release body (`--body`) lists the first 50 engine and reason-code changes, summarises score-only drift (counts per sample and per score change), keeps 40 changelog lines and stays under GitHub's body limit; `release-changes.md`, attached to the release, carries every row and line. The step is non-blocking: if it fails, the release is published with a warning line in its body. Step 3 previews it before tagging.

---

## Follow-ups

A review, a PR description or a code comment often lists a follow-up or a known limit. Each one becomes an issue before the PR merges, and the PR says which issue it is. A limit that lives only in prose is lost when the PR closes.

- Every follow-up issue joins an epic as a sub-issue, so the epic shows its progress. If no epic fits, say so in the issue and ask a maintainer to place it.
- Before you file one, search for an open issue that already covers it, and comment there instead of filing a duplicate.
- A weekly triage pass closes or merges duplicates and checks that every open issue has an epic, a label and an assignee.

## Working with a fast-moving main

Many pull requests land on `main` every day, so a branch that was green yesterday can conflict or break today.

- Run `./ci/pre_merge.sh` before you push and before you ask for a merge. It fetches main, reports how far behind you are, checks for conflicts and contract version problems (a hand-written version that collides with main's, a contract change with no bump or pending bump), checks the docs-impact map (above), runs your new and changed tests against main's code and lists those that pass there, runs lint and tests (`--quick` skips only the full test suite), and prints the `needs_llm_run` verdict.
- Rebase onto the latest main and re-test right before merging, not only when you opened the PR.
- After a red CI run, re-run its failed tests locally before you push again: `./ci/rerun_failed.sh` (the current branch's PR, or give the PR number). `./ci/pre_merge.sh` prints that command when the last run was red.
- Do not take a contract version number yourself: declare the bump kind with `uv run python scripts/bump_contract.py` and merge with the pending file; the release resolves it (a MAJOR: run `uv run python scripts/resolve_contract_versions.py` after your last rebase; see Contract Changes, ADR-008 Decision 8).
- Never hand-merge a generated snapshot (the routing snapshot, `tests/contract/schema_snapshot.json`, `tests/contract/value_set_snapshot.json`, rendered catalog docs). Take main's copy and regenerate it on top of it; the two contract snapshots you only take from main, since the release resolve writes the entries of a contract with a pending bump.
- Run `./ci/needs_llm_run.sh` early. If it says `e2e-llm: required` (a change to what the model is told: commands, skills, prompt framing, model-input builders, model-output contracts), start that run before the review rounds, not after. `nightly` needs no run from you and no result line: the maintainers run e2e-llm and the Bedrock smoke on `main` each night (ci/README.md, `needs_llm_run.sh`).
- `main` has a merge queue. Use "Merge when ready" to add your PR to it: the queue re-runs the required checks on top of main and the PRs ahead of yours, then merges it, or removes it from the queue and shows the failing check.

## Maintainer Sync

Maintainers can mirror this repository into a separate target deployment
repository with `scripts/maintainer-sync.sh`. It exports one git ref
(`--ref`, default `HEAD`) with `git archive` and syncs it as a single
snapshot commit, branched from a freshly fetched copy of the target
repository's default branch, recording a `Source-Commit: <sha>` trailer on
the sync commit.

Main options (each has an environment variable equivalent; the script's own
header comment has the full list):

- `--ref <ref>` -- the git ref to sync (env `SYNC_REF`, default `HEAD`)
- `--branch <name>` -- target branch name (env `SYNC_TARGET_BRANCH`, default
  the current source branch)
- `--message <text>` -- commit message, before the `Source-Commit:` trailer
  is appended (env `SYNC_COMMIT_MESSAGE`)
- `--force` -- overwrite a remote target branch even if its latest commit
  has no `Source-Commit:` trailer
- `SYNC_DRY_RUN=true` -- preview the sync in a disposable worktree; nothing
  in the real target clone is stashed, committed or pushed
- `TARGET_REPO_PATH` -- required; path to the target repository's local clone

**Snapshot model:** each sync is one commit covering everything new since
the last sync, not one commit per source commit, so an internal validation
pipeline is never run against the target repository's intermediate states.
This is an accepted trade-off: it matches this project's one-commit-per-issue
convention (the unit that was validated is already one issue, one commit),
and bisecting a regression in the target repository is still possible by
syncing one `--ref <sha>` at a time.

**Safety:** the script stashes a dirty target clone before syncing, refuses
to run if the target's default branch has diverged from or is ahead of its
remote, protects paths the target's own `.gitignore` excludes from deletion,
and refuses to overwrite a target branch whose latest commit isn't one of
its own prior sync commits unless `--force` is given.

---

## Questions?

- **General questions**: Create a discussion on GitHub
- **Bug reports**: Use the bug report issue template
- **Feature requests**: Use the feature request issue template

---

**Thank you for contributing to Database Modernizer Assessment!**
