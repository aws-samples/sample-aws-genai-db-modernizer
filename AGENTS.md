# AGENTS.md

Working guide for coding agents and power users of this repository. It is a
map, not a manual: human onboarding lives in [CONTRIBUTING.md](CONTRIBUTING.md)
and the CI scripts are documented in [ci/README.md](ci/README.md).

> **Running a pipeline command?** If this session was started to run
> `/modernize` or any other `.claude/commands/*.md` command, that command file
> is authoritative. Nothing here adds steps, questions or tools to it.

## Repo map

| Path | What lives there |
|---|---|
| `src/agents/` | Pipeline agents: `collector/`, `analysis/` (one per engine), `referee/` (triage, reality check, synthesis), `schema_design/`, `load_test/`; `prompt_framing.py` |
| `src/contracts/` | Pydantic models for every agent's input and output (contract changes: see CONTRIBUTING.md) |
| `src/tools/` | Deterministic tools: `analysis/`, `schema/`, `validation/`, `database/`, `aws/` |
| `src/report/` | Deliverable renderers (engineering Markdown, landscape executive report PDF) from one synthesis `report.json`; `escaping.py` |
| `src/shared/` | Cross-cutting helpers, including `engine_names.py` (engine display names) |
| `src/catalog/` | Rule catalog (ADR-031 Decision 8): `engines/` one TOML per engine (display name, colours, status, role), `patterns/` one TOML per pattern type (per-engine fit score, anti-pattern severity, penalty, why), `gaps/` one TOML per relational-move gap rule with `aurora_versions.toml` (#471, read at synthesis, never by routing), loaded by `src/shared/catalog/`; `scripts/render_catalog.py` generates the UI's `engineCatalog.generated.js` and `docs/architecture/rule-catalog.md` (`--check` runs in lint) |
| `src/skills/` | Markdown prompts (data-modeling and PE-review guidance per engine) that agents and commands load |
| `src/orchestrator/`, `src/storage/`, `src/graph/` | Local phase orchestrator, artifact store and assignment versioning, context graph |
| `src/api/`, `src/ui/` | FastAPI backend and React UI (the results page, with the wave plan and the deliverables download panel) |
| `src/atx_orchestrator/` | AWS Transform integration |
| `scripts/` | CLI entry points (`run_*.py`) the commands call, plus validators and helpers |
| `.claude/commands/` | Claude Code slash commands (`/modernize`, `/analyze-<engine>`, `/design-schema-<engine>`, `/reality-check`, `/synthesize`, ...) |
| `.claude/settings.ci.json` | Permission allowlist for headless runs |
| `ci/` | CI-agnostic entry points (`lint.sh`, `test.sh`, `e2e.sh`, `e2e-llm.sh`) and the quality judge (`ci/llm/`) |
| `tests/` | `unit/`, `contract/`, `property/`, `graph/` (gated), `e2e/` (deterministic end-to-end), `integration/` (live stack, not gated) |
| `benchmarks/` | Assessment quality benchmark: scores pipeline output against reviewed answer keys (ADR-030); how to run it: `benchmarks/README.md` |
| `docs/examples/` | Sample fixtures: `wordpress/` and `discourse/` |
| `mkdocs.yml`, `docs/` (minus `exclude_docs`) | Help site sources (`scripts/build_docs_sample.py` renders the sample report into it; `.github/workflows/docs.yml` builds and deploys it) |

## How commands, skills and scripts fit together

- A command (`.claude/commands/<name>.md`) tells Claude Code which
  `uv run python scripts/<script>.py ...` to run and which `src/skills/*.md`
  prompt to follow. The scripts do all deterministic work and write artifacts.
- `/modernize` is a lightweight orchestrator: it reads `.modernizer-state.json`
  and script stdout, and dispatches one fresh subagent per LLM phase.
- `uv run python scripts/validate_skills.py` checks that every script and
  `src/skills/` path a command names exists (also part of `make lint`).

### The LLM seam pattern

Every phase that can use a model splits into three pieces, so the same code
serves Bedrock, Claude Code and fully deterministic runs:

1. `run_*_deterministic(...)`: the complete result with no model call
   (e.g. `run_reality_check_deterministic`, `run_synthesis_deterministic`).
2. `prepare_*_llm_input(det)`: the payload a model reasons over
   (`prepare_reality_check_llm_input`, `prepare_synthesis_llm_input`,
   `prepare_schema_design_input`).
3. `apply_*_llm_output(det, llm_output)`: merges and validates the model's
   answer on top of the deterministic result (`apply_reality_check_llm_output`,
   `apply_synthesis_llm_output`; schema design uses `finalize_schema_design`).

Analysis agents expose the same shape (see the docstrings in
`src/agents/analysis/aurora_*_analysis_agent.py`).

### The three LLM modes (`--llm-mode`)

| Mode | Who reasons | Notes |
|---|---|---|
| `external` | Claude Code (the default for `run_assessment.py`) | The script writes `llm_input.json` and stops (`awaiting_llm`); the command writes the response, then runs `--finalize` / `--resume-reality-check` |
| `bedrock` | Strands agents on Amazon Bedrock | `run_assessment.py --file <f> --all -y --llm-mode bedrock` |
| `none` | Nobody | Deterministic only, no model calls |

With `--llm-mode none`: reality check keeps its deterministic result,
synthesis keeps the deterministic executive summary, and **schema design is
skipped** with reason `llm_mode=none: every schema designer needs a model`
(every designer needs a model). `run_assessment.py` always runs the analysis
phase deterministically, whatever `--llm-mode` says.

## Setup

```bash
./scripts/setup_dev.sh                       # uv sync --extra dev + pre-commit hooks
uv sync --extra dev --extra e2e              # add the e2e extra (Playwright, pypdf)
uv sync --extra docs                         # add the docs extra (mkdocs, mkdocs-material) to build the help site
uv pip install "agent-builder-sdk-aws-transform>=1.0.0"   # needed for a fully green make test
```

The UI needs Node 22 (`npm ci`, then `npm run build`, in `src/ui/`); `ci/e2e.sh`
and `ci/e2e-llm.sh` build it for you.

## Parallel worktrees

- Run `uv sync` in each worktree; a worktree's `.venv` is its own.
- The main clone's editable install points at its own `src/`, so if you reuse
  it from another worktree, set `PYTHONPATH` and `UV_NO_SYNC=1` instead.
- Before fanning out, list each issue's files. Issues that share a file are
  not independent: land the shared extraction first, decide the merge order,
  then parallelize the rest.
- Regenerate generated baselines (the routing snapshot, the contract schema
  snapshot) on top of main on every rebase; never hand-merge them.

## Which test tier for which change

| Tier | Command | Checks | Cost / time |
|---|---|---|---|
| Lint | `make lint` (all pre-commit hooks) or `./ci/lint.sh` (ruff, black, isort, mypy, `validate_skills.py`) | style, types, markdownlint, secrets, command references | free: `ci/lint.sh` seconds, `make lint` about 2 minutes |
| Unit/contract | `make test` (or `./ci/test.sh -q`) | unit, contract, property, graph; `--fail-on-skip`, no network | free, about 2 minutes |
| Deterministic e2e | `make e2e` (or `./ci/e2e.sh`) | full pipeline on both samples with no model, HTML in Chromium + WebKit, PDF, UI smoke, the built help site | free, minutes (UI build + browsers) |
| Headless LLM e2e | `make e2e-llm` (both mode by default, `E2E_LLM_FIXTURE=wordpress` by default) | real `/modernize --auto` run, the same deliverable checks, a rubric judge | real tokens, both mode: wordpress about $4 and roughly 6 minutes; discourse about $18-20 and 14-15 minutes (session cap `E2E_LLM_MAX_BUDGET_USD`, default 50) |
| e2e-llm dry run | `E2E_LLM_DRY_RUN=1 E2E_LLM_TRANSCRIPT=<transcript.jsonl> ./ci/e2e-llm.sh chat wordpress` | everything after the model call, replayed from a saved transcript | free, needs an existing job under `artifacts/` |

- Python change in `src/` or `scripts/`: `make lint` and `make test`.
- Before pushing or asking for a merge: `./ci/pre_merge.sh` (fetch, behind count, conflict and contract-version checks, new tests against main, lint, tests, `needs_llm_run` verdict); see CONTRIBUTING.md's "Working with a fast-moving main".
- A `tests/e2e` test that works only on the deterministic job (it skips when `run.external`) needs `@pytest.mark.deterministic`, or `e2e-llm` fails on `--fail-on-skip`; `tests/unit/ci/test_e2e_deterministic_markers.py` enforces it.
- Pipeline output or `src/report/`: add `make e2e` (or
  `uv run pytest tests/e2e/test_pipeline.py -p no:xdist -q` for a quick pass).
- `make e2e-llm` when `./ci/needs_llm_run.sh` prints `e2e-llm: required` (its
  lists are the source of truth; `bedrock-smoke` covers Bedrock call sites).
- `E2E_LLM_MODE` is `chat`, `ui` or `both`; `E2E_LLM_FIXTURE` is `wordpress`
  (fast iteration) or `discourse` (scale).
- Read e2e-llm results in `test-results/llm-<mode>-<fixture>/`: `results.json`
  (one merged row: checks, cost, turns, `pass`), `judge.json` (rubric scores,
  rubric in `ci/llm/rubric.md`), `transcript.jsonl`, `steps.json`,
  `deliverables/`, `job-logs/`.
- Tests never call a real model; patch the model entry points.

## Headless rules (`/modernize --auto`)

`ci/e2e-llm.sh` runs `claude -p "/modernize <collection.json> --auto --mode <chat|ui|both>"`
with `--permission-mode dontAsk` and `.claude/settings.ci.json`.

- `--auto` never asks the user anything; `/modernize` never asks an
  experience-mode question at all. Default (and `--auto` alone) means `both`
  (chat + local UI); `--mode chat` is chat only, `--mode ui` is UI-first
  (chat gives only the approval-gate numbers, UI pointers, and the result
  line -- no phase summaries).
  If the UI can't start, it falls back to chat and keeps going rather than
  failing the run.
- The run ends with exactly one line
  `MODERNIZE_RESULT: complete job_id=<id> db=<db> mode=<mode>` or
  `MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`.
- The headless session may have no Grep tool, and `grep` through Bash is
  denied. Search with `uv run python scripts/search_artifacts.py <regex> <path>`
  (read-only, repo-contained, capped output).
- Every command, and every dispatch's task text, carries this tool-use rule
  word for word (enforced by `tests/unit/scripts/test_modernize_command.py`):

  > Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`.

- `.claude/settings.ci.json` allows only the pipeline's `uv run python scripts/...`
  commands, `Edit`/`Write` under `artifacts/**` and `.modernizer-state.json`,
  `Agent`, and the dispatched skills; it denies web access, `git push`/`commit`,
  `rm`, `curl` and secret paths. A new script a command runs needs an allow
  rule there (rationale for each entry: ci/README.md).
- Subagents nest one level deep: only the orchestrator dispatches, and every
  dispatch says "Do not dispatch subagents yourself."
- Waiting Rule (in `modernize.md`): never end a turn waiting unless a
  dispatched subagent is still running.
- Script stdout stays compact for headless sessions: JSON status lines only.
  `run_assessment.py` writes its progress to
  `artifacts/<db>/<job>/_logs/run_assessment.log` and prints a final
  `{"log": ...}` line (`--verbose` restores full stdout).
- Allowlisted scripts refuse paths outside the repo under
  `MODERNIZER_CI_SANDBOX=1` (`scripts/_sandbox.py`).

## Where artifacts live

Local runs write under `artifacts/<db>/<job>/` (`ARTIFACT_DIR`, default
`./artifacts`):

- `collector/output.json`, `collector/sql_shadow.json` (sqlglot facade vs regex table references; the facade drives `tables_accessed`, #557, #553), `referee-triage/triage.json`
- `analysis-<engine>/analysis.json` (+ `decision-trace.json`)
- `assignment/v<N>/assignment.json`: versioned; reality check and customer
  edits write a new version, never overwrite one
- `assignment/v<N>/access_patterns.json`: each query's access-pattern
  class, class scores and parity-anchor notes, as routing scored it (#728, #936)
- `reality-check/` (`llm_input.json`, `output.json`)
- `schema-<engine>/v<N>/schema_output.json`, `synthesis/v<N>/report.json`
  plus a per-version copy of the rendered files, keyed on the assignment version
- `deliverables/`: the customer folder `scripts/run_report.py` fills for the
  latest version, with stable names and an `index.md`; the one list of its
  files is `FOLDER_ENTRIES` in `src/report/deliverables.py`, and
  `tests/unit/docs/test_deliverables_lists.py` keeps every docs/command list
  in step with it. Model I/O and traces never go there
- `decisions/` (recorded approvals), `_logs/` (script logs)

`artifacts/.trash/` (outside any job folder): job folders being deleted by
`DELETE /assessments/{job_id}/artifacts`; leftover tombstones older than an
hour are removed, up to 5 per later delete call.

`.modernizer-state.json` at the repo root is the job cursor, not the source of
truth.

## Key invariants

- **Assignments map queries to engines.** A table can be served by several
  engines; the single recommended engine in `table_mappings` is not ownership.
- **Deliverables agree.** The executive report, the engineering report and the UI
  render the same `report.json`; a fact changed in one must change in all.
  Engine names come from `src/shared/engine_names.py` (`display_engine`),
  never a local copy.
- **Renderers only format.** Synthesis computes each customer-facing figure once into `report.json`'s `facts`; renderers read it and never count or total it, in Python (`tests/unit/report/test_renderers_only_format.py`) and in the UI (`src/ui/src/utils/__tests__/rendererOnlyFormat.test.js`). A new figure goes into `src/shared/report_facts.py` first ([ADR-032](docs/architecture/decisions/ADR-032-one-facts-layer-renderers-only-format.md)).
- **Untrusted text is data.** Customer and LLM text is wrapped with
  `frame_untrusted` (`src/agents/prompt_framing.py`) in prompts and escaped
  with the helpers in `src/report/escaping.py` (`md_cell`, `md_text`,
  `md_code`, `mermaid_label`, ...) when rendered. Generated DDL
  types pass an allowlist (`validate_aurora_type` in
  `src/contracts/aurora_design_delta.py`), so collector text never reaches
  DDL unchecked.
- Engine/query assignment is deterministic; a model may explain it, not decide it.

## Routing changes

The routing model lives in one place, [ADR-031](docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md).
This section is a pointer, not a restatement.

- The query is the unit everywhere: routing, waves, deliverable figures, cost
  and scope. A table's `primary_engine` is a summary label.
- There is one scorer: `query_scoring.score_query_for_engine`, used with
  `ScoringContext.from_pipeline`. Never add a private fit function.
- A query's pattern dimension is its own access-pattern class's score
  (`query_pattern.QueryPatternModel`, ADR-031 Decision 14 Amendments 5 and 7),
  never its tables' pattern scores.
- Ties go through `tie_break.break_tie`, using `settled_counts` only.
  [`test_no_engine_bias.py`](tests/unit/agents/referee/test_no_engine_bias.py)
  enforces this. Engine removal keeps the source-compatible engine when it scores every query at least as well, unless the alternative is at least 40% and $100/mo cheaper (`tie_break.source_compatible_engine_survives`, ADR-031 Decision 13).
- An override onto an engine that can't serve the query is rejected.
- A table has one writer (ADR-031 Amendment 9): its writes and locking reads form a write group placed as one unit with any co-dependency group it touches (`write_groups.placement_groups`) in every pass; its reads stay per query, as CDC replicas.
- Every engine change is a Move: only `move_ledger.apply` / `apply_to_assignment`
  write `assigned_engine` and `tied_engines`, with no exceptions (ADR-031 Amendment 3,
  [`test_only_the_ledger_writes_engines.py`](tests/unit/agents/referee/test_only_the_ledger_writes_engines.py)).
- Explain drift with the [routing snapshot](tests/e2e/routing_snapshot.py),
  and regenerate it on rebase. Never hand-merge it.

## Before asking for review

Self-review checklist, from findings that recur in review. Each item says what to do, then why.

1. Every new test fails on main. Run it against `origin/main`'s code; the reviewer will, and a test that passes there guards nothing. `./ci/pre_merge.sh` (also with `--quick`) runs the branch's new and changed tests on main's code and lists the ones that pass there; mark a test that guards behaviour main already has `@pytest.mark.passes_on_main` (see ci/README.md).
2. Revert each part of the fix and check that some test fails. If none fails, that part is untested.
3. Cover the negative and edge cases: what must not change, ties, empty input, ambiguity. Bugs hide where the happy path does not look.
4. The PR body states only what this branch shows: its own test counts, timings and drift. "Covered by existing tests" names the tests. Numbers from another branch mislead the reviewer.
5. Never bend a sample, fixture or expectation to keep a result. Fix the cause, or change the expectation with a per-query explanation (see [Routing changes](#routing-changes)).
6. After renaming an id, field or capability, old saved artifacts must still load (map the legacy value), with a test that loads an old file. Saved jobs outlive the code that wrote them.
7. Check the change against every ADR decision it touches, not just the one in the issue. ADR text must never claim more than a guard enforces.
8. Make timing and resource checks relative (a ratio against a same-process reference), so local runs match CI.
9. Run `./ci/pre_merge.sh` before pushing: it checks conflicts, contract versions, new tests against main, lint, tests and the `needs_llm_run` verdict in one pass (see [ci/README.md](ci/README.md)).
10. Pass the source dialect or engine wherever SQL is parsed. A default dialect silently misparses other engines' syntax.
11. Fill in the PR template's "Maintainer decision this implements" with a link to the issue comment that recorded the decision (or "None"), so the reviewer can check the PR against it.

## Conventions

- Issues use the templates in `.github/ISSUE_TEMPLATE/`: `[Bug] ...`
  (label `bug`) or `[Feature] ...` (label `enhancement`), every section filled in.
- One commit per issue: a short Conventional Commits subject (at most 72
  characters) plus a 2-4 line body (a line or two each on the problem and
  the fix), ending `Closes #N` (or `Refs #N` for follow-ups). The full
  explanation, review notes and test results go in the issue and the PR,
  not the commit (see CONTRIBUTING.md's Commit Message Format). A
  `check-commit-message` pre-commit hook and `ci/check_commits.sh` enforce
  this; merge and fixup/squash/amend commits are skipped (#570).
- Claude Code hooks in `scripts/hooks/` (wired in `.claude/settings.json` and `.claude/settings.ci.json`) refuse `git commit --no-verify`, hand edits of generated baselines and, in headless runs, any Bash call that breaks the tool-use rule; see ci/README.md.
- Never add `Co-Authored-By` trailers (also hook-enforced, see above); never use `--no-verify`.
- Write tests first; keep PRs small; open them against `main`.
- Public wording everywhere (issues, commits, docs, code): this is a public
  sample repository, so no internal system names or internal links.
- The "Maintainer Decision" label marks an issue or PR waiting for a
  maintainer, and blocks a PR from merging. Agents may add it; agents never
  remove it unless a human explicitly says to in that session.
- Maintainers mirroring this repo into a deployment target: see
  CONTRIBUTING.md's Maintainer Sync section (`scripts/maintainer-sync.sh`).
