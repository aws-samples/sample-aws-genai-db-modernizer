
# /analyze

Runs analysis for all selected engines. This phase is fully deterministic (no LLM needed) and runs fast.

## CRITICAL RULE

**Do NOT launch subagents for analysis.** Analysis is deterministic and runs in seconds per engine. Just run the scripts sequentially or let the orchestrator handle parallelism internally.

## Steps

1. **Read state**
   Read `.modernizer-state.json` to get `job_id`, `database_name`, and `selected_engines`.

2. **Run analysis for all engines**
   For each engine in `selected_engines`, run:

   ```bash
   uv run python scripts/run_analysis.py --job-id {job_id} --db {database_name} --engine {engine} --llm-mode none
   ```

   Run them sequentially. They each complete in under a second.

3. **Present combined summary**
   For each engine, show:
   - Status (complete/failed)
   - Number of table recommendations
   - Patterns detected
   - Anti-patterns found

4. **Update state**
   Set `phase_status.analysis` = "complete"
