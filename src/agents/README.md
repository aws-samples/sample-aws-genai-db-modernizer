# Agent Implementations

This directory contains all Strands agent implementations.

## Structure

- **collector/** - Database collector agents (MySQL, PostgreSQL, SQL Server, etc.)
- **analysis/** - Analysis agents, one per target engine (DynamoDB, DocumentDB, Aurora, etc.)
- **referee/** - Referee agent: triage (fan-out to analysis) and synthesis (rank + merge recommendations)
- **schema_design/** - Schema design agents, one per target engine, with PE review loop
- **entrypoint.py** - Dispatches to the correct agent based on `AGENT_TYPE` env var

## Agent Types

| `AGENT_TYPE` value | Handler |
|--------------------|---------|
| `collector` | `collector/handler.py` |
| `<engine>` (e.g. `dynamodb`) | `analysis/handler.py` |
| `referee-triage` | `referee/triage_handler.py` |
| `referee-synthesis` | `referee/synthesis_handler.py` |
| `schema-design` | `schema_design/handler.py` (requires `TARGET_TYPE` env var) |

## Implementation Guidelines

All agents should:

- Follow Strands SDK patterns (see `docs/guides/`)
- Implement contracts defined in `src/contracts/`
- Include comprehensive tests in `tests/`

## Untrusted content handling (threat model R1 / R3)

Customer-supplied database content — schema, table and column names, column
defaults, view and procedure bodies, and raw SQL `query_text` from the uploaded
collection (leading comments included) — is **untrusted input**, and so is
customer free text (revision requests, free-text answers). It flows into LLM
prompts at every phase that uses a model (analysis advisors, reality-check, schema
design and PE review, synthesis, free-text answer validation), into the request
files an external session reads, into the AWS Transform orchestrator's tool
results, and into the rendered deliverables. Two controls apply, and new code that touches either path
must preserve them:

- **Into prompts (R1):** customer content is framed as data, not instructions, via
  `src/agents/prompt_framing.py` (`frame_untrusted`, `frame_customer_requests`, and
  `SYSTEM_PROMPT_DATA_DIRECTIVE`). It only ever appears in the user turn or a tool
  result, never in a system prompt. When adding a new prompt that embeds customer
  content, wrap that content with these helpers. JSON a model reads instead of a
  prompt (request files, tool results) gets `with_untrusted_notice`, which adds an
  `untrusted_data_notice` key (first, or right after the reality-check request's
  `read_pages`). `tests/unit/agents/test_collector_text_is_framed.py` plants
  injection-like text in every customer field and fails when a model input carries
  it outside the frame.
- **Out of the LLM (R3):** LLM output is itself treated as untrusted. It is never
  executed and is only ever rendered as **escaped** document content
  (`src/report/escaping.py`). Engine and query assignment are
  deterministic and are not decided by the LLM.

This is defense-in-depth: framing reduces, but does not eliminate, prompt-injection
risk. Bedrock Guardrails' prompt-attack filter is tracked separately (#144).
