
# /modernize

Full end-to-end database modernization pipeline. The orchestrator is LIGHTWEIGHT — it only tracks state and dispatches subagents. It NEVER reads large artifacts or produces LLM responses itself.

## Arguments

- `<collector_file>` — path to collector output JSON (required)
- `--auto` — skip decision gates, auto-approve all (equivalent to `-y`)

## Step 0: Experience Mode (ASK FIRST)

Before anything else, ask the user:

> How would you like to follow the modernization?
>
> 1. **Chat only** — all results shown here in the terminal
> 2. **UI only** — I'll start the local API and frontend, check results at <http://localhost:3000>
> 3. **Both** — results in chat AND the UI running alongside
>
> (Pick 1, 2, or 3)

**If user picks 2 or 3**, start the local services:

```bash
# Start API server
STORAGE_TYPE=local ARTIFACT_ROOT=./artifacts uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 &

# Build and serve frontend (install deps on first run)
cd src/ui && npm install && REACT_APP_API_URL=http://localhost:8000/api/v1/ npx react-scripts build && npx serve -s build -l 3000 &
```

Wait a few seconds for both to start, then verify:
- API health: `curl -s http://localhost:8000/health`
- Frontend: `curl -s -o /dev/null -w "%{http_code}" http://localhost:3000` (expect 200)

Tell the user:
- API running at <http://localhost:8000>
- Frontend running at <http://localhost:3000>
- Wait for the user to confirm the UI is loaded before proceeding

Store the choice in `.modernizer-state.json` as `"experience_mode": "chat"|"ui"|"both"`.

## CRITICAL: Subagent Isolation Rule

**Every phase that involves LLM reasoning MUST run as a subagent.** This prevents context bloat and hallucination.

The orchestrator's job is ONLY:

- Read `.modernizer-state.json` for current state
- Dispatch subagents for each phase
- Read the script's stdout (1-line JSON status)
- Update `.modernizer-state.json`
- Present brief summaries to the user
- Handle errors and decision gates

The orchestrator NEVER:

- Reads collector output, analysis results, or schema designs
- Reads `llm_requests/` or `llm_input.json` files
- Produces LLM responses or writes to `llm_responses/`
- Reads `output_schema` contents
- Makes consolidation validation decisions

## Pipeline

### Phases 1-5: Collect → Triage → Analysis → Assignment → Reality Check

Run the full deterministic pipeline in one command (no subagent needed):

```bash
uv run python scripts/run_assessment.py --file {collector_file} --db {database_name}
```

The database name is derived from the collector filename (e.g., `wordpress-collection.json` → `wordpress`). The script outputs one JSON line per phase to stdout and updates `.modernizer-state.json` after each phase so the UI shows progress.

**If reality check returns `awaiting_llm` (this is the expected path):**

1. Tell user: "Deterministic phases complete. Dispatching consolidation validator..."
2. **Dispatch a subagent:** "Run /reality-check for job_id={job_id} db={database_name}"
3. After subagent completes, resume:
   ```bash
   uv run python scripts/run_assessment.py --job-id {job_id} --db {database_name} --resume-reality-check
   ```

**DO NOT read the LLM input file yourself. DO NOT produce the consolidation response yourself. The subagent handles this with a clean context following the /reality-check skill.**

**After resume-reality-check completes**, present a brief summary:
- Selected engines and why
- Query distribution across engines
- Reality check consolidations and any reversals
- Architecture patterns detected

**If UI mode:** Tell user "Assessment complete — check the UI for full results."

### Decision Gate: Assignment Approval

After reality check, present the final assignment to the user:
- Which engines survived consolidation
- Query distribution across engines
- Any queries that were redirected by the LLM validator

Ask: "Approve this assignment and continue to Schema Design, or modify?"

Only proceed to schema design after user approval (unless `--auto`).

### Phase 6: Schema Design (Parallel Subagents)

Launch ONE subagent per engine in a SINGLE message:

- Subagent 1: "Run /design-schema-dynamodb"
- Subagent 2: "Run /design-schema-elasticache"
- etc.

(Only for engines in `selected_engines` after reality check.)

Wait for all to complete.

**If UI mode:** Tell user "Schema designs ready — browse table definitions, access patterns, and GSIs in the UI."

### Phase 7: Synthesis

**Dispatch subagent** with task: "Run /synthesize"

### Completion

**If chat or both:**

- Show final report summary (engines, architecture recommendation, TCO)
- Show artifact location: `./artifacts/{db}/{job}/referee-synthesis/report.json`

**If UI mode:**

- Tell user "Final report available in the UI — includes engine rankings, TCO comparison, and migration roadmap."

## Subagent Dispatch Rules

1. **Every phase = fresh subagent.** No exceptions. Each gets a clean context window.
2. **Parallel phases launch in a SINGLE message** to enable true concurrency.
3. **Only dispatch for selected engines.** If triage selects 2 engines, launch 2 subagents — not 4.
4. **Subagent task descriptions are minimal.** Just the skill name and any required args. The subagent loads the skill and follows it.
5. **The orchestrator reads ONLY `.modernizer-state.json` and script stdout.** Never artifact contents.
6. **The reality check subagent is NON-OPTIONAL.** The orchestrator must NEVER attempt to read llm_input.json or write llm_responses/ itself.

## Error Handling

If any phase fails:

- Present the error to the user
- Ask: "Retry this phase, skip it, or abort?"
- If retry: dispatch a new subagent for that phase
- If skip: mark phase as "skipped" in state, continue
- If abort: stop pipeline, preserve all artifacts produced so far
