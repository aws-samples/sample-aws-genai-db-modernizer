
# /assign

Maps each query pattern to the best-fit target engine based on analysis results. Fully deterministic.

## Prerequisites

- Analysis phase complete for all selected engines

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `job_id`, `database_name`.

2. **Run assignment**

   ```bash
   uv run python scripts/run_assignment.py --job-id {job_id} --db {database_name}
   ```

3. **Present results**
   Show:
   - Query distribution per engine (count and percentage)
   - Total queries assigned
   - Any queries with low workload fit that may need review

4. **Decision gate**
   Run `uv run python scripts/start_local_ui.py --page assignments --job-id {job_id}` and ask user: "Review assignments in the UI at <ui_url> or approve here.", with the `ui_url` it printed, verbatim.
   - Poll for decision: `uv run python scripts/check_decision.py --job-id {job_id} --db {database_name} --decision assignment_approval`. Exit 0: the record is printed, the user approved. Exit 1: nothing recorded yet, keep polling. Exit 2: the record or an argument is bad (the output is `{"status": "error", "message": ...}`): stop, report the message to the user, and never keep polling.
   - Or accept "approve" from terminal

5. **Update state**
   Set `phase_status.assignment` = "complete", `current_phase` = "reality_check"
