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
| `src/report/` | Deliverable renderers (decision/analysis HTML, engineering Markdown, PDF, PPTX) from one synthesis `report.json`; `escaping.py` |
| `src/shared/` | Cross-cutting helpers, including `engine_names.py` (engine display names) |
| `src/skills/` | Markdown prompts (data-modeling and PE-review guidance per engine) that agents and commands load |
| `src/orchestrator/`, `src/storage/`, `src/graph/` | Local phase orchestrator, artifact store and assignment versioning, context graph |
| `src/api/`, `src/ui/` | FastAPI backend and React UI (the interactive report template is extracted from `src/ui/src/utils/ExportReport.js`) |
| `src/atx_orchestrator/` | AWS Transform integration |
| `scripts/` | CLI entry points (`run_*.py`) the commands call, plus validators and helpers |
| `.claude/commands/` | Claude Code slash commands (`/modernize`, `/analyze-<engine>`, `/design-schema-<engine>`, `/reality-check`, `/synthesize`, ...) |
| `.claude/settings.ci.json` | Permission allowlist for headless runs |
| `ci/` | CI-agnostic entry points (`lint.sh`, `test.sh`, `e2e.sh`, `e2e-llm.sh`) and the quality judge (`ci/llm/`) |
| `tests/` | `unit/`, `contract/`, `property/`, `graph/` (gated), `e2e/` (deterministic end-to-end), `integration/` (live stack, not gated) |
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

## Which test tier for which change

| Tier | Command | Checks | Cost / time |
|---|---|---|---|
| Lint | `make lint` (all pre-commit hooks) or `./ci/lint.sh` (ruff, black, isort, mypy, `validate_skills.py`) | style, types, markdownlint, secrets, command references | free: `ci/lint.sh` seconds, `make lint` about 2 minutes |
| Unit/contract | `make test` (or `./ci/test.sh -q`) | unit, contract, property, graph; `--fail-on-skip`, no network | free, about 2 minutes |
| Deterministic e2e | `make e2e` (or `./ci/e2e.sh`) | full pipeline on both samples with no model, HTML in Chromium + WebKit, PDF, UI smoke, the built help site | free, minutes (UI build + browsers) |
| Headless LLM e2e | `make e2e-llm` (both mode by default, `E2E_LLM_FIXTURE=wordpress` by default) | real `/modernize --auto` run, the same deliverable checks, a rubric judge | real tokens, both mode: wordpress about $4 and roughly 6 minutes; discourse about $11 |
| e2e-llm dry run | `E2E_LLM_DRY_RUN=1 E2E_LLM_TRANSCRIPT=<transcript.jsonl> ./ci/e2e-llm.sh chat wordpress` | everything after the model call, replayed from a saved transcript | free, needs an existing job under `artifacts/` |

- Python change in `src/` or `scripts/`: `make lint` and `make test`.
- Pipeline output or `src/report/`: add `make e2e` (or
  `uv run pytest tests/e2e/test_pipeline.py -p no:xdist -q` for a quick pass);
  after a UI report change run `uv run python scripts/sync_report_template.py --check`.
- `.claude/commands/`, `src/skills/`, `.claude/settings.ci.json`, `ci/llm/`, or
  anything loaded into headless sessions (including this file): `make e2e-llm`.
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

- `collector/output.json`, `referee-triage/triage.json`
- `analysis-<engine>/analysis.json` (+ `decision-trace.json`)
- `assignment/v<N>/assignment.json`: versioned; reality check and customer
  edits write a new version, never overwrite one
- `reality-check/` (`llm_input.json`, `output.json`)
- `schema-<engine>/v<N>/schema_output.json`, `synthesis/v<N>/report.json`
  plus the rendered deliverables, keyed on the assignment version
- `decisions/` (recorded approvals), `_logs/` (script logs)

`.modernizer-state.json` at the repo root is the job cursor, not the source of
truth.

## Key invariants

- **Assignments map queries to engines.** A table can be served by several
  engines; the single recommended engine in `table_mappings` is not ownership.
- **Deliverables agree.** The decision report, engineering report, deck/PDF and
  UI render the same `report.json`; a fact changed in one must change in all.
  Engine names come from `src/shared/engine_names.py` (`display_engine`),
  never a local copy.
- **Untrusted text is data.** Customer and LLM text is wrapped with
  `frame_untrusted` (`src/agents/prompt_framing.py`) in prompts and escaped
  with the helpers in `src/report/escaping.py` (`html_text`, `html_attr`,
  `md_cell`, `md_text`, `mermaid_label`, ...) when rendered. Generated DDL
  types pass an allowlist (`validate_aurora_type` in
  `src/contracts/aurora_design_delta.py`), so collector text never reaches
  DDL unchecked.
- Engine/query assignment is deterministic; a model may explain it, not decide it.

## Conventions

- Issues use the templates in `.github/ISSUE_TEMPLATE/`: `[Bug] ...`
  (label `bug`) or `[Feature] ...` (label `enhancement`), every section filled in.
- One commit per issue, Conventional Commits subject, a body explaining the
  problem and the fix, ending `Closes #N` (or `Refs #N` for follow-ups).
- Never add `Co-Authored-By` trailers; never use `--no-verify`.
- Write tests first; keep PRs small; open them against `main`.
- Public wording everywhere (issues, commits, docs, code): this is a public
  sample repository, so no internal system names or internal links.
