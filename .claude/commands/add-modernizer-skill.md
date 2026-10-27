
# /add-modernizer-skill

Guides creation of a new pipeline phase. Follows strict priority order — each step must be complete before the next begins.

The key principle: **deterministic code first, command second.** A "skill" here is a Claude Code command: a markdown file under `.claude/commands/` that Claude Code follows when invoked as `/your-phase`. The command is only the thin layer that tells the model how to work with code that already runs. If the phase cannot run from its script with `--llm-mode none`, it is not ready for a command.

## The LLM seam pattern

Every phase that can use a model splits into three functions, so the same code serves Bedrock, Claude Code and deterministic runs:

- `run_{phase_name}_deterministic(input)`: the complete result with no model call.
- `prepare_{phase_name}_llm_input(contract, input)`: the payload a model reasons over (context and candidates; the script adds `output_schema`).
- `apply_{phase_name}_llm_output(contract, llm_typed)`: merges the model's already-validated answer into the contract.

The script chooses by `--llm-mode`: `none` calls only the deterministic function; `external` also writes the request to disk and stops with `awaiting_llm` for Claude Code to answer; `bedrock` runs the whole flow with a Strands agent; `--finalize` reads the response, validates it through the Pydantic model and applies it.

Where the phase sits: the pipeline order is collect, triage, analyze, assign, reality-check, then the assignment review gate, then design-schema, then synthesize. Each phase reads the previous phases' artifacts; `.modernizer-state.json` is the cursor, not the source of truth.

## Required Input

Ask the user for:

- **Phase name**: e.g. `migrate-ddl`, `validate-schema`, `cost-estimate`
- **Purpose**: What this phase does in one sentence
- **Needs LLM?**: Whether this phase requires LLM judgment (yes/no)
- **Input**: What artifacts from previous phases it reads
- **Output**: What it produces

## Phase 1: Contract (MUST complete first)

1. Create `src/contracts/{phase_name}_output.py`
2. Define the output Pydantic model with:
   - Clear field descriptions
   - Appropriate types and constraints
   - Validators where needed
3. If LLM is needed, define the LLM output model separately (keep it minimal — only what requires judgment)
4. Verify:

   ```bash
   uv run python -c "from src.contracts.{phase_name}_output import *; print('OK')"
   ```

**STOP**: Do not proceed until the contract compiles and the user approves the field design.

## Phase 2: Deterministic Logic

1. Create `src/agents/{phase_name}/handler.py`
2. Implement `run_{phase_name}_deterministic(input) -> contract`
3. If LLM is needed, implement:
   - `prepare_{phase_name}_llm_input(contract, input) -> dict`
   - `apply_{phase_name}_llm_output(contract, llm_typed) -> updated_contract`
4. Write tests first, in `tests/unit/agents/test_{phase_name}.py`. Test the seam functions with a canned model response, never a real call (tests patch the model entry points)
5. Verify:

   ```bash
   uv run pytest tests/unit/agents/test_{phase_name}.py -v
   ```

**STOP**: Do not proceed until tests pass.

## Phase 3: Script

1. Create `scripts/run_{phase_name}.py`
2. Must support: `--job-id`, `--db`, `--llm-mode` (none/external/bedrock), `--finalize`. Add `--artifact-root` as `scripts/run_triage.py` and `scripts/run_reality_check.py` do. Print compact JSON status lines only; send progress to a log file, as `run_assessment.py` does
3. In external mode, inject `output_schema`:

   ```python
   llm_request["output_schema"] = LlmModel.model_json_schema()
   ```

4. In finalize mode, deserialize through Pydantic:

   ```python
   llm_typed = LlmModel.model_validate(llm_response)
   updated = apply_llm_output(contract, llm_typed)
   ```

5. Output JSON to stdout: `{"status": "complete"}` or `{"status": "awaiting_llm", "llm_request": "..."}` or `{"status": "validation_failed", "errors": [...]}`
6. Verify deterministic mode works:

   ```bash
   uv run python scripts/run_{phase_name}.py --job-id test --db test --llm-mode none
   ```

7. In `finalize`, a failed validation returns `{"status": "validation_failed", "errors": [...]}` with the exact Pydantic errors.

**STOP**: Do not proceed until the script runs successfully.

## Phase 4: Domain Expertise (if LLM phase exists)

1. Create `src/skills/{phase_name}-expertise.md`
2. Write the domain knowledge the LLM needs to make good decisions
3. Keep it focused — principles and anti-patterns, not implementation details

## Phase 5: Command

1. Create `.claude/commands/{phase_name}.md`
2. No frontmatter. Follow this exact template, matching the other shipped commands:

```markdown
# /{phase_name}

{Brief description.}

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.

## Prerequisites

- {What must be true}

## Steps

1. **Run phase**

   ```bash
   uv run python scripts/run_{phase_name}.py --job-id {job_id} --db {database_name} --llm-mode external
   ```

2. **If status is `awaiting_llm`:**
   a. Read: `artifacts/{database_name}/{job_id}/llm_requests/{phase_name}.json`
      - Contains context data and `output_schema`
   b. Read: `src/skills/{phase_name}-expertise.md`
   c. Produce JSON conforming to `output_schema`
   d. Write to: `artifacts/{database_name}/{job_id}/llm_responses/{phase_name}.json`
   e. Finalize:

      ```bash
      uv run python scripts/run_{phase_name}.py --job-id {job_id} --db {database_name} --finalize
      ```

   If validation fails, fix errors and retry up to 3 times.

3. **Update state**
   Set `phase_status.{phase_name}` = "complete"

```

3. Copy the Tool Use paragraph above word for word. Every command a headless run follows carries it, and `tests/unit/scripts/test_modernize_command.py` checks the wording
4. Add an allow rule for the new script to `.claude/settings.ci.json` (`"Bash(uv run python scripts/run_{phase_name}.py *)"`), plus a `Skill(...)` rule if the command is dispatched. `tests/unit/scripts/test_settings_ci.py` fails on a script a command runs without one
5. Validate:

   ```bash
   uv run python scripts/validate_skills.py
   ```

   The validator checks that every `uv run python scripts/...py` reference points to a script that exists, that every `src/skills/*.md` reference exists, and that no command uses a dot-prefixed artifacts path (scripts write `./artifacts/`). It does not lint prose or JSON schemas; running the script does that. The pre-commit hook runs it too.

## Phase 6: Integration

1. Add the new phase to the `/modernize` orchestrator skill if it belongs in the main pipeline
2. Update `.claude/commands/modernize.md` with the new step in the correct position
3. Run the pre-commit hook to verify everything links up:

   ```bash
   uv run python scripts/validate_skills.py
   ```

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Contract details in the command | The script injects `output_schema`; the command says "conform to it" |
| Telling the model to read Python files | Never; the request file has everything |
| A large model output contract | Push logic into deterministic code; the model makes judgment calls only |
| Skipping Pydantic validation in finalize | Always `Model.model_validate(response)` before `apply_llm_output()` |
| Hardcoding job IDs | Scripts generate them; the state file tracks them |
| Testing the command before the script works | Get `--llm-mode none` working first |
| Frontmatter or a `SKILL.md` file | Commands are plain `.claude/commands/<name>.md` files with a `# /<name>` heading |
| A dot-prefixed artifacts folder in a path | Use `artifacts/`; the validator rejects the dotted form |

## Checklist

- [ ] Contract in `src/contracts/`; a new or changed contract follows CONTRIBUTING.md "Contract Changes"
- [ ] Deterministic logic works with `--llm-mode none`
- [ ] Seam functions implemented and tested
- [ ] Script injects `output_schema` in external mode and validates through Pydantic in finalize
- [ ] Command references only the script, the request file and the domain expertise
- [ ] Tool Use paragraph copied verbatim; allow rule added to `.claude/settings.ci.json`
- [ ] `validate_skills.py` passes
- [ ] Domain expertise in `src/skills/*.md` (if there is an LLM phase)

## Rules

- NEVER skip a phase. Each phase depends on the previous.
- NEVER put contract details in the skill. The script injects `output_schema`.
- NEVER reference Python source files from skills.
- Keep LLM output contracts minimal. If logic can be deterministic, it MUST be.
- The script is the source of truth for validation — the skill just runs it.
