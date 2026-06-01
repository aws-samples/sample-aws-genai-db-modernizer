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
