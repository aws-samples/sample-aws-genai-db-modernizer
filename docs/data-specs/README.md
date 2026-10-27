# Data specifications

This directory describes what the collection scripts gather from a source database and where each field comes from. The scripts are the one source of the queries: `scripts/collect-*.sql` is what users run and what the offline parser (`src/tools/database/offline_parser.py`) reads. There are no separate query templates to keep in step.

## Index

| Document | Purpose | Audience |
|----------|---------|----------|
| [database-collection-matrix.md](database-collection-matrix.md) | What each collection script emits, section by section and field by field | Collector developers |

## Coverage

| Source database | Script | Collector | Status |
|---|---|---|---|
| MySQL 8.0+ | `scripts/collect-mysql.sql` | `mysql_collector.py` | Shipped |
| MariaDB | `scripts/collect-mysql.sql` | `mariadb_collector.py` | Shipped |
| PostgreSQL 12+ | `scripts/collect-postgresql.sql` | `postgres_collector.py` | Shipped |
| SQL Server 2016+ | `scripts/collect-sqlserver.sql` | `sqlserver_collector.py` | Shipped |
| Oracle 12.2+ | `scripts/collect-oracle.sql` | `oracle_collector.py` | Shipped |
| Db2 | none | none | An enum value only; no script or collector |

How to run each script, and what privileges it needs, is in [Analyzing your own database](../use-claude-code.md#analyzing-your-own-database). Redis is not a source database: the pipeline reads relational sources and has no Redis collector.

## How the data flows

1. The user runs a script against their own database. It writes one JSON object (`collection_version`, `metadata`, `tables`, `columns`, `indexes`, `foreign_keys`, `primary_keys`, `views`, `procedures`, `triggers`, `queries`, `global_stats`; SQL Server and Oracle add `io_stats`, `wait_events` and `os_stats`).
2. `run_assessment.py` (or `/collect`) recognises it as a raw collection and calls `parse_offline_collection`, then `build_offline_collector_output` builds the `CollectorOutputContract` (see the [collector guide](../guides/strands-collector-guide.md)).
3. The contract is the interface the rest of the pipeline reads ([contracts](../contracts/agent-contracts-spec.md)).

## Principles

- **Read-only.** The scripts run `SELECT` on catalog and statistics views only.
- **No row data.** No script reads table rows (#387), and column statistics are limited to counts such as cardinality estimates. No literal value is kept either (#1625): the parser replaces each string, number, date and binary literal in query text with a placeholder in the source dialect (MySQL and PostgreSQL text arrives normalised already), redacts each string literal in view, procedure and trigger definitions to `'?'`, and removes e-mail addresses and runs of nine or more digits from comments and query tags, and every quoted run from a comment that is not a sqlcommenter tag (a sqlcommenter tag, a comment that is only comma-separated `key='value'` pairs, keeps its keys and values, which name code). What the tool writes is redacted: the collector output, the live collectors' stage checkpoints, the scripts' copy of the raw file, the AWS Transform staging copy, the model inputs and the reports; a collector output saved before #1625 is redacted when it is read. A file uploaded through the API is kept as the customer sent it, as for table rows, and what is derived from it is redacted. Numbers in definitions and plain words in comments are kept. A collection that still carries table rows (written before #387) has them dropped, with a count, from every copy the tool writes; a file uploaded through the API or AWS Transform is kept as the customer sent it, and only what is derived from it drops the rows.
- **Common plus optional.** The sections every script emits are guaranteed; counters a database does not track are zero or absent, and analysis works with what is there.
- **Ingestion limits.** Every ingestion path rejects a collection above the limits in `src/tools/validation/ingestion_limits.py` (see the use-claude-code page).

## Adding a source database

1. Write `scripts/collect-<database>.sql` that emits the same top-level sections, with the same field names (the existing scripts follow the MySQL convention; the Oracle script translates inline).
2. Teach the offline parser to recognise the engine (`detect_source_engine` in `src/tools/database/offline_parser.py`) and add the engine tools and collector under `src/agents/collector/`.
3. Add the engine to the collector contract's source-engine values (a MINOR bump, see [contracts](../contracts/agent-contracts-spec.md)).
4. Add the engine's relational-move gap rules under `src/catalog/gaps/` if it can be a source.
5. Add tests with a small fixture collection, and update the matrix and `docs/use-claude-code.md`.
