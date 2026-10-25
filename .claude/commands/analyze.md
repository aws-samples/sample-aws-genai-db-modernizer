
# /analyze

Runs analysis for each engine triage selected (`selected_engines`), which never includes an engine outside the customer's engine scope. This phase is fully deterministic (no LLM needed) and runs fast.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.

## CRITICAL RULE

**Do NOT launch subagents for analysis.** Analysis is deterministic and runs in seconds per engine. Just run the scripts sequentially or let the orchestrator handle parallelism internally.

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id`, `database_name`, and `selected_engines`.

2. **Run analysis for each selected engine**
   For each engine in `selected_engines`, run:

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine {engine} --llm-mode none
   ```

   Run them sequentially. They each complete in under a second. The script refuses an engine outside the job's engine scope (`"status": "error"`): report its message and leave that engine out.

3. **Present combined summary**
   For each engine, show:
   - Status (complete/failed)
   - Number of table recommendations
   - Patterns detected
   - Anti-patterns found

4. **Update state**
   Set `phase_status.analysis` = "complete"
