# ADR-033: Data Movement Driven by a Transformation Contract

**Status:** Accepted (2026-11-02). The open questions and the assumptions the pilot tests are listed below; epic #1777 implements the transformation contract.
**Date:** 2026-11-02
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-029 (Assignment Re-Entry), ADR-031 (Query-First Routing and the Engine Rule Catalog), ADR-032 (One Facts Layer); the adoption paths design (`docs/architecture/designs/adoption-paths.md`), with the slice-first amendment planned under #1128

---

## Context

The assessment ends at schema design and synthesis: for each target engine it produces a data model, the access patterns it serves, and a plan of migration waves (`report.json`'s `migration_waves`, built by `src/agents/referee/migration_waves.py`). Wave 1 carries a `homogeneity` marker (`homogeneous`, `cross_engine`, `heterogeneous`, `no_move`) and moves every collected table to the Aurora engine the assessment chose, when there is one.

Part of the data movement layer exists. `src/tools/data_movement/plan.py` writes one `DataMovementPlan` per migration wave (wave 1 by DMS with no hub; later waves by Debezium into a hub of one compacted topic per source table). `src/tools/data_movement/debezium_connector.py` generates Debezium source connectors for MySQL and PostgreSQL on MSK Connect, with `tombstones.on.delete=true`, `cleanup.policy=compact` and one topic per table. `src/tools/data_movement/initial_load.py` implements the hand-off from initial load to CDC for MySQL (binlog position or GTID set) and PostgreSQL (replication slot and publication); its AWS and database adapters are #1436, and other dialects are #1435. What is missing is the transformation: nothing yet states, in a form code can validate and run, how source rows become target items.

Denormalised targets make that hard. A DynamoDB item or a cache value can be built from several normalised tables, so one source change can affect many items, and one item can need rows that arrive as separate change events. An earlier proof of concept had a model write the joins, and it failed on exactly that. Two saved DynamoDB designs show how common this is, one from a MySQL source and one from PostgreSQL: the wordpress design (MySQL 8.0) (17 tables, 146 attributes) carries no `join` lineage, yet 11 of its 17 tables have an `aggregate_pattern` other than `separate` (item collections or identifying relationships), and `wp_posts` is built from two source tables; the discourse design (PostgreSQL 16; 140 tables, about 1,540 attributes) has 3 foreign-key lookups in its lineage and 43 tables whose `aggregate_pattern` is not `separate` (37 identifying relationships, 6 item collections). The design's join lineage alone understates how often rows of one table depend on another.

The plan is moving to slice-first steps (planned: #1129 move units and stages, #1698 dependency graph and slices, under #1128): step 1 moves the whole database to the source-compatible Aurora engine in one cutover; each later step moves one slice, a set of move units that belong together, to one engine, through stages a to d, introducing at most one engine the customer does not run yet. `report.json` gains a separate `slice_plan` for this; `migration_waves` stays unchanged until a reader review switches the default view.

## Decision

### 1. A transformation contract per step, separate from the designs

Every step has a transformation contract, `transformation-contract/v<N>/step-<k>.json`, where `v<N>` is the assignment version and `k` the step number in `slice_plan` (planned, #1698). Rules are grouped by move unit (#1129), so a write group, a co-dependency group or a procedure unit is never split. A step's rules are exactly those that serve the step's queries: each query the design serves maps to an access pattern (its `query_ids`, `table_name` and `gsi_name`), each access pattern to the attributes it returns, and each rule lists the `query_ids` it serves. Different queries on one table can need different target shapes (an entity, a GSI, a cache key), each its own rule with its own denormalisation. A table read by queries in several steps is an input in each of those steps' contracts, each with only its own rules. A rule that serves queries in two steps belongs to the first step that needs it, and the later step references it through `depends_on`. Each file is hashed, and a step pins the hashes of the steps whose output it reads. The dry run and the data movement layer read only these files. Every step is classified by its starting point and its destination alone: homogeneous when the destination is the same engine family on Aurora (MySQL or MariaDB to Aurora MySQL, PostgreSQL to Aurora PostgreSQL), heterogeneous (cross-engine) otherwise. A homogeneous step is a whole-table mirror; a heterogeneous step runs on contract rules. A scope without an Aurora engine has no relational step, and every step in it is heterogeneous. Step 1 is built from wave 1 and does not wait for the slice plan. Until the slice plan exists, and for the sources it does not cover, later steps follow the migration waves; `DataMovementPlan` moves from one plan per wave to one plan per step (`data-movement/step-<k>/plan.json`) in the same change.

### 2. The contract's boundary

The contract's input is relational rows per source table, as change events (insert, update, delete, with the row before and after) read from Kafka. Its output is target items (for DynamoDB, keys and attributes; for the cache, keys and values) and what each input change does to them. Where the rows come from, which engine is read, who writes after a cutover, ordering, buffering and delivery belong to the data movement layer. The runtime never sees or classifies queries: change events carry the resulting rows, not the statements, and every query-specific decision is made at design time and compiled into rules: the access-pattern class (analysis and routing), the target shape and its lineage (schema design), and above all its joins, as join-path ids from the precomputed catalog (Decision 5), compiled into the functions that find the items a changed row affects and rebuild them (Decision 3). The only upstream fact a rule needs is its input schema: the collector's schema, or the schema step 1's contract produces when step 1 converted types; with no step 1 (for example a scope without an Aurora engine) it is the collector's.

### 3. The contract is a catalog for the data movement layer

Per input table, the contract states what it produces (the rules whose items it is the grain of, with the target table, entity and key functions), what it feeds (the rules it is a dependency of, as a lookup parent or an embed or aggregate child, each with a compiled function from a changed row to the item keys it affects), and per rule its required inputs, its delete semantics (an item delete, `on_parent_delete`, a row leaving the filter) and whether it is transaction-scoped (built only from a whole source transaction). The dry run and the tests call these functions; the runtime compiles the same intermediate plan into its own engine (Decision 10) and must produce the same items, which an equivalence test checks. Nothing re-derives a join from the designs. The catalog is indexed by input table because change events arrive per table, not because tables move whole: a step that moves only some of a table's queries (a read model fed from the table while its writes and other queries stay, as in stage a) has rules for those queries only. In the first delivery `transaction_scoped` is always false; a follow-up sets it from transaction evidence (captured transaction groups, #1682 and #1683; code constructs, #1680; procedure units, #1654) once those land.

### 4. Lineage is written at design time, in typed operations from a closed catalog

The schema designers write the lineage inside their designs, because the model that chose to denormalise is the one that knows how. They pick operations from a closed catalog (`copy`, `transform`, `compose_key`, `lookup`, `chain_lookup`, `polymorphic_lookup`, `embed_many`, `aggregate`, `union`, `filter`, `conditional`, `constant`) and fill typed parameters, as a Pydantic discriminated union; tables and columns are referenced by collector id. They never write SQL or free-text conditions. A field no operation can express is `unsupported`, with a plain-language description, and becomes a named customer hook.

The join is part of the lineage, and the most important part. Every operation that reads a table other than the item's own carries its join, as a join-path id from the precomputed catalog (Decision 5), never as SQL:

- many-to-one: `lookup` (one hop, including a self-join such as a parent category), `chain_lookup` (several hops), `polymorphic_lookup` (the parent table depends on a type column);
- one-to-many: `embed_many` (child rows as a list or map, with order and limit; one column gives a set), `aggregate` (count, sum, min, max, average, and exists for a boolean flag);
- many-to-many through a junction table: `embed_many` or `aggregate` over a two-hop path;
- composite foreign keys are paths with several column pairs.

The DynamoDB design's legacy join types map onto these: `foreign-key`, `self-join` and `multi-column` to `lookup`; `chain` to `chain_lookup`; `polymorphic-lookup` to `polymorphic_lookup`; `conditional` to a `lookup` whose path carries a typed predicate on the joined table; `json-construction` and `lookup-table` to `embed_many`; `aggregated-list` to `embed_many` of one column; `exists-check` and the `aggregate` calculation to `aggregate`.

The internal design contracts change additively: the DynamoDB design contract (`src/contracts/dynamodb_model_output.py`) from 1.2 to 1.3, and the ElastiCache design contract (`elasticache_model_output.py`) from 1.1 to 1.2. These are not the `assess()` contract (1.3): its stable `designs` block exposes `pk_template` and `sk_template` (since `assess()` 1.2) and the cache key pattern (as `keys[].pattern`), and those fields stay as they are. Only `artifacts.schema_designs`, which `docs/contracts/assess.md` documents as internal with no compatibility promise, gains fields. Old designs translate: legacy foreign-key joins become join paths, `case` calculations become `conditional`, transforms become `transform`; what does not translate is `unsupported`, with a finding.

### 5. Joins come from a precomputed catalog of join paths

Before schema design, our code builds the join paths from the collector's foreign keys (`Table.foreign_keys`) and from the joins the source queries perform, parsed with sqlglot in the source dialect; once procedure and trigger bodies are extracted into derived statements (#1653) and placed in routing (#1654), their joins are included too. Each path has a stable id, its hops and its cardinality (from collector statistics), and is compiled into the functions of Decision 3. Each hop is the join condition, written as data: the pairs of columns that must be equal on both sides (several pairs for a composite foreign key), with an optional typed predicate on the joined table (`{column, operator, value}`, the shape `filter` uses) for a join that also requires a constant condition, such as only active users. A `polymorphic_lookup` branch is a path plus the type value that selects it. A join on anything other than equality between columns (a range, an inequality) is unsupported in this release and names the source query that performs it. The schema design input (`prepare_schema_design_input`) carries the paths; designers pick a path id for every lookup, embed or aggregate. A join outside the catalog is allowed only as a declared join backed by a served query, with a reason that appears in the report.

### 6. Our code validates and compiles; nothing a model writes reaches the data unchecked

A deterministic validator checks every rule against the collector and the design: references exist, casts are allowed (`validate_aurora_type` and the Aurora type map for both Aurora engines), coverage holds both ways, key parts render the design's templates byte for byte, key grain is unique, every query of a step reaches at least one rule and every rule serves at least one query, and each join and filter agrees with the source queries it serves (#554). The compiler turns every operation into an intermediate plan with DuckDB SQL as its first backend (DuckDB is MIT-licensed and runs in memory); identifiers come only from validated ids and every model-supplied value is a bound parameter. Rules run on synthetic rows generated from collector statistics, with the edge values that break conversions, inside `finalize_schema_design`, so a design whose rules do not produce the promised shape never leaves schema design. An error blocks only its own rule; a warning never blocks. Engine limits the validator enforces: a DynamoDB number has at most 38 digits of precision, and an item is at most 400 KB including attribute names (size estimates stay in the dry run). The runtime chosen later must pass the same golden fixtures.

### 7. Step 1 needs no model

Step 1's rules mirror every source table and column into Aurora, with a type conversion only where the Aurora design delta or the Aurora type map changes a type, and are checked against the generated DDL. Mirroring whole tables keeps Aurora a complete copy for the later steps to read. A table with no primary key or unique index needs declared key columns, both for Debezium's message key (`message.key.columns`) and because DMS data validation skips such tables. Step 1 covers homogeneous sources (MySQL, MariaDB, PostgreSQL into the source-compatible Aurora engine); cross-engine and heterogeneous sources wait for their staged relational move (#1702).

### 8. Later steps build only on proven earlier steps

A later step's rules are rebased from legacy columns to the columns step 1 produced. A column that a later rule reads and that step 1 drops, renames without a mapping or converts lossily is a finding that blocks the rule. On the same synthetic rows, step 1 followed by the rebased rule must equal the designer's lineage on the legacy rows. At run time a step deploys only after the steps it depends on have passed their gates.

### 9. The cache step

The cache always comes after step 1, and moving to a cache is never offered as a modernization step of its own; this supersedes the `cache_first` path of the adoption paths design (section 8.2 and decision 4), through the planned amendment. With no cached reads there is no cache step, and with no relational engine to front there is none either. Every cache key has one writer, decided by a deterministic rule that runs in synthesis before step planning: pipeline keys, whose values derive from source rows through catalog operations, are a CDC-fed mirror the application only reads; application keys (rate limits, counters, sessions, data types that cannot be fed, keys over the memory budget) stay cache-aside with their TTL. The memory budget goes first to the keys that serve the most reads per GB.

In the first delivery pipeline keys have no TTL. Under ElastiCache's default `maxmemory-policy` (`volatile-lru`), only keys with a TTL are evicted, so the application keys are evicted first and the mirror is kept; once no key with a TTL is left, writes fail rather than evict. The memory budget is therefore a hard limit, sized against the node's memory less the reserved share (`reserved-memory-percent`, 25% by default). A TTL with refill by the pipeline follows as a fast follow. Values use one canonical encoding: JSON for string keys (the JSON data type needs Valkey 7.2 or later), one field per column in hashes, ISO-8601 UTC dates, decimals as strings.

### 10. Data movement: Debezium on MSK Connect into compacted topics, with DMS for step 1

DMS moves the source into Aurora for step 1 (full load, then CDC). DMS is used for step 1 because it is a proven, managed path for a same-engine move, with full load, CDC and built-in validation. Replacing it with Debezium is a possible future direction; doing so would first have to solve how the hub's source switches from the legacy database to Aurora at the step 1 cutover. Every later step reads Aurora through Debezium source connectors on MSK Connect, one compacted, primary-key-keyed topic per source table, tombstones on, so the stream holds the latest value of every row and a target can be rebuilt without reading the source again. Without a step 1, Debezium reads the source database (MySQL or PostgreSQL only, as the connector generator supports today).

Prerequisites, from the Aurora and Debezium documentation: on Aurora MySQL, a cluster parameter group with `binlog_format=ROW` (a writer reboot) and `binlog_row_image=FULL`, read from the writer only, since binary logs are not available on replicas; on Aurora PostgreSQL, `rds.logical_replication=1` with the `pgoutput` plugin and the `rds_replication` role, again from the writer, since Aurora PostgreSQL does not support logical decoding from replicas. Binary logging adds write I/O on Aurora. Topics are created compacted before the connectors start: MSK Serverless allows compaction only at topic creation, and Debezium's auto-created topics would otherwise use deletion.

The runtime is Amazon Managed Service for Apache Flink (Flink 2.2). Our compiler generates each step's job as Flink SQL from the same intermediate plan the DuckDB backend runs in tests; Flink keeps the join and aggregate state, so a change on either side of a join updates every item it affects, and a delete retracts what it produced. Every job deduplicates source events (`table.exec.source.cdc-events-duplicate` with a primary key on each source table), which with idempotent sinks makes duplicates, the load-to-CDC overlap and replays harmless. State has no time-to-live, because Flink documents that a TTL can affect correctness; it is sized from the collector's table sizes and rebuilt by replaying the topics when lost. We write our own sinks in Java for DynamoDB and ElastiCache (idempotent upserts keyed by the item key, deletes, a dead-letter topic per target) and our own retraction-capable aggregate for ordered and limited child lists, which Flink's list aggregates cannot express. An equivalence test checks, for every golden fixture and random sequences of changes, that Flink's items equal a full DuckDB rebuild of the same rows. Aurora PostgreSQL sources need `REPLICA IDENTITY FULL`. A rebuild from a topic must reach its head within the topic's `delete.retention.ms` (24 hours by default), or it can miss deletes; compaction is not immediate, so a topic can still hold older values of a key. Everything deployed into a customer account is a tagged CloudFormation stack, and gates never write into the source.

### 11. Release limits

This release is one-way: data flows from the engine that holds the writes to the targets, and nothing is written back to the source or to Aurora. Reverse feeds (the replicas of tables whose writes move to a non-relational engine, #1425, and trigger side effects reproduced by the pipeline) come later; after a cutover, reads elsewhere of a moved table stop getting fresh data, and the deliverables say so. This release does not support source schema changes during the migration window: the deliverables require a schema freeze while steps run, and a detected change stops the rules it touches and asks for a re-assessment. Cutover itself stays the customer's decision; there is no cutover tooling.

## Enforcement

| Decision | Enforced by |
|---|---|
| 1. A transformation contract per step | none yet -- #1764 |
| 2. The contract's boundary | none yet -- #1766 |
| 3. The contract is a catalog | none yet -- #1766 |
| 4. Lineage at design time, in typed operations | none yet -- #1772 |
| 5. Joins from a precomputed catalog of join paths | none yet -- #1765 |
| 6. Our code validates and compiles | none yet -- #1763 |
| 7. Step 1 needs no model | none yet -- #1764 |
| 8. Later steps build only on proven earlier steps | none yet -- #1768 |
| 9. The cache step | none yet -- #1770 |
| 10. Debezium on MSK Connect into compacted topics, DMS for step 1 | `tests/unit/tools/data_movement/test_plan.py::test_wave_one_is_dms_from_the_legacy_source_with_no_hub`<br>`tests/unit/tools/data_movement/test_plan.py::test_later_waves_are_debezium_from_the_aurora_engine`<br>`tests/unit/tools/data_movement/test_debezium_connector.py::test_every_table_topic_is_created_compacted_with_its_partitions` |
| 11. Release limits | by design -- a scope limit of this release (one-way, no source schema changes during the migration window); the deliverables state it with #1426 |

## Consequences

- One artifact per step to version and validate, in exchange for a contract that matches what the customer moves and lets the contract and the data movement layer change independently. `DataMovementPlan` changes from per wave to per step.
- The designers' prompts and the schema design input change (typed lineage, path ids), in exchange for joins that are right by construction; repeatability is measured as the share of attributes picking the same path id across runs.
- Some fields need customer code (`unsupported` hooks) until the catalog grows.
- Two CDC tools (DMS, Debezium) and two hops of latency for later steps; Aurora needs binary logging or logical replication, with a reboot, from the start of step 1.
- The cache's memory budget is a hard limit while pipeline keys have no TTL.
- One-way and the schema freeze narrow what this release can move while other engines still read a table, in exchange for a much simpler first release.

## Assumptions to test in the pilot

The pilot runs wordpress (MySQL 8.0) first as a smoke run of the whole chain, then discourse (PostgreSQL 16) as the pilot that counts: the PostgreSQL path, scale and the tables that are not one-to-one. A SQL Server sample with stored procedures waits for the staged relational move (#1702).

- DMS's writes into Aurora, including full-load rows (`LOAD DATA` on Aurora MySQL), appear in Aurora's binary log or logical replication stream, so Debezium on Aurora sees them. No AWS document states this; it follows from DMS connecting as an ordinary SQL user.
- DMS's transactional apply keeps per-key order closely enough for Debezium reading Aurora downstream.
- MSK Connect against MSK Serverless, if Serverless is chosen: the MSK Connect documentation supports any Kafka cluster reachable from the VPC, but names neither cluster type explicitly.

## Open questions

- Sizing and cost per step: Flink parallelism and state, and MSK. MSK Serverless: 120 compacted partitions per cluster (adjustable), 250 GB per compacted partition, 5 MBps ingress per partition. MSK Connect: 60 workers per account, 10 per connector.
- Reconciliation evidence for the gates without writing into the source. DMS data validation covers step 1 for tables with a primary key or unique index (not views, not Db2 for z/OS; it stops after 10,000 failed records); later steps need counts and checksums per rule.
- Incremental snapshots without a step 1: Debezium's signal table needs write access in the database it reads, which would be the source. Use the MySQL read-only (GTID) mode, or no incremental snapshots there.
- Failed transforms: dead-letter topics per target, reporting and replay.
- Security and observability of the pipeline (credentials, encryption, least privilege, lag and failure metrics).
- DocumentDB and OpenSearch contracts (parked).

## Alternatives considered

- **A model writes the joins or the transformation code.** Rejected: an earlier proof of concept failed on the joins, and nothing a model writes should reach the data unchecked.
- **One contract for the whole migration, or one per engine wave.** Rejected: a step spans parts of several engine waves, and the contract would not match what the customer moves at once.
- **DMS writing into Kafka for every target.** Rejected. DMS transformations work per row of one table, with no joins across tables. Its Kafka target describes deletes as records with `operation: delete` (no tombstones are documented), publishes each row change as one message "regardless of transactions" (transaction ids can be attached, but there is no end-of-transaction record), does not support full LOB mode or batch apply, supports one topic per table only from version 3.4.6, and does not support IAM access control for Amazon MSK or MSK Serverless.
- **A Debezium JDBC sink for step 1 instead of DMS.** Rejected. A hub fed from the legacy source would have to switch to Aurora after the step 1 cutover. The sink writes record by record with no bulk load comparable to a DMS full load, does not delete rows by default (`delete.enabled` must be set), its schema evolution cannot change column types, drop columns or change primary keys, and batches with a repeated key can fail on PostgreSQL. DMS covers SQL Server, Oracle and Db2 sources into both Aurora engines, maps data types, works with DMS Schema Conversion for DDL, and has built-in data validation.
- **A Valkey Streams broker instead of Kafka.** Rejected for the hub. A stream supports several consumer groups and keeps entries until they are trimmed, but it lives in memory within `maxmemory` and has no compaction by key, so it cannot hold a bounded latest-state copy of each source table that rebuilds a target without reading the source; Kafka's key compaction does. Norbert Funke and Dinesh Kumar's CDC-to-ElastiCache prototype, which uses one, remains the prior proof of the Debezium source side (#1412).
- **Flink CDC sources reading Aurora directly, with no Kafka.** Rejected: one service fewer and cheaper, but rebuilding a target would re-snapshot Aurora, which after the step 1 cutover is the customer's live database; every step's job would open its own binlog reader or replication slot; and a stopped job would fall behind on Aurora itself, where a stalled PostgreSQL slot keeps WAL until storage fills. The compacted topics are captured once, shared by every step, and replayed without touching the database.
- **Reverse feeds and run-time schema change handling in the first release.** Deferred (Decision 11).

## Sources

- AWS DMS: Kafka target (`CHAP_Target.Kafka.html`), table mapping (`CHAP_Tasks.CustomizingTasks.TableMapping.html`), sources and targets (`CHAP_Introduction.Sources.html`, `CHAP_Introduction.Targets.html`), Aurora MySQL target (`CHAP_Target.MySQL.html`), data validation (`CHAP_Validating.html`).
- Amazon Aurora: MySQL binary logging (`USER_LogAccess.MySQL.BinaryFormat.html`), PostgreSQL logical replication (`AuroraPostgreSQL.Replication.Logical.html`).
- Amazon MSK: MSK Connect (`msk-connect.html`, `msk-connect-service-execution-role.html`), MSK Serverless configuration (`serverless-config.html`), quotas (`limits.html`).
- Amazon DynamoDB: quotas (`Constraints.html`), `TransactWriteItems`, Streams.
- Amazon ElastiCache: JSON (`json-gs.html`), parameters (`maxmemory-policy`, `reserved-memory-percent`).
- Debezium: MySQL and PostgreSQL connectors, the JDBC sink, the reselect columns post-processor.
- Apache Flink: the Debezium formats, joins, group aggregation, built-in functions, the upsert Kafka connector, configuration (`table.exec.source.cdc-events-duplicate`, `table.exec.sink.upsert-materialize`). Amazon Managed Service for Apache Flink: Flink 2.2 and 1.20, Python applications, fault tolerance, scaling.
- Apache Kafka: design (log compaction), Kafka Streams core concepts. Valkey: streams introduction, key eviction. DuckDB: FAQ.
