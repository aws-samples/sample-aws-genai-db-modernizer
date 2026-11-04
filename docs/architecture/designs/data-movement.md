# Design: data movement, step by step

**Status:** Accepted (maintainer decisions, 2026-11-03). Design only: no code changes with it. Part E (gates for the steps after step 1) is a high-level design, to revisit when it is built.
**Issue:** #1795 (epic #1813). **Decisions:** ADR-033, data movement driven by a transformation contract (#1778). **Related:** epic #1777 (the transformation contract), #1128 and #1698 (slice-first steps), #1702 (cross-engine and heterogeneous sources), #1412 (the CDC-to-ElastiCache prototype by Norbert Funke and Dinesh Kumar, the prior proof of the Debezium source side).

## 1. Summary

1. Data moves one step of the migration plan at a time. Step 1 moves the whole source database to the source-compatible Aurora engine by AWS DMS. Every later step moves one slice to one engine (#1698), reading Aurora through one hub of compacted Kafka topics per job, with a Managed Service for Apache Flink job per step that applies the step's transformation contract (epic #1777) and writes through our own sinks.
2. The customer owns their databases. For step 1 we provide a suggested Aurora template and the DDL; the customer creates and runs them. Our stacks only deploy the pipeline (DMS, MSK, MSK Connect, Managed Flink) and remove it at the end.
3. Every step has read-only evidence (lag, completeness, integrity) read from outside the customer's VPC, and every place that shows it states that the cutover is the customer's decision.
4. This release is one-way (nothing is written back to the source or to Aurora) and does not support source schema changes during the migration window (ADR-033 Decision 11).

| Part | What it covers | Issues |
| --- | --- | --- |
| A. Step 1 delivery | suggested Aurora template, DDL file, our DMS stack, sizing, preflight, gate, cutover message | #1797, #1798, #1799, #1800, #1801 |
| B. The hub | one Debezium connector on Aurora per job, a provisioned MSK cluster, topics, retention, sizing, the hub gate | #1802, #1803, #1804, #1433 |
| C. The runtime | Managed Flink, the Flink SQL compiler backend, our Java aggregate and sinks, recovery, the equivalence test | #1805, #1806, #1807, #1808, #1428, #1809 |
| E. Gates for later steps | the pipeline self-check: counters, read-back sampling, lag, health | #1810, #1811 |
| Pilot | wordpress (MySQL) as a smoke run, then discourse (PostgreSQL) | #1812 |

## 2. Shared: one plan per step (#1797)

Today `src/tools/data_movement/plan.py` writes one `DataMovementPlan` per migration wave. It becomes one plan per step, `data-movement/step-<k>/plan.json` (`k` the slice plan's step number; step 1 from wave 1; without a slice plan, the steps follow the waves), plus one hub plan per job, `data-movement/hub/plan.json`. A step's plan references its transformation contract by path and hash, its stacks in deploy order, its gate and its unsupported items. Old per-wave plans still load.

A step is classified by its starting point and its destination only: homogeneous when the destination is the same engine family on Aurora (MySQL or MariaDB to Aurora MySQL, PostgreSQL to Aurora PostgreSQL), heterogeneous otherwise. Homogeneous steps are a whole-table mirror by DMS; heterogeneous steps run on contract rules in Flink. A scope without an Aurora engine has no step 1, and the hub then reads the source database.

## 3. Part A: step 1 delivery

### 3.1 What the customer owns, and what we provide

The customer creates and owns the Aurora cluster and runs the schema. A new renderer (#1798) renders, from `step-1.json` and the step 1 plan, in the pattern of `scripts/render_debezium_connector.py` (generated, never hand-edited, parameters only, no account, region or endpoint defaults):

- `aurora-target.yaml`: a suggested cluster template, headed by a prominent disclaimer that creating and running it is the customer's responsibility, and listing each required setting with its reason: the engine family and version; on Aurora MySQL `binlog_format=ROW` and `binlog_row_image=FULL`; on Aurora PostgreSQL `rds.logical_replication=1`; encryption at rest; a writer endpoint DMS can reach.
- `step-1.ddl.sql`: the DDL the Aurora design generates, with instructions to run it. On Aurora PostgreSQL it also recommends `REPLICA IDENTITY FULL` on every table, under the same disclaimer: the later steps need the before images, it costs more WAL, and the customer decides.
- `dms-step-1.yaml`, our stack: a replication subnet group, a provisioned replication instance, source and target endpoints (Secrets Manager ARNs passed in; TLS required, server verification when a CA bundle is given), and one full-load-and-CDC task.

No custom resource runs inside a customer's cluster.

### 3.2 DMS settings and sizing

- Task: `TargetTablePrepMode: DO_NOTHING` (the customer's DDL created the tables); transactional apply, batch apply off, to keep per-key order for the connector that later reads Aurora; data validation on; limited LOB mode sized from the largest value the collector saw; full LOB mode for the whole task when any table's LOB size is unknown. It is a task setting because DMS rejects the per-table full LOB rule (`test_the_task_runs_in_full_lob_mode_when_a_table_needs_it`, `test_no_mapping_rule_carries_lob_settings`).
- Table mappings: one explicit selection rule per contract table, no wildcard. No transformation rules: with existing tables, Aurora converts each value into the declared column type on insert, and DMS's transformation rules have per-column limits (one action per column, none on column names with special characters, only a drop for BLOB and CLOB columns). A conversion Aurora cannot do is an `unsupported` field in the contract.
- Sizing, from a rule-catalog file with a reason per row, keyed on the collector's database size and write rate (AWS's guidance: CDC uses three memory buffers, and memory-optimized classes prevent memory pressure under high transaction rates):

| Source size | Write rate | Instance class |
| --- | --- | --- |
| up to 10 GB | up to 50/s | `dms.t3.medium` (burstable; demo and small databases) |
| up to 500 GB | up to 1,000/s | `dms.r6i.large` |
| up to 2 TB | up to 5,000/s | `dms.r6i.xlarge` |
| above | above | `dms.r6i.2xlarge`, with a note to size it with the customer's team |

Multi-AZ is a parameter that defaults to true. `InstanceClass` can be overridden.

### 3.3 Preflight (#1799)

Before the DMS stack deploys, read-only AWS calls, stopping at the first failure: the cluster exists with the right engine family and a version at least the design's; its parameter group has the values above (a value pending reboot fails); both secrets exist and are readable by the stack's role (metadata only); the instance class is offered in the region; the source's CDC prerequisites where it is RDS or Aurora, and a checklist for a self-managed source.

### 3.4 Gate (#1800)

On demand, read-only DMS and CloudWatch calls: the task runs in CDC, full load at 100%, every contract table loaded without errors; every latency datapoint in the last 15 minutes under the plan's `max_lag_seconds`; every table with a primary or unique key validated (still validating keeps the gate pending; a validation failure fails it). Tables without a primary or unique key, and views, which DMS validation skips, pass as named exceptions with their DMS row counts. The result is `pass`, `pending` or `fail`, with the contract hash and `cutover_decision: "customer"`.

### 3.5 The cutover message

A fixed template, never model text, shown wherever gate evidence appears (#1801):

> **When to cut over is your decision.** The data movement for this step keeps your Aurora copy in sync with your source and reports the evidence below: replication lag, rows loaded, and validation per table (with the tables DMS could not validate, and why). These figures help you decide; they are not a recommendation to switch. Choosing the moment to cut over, and preparing your applications for it, remains entirely your responsibility.

DMS is used for step 1 because it is a proven, managed path for a same-engine move, with full load, CDC and built-in validation. Replacing it with Debezium is a possible future direction; it would first have to solve how the hub's source switches from the legacy database to Aurora at the step 1 cutover.

## 4. Part B: the hub

### 4.1 One hub per job (#1802)

From step 2 on, one Debezium connector on MSK Connect captures every table of the step 1 copy in Aurora into one compacted, primary-key-keyed topic per table, and each step's Flink job reads the topics it needs: one replication slot, one heartbeat, one copy of each table, and later steps never re-snapshot. The existing generator (`src/tools/data_movement/debezium_connector.py`) already sets message keys, `tombstones.on.delete=true`, topics created compacted by Kafka Connect on their first record (KIP-158), heartbeats, MySQL schema history, IAM authentication and Secrets Manager credentials. It changes from per wave to per job: no wave number in any name; one connector over every contract table; `snapshot.mode=initial` (Debezium's own snapshot is the initial load); the incremental-snapshot signal channel only when the source is Aurora, so with no step 1 nothing is written into the customer's source; on PostgreSQL, every table listed as needing `REPLICA IDENTITY FULL`.

### 4.2 MSK (#1803)

Our per-job stack creates a provisioned MSK cluster with IAM access control, TLS and encryption at rest, removed with the migration; `ExistingClusterArn` lets a customer use their own. MSK Serverless is excluded: it allows 120 compacted partitions per cluster (adjustable through support), below real schemas.

Sizing, from rule-catalog rows with reasons: production 3 brokers across 3 Availability Zones, replication factor 3, `min.insync.replicas=2`; a demo with 2 brokers; broker type from peak write throughput; storage from rows times average row size times replication factor, with headroom for records not yet compacted; 1 partition per topic, more for hot tables, fixed at creation; `delete.retention.ms` raised to 7 days, because a rebuild must reach a topic's head within it or it can miss deletes; one MSK Connect worker, since the MySQL and PostgreSQL connectors read the log with one task. Prices come from the price table (#1433).

### 4.3 Hub gate (#1804)

Read-only MSK Connect and CloudWatch calls: the connector running, the snapshot complete for every table, heartbeat lag under the threshold. Later-step gates require it.

### 4.4 Why both Kafka and Flink

MSK is the durable, replayable copy of every table; Flink is the compute. Flink CDC sources reading Aurora directly were rejected: rebuilding a target would re-snapshot Aurora, the customer's live database after the step 1 cutover; each step's job would open its own binlog reader or replication slot; a stopped job would fall behind on Aurora itself, where a stalled PostgreSQL slot keeps WAL until storage fills.

## 5. Part C: the runtime

### 5.1 Shape

```
compacted topics (one per table, Debezium envelope)
   | kafka + debezium-json, PRIMARY KEY declared, cdc-events-duplicate=true
   v
one Managed Flink job per step (Flink 2.2), generated from step-<k>.json
   | source tables -> joins -> aggregates and ordered lists (our Java aggregate)
   | state in RocksDB, no time-to-live, checkpoints
   v
sink tables, PRIMARY KEY = target item key (upserts and deletes)
   v
our Java sinks: DynamoDB (#1808), ElastiCache (#1428) -> targets; failures -> a dead-letter topic per target
```

- Our Python compiler generates each step's job as Flink SQL (#1806) from the same intermediate plan the DuckDB backend runs in tests. PyFlink only assembles and submits it; no row-by-row Python runs.
- Our Java library (#1805), one fat JAR: a retraction-capable ordered-list aggregate (Flink's list aggregates cannot order, and their retraction is undocumented), the conversion functions with no exact Flink built-in, and the sinks.
- Recovery: checkpoints for restarts; lost state or a new contract version is rebuilt by replaying the topics.
- One Managed Flink application per step (#1809), with the step contract's hash in its configuration, sized from the collector's row counts for the tables it joins.

Rejected: our own Python workers (we would build join state, recovery and scaling), Kafka Streams (a second implementation, run and scaled by us), lookups against Aurora (read load, replica lag), and the AWS DynamoDB sink connector.

### 5.2 Operations in Flink SQL

| Operation | Flink SQL |
| --- | --- |
| `copy`, `constant`, `conditional`, `compose_key` | projections, `CASE`, fixed-format concatenation |
| `transform` | an exact built-in, or our Java function |
| `filter` | `WHERE` (a row leaving it is retracted, so its item is deleted) |
| `lookup`, `chain_lookup` | joins along the join path: each hop's equal column pairs as `ON`, plus its optional typed predicate on the joined table; `LEFT` when `on_parent_delete` nulls the field, `INNER` when it deletes the item |
| `polymorphic_lookup` | one `LEFT JOIN` per branch plus `CASE` |
| `embed_many` | `GROUP BY` the parent key with our ordered-list aggregate |
| `aggregate` | `COUNT`, `SUM`, `MIN`, `MAX`, `AVG`; `exists` as `COUNT(*) > 0` |
| `union` | `UNION ALL` with the type field |

### 5.3 Rules the Flink documentation imposes

- Regular joins accept updating inputs, and a change on either side updates the whole join result; they keep both sides in state forever. A state time-to-live "might affect the correctness of the query result", so no job sets one; state is sized instead.
- Debezium is at-least-once on failover: every job sets `table.exec.source.cdc-events-duplicate=true` with a primary key on each source table, and the sinks are idempotent upserts keyed by the target item key; a key change deletes the old key.
- Aurora PostgreSQL sources need `REPLICA IDENTITY FULL`, or Flink cannot interpret the Debezium data.
- Not documented, so tested: how `debezium-json` handles the null record after a delete, and the exact changelog mode a custom sink declares.

### 5.4 What the sinks guarantee

Both: idempotent writes; deletes; a key change deletes the old key; writes flushed at every checkpoint; a dead-letter record after bounded retries; TLS on. DynamoDB: `BatchWriteItem` batches of up to 25, unprocessed items retried with backoff; an item over 400 KB or a number over 38 digits to the dead-letter topic, never written partly. ElastiCache: hashes with `HSET` plus `HDEL` for fields no longer mapped, in one transaction; strings hold the canonical JSON; sets and sorted sets replaced atomically; no TTL while the key's `ttl_policy` is `none` (#1775), TTL with refill by the pipeline as the fast follow (#1776).

### 5.5 The equivalence test (#1807)

For every golden fixture and generated sequences of inserts, updates, deletes, duplicates and replays, the items Flink produces on a local mini-cluster equal a full DuckDB rebuild of the same final rows. It runs in its own CI job (a JVM and the connector JARs), not in `make test`.

## 6. Part E: gates for later steps (high level)

Parity is a pipeline self-check (#1810, #1811): the sinks read back a sample of the keys they wrote and compare them with what Flink computed; Flink and the sinks emit per-rule counters (items expected, written, deleted, dead-lettered) and end-to-end lag; with the health of the Flink application and the connector, these go to CloudWatch, and the gate reads them from outside the VPC. A step passes when the hub gate and the steps it depends on passed, lag stays under the threshold, written minus deleted equals expected per rule, read-back mismatches are zero over a minimum sample, and there are no dead letters; `unsupported` fields and application-maintained cache keys are named exceptions. The transformation logic itself is proven before deployment by the equivalence test.

Deferred, and stated in the deliverables: exact counts by scanning the targets, access-pattern parity against the source, an independent verifier. To decide when building: the sampling rate, counter semantics under replays, how expected counts are emitted, the window lengths.

## 7. The pilot (#1812)

Run with the maintainer against a pre-created Aurora cluster: wordpress (MySQL 8.0) first as a smoke run of the whole chain, then discourse (PostgreSQL 16) for the PostgreSQL path and scale. It checks ADR-033's assumptions: DMS's writes into Aurora, including full-load rows, reach the binary log and WAL; DMS's transactional apply keeps per-key order for Debezium; Flink's handling of the null record after a delete. A SQL Server sample with stored procedures waits for #1702.

## 8. `assess()`

The data movement adds nothing to the `assess()` stable surface for contract 1.4. The design contract changes only add fields under `artifacts.schema_designs`, which has no compatibility promise; designs may change in value once designers pick join paths, which the 1.4 proposal mentions. The inventory, with later candidates, is #1796.

## 9. Sources

AWS DMS (Kafka target, table mapping and transformation rules, data validation, replication instance classes); Amazon Aurora (MySQL binary logging, PostgreSQL logical replication); Amazon MSK and MSK Connect (connectors, quotas, MSK Serverless configuration); Amazon Managed Service for Apache Flink (Flink 2.2 and 1.20, Python applications, fault tolerance, scaling); Apache Flink (Debezium formats, joins, group aggregation, built-in functions, configuration); Debezium (MySQL and PostgreSQL connectors); Apache Kafka (log compaction); Amazon DynamoDB (quotas, `BatchWriteItem`); Amazon ElastiCache (`maxmemory-policy`, reserved memory).
