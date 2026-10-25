
# /design-schema

Dispatches schema design, in parallel subagents, to exactly the engines in `schema_design_engines`, as `/modernize` Phase 6 does.

## Steps

1. **Pick the engines**
   Design exactly the engines in `schema_design_engines` from the `reality_check` status line (or the `engine_choices` line, when engine choices were settled), never re-derived from `after_distribution` (it lists owners only, so the cache-layer engine would be dropped). When neither line is in this session, read `selected_engines` from `.modernizer-state.json`: `scripts/run_assessment.py` sets it to the same list after the reality check and after each `--choose` or `--decide-for-me` (triage's selection, inside the customer's engine scope, filtered to the engines with an in-scope query, the cache layer included).

2. **Launch parallel subagents**
   Launch one subagent per engine from step 1 other than DynamoDB in a SINGLE message:
   - Each subagent invokes `/design-schema-{engine}`, with every `_` in the engine id replaced by `-` (`aurora_mysql` → `/design-schema-aurora-mysql`, `aurora_postgresql` → `/design-schema-aurora-postgresql`)
   - Every task text ends with: "Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it."

   CRITICAL: All launches MUST be in a single message for true parallelism.

   DynamoDB is always split into groups of ~20 queries regardless of total count, matching cloud production behavior. Run it exactly as `/modernize` Phase 6 does (6a-6d), with its **DynamoDB labels** (`DynamoDB design, part {P} of {T} (queries {F}-{L} of {X})`, never "group {G} draft"): run `--split` yourself first, add one **Group draft task** subagent per group to the same single message, then run `--status` and `--merge` yourself, with **Merge fix task** subagents and at most 3 `--merge` attempts. Never dispatch `/design-schema-dynamodb` as one subagent: nesting is one level deep, and its group subagents would report to you, not to it.

3. **Wait for all subagents**
   Do not end your turn while any subagent you dispatched is still running, and do not end it waiting once they have all reported: run the PE review and finish the DynamoDB merge, then the post-schema reroute, exactly as `/modernize` Phase 6 does them (6c, 6d, then 6e with the redesign of the engines it lists), the same way Bedrock mode reviews every design and reroutes after schema design. Set each non-DynamoDB engine's `phase_status.schema_design_<engine>` from its `--status` line as `/modernize` 6b does, not from the subagent's report. Then go on.

4. **Present combined summary**
   For each engine:
   - Number of tables/collections/indexes designed
   - Number of access patterns covered
   - Any unsupported patterns
   - Key trade-offs

5. **Update state**
   Only if every engine subagent completed: set `phase_status.schema_design` = "complete", `current_phase` = "synthesis". If any returned `failed` (its `phase_status.schema_design_<engine>` = "failed"), leave `phase_status.schema_design` unset and return `failed` with that engine and its errors.
