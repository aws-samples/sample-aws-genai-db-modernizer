
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
      - Contains: filtered queries, tables, analysis results, `output_schema`
        (the exact JSON Schema your output must conform to), and — same as the
        automated Bedrock path (ADR-028) — a `deterministic_draft` plus
        `migration_strategy`, both built by the script from the shared draft
        builder (`src/tools/schema/aurora_common/draft_builder.py`).
        `deterministic_draft` already contains, per column, a resolved
        `aurora_type`, its `source_type`, and provenance
        (`script_derived`/`needs_judgment`), along with `full_ddl` and a
        `residuals` list of columns the script could not resolve confidently.
   b. Read: `src/skills/aurora_postgresql-data-modeling.md` (domain expertise
      guide — the same skill the automated designer follows)

3. **Trust the migration strategy (ADR-028 target rule)**
   Use `migration_strategy` from the request as-is — do not re-derive it:
   - `carry_over` (source is PostgreSQL): column types map 1:1; your value-add
     is Aurora optimizations, not translation.
   - `translate` (any other source engine): the draft has already translated
     every column it could resolve confidently; only `residuals` need your
     judgment.

4. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Treat
   `deterministic_draft` as authoritative and reconcile it — do not re-derive
   types from scratch:
   - For every column where the draft's `script_derived` is `true` and
     `needs_judgment` is `false`, reproduce its `aurora_type` and `source_type`
     unchanged, keeping `script_derived=true`
   - For every entry in `deterministic_draft.residuals`, use your judgment to
     pick the right Aurora type, then set that column's `needs_judgment=true`
     and `script_derived=false` in your output, and update the DDL accordingly
   - Start `generated_ddl` from `deterministic_draft.full_ddl` and only change
     the fragments needed to resolve residuals — never regenerate DDL for
     columns the draft already resolved
   - Any source feature that cannot be expressed as Aurora DDL (triggers,
     sequences, stored procedures, packages) goes in `app_layer_notes` with a
     concrete recommendation — never fabricate DDL for it
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
