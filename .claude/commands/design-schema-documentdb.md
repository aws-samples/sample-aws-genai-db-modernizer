
# /design-schema-documentdb

Designs DocumentDB collections: embedding decisions, index strategy, and access patterns.

## Tool Use

Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.

## Prerequisites

- Assignment phase complete

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine documentdb --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `artifacts/{database_name}/{job_id}/llm_requests/schema_design_documentdb.json`
      - Contains: filtered queries, tables, analysis results, and `output_schema` (the exact JSON Schema your output must conform to)
   b. Read: `src/skills/documentdb-data-modeling.md` (domain expertise guide)

3. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Key principles:
   - Design only the tables and queries assigned to this engine; finalize rejects others. The request's `collector_output` holds exactly that scope: reference only its tables and `query_id`s
   - Embedding vs referencing for each parent-child relationship
   - Index strategy (compound, text, partial, unique)
   - Document size estimates (must stay under 16MB limit)
   - Pattern IDs must be prefixed with `DOC-AP-`

4. **Write, validate, persist**
   Write to `artifacts/{database_name}/{job_id}/llm_responses/schema_design_documentdb.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine documentdb --finalize
   ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix and retry up to 3 times.

   A `"status": "validation_failed"` with an `output_path` means the design is contract-valid but out of scope: each error names a source table or query ID assigned to another engine (or out of scope). Remove those from the design, rewrite the response file and re-run `--finalize`; the written output keeps `validation_passed: false` until it passes.

5. **Update state**
   Set `phase_status.schema_design_documentdb` = "complete"
