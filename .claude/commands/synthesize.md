
# /synthesize

Produces the final synthesis report with rankings, TCO analysis, risk assessment, and architecture recommendation.

## Prerequisites

- Schema design phase complete for all engines

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `job_id`, `database_name`.

2. **Run synthesis**

   ```bash
   uv run python scripts/run_synthesis.py --job-id {job_id} --db {database_name} --llm-mode external
   ```

3. **If status is `awaiting_llm`:**
   a. Read LLM request: `.artifacts/{database_name}/{job_id}/synthesis/llm_input.json`
   b. Write a 3-4 sentence executive summary for a CTO audience. Rules:
      - Reference the deterministic summary provided for factual grounding
      - No confidence scores, no cost figures (those are in the report)
      - Mention specific AWS service names (DynamoDB, OpenSearch Service, etc.)
      - No em dashes, no hedging, no buzzwords
      - Focus on: what engines were selected, why, and what the migration enables
   c. Output:

      ```json
      {"executive_summary": "..."}
      ```

   d. Write to: `.artifacts/{database_name}/{job_id}/synthesis/llm_response.json`
   e. Finalize:

      ```bash
      uv run python scripts/run_synthesis.py --job-id {job_id} --db {database_name} --finalize
      ```

4. **Present report**
   Show:
   - Engine ranking with scores
   - Architecture recommendation (single/multi/hybrid)
   - TCO comparison (current RDS vs target)
   - Top risks and mitigations
   - Executive summary

5. **Update state**
   Set `phase_status.synthesis` = "complete"
   Tell user: "Full report available at ./artifacts/{db}/{job}/referee-synthesis/report.json"
