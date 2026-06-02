
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
   - Any queries with low confidence that may need review

4. **Decision gate**
   Ask user: "Review assignments in the UI at <http://localhost:3000/assignments> or approve here."
   - Poll for decision: `uv run python scripts/check_decision.py --job-id {job_id} --db {database_name} --decision assignment_approval`
   - Or accept "approve" from terminal

5. **Update state**
   Set `phase_status.assignment` = "complete", `current_phase` = "reality_check"
