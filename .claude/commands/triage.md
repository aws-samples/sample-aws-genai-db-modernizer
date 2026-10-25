
# /triage

Analyzes workload signals and selects candidate target engines. Fully deterministic (no LLM).

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.

## Prerequisites

- Collector phase complete (`.modernizer-state.json` exists with `job_id` and `database_name`)
- Optional: `--target-databases <ids>`, comma-separated engine ids (for example `dynamodb,aurora_postgresql`): the customer's engine scope, the only engines the plan may route to. Never ask for it: absent means the job keeps the scope it recorded (none: every engine).

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `job_id` and `database_name`.

2. **Run triage**

   ```bash
   uv run python scripts/run_triage.py --job-id {job_id} --db {database_name}
   ```

   When `--target-databases` was given, add `--target-databases <ids>` to the command: the script records it as the job's engine scope before triage runs. A JSON line with `"status": "error"` (a refused scope included) is the reason to report; stop there.

3. **Present results**
   Show:
   - The customer's engine scope, when the line has `engine_scope`: "Engines in scope: <names> (customer's choice)", each id by its AWS service name
   - Selected engines with signals detected
   - Every skipped engine with its reason from `skipped_reasons`, including each one the scope left out ("Not in the customer's engine scope")
   - Deferred engines (need more data)

4. **Decision gate**
   Ask user: "Do you want to proceed with these engines, or modify the selection?"
   - If user approves: continue
   - If user modifies: note the modified selection. Refuse any engine outside `engine_scope` (when the line has one): say it is outside the customer's engine scope, that a job's scope is fixed once it starts, and that a new job whose scope adds it can use it; never add it to the selection (`run_analysis.py` refuses it too). Engines the scope leaves out never come back through this gate.

5. **Update state**
   Update `.modernizer-state.json`:
   - Set `selected_engines` to the approved list
   - Set `phase_status.triage` = "complete"
   - Set `current_phase` = "analysis"
