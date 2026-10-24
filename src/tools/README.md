# Strands Tools

Custom Strands tools used by agents.

## Structure

- **analysis/** - Pattern detection, scoring, and LLM advisor tools for analysis agents
- **aws/** - AWS service tools (RDS metadata, CloudWatch metrics, Performance Insights)
- **database/** - Database connection and query tools (MySQL, PostgreSQL, SQL Server, etc.)
- **data_movement/** - Data movement (#1412): `plan.py` builds the DataMovementPlan; `debezium_connector.py` renders the Debezium MSK Connect connector configuration and stack (`infrastructure/cloudformation/data-movement/`)
  - `initial_load.py` — the initial-load-to-CDC handoff: position capture (MySQL, PostgreSQL), key-range splitting, the per-row transform hook, idempotent target writes, the Debezium start offset, cleanup on failure, and readiness from the connector's committed position against a source barrier plus source row counts
- **schema/** - Schema design tools
  - `dynamodb_schema_agent.py` — Strands agent with PE review loop for DynamoDB schema design
- **sql/** - SQL-text tools shared across collectors, detectors and parsers
  - `normalize.py` — normalizes collector digest text before SQL parsing
- **validation/** - Contract validation tools

## Implementation Guidelines

All tools should:

- Follow Strands SDK `@tool` decorator patterns
- Be reusable across multiple agents
- Include unit tests
