
# /design-schema-documentdb

Designs DocumentDB collections: embedding decisions, index strategy, and access patterns.

## Prerequisites

- Assignment phase complete

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine documentdb --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `.artifacts/{database_name}/{job_id}/llm_requests/schema_design_documentdb.json`
      - Contains: filtered queries, tables, analysis results, and `output_schema` (the exact JSON Schema your output must conform to)
   b. Read: `src/skills/documentdb-data-modeling.md` (domain expertise guide)

3. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Key principles:
   - Embedding vs referencing for each parent-child relationship
   - Index strategy (compound, text, partial, unique)
   - Document size estimates (must stay under 16MB limit)
   - Pattern IDs must be prefixed with `DOC-AP-`

4. **Write, validate, persist**
   Write to `.artifacts/{database_name}/{job_id}/llm_responses/schema_design_documentdb.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine documentdb --finalize
   ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix and retry up to 3 times.

5. **Update state**
   Set `phase_status.schema_design_documentdb` = "complete"
