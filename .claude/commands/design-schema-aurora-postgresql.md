
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
      - In reading order it contains `migration_strategy`, `draft_fingerprint`
        (checked by `--finalize`; do not copy it), `input_pages` (the Read
        pages that cover the file), `output_schema` (the JSON
        Schema of the delta you write, `AuroraDesignDeltaContract`) and
        `design_view`, a compact view of the deterministic draft built by the
        script from the shared draft builder
        (`src/tools/schema/aurora_common/draft_builder.py`), the same draft the
        automated Bedrock path uses (ADR-028):
        - `residual_types`: the residual columns grouped by source data type,
          with a count and examples
        - `hot_queries`: the busiest in-scope queries by database load and by
          call rate, with the tables, the filter/sort columns, rows examined,
          timings and index-miss counts
        - `analysis` and `source_features` (triggers, procedures, views)
        - `tables` (last, one table per line): `row_count`, `read_qps` /
          `write_qps`, `primary_key`, `columns` (one line each:
          `name TYPE [NOT NULL] [AI] [DEFAULT x]`, then `(residual; source t)`
          when the script could not resolve the type, or `(source t)` when the
          source type differs from the draft type), `indexes`
          (`name [UNIQUE] (columns)`) and `foreign_keys` (`col -> table(col)`)
      - The draft itself and the collector output are not in the file, and you
        do not need them.
      - First Read it with `offset: 1` and `limit: 40` to see `input_pages`
        (one page per line, right after `draft_fingerprint`). Then read
        exactly the pages listed in `input_pages`, in order: one Read call per page with
        that page's `offset` and `limit` (each page is sized to fit in one
        Read). Every page's `section` says what it holds. To find one table
        or section again, search the file with
        `uv run python scripts/search_artifacts.py '"table_name": "orders"' <request file>`.
        Never read it with `cat`, `sed`, `jq`, `grep` or a script.
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
   - `tables[].add_indexes` and `tables[].modify_indexes`: structured index
     entries `{"index_name": "idx_orders_status", "columns": ["status"], "unique": false}`
     on that table's columns; the script renders and quotes the DDL. Use them
     only where `hot_queries` justify it, e.g. a frequent filter/sort column
     without an index. `modify_indexes` replaces the draft index with the same
     `index_name`; `tables[].remove_indexes` lists names to drop. Both must
     name an index listed in that table's `indexes`.
     Aurora PostgreSQL also takes `method` (`btree`, `hash`, `gin`, `gist`,
     `brin`), `include` (covering columns) and `where` (a partial-index
     predicate over this table's columns: comparisons, `IS [NOT] NULL`,
     `IN (...)`, `AND`/`OR`/`NOT`, numbers, `'strings'`).
   - Types (`aurora_type`) are plain SQL types only: `BIGINT`, `VARCHAR(255)`,
     `NUMERIC(10,2)`, `TIMESTAMP WITH TIME ZONE`, `TEXT[]`. No
     constraints, defaults or anything else; `--finalize` rejects them
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
         "add_indexes": [{"index_name": "idx_orders_status", "columns": ["status"]}],
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

   If validation fails, the errors tell you exactly which entries are wrong (an unknown table, column or index, a type that is not a plain SQL type, a malformed delta). Fix the response file and re-run `--finalize`. Make **at most 3 `--finalize` attempts in total**, shared between contract-validation failures and scope failures.

   On success `--finalize` also prints a `delta_summary` and may print `warnings`; they do not fail the phase. A warning that residual columns keep the draft's fallback type (`residuals_unresolved` above 0) means a source type has no `type_rules` entry; add one if you can decide it and re-run `--finalize` (this counts as an attempt). A `draft_fingerprint` mismatch error means the inputs changed after step 1: re-run step 1 and rewrite the delta from the new request.

   A `"status": "validation_failed"` with an `output_path` means the design is contract-valid but out of scope: each error names a source table or query ID assigned to another engine (or out of scope) and where the design references it. Remove those from the design, rewrite the response file and re-run `--finalize`; the written output keeps `validation_passed: false` until a re-run passes.

   If the third attempt still prints `"status": "validation_failed"`, stop: set `phase_status.schema_design_aurora_postgresql` = "failed" and return `failed` with the `errors` as your result. Do not mark the phase complete.

6. **Update state**
   Only after `--finalize` printed `"status": "complete"`: set `phase_status.schema_design_aurora_postgresql` = "complete"
