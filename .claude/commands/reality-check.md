
# /reality-check

Validates consolidation decisions from the deterministic reality check. Your role is a CRITICAL REVIEWER — you challenge consolidations that don't make architectural sense, not rubber-stamp them.

## Context

The deterministic pipeline already ran and decided to move queries between engines to reduce operational complexity. Your job is to catch bad moves: queries that the target engine genuinely cannot serve well.

This is the same validation that runs in production via Bedrock (see `src/agents/referee/consolidation_validator.py`). You are providing the LLM judgment that the deterministic engine cannot.

## Prerequisites

- Assignment phase complete (`.modernizer-state.json` shows `reality_check: awaiting_llm`)

## CRITICAL: How This Works

You have ONE job: read `llm_input.json`, apply the engine capabilities reference below to challenge each consolidation, and write the response. That file contains ALL the information you need — the SQL, the signals, the consolidation decisions. Do NOT:

- Run ad-hoc Python scripts to "explore" the data
- Read other artifact files (analysis results, collector output, assignment files)
- Read portions of files incrementally — read the full `llm_input.json` in one shot
- Investigate the codebase to "understand" how things work

You are a reviewer receiving a complete brief. Read it, apply judgment, write the verdict.

## Steps

1. **Read state**
   Read `.modernizer-state.json` for `job_id`, `database_name`.

2. **Read the LLM input (ONE read, full file)**
   Read `./artifacts/{database_name}/{job_id}/reality-check/llm_input.json`

   Focus on:
   - `consolidation_validation.consolidations` — what was moved and why
   - `consolidation_validation.query_signals` — the signal map per query (keyed by query_id)
   - `executive_summary.before_distribution` / `after_distribution` — the shift
   - `executive_summary.unique_value_assessment` — what each engine uniquely provides

3. **For EACH consolidation, validate the moved queries (using ONLY what you just read)**

   For each consolidation entry (from_engine → to_engine), find the moved queries in `query_signals` and check:
   - What SQL patterns do these queries have? (the SQL is in the file you already read)
   - What signals were detected (complex_joins, aggregations, subqueries, text_search, etc.)? (in the file you already read)
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
         "query_id": "hash_from_query_signals",
         "original_engine": "aurora_mysql",
         "reason": "3-table JOIN with GROUP BY and HAVING clause requires relational engine"
       }
     ],
     "executive_summary": "..."
   }
   ```

   - Each correction: `query_id`, `original_engine` (the engine it was moved FROM), `reason`
   - If ALL consolidations are genuinely valid (rare for Aurora consolidations), use `[]`
   - **Do not default to empty.** Actually read the SQL and think critically.

   **Executive summary rules:**
   - 2-3 sentences for a CTO audience
   - No first person ('I found'), no hedging, no confidence scores
   - Use second person ('Your workload...') or passive ('The analysis shows...')
   - Mention specific AWS service names
   - Reference zero-ETL integrations if applicable (DynamoDB → OpenSearch via OpenSearch Ingestion)
   - No em dashes, no marketing buzzwords ('leverage', 'robust', 'seamless')
   - Frame as the confident recommendation of a senior architect

5. **Finalize**

   ```bash
   uv run python scripts/run_assessment.py --job-id {job_id} --db {database_name} --resume-reality-check
   ```

6. **Present results**
   Show: consolidations made/reversed, architectural patterns detected, updated engine distribution.

7. **Update state**
   Set `phase_status.reality_check` = "complete", `current_phase` = "schema_design"
