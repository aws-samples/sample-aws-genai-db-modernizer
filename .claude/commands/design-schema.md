
# /design-schema

Dispatches schema design to all selected engines in parallel using subagents.

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `selected_engines`.

2. **Launch parallel subagents**
   Launch one subagent per selected engine other than DynamoDB in a SINGLE message:
   - Each subagent invokes `/design-schema-{engine}`, with every `_` in the engine id replaced by `-` (`aurora_mysql` → `/design-schema-aurora-mysql`, `aurora_postgresql` → `/design-schema-aurora-postgresql`)
   - Every task text ends with: "Do not dispatch subagents yourself. Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains."

   CRITICAL: All launches MUST be in a single message for true parallelism.

   DynamoDB is always split into groups of ~20 queries regardless of total count, matching cloud production behavior. Run it exactly as `/modernize` Phase 6 does (6a-6c): run `--split` yourself first, add one **Group draft task** subagent per group to the same single message, then run `--status` and `--merge` yourself, with **Merge fix task** subagents and at most 3 `--merge` attempts. Never dispatch `/design-schema-dynamodb` as one subagent: nesting is one level deep, and its group subagents would report to you, not to it.

3. **Wait for all subagents**
   Do not end your turn while any subagent you dispatched is still running, and do not end it waiting once they have all reported: finish the DynamoDB merge, then go on.

4. **Present combined summary**
   For each engine:
   - Number of tables/collections/indexes designed
   - Number of access patterns covered
   - Any unsupported patterns
   - Key trade-offs

5. **Update state**
   Only if every engine subagent completed: set `phase_status.schema_design` = "complete", `current_phase` = "synthesis". If any returned `failed` (its `phase_status.schema_design_<engine>` = "failed"), leave `phase_status.schema_design` unset and return `failed` with that engine and its errors.
