# FAQ / troubleshooting

## Amazon Bedrock model access

`AccessDeniedException` or similar from `--llm-mode bedrock` almost always means the account can't invoke the Anthropic models yet: **Claude Sonnet** (used for Reality Check validation and for Synthesis) and **Claude Opus** (used for Schema Design). Most Bedrock model access is on by default, but Anthropic models need a one-time use-case form submitted once per account — select the model in the Bedrock console's model catalog for the same region as `AWS_DEFAULT_REGION`/`AWS_REGION`, or call `PutUseCaseForModelAccess`. See [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html). Access is per-account and can take a few minutes to propagate after you submit the form.

## Claude Code permissions

`/modernize` and the other commands run `uv run python scripts/...` and start a local uvicorn/npm process for the UI. If Claude Code keeps asking to approve the same command, check `.claude/settings.json` (or `.claude/settings.local.json`) for an allowlist entry, and see [`.claude/settings.ci.json`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/.claude/settings.ci.json) for the full list the headless CI run uses as a reference.

## The UI didn't start

`/modernize` falls back to chat-only and keeps going if the UI can't start (port 3000 or 8000 already in use, the Node build failed, or Node itself is missing) — the chat still carries every key number. Check for another process on those ports, confirm Node 22 is installed (`node --version`), and look for a UI build error in the command's own output before re-running.

## Collector connection issues

- **PostgreSQL:** `collect-postgresql.sql` needs the `pg_stat_statements` extension enabled on the database (`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`, needs superuser or `rds_superuser` on RDS) and `SELECT` on `information_schema`.
- **MySQL/MariaDB:** `collect-mysql.sql` needs `performance_schema` enabled (on by default since MySQL 5.6) and `SELECT` on `information_schema` and `performance_schema`.
- Both scripts are read-only — if a connection works for a plain `SELECT` but the collection script fails, it's almost always a missing grant on one of those schemas, not a credentials problem.

## Something else

Check [open issues](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues) first, then file a [bug report](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/new?template=bug_report.md) with the steps to reproduce — see [Contribute](contribute.md) for the template conventions.
