
# /design-schema-dynamodb

Designs the complete DynamoDB schema: table structure, access patterns, GSIs, and trade-offs.

**ALWAYS uses the split→per-group→merge pattern** regardless of query count. This matches cloud production behavior where queries are split into groups of ~20 for parallel processing.

## Prerequisites

- Assignment phase complete

## Steps

1. **Split queries into groups**

   ```bash
   uv run python -c "
   from src.storage.local_store import LocalArtifactStore
   from src.agents.schema_design.handler import run_schema_split
   store = LocalArtifactStore(base_dir='./artifacts')
   run_schema_split(job_id='{job_id}', database_name='{database_name}', target_type='dynamodb', store=store, assignment_version=1)
   "
   ```

   This produces:
   - `artifacts/{database_name}/{job_id}/schema-dynamodb/v1/groups_manifest.json` — group metadata
   - `artifacts/{database_name}/{job_id}/schema-dynamodb/v1/input_group_{N}.json` — per-group input

2. **Read the manifest**

   Read `artifacts/{database_name}/{job_id}/schema-dynamodb/v1/groups_manifest.json` to get the list of groups.

3. **Launch parallel subagents — one per group**

   For each group in the manifest, launch a subagent (ALL in a single message for true parallelism). Each subagent:

   a. Reads its group input: `artifacts/{database_name}/{job_id}/schema-dynamodb/v1/input_group_{N}.json`
      - Contains: `collector_output` (filtered queries + tables) and `analysis_output`
   b. Reads the domain expertise: `src/skills/dynamodb-data-modeling.md`
   c. Reads the output contract: `src/contracts/dynamodb_model_output.py`
   d. Designs the schema following Phase 3 from the skill
   e. Writes output to: `artifacts/{database_name}/{job_id}/schema-dynamodb/v1/schema_draft_group_{N}.json`

   Key rules for each group output:
   - `access_patterns[].pattern_id` prefixed with `DDB-AP-` (sequential within group)
   - `table_definitions[].gsis[].partition_key` and `sort_key` must be LISTS of KeyDefinition
   - Base table `partition_key` and `sort_key` are single KeyDefinition objects
   - `trade_offs` must be objects with: description, impact, source_tables, target_tables, query_ids, engine
   - `unsupported_patterns` for text search (LIKE '%...%') and aggregation (COUNT, GROUP BY) queries
   - Include `hot_partition_analysis` for each table
   - Set `validation_passed` to true if all checks pass

4. **Wait for all subagents to complete**

5. **Merge group drafts**

   ```bash
   uv run python -c "
   from src.storage.local_store import LocalArtifactStore
   from src.agents.schema_design.handler import run_schema_merge
   store = LocalArtifactStore(base_dir='./artifacts')
   run_schema_merge(job_id='{job_id}', database_name='{database_name}', target_type='dynamodb', store=store, assignment_version=1)
   "
   ```

   This produces the final merged output at `artifacts/{database_name}/{job_id}/schema-dynamodb/v1/schema_output.json`.

6. **Update state**
   Set `phase_status.schema_design_dynamodb` = "complete"
