
# /reality-check

Validates consolidation decisions from the deterministic reality check. Your role is a CRITICAL REVIEWER — you challenge consolidations that don't make architectural sense, not rubber-stamp them.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`.

## Context

The deterministic pipeline already ran and decided to move queries between engines to reduce operational complexity. Your job is to catch bad moves: queries that the target engine genuinely cannot serve well.

This is the same validation that runs in production via Bedrock (see `src/agents/referee/consolidation_validator.py`). You are providing the LLM judgment that the deterministic engine cannot.

## Prerequisites

- Assignment phase complete (`.modernizer-state.json` shows `reality_check: awaiting_llm`)

## CRITICAL: How This Works

You have ONE job: read `llm_input.json`, apply the engine capabilities reference below to challenge each consolidation, and write the response. That file contains ALL the information you need: for each consolidation, the queries it moved with their SQL and signals, plus the distributions for the summary. Do NOT:

- Run ad-hoc Python scripts to "explore" the data
- Read other artifact files (analysis results, collector output, assignment files)
- Read the file in any other slices than its `read_pages`
- Investigate the codebase to "understand" how things work

You are a reviewer receiving a complete brief. Read it, apply judgment, write the verdict.

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `job_id`, `database_name`.

2. **Read the LLM input, page by page**
   File: `./artifacts/{database_name}/{job_id}/reality-check/llm_input.json`

   The file is written one record per line and is small (tens of KB to a few hundred KB). Its second line is `"read_pages": [{"offset": …, "limit": …}, …]`: the Read tool pages that cover the whole file, each small enough for one Read call. First Read it with `offset` 1 and `limit` 2 to get `read_pages`, then Read every page in order with its `offset` and `limit` (the first page starts at line 1). If Read reports a page is too large, read it in two halves (half the `limit` each). Do not page it any other way.

   Focus on:
   - `consolidation_validation.consolidations`: what was moved and why. Each entry has `from_engine`, `to_engine`, `query_count` and `moved_queries`, one record per moved query: `query_id`, `type`, `cps` (calls per second), `tables`, `signals` (triage signals such as `complex_joins`, `aggregations`, `subqueries`, `text_search`) and `sql` (the first 500 characters; `sql_chars` gives the full length when it was cut)
   - `executive_summary.before_distribution` / `after_distribution`: the shift
   - `executive_summary.unique_value_assessment`: what each engine uniquely provides (query counts per engine)
   - `executive_summary.absorption_candidates` and `executive_summary.scope` (tables, queries and engines evaluated)

3. **For EACH consolidation, validate its moved queries (using ONLY what you just read)**

   For each consolidation entry (from_engine → to_engine), go through its `moved_queries` and check:
   - What SQL patterns do these queries have? (`sql`)
   - What signals were detected? (`signals`)
   - Can the target engine actually serve these patterns? (use the reference below)

   You do NOT need to read any other files. Everything is in `llm_input.json`.

### Engine Capabilities Reference

Use these to judge whether a target engine can handle the moved queries:

**DynamoDB** — Key-value/document store

- CAN DO: single-item lookups by PK, range queries on sort key, denormalized data via GSI, key-value CRUD, session storage, metadata lookups
- CANNOT DO: ad-hoc multi-table JOINs, full-text search (LIKE '%...%'), complex aggregations across partitions, transactions >25 items, queries without a known partition key

**DocumentDB** — MongoDB-compatible document DB

- CAN DO: flexible schemas, nested document queries, aggregation pipelines, $lookup JOINs, multi-document ACID, basic regex matching
- CANNOT DO: full-text search at scale (no inverted index), extreme write throughput

**OpenSearch** — Search and analytics engine

- CAN DO: full-text search, fuzzy matching, aggregations/analytics, time-series, geo-spatial, faceted search
- CANNOT DO: ACID transactions, strong consistency, primary write path, frequent single-doc updates

**ElastiCache** — In-memory data structures

- CAN DO: sorted sets, counters, session storage, pub/sub, hot-path caching, leaderboards
- CANNOT DO: complex queries, persistence as source of truth, multi-dimension filters, JOINs

**Aurora MySQL/PostgreSQL** — Relational database

- CAN DO: multi-table JOINs, complex GROUP BY, subqueries (correlated, EXISTS), ACID transactions, ad-hoc queries, window functions
- CANNOT DO: extreme horizontal scale beyond a few TB, single-digit-ms at millions of TPS for simple lookups

### Flag Criteria

Only flag queries where the target engine is a genuinely poor fit:

- Multi-table JOINs (3+ tables) with aggregations moved to DynamoDB or ElastiCache → **FLAG**
- Subqueries with correlated filters moved away from Aurora → **FLAG**
- Complex GROUP BY across multiple tables moved to non-relational → **FLAG**
- Full-text search (LIKE '%...%', MATCH, tsvector) moved to DynamoDB → **FLAG**
- Queries with `complex_joins` or `subqueries` signals moved FROM Aurora TO DynamoDB → **almost always FLAG**

Do NOT flag:

- Simple key-value lookups moved to DynamoDB (that's correct)
- Patterns that just need denormalization (expected for NoSQL)
- Low-frequency admin queries that any engine can handle

4. **Write the response**

   Write to: `./artifacts/{database_name}/{job_id}/llm_responses/reality_check.json`

   ```json
   {
     "consolidation_corrections": [
       {
         "query_id": "query_id_from_moved_queries",
         "original_engine": "aurora_mysql",
         "reason": "3-table JOIN with GROUP BY and HAVING clause requires relational engine"
       }
     ],
     "executive_summary": "..."
   }
   ```

   - Each correction: `query_id` (copied exactly from `moved_queries`), `original_engine` (the consolidation's `from_engine`), `reason`
   - If ALL consolidations are genuinely valid (rare for Aurora consolidations), use `[]`
   - **Do not default to empty.** Actually read the SQL and think critically.

   **Executive summary rules:**
   - Describe the outcome *after* your corrections: an engine you send queries back
     to stays in the architecture. Finalize can still move queries (a restored Aurora
     can absorb a small engine), so name an engine as kept or eliminated only as the
     final records will show it.
   - Do not state the final fate of any engine listed in
     `executive_summary.absorption_candidates`: it is small enough for Aurora to
     absorb it at finalize if your corrections keep Aurora, so neither "stays" nor
     "is eliminated" is known yet. Describe the work it does, not whether it stays.
   - Finalize checks the summary against the final records. If it names a kept engine
     as eliminated, or an eliminated one as kept, the customer sees a summary built
     from the records instead (`executive_summary_source: deterministic_fallback`);
     your text stays in `executive_summary_llm` for audit.
   - 2-3 sentences for a CTO audience
   - No first person ('I found'), no hedging, no confidence scores
   - Use second person ('Your workload...') or passive ('The analysis shows...')
   - Mention specific AWS service names
   - Reference zero-ETL integrations if applicable (DynamoDB → OpenSearch via OpenSearch Ingestion)
   - No em dashes, no marketing buzzwords ('leverage', 'robust', 'seamless')
   - Frame as the confident recommendation of a senior architect

5. **Stop and report back**

   Return a one-paragraph summary to the caller: how many consolidations you
   reviewed, how many you reversed (with query IDs), and the response path.

   When dispatched by `/modernize`, do NOT finalize the reality check and do NOT
   update `.modernizer-state.json`: the orchestration step that dispatched you
   merges your response and advances the state after you return.

   When invoked on its own (not from `/modernize`), finalize after writing the
   response:

   ```bash
   uv run python scripts/run_assessment.py --job-id {job_id} --db {database_name} --resume-reality-check
   ```

   It prints a `{"phase": "reality_check", ...}` status line, then a `{"log": ...}` line
   naming the progress log. A `"status": "error"` line's `message` is the reason.
