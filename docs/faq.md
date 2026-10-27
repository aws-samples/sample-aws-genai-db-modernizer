# FAQ / troubleshooting

## Does the tool read my data?

No table rows. The collection scripts are read-only: they run `SELECT` on the catalog and statistics views only, and the collection holds the schema, query shapes and counters. Query text and view, procedure and trigger definitions can still carry literal values, so review a collection before you share it. A collection made before this rule that still carries table rows has them dropped, with a count, from every copy the tool writes.

## Does the database name have to match the collection?

No. `/modernize` names the run from the file name (`shop-collection.json` becomes `shop`), and `scripts/run_assessment.py` from `--db`, but the tables are keyed by the database name the collection records, so any name gives the same plan. When the two differ, the `collect` line also shows the collected name as `collected_database_name`. One case stops the run with an error: a collection that records no database name and whose statements belong to several databases, none of them the name you gave; name the run after the database you collected.

## Amazon Bedrock model access

An `AccessDeniedException` or a similar error from `--llm-mode bedrock` usually means the account can't invoke the Anthropic models yet: **Claude Sonnet** (used for Reality Check validation and for Synthesis), **Claude Opus** (used for Schema Design) and **Claude Haiku** (used to validate free-text answers). Most Bedrock model access is on by default, but Anthropic models need a use-case form, submitted once per account. Select the model in the Bedrock console's model catalog for the same region as `AWS_DEFAULT_REGION`/`AWS_REGION`, or call `PutUseCaseForModelAccess`. See [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html). Access is per-account and can take a few minutes to propagate after you submit the form.

## Claude Code permissions

`/modernize` and the other commands run `uv run python scripts/...` and start a local uvicorn/npm process for the UI. If Claude Code keeps asking to approve the same command, check `.claude/settings.json` (or `.claude/settings.local.json`) for an allowlist entry, and see [`.claude/settings.ci.json`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/.claude/settings.ci.json) for the full list the headless CI run uses as a reference.

## The UI didn't start

`/modernize` falls back to chat-only and keeps going if the UI can't start (port 3000 or 8000 already in use, a failed Node build, or no Node at all). The chat still shows every key number. Check for another process on those ports, confirm Node 22 is installed (`node --version`), and look for a UI build error in the command's own output before re-running. The UI keeps running after the run so you can browse the results; stop it with `uv run python scripts/start_local_ui.py --stop`.

## Collector connection issues

- **PostgreSQL:** `collect-postgresql.sql` needs the `pg_stat_statements` extension enabled on the database (`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`, needs superuser or `rds_superuser` on RDS) and `SELECT` on `information_schema`.
- **MySQL/MariaDB:** `collect-mysql.sql` needs `performance_schema` enabled (on by default since MySQL 5.6) and `SELECT` on `information_schema` and `performance_schema`.
- **SQL Server:** `collect-sqlserver.sql` needs SQL Server 2016 or later, `SELECT` on the `sys` catalog views and DMVs, and `VIEW SERVER STATE` for the query section (without it the queries section is empty but everything else is collected).
- **Oracle:** `collect-oracle.sql` needs Oracle 12.2 or later and `SELECT` on the `V$` views, the `ALL_*` catalog views and `DBA_SEGMENTS` (grant `SELECT_CATALOG_ROLE` for full visibility). It reports one query per statement shape: `V$SQL` rows that differ only in literal values share a `FORCE_MATCHING_SIGNATURE`, so their counters are summed (a collection made with an older script is merged by the offline parser).
- Every script is read-only. If a plain `SELECT` works but the collection script fails, the cause is usually a missing grant on one of those views, not the credentials.

## Something else

Check [open issues](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues) first, then file a [bug report](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/new?template=bug_report.md) with the steps to reproduce. See [Contribute](contribute.md) for the template conventions.
