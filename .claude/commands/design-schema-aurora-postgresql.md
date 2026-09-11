
# /design-schema-aurora-postgresql

Designs the Aurora PostgreSQL target schema: resolved table/column types, DDL,
app-layer notes, and Aurora-specific optimizations.

Single-pass (ADR-028) — no `run_schema_split` / `run_schema_merge`, unlike
DynamoDB. Aurora runs once, like OpenSearch.

## Prerequisites

- Assignment phase complete

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine aurora_postgresql --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `.artifacts/{database_name}/{job_id}/llm_requests/schema_design_aurora_postgresql.json`
      - Contains: filtered queries, tables, analysis results, and `output_schema` (the exact JSON Schema your output must conform to)
   b. Read: `src/skills/aurora_postgresql-data-modeling.md` (domain expertise guide)

3. **Determine the migration strategy (ADR-028 target rule)**
   - Source engine is PostgreSQL → **carry_over**: column types map 1:1; your
     value-add is Aurora optimizations, not translation.
   - Any other source engine (MySQL, Oracle, SQL Server, ...) → **translate**:
     full type/DDL translation from the source's normalized types.

4. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Key principles:
   - Resolve every column to a concrete `aurora_type`, carrying `source_type`
     and provenance (`script_derived`, `needs_judgment`)
   - Generate complete DDL (`CREATE TABLE` / indexes / foreign keys) in `generated_ddl`
   - For `translate` strategy, any source feature that cannot be expressed as
     Aurora DDL (triggers, sequences, stored procedures, packages) goes in
     `app_layer_notes` with a concrete recommendation — never fabricate DDL for it
   - Add Aurora-specific `optimizations` driven by the query patterns:
     partitioning for large hot tables, added indexes for frequent unindexed
     filter/sort columns, read-replica routing for read-heavy patterns,
     I/O-Optimized for write-heavy throughput
   - Record the significant decisions in `trade_offs`, each with a
     `description` and an `impact` written for a CTO
   - No pattern-ID prefix — this contract is table-shaped, not access-pattern-shaped

5. **Write, validate, persist**
   Write to `.artifacts/{database_name}/{job_id}/llm_responses/schema_design_aurora_postgresql.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine aurora_postgresql --finalize
   ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix and retry up to 3 times.

6. **Update state**
   Set `phase_status.schema_design_aurora_postgresql` = "complete"
