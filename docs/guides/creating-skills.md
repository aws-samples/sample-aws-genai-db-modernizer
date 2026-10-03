# Creating Skills: Developer Guide

This guide explains how to add a new phase to the modernizer pipeline. The key principle: **deterministic code first, skill second**. The skill is just the thin orchestration layer that tells the LLM how to interact with your already-working code.

A "skill" here means a Claude Code command: a markdown file under `.claude/commands/` that Claude Code loads and follows when invoked as `/your-phase`.

## Priority Order

1. **Pydantic contracts** — Define the input/output shapes
2. **Deterministic logic** — The agent code that does the heavy lifting without an LLM
3. **LLM seam functions** — `prepare_llm_input()` and `apply_llm_output()`
4. **Script** — The CLI entry point that wires it all together
5. **Command** — The orchestration instructions for Claude Code

Do NOT start with the command. If you can't run your phase from a script with `--llm-mode none`, it's not ready for a command.

## Architecture: The LLM Seam Pattern

Every LLM-capable phase exposes three functions:

```python
def run_deterministic(input) -> (contract, trace, ...):
    """Does all the work that doesn't need an LLM."""

def prepare_llm_input(contract, input) -> dict:
    """Formats the request for the LLM. Includes context + output_schema."""

def apply_llm_output(contract, llm_response) -> updated_contract:
    """Merges the LLM's typed response back into the contract."""
```

The script orchestrates these based on `--llm-mode`:

- `none` — Only calls `run_deterministic()`
- `external` — Calls `run_deterministic()` + `prepare_llm_input()`, writes to disk, exits with `awaiting_llm`
- `bedrock` — Runs the full flow end-to-end via Strands agent
- `--finalize` — Reads the LLM response from disk, deserializes through Pydantic, calls `apply_llm_output()`

## Step 1: Define the Contract

Create `src/contracts/your_phase_output.py`:

```python
from pydantic import BaseModel, Field

class YourPhaseOutput(BaseModel):
    """Output contract for your phase. Every field must be documented."""

    results: list[YourResult] = Field(description="What this contains")
    summary: str = Field(description="Human-readable summary")
```

If your phase needs LLM input, also define the LLM output contract:

```python
class LlmYourPhaseOutput(BaseModel):
    """What the LLM must produce. Keep this minimal."""

    decisions: list[Decision] = Field(description="LLM decisions")
    rationale: str = Field(description="Why these decisions")
```

**Rule**: The LLM output contract should be as small as possible. Push everything you can into deterministic code.

## Step 2: Write the Deterministic Logic

Create `src/agents/your_phase/handler.py`:

```python
def run_your_phase_deterministic(input: YourInput) -> YourPhaseOutput:
    """All the logic that doesn't need an LLM."""
    # Pattern matching, heuristics, scoring, filtering...
    pass

def prepare_llm_input(contract: YourPhaseOutput, input: YourInput) -> dict:
    """Format what the LLM needs to see."""
    return {
        "context": ...,       # filtered data relevant to LLM decisions
        "candidates": ...,    # what the LLM needs to evaluate
        # output_schema is injected by the script, NOT here
    }

def apply_llm_output(contract: YourPhaseOutput, llm: LlmYourPhaseOutput) -> YourPhaseOutput:
    """Merge LLM decisions back. llm is already Pydantic-validated."""
    # Update contract with LLM decisions
    pass
```

**Test this independently** before moving on:

```bash
uv run pytest tests/agents/test_your_phase.py
```

## Step 3: Write the Script

Create `scripts/run_your_phase.py`. The script:

1. Parses CLI args (`--job-id`, `--db`, `--llm-mode`, `--finalize`, `--artifact-root`)
2. In `external` mode: runs deterministic, injects `output_schema` into the request, writes to `llm_requests/`
3. In `finalize` mode: reads `llm_responses/`, deserializes through Pydantic, calls `apply_llm_output()`

**Critical**: The script injects the JSON Schema into the LLM request:

```python
def run_external(store, job_id, db):
    contract = run_your_phase_deterministic(input)
    llm_request = prepare_llm_input(contract, input)

    # THIS IS THE KEY LINE — schema goes in the request payload
    llm_request["output_schema"] = LlmYourPhaseOutput.model_json_schema()

    store.write_json(f"{db}/{job_id}/llm_requests/your_phase.json", llm_request)
    print(json.dumps({"status": "awaiting_llm", "llm_request": ...}))
```

**Critical**: Finalize deserializes through the typed model, not raw dict:

```python
def run_finalize(store, job_id, db):
    llm_response = store.read_json(f"{db}/{job_id}/llm_responses/your_phase.json")

    # Validate through Pydantic BEFORE passing to apply function
    llm_typed = LlmYourPhaseOutput.model_validate(llm_response)
    updated = apply_llm_output(contract, llm_typed)

    store.write_json(output_path, updated.model_dump())
    print(json.dumps({"status": "complete"}))
```

If validation fails, return `{"status": "validation_failed", "errors": [...]}` with the exact Pydantic errors.

## Step 4: Write the Command

Create `.claude/commands/your-phase.md`. No frontmatter — just a `# /your-phase` heading followed by plain markdown. Match the shipped commands exactly (see `.claude/commands/collect.md`, `.claude/commands/synthesize.md`, or `.claude/commands/design-schema-documentdb.md` for real examples):

```markdown
# /your-phase

Brief description.

## Prerequisites

- What must be true before this runs

## Steps

1. **Run phase**

   ```bash
   uv run python scripts/run_your_phase.py --job-id {job_id} --db {database_name} --llm-mode external
   ```

2. **If status is `awaiting_llm`:**
   a. Read: `.artifacts/{database_name}/{job_id}/llm_requests/your_phase.json`
      - Contains: context data and `output_schema` (the exact JSON Schema your output must conform to)
   b. Read: `src/skills/your-domain-expertise.md` (domain expertise guide)
   c. Produce JSON conforming to `output_schema` from the request file
   d. Write to: `.artifacts/{database_name}/{job_id}/llm_responses/your_phase.json`
   e. Finalize:

      ```bash
      uv run python scripts/run_your_phase.py --job-id {job_id} --db {database_name} --finalize
      ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix and retry up to 3 times.

3. **Update state**
   Set `phase_status.your_phase` = "complete"

```

**Rules for commands:**
- No `name`/`description` frontmatter — Claude Code loads anything under `.claude/commands/*.md` and the `# /your-phase` heading is the only identity it needs
- Never reference Python source files — the schema is in the request payload
- Never inline JSON schemas in the command — the script handles that
- Keep it short — the LLM reads one file (request), writes one file (response), runs one command (finalize)
- Domain expertise goes in `src/skills/*.md` prompt files, not in the command itself

If the new phase belongs in the main pipeline, also add it to `.claude/commands/modernize.md` in the correct position.

## The Pipeline Order

```

collect → triage → assign → analyze-{engine} → reality-check → design-schema-{engine} → synthesize

```

Each phase reads from the previous phase's artifacts. The `.modernizer-state.json` tracks progress but is NOT the source of truth — artifacts are.

## Validation

Run the validator to catch broken references before you commit:

```bash
uv run python scripts/validate_skills.py
```

It scans every file in `.claude/commands/*.md` and checks three things:

1. Every `uv run python scripts/...py` reference points to a script that actually exists
2. Every `src/skills/*.md` reference points to a prompt file that actually exists
3. No command references `.artifacts/` — scripts write to `./artifacts/`, and a stray `.artifacts/` reference is a broken path waiting to happen

It does not check anything else (it doesn't lint prose or validate JSON schemas) — those come from running the script itself. The pre-commit hook runs this automatically.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Putting contract details in the command | The script injects `output_schema` — command just says "conform to it" |
| Telling the LLM to read Python files | Never. The request file has everything. |
| Large LLM output contracts | Push logic into deterministic code. LLM should only make judgment calls. |
| Skipping Pydantic deserialization in finalize | Always `Model.model_validate(response)` before `apply_llm_output()` |
| Hardcoding job IDs | Scripts generate them. State file tracks them. |
| Testing the command before the script works | Get `--llm-mode none` working first. |
| Adding frontmatter or a `SKILL.md` file | Commands are plain `.claude/commands/<name>.md` files with a `# /<name>` heading — no frontmatter, no per-command directory |

## Checklist

Before your PR is ready:

- [ ] Contract defined in `src/contracts/`
- [ ] Deterministic logic works with `--llm-mode none`
- [ ] LLM seam functions implemented and tested
- [ ] Script injects `output_schema` in external mode
- [ ] Script finalize deserializes through Pydantic
- [ ] Command at `.claude/commands/your-phase.md` references only: the script, the request file, and domain expertise
- [ ] `validate_skills.py` passes
- [ ] Domain expertise in `src/skills/*.md` (if LLM phase exists)
