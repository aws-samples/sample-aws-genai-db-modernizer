
# /design-schema-elasticache

Designs Redis key patterns, data structures, and TTL policies.

## Prerequisites

- Assignment phase complete

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine elasticache --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `.artifacts/{database_name}/{job_id}/llm_requests/schema_design_elasticache.json`
      - Contains: filtered queries, tables, analysis results, and `output_schema` (the exact JSON Schema your output must conform to)
   b. Read: `src/skills/elasticache-data-modeling.md` (domain expertise guide)

3. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Key principles:
   - Key naming patterns with appropriate data structures (hash, sorted_set, list, etc.)
   - TTL policies per key pattern
   - Cache invalidation strategies
   - Pattern IDs must be prefixed with `EC-AP-`

4. **Write, validate, persist**
   Write to `.artifacts/{database_name}/{job_id}/llm_responses/schema_design_elasticache.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine elasticache --finalize
   ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix and retry up to 3 times.

5. **Update state**
   Set `phase_status.schema_design_elasticache` = "complete"
