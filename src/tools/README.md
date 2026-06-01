# Strands Tools

Custom Strands tools used by agents.

## Structure

- **analysis/** - Pattern detection, scoring, and LLM advisor tools for analysis agents
- **aws/** - AWS service tools (RDS metadata, CloudWatch metrics, Performance Insights)
- **database/** - Database connection and query tools (MySQL, PostgreSQL, SQL Server, etc.)
- **schema/** - Schema design tools
  - `dynamodb_schema_agent.py` — Strands agent with PE review loop for DynamoDB schema design
  - `dynamodb_schema_designer.py` — Legacy LLM-based schema designer (pre-contract)
- **validation/** - Contract validation tools

## Implementation Guidelines

All tools should:

- Follow Strands SDK `@tool` decorator patterns
- Be reusable across multiple agents
- Include unit tests
