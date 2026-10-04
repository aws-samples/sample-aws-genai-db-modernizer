
# /design-schema-dynamodb

Designs the complete DynamoDB schema: table structure, access patterns, GSIs, and trade-offs.

**ALWAYS uses the split→per-group→merge pattern** regardless of query count. This matches cloud production behavior where queries are split into groups of ~20 for parallel processing: at most 20 queries per group, and fewer when a group's input would not fit in two Read pages (`--split` halves such a group until each part fits).

## Tool Use

Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.

## Who runs which part

Nesting is one level deep: only the top-level session dispatches subagents, because a subagent's own subagents report to the top-level session, not to it.

- **Dispatched by `/modernize` or `/design-schema`** with a task that names the **Group draft task** or the **Merge fix task**: do only that section below. The dispatcher runs `--split`, `--status` and `--merge` itself and updates `.modernizer-state.json`. Do not dispatch subagents yourself.
- **Run on its own** (you are the top-level session, not a subagent): follow Steps 1-6. Step 3 may dispatch one subagent per group.
- **If you are yourself a subagent** and your task names neither section (for example "Run /design-schema-dynamodb"): follow Steps 1-6, but do not dispatch subagents yourself. Do the Group draft task for every group in turn, then merge.

## Prerequisites

- Assignment phase complete

## Steps

1. **Split queries into groups**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --split
   ```

   The script resolves the effective assignment version itself (v2 when Reality Check consolidated, else v1) and prints it as `assignment_version` in its JSON status line. Use that number as `{N}` in every path below. Do not pick the version yourself.

   This produces:
   - `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/groups_manifest.json` — group metadata
   - `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/input_group_{G}.json` — per-group input (`{G}` is the group number)

2. **Read the groups**

   The `--split` status line lists `groups`: one entry per group with `group_index`, `primary_tables`, `input_file`, `input_pages` (the Read `offset`/`limit` pages that cover `input_file`) and `draft` (paths under the artifact root, e.g. `artifacts/{database_name}/…`). Use it instead of reading the manifest.

3. **Write one draft per group**

   Run on its own, launch one subagent per group, ALL in a single message for true parallelism, each with this task text (`{INPUT_FILE}`, `{INPUT_PAGES}` and `{DRAFT}` = that group's `input_file`, `input_pages` and `draft`, `{OTHER_GROUPS}` = the other groups' `group_index` and `primary_tables`):

   ```text
   Follow /design-schema-dynamodb **Group draft task** for job_id={job_id} db={database_name} assignment_version={N} group={G}. Input: {INPUT_FILE}, in Read pages {INPUT_PAGES}. Read the input with the Read tool, one call per page (`offset`, `limit`); search with the Grep tool if this session has one, else Read the page again; write the draft with one Write tool call. Never use `sed`, `cat`, `grep`, heredocs or scripts to read, search or write files. Other groups' primary_tables: {OTHER_GROUPS}. Write only {DRAFT}. Do not run `--merge` or `--finalize` and do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.
   ```

   If you are yourself a subagent, do the Group draft task for each group yourself, one after another.

4. **Wait for every group**

   Do not end your turn while any group subagent is still running: their results come back to you, and nobody else will merge them. When all have reported, check the drafts:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --status
   ```

   - `"status": "drafts_pending"`: `drafts_missing` lists the groups without a draft and `drafts_invalid` those whose draft is not a readable JSON object. Redo the Group draft task for each of them (one fresh subagent each, or yourself if you are a subagent), **at most once** per run. If `--status` still prints `drafts_pending` after that redo, stop: set `phase_status.schema_design_dynamodb` = "failed" and return `failed` with the missing and invalid groups as your result.
   - `"status": "merge_pending"`: go to Step 5.
   - `"status": "merged"`: the last `--merge` ran on exactly these drafts and passed; go to Step 6.
   - `"status": "merge_failed"`: the last `--merge` ran on these drafts and failed (`errors`). Treat it as a `--merge` attempt that printed `validation_failed` (Step 5).
   - Any other output or a non-zero exit: set `phase_status.schema_design_dynamodb` = "failed" and return `failed` with that output.

5. **Merge group drafts**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --merge
   ```

   This produces the final merged output at the `output_path` the script prints, `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/schema_output.json`.

   The merge gives each source table one DynamoDB home. Tables from different groups that design the same source table with compatible keys (same partition/sort key names and types, the same shape, at least one identical entity, no conflicting entity, SK prefix or GSI) are merged into the first one: the absorbing group's access patterns, `hot_partition_analysis` and trade-offs are re-pointed to it, its hot-partition load is re-aggregated per table, GSI and operation, and a trade-off records the merge. When a source table still has its own entity in tables from two or more groups and no trade-off names all of them, the merge adds a review trade-off (`DynamoDB merge review: … modelled independently by design groups …`) and reports it in `warnings`. Overlaps inside one group, and denormalized copies, are not flagged.

   If it prints `"status": "drafts_pending"`, it refused and wrote nothing: the groups in `missing_groups` have no draft and those in `invalid_groups` an unreadable one. Handle it as in Step 4 (the same single redo), then re-run `--merge`. This does not count as a `--merge` attempt. Any other output than `complete`, `validation_failed` or `drafts_pending`, or a non-zero exit, fails the phase as in Step 4.

   If it prints `"status": "validation_failed"`, do the Merge fix task below with its `errors` and `warnings`, then re-run `--merge`. Make **at most 3 `--merge` attempts in total** (DynamoDB has no separate contract-validation retry at this step; the drafts' own checks are the Group draft task). The merged output keeps `validation_passed: false` until a re-run passes; merge failures clear only by re-running `--merge`, not by `--finalize`.

   `warnings` never fail the phase. If they include `DynamoDB merge review: …` overlap notes and attempts remain, do the Merge fix task for them and re-run `--merge`.

   If the third attempt still prints `"status": "validation_failed"`, stop: set `phase_status.schema_design_dynamodb` = "failed" and return `failed` with the `errors` as your result. Do not mark the phase complete.

   Do not run `--finalize` for DynamoDB; `--merge` is the final step. (DynamoDB never writes an `llm_responses/` file, so `--finalize --engine dynamodb` only reports whether the merged output exists.)

6. **Update state**
   Only after `--merge` printed `"status": "complete"`: set `phase_status.schema_design_dynamodb` = "complete"

## Group draft task

Read the input with the Read tool, one call per page (`offset`, `limit`); search with the Grep tool if this session has one, else Read the page again; write the draft with one Write tool call. Never use `sed`, `cat`, `grep`, heredocs or scripts to read, search or write files.

For one group `{G}` of version `{N}`. Write only `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/schema_draft_group_{G}.json` (and run `--check-costs` on it). Do not run `--merge` or `--finalize`, do not update `.modernizer-state.json`, and do not touch other groups' drafts. Do not dispatch subagents yourself. Return a short summary: tables, access patterns, `validation_passed`.

1. Read your group input: `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/input_group_{G}.json`
   - Read it with the Read tool, one call per page of the group's `input_pages` (`offset`, `limit`); with no pages given, read 400 lines per call (`offset` 1, 401, 801, …) until a call returns fewer lines. Each page fits one Read call, so do not page it any other way.
   - Contains: `collector_output` (the group's queries and the tables they touch, with only the fields the design uses) and `analysis_output` (the patterns, anti-patterns, aggregates and table recommendations for those queries and tables). One record per line; a `query_text` too long for one line is also given as `query_text_lines`.
2. Read the domain expertise: `src/skills/dynamodb-data-modeling.md`
3. Read the output contract: `src/contracts/dynamodb_model_output.py`
4. Design the schema following Phase 3 from the skill
5. Write the draft to: `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/schema_draft_group_{G}.json`, the whole JSON in one Write tool call (fix it later with Edit). Do not generate it with a script.
6. Run the cost / hot-partition check on that draft (the external-mode equivalent of the Bedrock agent's `compute_performances_and_costs` tool — same computation):

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --check-costs artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/schema_draft_group_{G}.json
   ```

   It prints one JSON line `{"status": "complete", "passed": ..., "results": [...], "per_table": [...], "hot_partition_findings": [...], "errors": [...]}` and does **not** modify the draft. `"status": "error"` (exit 1) means the check could not run (e.g. the path is not under the job's `schema-dynamodb/` directory).
7. Set `validation_passed` / `validation_failures` in the draft per the skill's `validation_passed` rules, using that output: the "cost check ran successfully" rule holds only when `--check-costs` returned `"passed": true`. If `"passed": false`, fix the `hot_partition_analysis` entries listed in `errors` and re-run `--check-costs`; if they cannot be fixed, set `validation_passed: false` and add each error to `validation_failures`.

Key rules for each group draft:

- Design only the tables and queries assigned to this engine; finalize rejects others. Reference only the tables and `query_id`s in the group's `collector_output` (`--merge` checks the whole design against the assignment)
- `access_patterns[].pattern_id` prefixed with `DDB-AP-` (sequential within group; when group IDs collide, `--merge` renumbers them `DDB-AP-1..N` across groups and rewrites the `DDB-AP-<n>` mentions in each group's draft text and design trace to match)
- `table_definitions[].gsis[].partition_key` and `sort_key` must be LISTS of KeyDefinition
- Base table `partition_key` and `sort_key` are single KeyDefinition objects
- `trade_offs` must be objects with: description, impact, source_tables, target_tables, query_ids, engine
- `unsupported_patterns` for text search (LIKE '%...%') and aggregation (COUNT, GROUP BY) queries
- Include `hot_partition_analysis` for each table
- Set `validation_passed` to true only if all the skill's checks pass, including `--check-costs` returning `"passed": true` (step 6)
- Give each source table one home. Groups run in parallel, so other groups may design the same source table (your task lists the other groups' `primary_tables`). Name key attributes after the column that identifies the owning entity (e.g. `post_id` for a post and its meta, `term_id` for a term and its taxonomy rows), so the same rows designed in two groups get the same keys, entity types and SK templates and merge automatically. If another table needs a source table's own rows under a different key schema, add a trade-off whose `source_tables` include it and whose `target_tables` name every table that holds it, saying which access patterns need each table and how writes keep the copies in sync. Denormalized copies (attributes copied from another table, with no entity of their own) need no such trade-off

## Merge fix task

For the `errors` and `warnings` a `--merge` attempt printed. Edit only the `schema_draft_group_*.json` files (re-run `--check-costs` on a draft whose tables or `hot_partition_analysis` you changed). Do not run `--merge` or `--finalize` and do not update `.modernizer-state.json`: whoever dispatched you (or Step 5) re-runs `--merge`. Do not dispatch subagents yourself. Return a short summary of what you changed per error or warning.

For each entry in `errors`:

- `Out of scope for dynamodb: …` names a source table or query ID the assignment gives another engine (or puts out of scope), and where the design references it. Remove those from the group drafts that reference them.
- `DynamoDB merge: …` is a true conflict: one table name used for two different designs, or the same entity type for the same source table with contradictory PK/SK templates in different groups. Fix the group drafts: rename one of the tables, or use one PK/SK template for those rows (or rename one entity type).

`warnings` never fail the phase. They are scope warnings (query IDs listed only in `unsupported_patterns` that are not in this engine's scope) and `DynamoDB merge review: …` overlap notes. Reconcile overlap notes if you can (the next `--merge`, within the same 3 attempts, picks the change up): consolidate the tables into the first one listed (move the entities, attributes and GSIs, point `access_patterns[].table_name` and `hot_partition_analysis[].table_name` there, delete the duplicate), or keep both and add a trade-off to one draft that names the source table and every listed table and explains why. If you can't, leave the note: it stays in the design's trade-offs for review before migration.
