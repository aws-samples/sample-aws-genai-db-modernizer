
# /design-schema

Dispatches schema design to all selected engines in parallel using subagents.

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `selected_engines`.

2. **Launch parallel subagents**
   Launch one subagent per selected engine in a SINGLE message:
   - Each subagent invokes `/design-schema-{engine}`

   CRITICAL: All launches MUST be in a single message for true parallelism.

   NOTE: DynamoDB uses an internal split→per-group→merge pattern with its own
   nested subagents. This is mandatory — it always splits queries into groups
   of ~20 regardless of total count, matching cloud production behavior.

3. **Wait for all subagents**

4. **Present combined summary**
   For each engine:
   - Number of tables/collections/indexes designed
   - Number of access patterns covered
   - Any unsupported patterns
   - Key trade-offs

5. **Update state**
   Set `phase_status.schema_design` = "complete", `current_phase` = "synthesis"
