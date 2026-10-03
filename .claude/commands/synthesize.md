
# /synthesize

Produces the final synthesis report with rankings, TCO analysis, risk assessment, and architecture recommendation.

## Tool Use

Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.

## Prerequisites

- Schema design phase complete for all engines

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
      - Ground every engine and table claim in `effective_architecture`: name a table
        under an engine only if it is in that engine's `tables` list (the tables its
        in-scope queries touch). A table may be listed under several engines.
        `recommended_engine_by_table` is secondary information, not the test. Never
        present an engine from `eliminated_engines` as part of the target; its work
        now runs on `absorbed_by`. Do not generalise a table to an engine because it
        shares a query group.
      - Finalize checks this deterministically: a sentence that names a table under an
        engine none of whose in-scope queries touch it rejects the whole summary, and
        the report shows the deterministic summary instead (your text is kept in
        `summary_llm`, the findings in `summary_validation_warnings`)
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
   - Engine ranking with scores
   - Architecture recommendation (single/multi/hybrid)
   - TCO comparison (current RDS vs target)
   - Top risks and mitigations
   - Executive summary

6. **Update state**
   Set `phase_status.synthesis` = "complete"
   Tell user where the deliverables are, using the `files` paths the render step printed
   (under `./artifacts/`): the decision report and analysis report (open in a browser),
   the engineering report (Markdown), and `summary-executive-report.pdf`.
