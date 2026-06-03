
# /add-modernizer-skill

Guides creation of a new pipeline phase. Follows strict priority order — each step must be complete before the next begins.

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
4. Write tests in `tests/agents/test_{phase_name}.py`
5. Verify:

   ```bash
   uv run pytest tests/agents/test_{phase_name}.py -v
   ```

**STOP**: Do not proceed until tests pass.

## Phase 3: Script

1. Create `scripts/run_{phase_name}.py`
2. Must support: `--job-id`, `--db`, `--llm-mode` (none/external/bedrock), `--finalize`, `--artifact-root`
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

**STOP**: Do not proceed until the script runs successfully.

## Phase 4: Domain Expertise (if LLM phase exists)

1. Create `src/skills/{phase_name}-expertise.md`
2. Write the domain knowledge the LLM needs to make good decisions
3. Keep it focused — principles and anti-patterns, not implementation details

## Phase 5: Skill

1. Create `.claude/skills/{phase_name}/SKILL.md`
2. Follow this exact template:

```markdown
---
name: {phase_name}
description: {one-line purpose}
---

# /{phase_name}

{Brief description.}

## Prerequisites

- {What must be true}

## Steps

1. **Run phase**

   ```bash
   uv run python scripts/run_{phase_name}.py --job-id {job_id} --db {database_name} --llm-mode external
   ```

2. **If status is `awaiting_llm`:**
   a. Read: `.artifacts/{database_name}/{job_id}/llm_requests/{phase_name}.json`
      - Contains context data and `output_schema`
   b. Read: `src/skills/{phase_name}-expertise.md`
   c. Produce JSON conforming to `output_schema`
   d. Write to: `.artifacts/{database_name}/{job_id}/llm_responses/{phase_name}.json`
   e. Finalize:

      ```bash
      uv run python scripts/run_{phase_name}.py --job-id {job_id} --db {database_name} --finalize
      ```

   If validation fails, fix errors and retry up to 3 times.

3. **Update state**
   Set `phase_status.{phase_name}` = "complete"

```

3. Validate:

   ```bash
   uv run python scripts/validate_skills.py
   ```

## Phase 6: Integration

1. Add the new phase to the `/modernize` orchestrator skill if it belongs in the main pipeline
2. Update `.claude/skills/modernize/SKILL.md` with the new step in the correct position
3. Run the pre-commit hook to verify everything links up:

   ```bash
   uv run python scripts/validate_skills.py
   ```

## Rules

- NEVER skip a phase. Each phase depends on the previous.
- NEVER put contract details in the skill. The script injects `output_schema`.
- NEVER reference Python source files from skills.
- Keep LLM output contracts minimal. If logic can be deterministic, it MUST be.
- The script is the source of truth for validation — the skill just runs it.
