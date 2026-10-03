
# /design-schema-dynamodb

Designs the complete DynamoDB schema: table structure, access patterns, GSIs, and trade-offs.

**ALWAYS uses the split→per-group→merge pattern** regardless of query count. This matches cloud production behavior where queries are split into groups of ~20 for parallel processing.

## Tool Use

Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.

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

2. **Read the manifest**

   Read `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/groups_manifest.json` to get the list of groups.

3. **Launch parallel subagents — one per group**

   For each group in the manifest, launch a subagent (ALL in a single message for true parallelism). Each subagent:

   a. Reads its group input: `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/input_group_{G}.json`
      - Contains: `collector_output` (filtered queries + tables) and `analysis_output`
   b. Reads the domain expertise: `src/skills/dynamodb-data-modeling.md`
   c. Reads the output contract: `src/contracts/dynamodb_model_output.py`
   d. Designs the schema following Phase 3 from the skill
   e. Writes output to: `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/schema_draft_group_{G}.json`
   f. Runs the cost / hot-partition check on that draft (the external-mode equivalent of the Bedrock agent's `compute_performances_and_costs` tool — same computation):

      ```bash
      uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --check-costs artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/schema_draft_group_{G}.json
      ```

      It prints one JSON line `{"status": "complete", "passed": ..., "results": [...], "per_table": [...], "hot_partition_findings": [...], "errors": [...]}` and does **not** modify the draft. `"status": "error"` (exit 1) means the check could not run (e.g. the path is not under the job's `schema-dynamodb/` directory).
   g. Sets `validation_passed` / `validation_failures` in the draft per the skill's `validation_passed` rules, using that output: the "cost check ran successfully" rule holds only when `--check-costs` returned `"passed": true`. If `"passed": false`, fix the `hot_partition_analysis` entries listed in `errors` and re-run `--check-costs`; if they cannot be fixed, set `validation_passed: false` and add each error to `validation_failures`.

   Key rules for each group output:
   - Design only the tables and queries assigned to this engine; finalize rejects others. Reference only the tables and `query_id`s in the group's `collector_output` (`--merge` checks the whole design against the assignment)
   - `access_patterns[].pattern_id` prefixed with `DDB-AP-` (sequential within group)
   - `table_definitions[].gsis[].partition_key` and `sort_key` must be LISTS of KeyDefinition
   - Base table `partition_key` and `sort_key` are single KeyDefinition objects
   - `trade_offs` must be objects with: description, impact, source_tables, target_tables, query_ids, engine
   - `unsupported_patterns` for text search (LIKE '%...%') and aggregation (COUNT, GROUP BY) queries
   - Include `hot_partition_analysis` for each table
   - Set `validation_passed` to true only if all the skill's checks pass, including `--check-costs` returning `"passed": true` (step f)

4. **Wait for all subagents to complete**

5. **Merge group drafts**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --merge
   ```

   This produces the final merged output at the `output_path` the script prints, `artifacts/{database_name}/{job_id}/schema-dynamodb/v{N}/schema_output.json`.

   If it prints `"status": "validation_failed"`, each entry in `errors` names a source table or query ID the assignment gives another engine (or puts out of scope), and where the design references it. Remove those from the group drafts that reference them and re-run `--merge` (up to 3 times); the merged output keeps `validation_passed: false` until it passes.

   Do not run `--finalize` for DynamoDB; `--merge` is the final step. (DynamoDB never writes an `llm_responses/` file, so `--finalize --engine dynamodb` only reports whether the merged output exists.)

6. **Update state**
   Set `phase_status.schema_design_dynamodb` = "complete"
