
# /design-schema-opensearch

Designs OpenSearch index mappings, data streams, and ISM policies.

## Prerequisites

- Assignment phase complete

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine opensearch --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `.artifacts/{database_name}/{job_id}/llm_requests/schema_design_opensearch.json`
      - Contains: filtered queries, tables, analysis results, and `output_schema` (the exact JSON Schema your output must conform to)
   b. Read: `src/skills/opensearch-index-modeling.md` (domain expertise guide)

3. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Key principles:
   - Search-workload tables become IndexMapping entries
   - Time-series-workload tables become DataStreamConfig entries with ISM policies
   - Field mappings with appropriate analyzers
   - Pattern IDs must be prefixed with `OS-AP-`

4. **Write, validate, persist**
   Write to `.artifacts/{database_name}/{job_id}/llm_responses/schema_design_opensearch.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine opensearch --finalize
   ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix and retry up to 3 times.

5. **Update state**
   Set `phase_status.schema_design_opensearch` = "complete"
