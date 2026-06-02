
# /triage

Analyzes workload signals and selects candidate target engines. Fully deterministic (no LLM).

## Prerequisites

- Collector phase complete (`.modernizer-state.json` exists with `job_id` and `database_name`)

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `job_id` and `database_name`.

2. **Run triage**

   ```bash
   uv run python scripts/run_triage.py --job-id {job_id} --db {database_name}
   ```

3. **Present results**
   Show:
   - Selected engines with signals detected
   - Any engines skipped and why
   - Deferred engines (need more data)

4. **Decision gate**
   Ask user: "Do you want to proceed with these engines, or modify the selection?"
   - If user approves: continue
   - If user modifies: note the modified selection

5. **Update state**
   Update `.modernizer-state.json`:
   - Set `selected_engines` to the approved list
   - Set `phase_status.triage` = "complete"
   - Set `current_phase` = "analysis"
