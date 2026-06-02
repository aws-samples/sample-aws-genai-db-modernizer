
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
# Install UI dependencies (first time only)
cd src/ui && npm install && cd ../..

# Start API server
STORAGE_TYPE=local ARTIFACT_ROOT=./artifacts uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 &

# Start frontend
cd src/ui && REACT_APP_API_URL=http://localhost:8000/api/v1/ npm start &
```

Wait a few seconds for both to start, then tell the user:

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
- Reads `llm_requests/` files
- Produces LLM responses
- Reads `output_schema` contents

## Pipeline

### Phase 1: Collect

**Dispatch subagent** with task: "Run /collect with file `{collector_file}`"

This creates `.modernizer-state.json` with `job_id`, `database_name`, and `phase_status`.

**If UI mode:** Tell user "Collection complete — refresh the UI to see the job."

### Phase 2: Triage

**Dispatch subagent** with task: "Run /triage"

- **If interactive:** Present engine selection from subagent output, wait for approval.
- **If --auto:** Auto-approve.

**If UI mode:** Tell user "Triage results available in the UI."

### Phase 3: Assignment

**Dispatch subagent** with task: "Run /assign"

- **If interactive:** Present query distribution from subagent output, wait for approval.
- **If --auto:** Auto-approve.

**If UI mode:** Tell user "Query assignments visible in the UI — review the distribution before continuing."

### Phase 4: Analysis (Parallel Subagents)

Read `selected_engines` from state. Launch ONE subagent per engine in a SINGLE message:

- Subagent: "Run /analyze-dynamodb"
- Subagent: "Run /analyze-elasticache"
- Subagent: "Run /analyze-documentdb"
- Subagent: "Run /analyze-opensearch"
- Subagent: "Run /analyze-aurora-postgresql"
- Subagent: "Run /analyze-aurora-mysql"

(Only for engines in `selected_engines`.)

Wait for all to complete.

**If UI mode:** Tell user "Analysis complete for all engines — check patterns and recommendations in the UI."

### Phase 5: Reality Check

**Dispatch subagent** with task: "Run /reality-check"

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

## Error Handling

If any phase fails:

- Present the error to the user
- Ask: "Retry this phase, skip it, or abort?"
- If retry: dispatch a new subagent for that phase
- If skip: mark phase as "skipped" in state, continue
- If abort: stop pipeline, preserve all artifacts produced so far
