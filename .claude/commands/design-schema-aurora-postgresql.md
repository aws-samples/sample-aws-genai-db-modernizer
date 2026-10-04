
# /design-schema-aurora-postgresql

Designs the Aurora PostgreSQL target schema: resolved table/column types, DDL,
app-layer notes, and Aurora-specific optimizations.

Single-pass (ADR-028) — no `run_schema_split` / `run_schema_merge`, unlike
DynamoDB. Aurora runs once, like OpenSearch.

The design is a 1:1 carry-over of a deterministic draft plus your Aurora-level
decisions, so you write **only a delta** against that draft. `--finalize`
rebuilds the draft, merges your delta into it, regenerates the DDL and
validates the full output contract and the scope. Your output stays small
however many tables and columns the schema has.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`.

## Prerequisites

- Assignment phase complete

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine aurora_postgresql --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `artifacts/{database_name}/{job_id}/llm_requests/schema_design_aurora_postgresql.json`
      - Contains `migration_strategy`, `output_schema` (the JSON Schema of the
        delta you write, `AuroraDesignDeltaContract`) and `design_view`, a
        compact view of the deterministic draft built by the script from the
        shared draft builder (`src/tools/schema/aurora_common/draft_builder.py`),
        the same draft the automated Bedrock path uses (ADR-028):
        - `tables`: per table, `row_count`, `read_qps` / `write_qps`,
          `primary_key`, `columns` (one `name TYPE` line each, marked
          `(residual; source <data_type>)` when the script could not resolve
          the type confidently) and the draft's `indexes` statements
        - `residual_types`: the residual columns grouped by source data type,
          with a count and examples
        - `hot_queries`: the busiest in-scope queries, with the tables and the
          filter/sort columns they use
        - `analysis` and `source_features` (triggers, procedures, views)
      - The draft itself and the collector output are not in the file, and you
        do not need them.
      - On a large schema the file still runs to thousands of lines. Read it in
        chunks with the Read tool's `offset` and `limit`, and use Grep to jump
        to a section (`"residual_types"`, `"hot_queries"`) or a table
        (`"table_name": "orders"`). Never read it with `cat`, `sed`, `jq` or a
        script.
   b. Read: `src/skills/aurora_postgresql-data-modeling.md` (domain expertise
      guide — the same skill the automated designer follows)

3. **Trust the migration strategy (ADR-028 target rule)**
   Use `migration_strategy` from the request as-is — do not re-derive it:
   - `carry_over` (source is PostgreSQL): column types map 1:1; your value-add
     is Aurora optimizations, not translation.
   - `translate` (any other source engine): the draft has already translated
     every column it could resolve confidently; only residuals need your
     judgment.

4. **Design the delta**
   Design only the tables and queries assigned to this engine: the tables in
   `design_view.tables`. A table name outside that list fails `--finalize`.

   Write JSON conforming to `output_schema`. List only what changes: every
   table, column, key, index and foreign key you do not mention carries over
   from the draft unchanged. Never copy tables, columns or DDL into the delta.
   - `"delta_version": "1.0"` (required)
   - `type_rules`: resolve residuals by source type, one entry per
     `residual_types[].source_data_type` you decide, e.g.
     `{"source_data_type": "integer", "aurora_type": "INTEGER"}`. A rule
     applies to every residual column of that source type (under `carry_over` the source type is normally the answer: `integer` → `INTEGER`, `jsonb` → `JSONB`)
   - `tables[].column_types`: only for a column that needs a different type
     than its rule gives (e.g. a sized `VARCHAR(n)`), or a draft type you
     change on purpose. Columns set here or by a rule are recorded with
     `needs_judgment=true` and `script_derived=false`
   - `tables[].add_indexes` (full `CREATE [UNIQUE] INDEX "name" ON "table" (...)`
     statements on that table), `tables[].modify_indexes`
     (`{"index_name", "statement"}`) and `tables[].remove_indexes` (index
     names): only where `hot_queries` justify it, e.g. a frequent filter/sort
     column without an index. `modify`/`remove` must name an index listed in
     that table's `indexes`
   - `optimizations`: partitioning for large hot tables, read-replica routing
     for read-heavy patterns, I/O-Optimized for write-heavy throughput,
     index recommendations you do not express as DDL
   - `app_layer_notes`: any source feature that cannot be expressed as Aurora
     DDL (triggers, sequences, stored procedures, packages) with a concrete recommendation —
     never fabricate DDL for it
   - `trade_offs`: the significant decisions, each with a `description` and an
     `impact` written for a CTO
   - No pattern-ID prefix — this contract is table-shaped, not access-pattern-shaped

   Example:

   ```json
   {
     "delta_version": "1.0",
     "type_rules": [{"source_data_type": "bigint", "aurora_type": "BIGINT"}],
     "tables": [
       {
         "table_name": "orders",
         "add_indexes": ["CREATE INDEX \"idx_orders_status\" ON \"orders\" (\"status\")"],
         "column_types": [{"column": "status", "aurora_type": "VARCHAR(16)"}]
       }
     ],
     "optimizations": [],
     "app_layer_notes": [],
     "trade_offs": [{"description": "...", "impact": "..."}]
   }
   ```

5. **Write, validate, persist**
   Write to `artifacts/{database_name}/{job_id}/llm_responses/schema_design_aurora_postgresql.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine aurora_postgresql --finalize
   ```

   If validation fails, the errors tell you exactly which entries are wrong (an unknown table, column or index, an index statement on another table, a malformed delta). Fix the response file and re-run `--finalize`. Make **at most 3 `--finalize` attempts in total**, shared between contract-validation failures and scope failures.

   On success `--finalize` also prints a `delta_summary`. `residuals_unresolved` above 0 means some residual columns kept the draft's fallback type; add a `type_rules` entry for their source type if you can decide it, then re-run `--finalize` (this counts as an attempt).

   A `"status": "validation_failed"` with an `output_path` means the design is contract-valid but out of scope: each error names a source table or query ID assigned to another engine (or out of scope) and where the design references it. Remove those from the design, rewrite the response file and re-run `--finalize`; the written output keeps `validation_passed: false` until a re-run passes.

   If the third attempt still prints `"status": "validation_failed"`, stop: set `phase_status.schema_design_aurora_postgresql` = "failed" and return `failed` with the `errors` as your result. Do not mark the phase complete.

6. **Update state**
   Only after `--finalize` printed `"status": "complete"`: set `phase_status.schema_design_aurora_postgresql` = "complete"
