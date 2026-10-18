
# /design-schema-opensearch

Designs OpenSearch index mappings, data streams, and ISM policies.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.

## Steps

1. **Prepare input**

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine opensearch --llm-mode external
   ```

2. **Read input and domain expertise**
   a. Read: `artifacts/{database_name}/{job_id}/llm_requests/schema_design_opensearch.json`
      - Contains: filtered queries, tables, analysis results, `decision_trace` (the analysis agent's pre-computed signals for exactly these queries and tables; absent when the analysis wrote none), `design_metrics` (the `design_rps` and sizing figures computed for you: copy them, never recompute them), and `output_schema` (the exact JSON Schema your output must conform to)
   b. Read: `src/skills/opensearch-index-modeling.md` (domain expertise guide)

3. **Design the schema**
   Produce JSON conforming to `output_schema` from the request file. Key principles:
   - Design only the tables and queries assigned to this engine; finalize rejects others. The request's `collector_output` holds exactly that scope: reference only its tables and `query_id`s
   - Search-workload tables become IndexMapping entries
   - Time-series-workload tables become DataStreamConfig entries with ISM policies
   - Field mappings with appropriate analyzers
   - Pattern IDs must be prefixed with `OS-AP-`

4. **Write, validate, persist**
   Write to `artifacts/{database_name}/{job_id}/llm_responses/schema_design_opensearch.json`, then:

   ```bash
   uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine opensearch --finalize
   ```

   If validation fails, the errors tell you exactly which fields are wrong. Fix the response file and re-run `--finalize`. Make **at most 3 `--finalize` attempts in total**, shared between contract-validation failures and scope failures.

   A `"status": "validation_failed"` with an `output_path` means the design is contract-valid but out of scope: each error names a source table or query ID assigned to another engine (or out of scope) and where the design references it. Remove those from the design, rewrite the response file and re-run `--finalize`; the written output keeps `validation_passed: false` until a re-run passes. `warnings` (query IDs listed only in `unsupported_patterns` that are not in this engine's scope, and `design_metrics: …` numbers that differ from the request's `design_metrics`) do not fail validation. A `design_metrics: …` warning means the written design keeps `validation_passed: false` with the mismatch in `validation_failures`; if attempts remain, copy the number from `design_metrics` into the response file and re-run `--finalize`.

   If the third attempt still prints `"status": "validation_failed"`, stop: set `phase_status.schema_design_opensearch` = "failed" and return `failed` with the `errors` as your result. Do not mark the phase complete.

5. **Update state**
   Only after `--finalize` printed `"status": "complete"`: set `phase_status.schema_design_opensearch` = "complete"

6. **PE review** (run on your own only)
   Dispatched by `/modernize` or `/design-schema`, stop after Step 5: your dispatcher runs the review, the one Bedrock mode runs after every OpenSearch design. Run on your own (you are the top-level session), request it:

   ```bash
   uv run python scripts/run_pe_review.py --job-id {job_id} --db {database_name} --engine opensearch
   ```

   If it prints `"status": "awaiting_llm"`, dispatch one subagent for its one review with the `/modernize` 6c task text (`Follow /review-schema ...`) and wait for it to report. Its routing flags are recorded for the post-schema reroute, which `/modernize` and `/design-schema` run after every engine is designed.
