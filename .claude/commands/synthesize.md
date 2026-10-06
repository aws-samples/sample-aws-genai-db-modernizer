
# /synthesize

Produces the final synthesis report with rankings, TCO analysis, risk assessment, and architecture recommendation.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`.

## Prerequisites

- Schema design phase run for in-scope engines. Not every engine necessarily
  has a design: a model-based designer can be skipped (`--llm-mode none`),
  fail, or never be dispatched. The LLM request's `schema_design_status`
  says which engines ended up designed and which didn't.

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `job_id`, `database_name`.

2. **Run synthesis**

   ```bash
   uv run python scripts/run_synthesis.py --job-id {job_id} --db {database_name} --llm-mode external
   ```

   The script resolves the effective assignment version itself (v2 when Reality Check consolidated, else v1) and prints the exact `llm_request` and `llm_response` paths. Use those paths as printed. Do not build versioned paths yourself.

3. **If status is `awaiting_llm`:**
   a. Read the LLM request at the `llm_request` path the script printed (under `./artifacts/`)
   b. Write a 3-4 sentence executive summary for a CTO audience. Rules:
      - Name a schema design (table/index counts, access patterns, "SOLVED" capability
        gaps) only for an engine listed in `schema_design_status.designed`. For an
        engine in `schema_design_status.not_designed`, say plainly that schema design
        has not run for it; never describe a schema, table count, or access pattern
        for it, and never imply one exists. If `not_designed` is non-empty, do not use
        "here is what we built" framing; prefer "here is how the workload is routed".
      - Ground every engine and table claim in `effective_architecture`: name a table
        under an engine only if it is in that engine's `tables` list (the tables its
        in-scope queries touch). A table may be listed under several engines.
        `recommended_engine_by_table` is secondary information, not the test. Never
        present an engine from `eliminated_engines` as part of the target; its work
        now runs on `absorbed_by`. Do not generalise a table to an engine because it
        shares a query group.
      - Finalize checks this deterministically, clause by clause: a table named in a
        clause must be served by an engine named in that clause (or in the clause it
        continues). A clear mismatch (the table written as `wp_x`/`db.x` or as a
        multi-word name like "post meta" in a clause naming a single engine, or an
        eliminated engine named as the server) rejects the whole summary and the
        report shows a deterministic summary instead. Your text is kept in
        `summary_llm` and every finding in `summary_validation_warnings`; weaker
        findings are recorded without rejecting
      - Reference the deterministic summary provided for factual grounding
      - No confidence scores, no cost figures (those are in the report)
      - Mention specific AWS service names (DynamoDB, OpenSearch Service, etc.)
      - No em dashes, no hedging, no buzzwords
      - Focus on: what engines were selected, why, and what the migration enables
   c. Output:

      ```json
      {"executive_summary": "..."}
      ```

   d. Write to the `llm_response` path the script printed (under `./artifacts/`)
   e. Finalize:

      ```bash
      uv run python scripts/run_synthesis.py --job-id {job_id} --db {database_name} --finalize
      ```

      The output carries `summary_source` (`llm`, or `deterministic_fallback` when the
      post-check rejected your summary) and `summary_validation_warnings`. On a
      fallback, tell the user the generated summary was withheld and quote the warnings.

4. **Render deliverables**

   ```bash
   uv run python scripts/run_report.py --job-id {job_id} --db {database_name}
   ```

   Prints `{"status": ..., "files": [...], "errors": [...], "warnings": [...]}`.
   `complete` means every deliverable rendered with nothing to flag; `partial`
   covers both outright failures (in `errors`) and successful-but-suspect renders
   (in `warnings`, e.g. an analysis report with zero query journeys embedded) —
   tell the user what it says, then continue; `error` means no synthesis report
   exists, or rendering itself blew up (stop and report it).

5. **Present report**
   Show:
   - Engine ranking, in report order (largest workload share first, cache layer last),
     with each engine's `routed_confidence`: the mean fit of the queries routed to it
     (cache layer: of the reads it fronts). Say "no table-level evidence" when
     `routed_confidence_evidence` is `signal_only`. `analysis_confidence` is the
     average over every analyzed table, kept for audit; do not present it as the
     engine's confidence
   - Architecture recommendation (single/multi/hybrid)
   - TCO comparison (current RDS vs target)
   - Top risks and mitigations
   - Executive summary

6. **Update state**
   Set `phase_status.synthesis` = "complete"
   Tell user where the deliverables are, using the `files` paths the render step printed
   (under `./artifacts/`): the decision report and analysis report (open in a browser),
   the engineering report (Markdown), and `summary-executive-report.pdf`.
