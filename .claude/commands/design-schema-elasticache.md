
# /design-schema-elasticache

Designs Redis key patterns, data structures, and TTL policies.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine elasticache --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `artifacts/{database_name}/{job_id}/llm_requests/schema_design_elasticache.json`
      - Contains: filtered queries, tables, analysis results, and `output_schema` (the exact JSON Schema your output must conform to)
      - Read the whole file in the step 1 status line's `input_pages` (each is a Read `offset`/`limit`), every page in order; when `oversized` is true there are more than `input_page_budget` of them, and none may be skipped
   b. Read: `src/skills/elasticache-data-modeling.md` (domain expertise guide)

3. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Key principles:
   - Example keys and values (`example_key`, `example_value`, `command_example`): Example values are obvious placeholders, never real-looking data: `<token>`, `<email>` or `user@example.com` for a secret or personal value, and IP addresses only from the documentation ranges 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 and 2001:db8::/32. A column that holds a credential (a `*_token`, `token_hash`, `*_password`, `*_secret` or `*_key` column) gets a placeholder such as `<token>`, `<password>` or `<secret>`, never a made-up value; a number, a date or a flag keeps its own type.
   - Design only the tables and queries assigned to this engine; finalize rejects others. The request's `collector_output` holds exactly that scope: reference only its tables and `query_id`s
   - ElastiCache is a cache layer and owns no query: its scope is the cache overlay, the hot reads (`cache_engine: "elasticache"`) that another engine owns. Design cache-aside keys for those reads; the owner engine serves every miss and every write. A read you cannot serve goes in `unsupported_patterns`: after schema design its cache overlay is dropped and its owner is unchanged
   - `collector_output.cache_invalidation_context.write_queries` lists the owners' writes on the cached tables (`context_only: true`). Use their `query_id` values only in `cache_invalidation[].source_write_query_ids`; they are not design scope, so never design keys or access patterns for them
   - Key naming patterns with appropriate data structures (hash, sorted_set, list, etc.)
   - TTL policies per key pattern
   - Cache invalidation strategies
   - Pattern IDs must be prefixed with `EC-AP-`

4. **Write, validate, persist**
   Write to `artifacts/{database_name}/{job_id}/llm_responses/schema_design_elasticache.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine elasticache --finalize
   ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix the response file and re-run `--finalize`. Make **at most 3 `--finalize` attempts in total**, shared between contract-validation failures and scope failures.

   A `"status": "validation_failed"` with an `output_path` means the design is contract-valid but out of scope: each error names a source table or query ID assigned to another engine (or out of scope) and where the design references it. Remove those from the design. A `Not designed by elasticache: …` error names a query ID the assignment gives this engine that the design input lists and no access pattern names: add it to an access pattern (a trade-off or an `unsupported_patterns` entry does not serve it). Then rewrite the response file and re-run `--finalize`; the written output keeps `validation_passed: false` until a re-run passes. `warnings` (query IDs listed only in `unsupported_patterns` that are not in this engine's scope) do not fail validation.

   If the third attempt still prints `"status": "validation_failed"`, stop: set `phase_status.schema_design_elasticache` = "failed" and return `failed` with the `errors` as your result. Do not mark the phase complete.

5. **Update state**
   Only after `--finalize` printed `"status": "complete"`: set `phase_status.schema_design_elasticache` = "complete"

6. **PE review** (run on your own only)
   Dispatched by `/modernize` or `/design-schema`, stop after Step 5: your dispatcher runs the review, the one Bedrock mode runs after every ElastiCache design. Run on your own (you are the top-level session), request it:

   ```bash
   uv run python scripts/run_pe_review.py --job-id {job_id} --db {database_name} --engine elasticache
   ```

   If it prints `"status": "awaiting_llm"`, dispatch one subagent for its one review with the `/modernize` 6c task text (`Follow /review-schema ...`) and wait for it to report. Its blockers hold the design's wave and its warnings are listed for the customer to accept; an ElastiCache review carries no routing flags, so it moves no query.
