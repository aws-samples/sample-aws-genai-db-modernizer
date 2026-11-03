# Design: the transformation contract

**Status:** Accepted (maintainer decisions, 2026-10-28 to 2026-11-03). Design only: no code changes with it.
**Issue:** epic #1777. **Decisions:** ADR-033, data movement driven by a transformation contract. **Related:** the data movement design (epic #1813), the `assess()` inventory (#1796), slice-first steps (#1128, #1698).

## 1. Summary

1. For every step of the migration plan, a transformation contract states how source rows become target items: for each target field, the source table and column it comes from, how it is joined to other tables, and how it is converted. The dry run and the data movement layer read only these files.
2. Nothing a model writes reaches the data unchecked. Schema designers choose operations from a closed catalog and join paths from a catalog our code builds; our code validates every rule, compiles it, and runs it on synthetic rows before anything moves.
3. One file per step, `transformation-contract/v<N>/step-<k>.json`, where `v<N>` is the assignment version and `k` the step number; each file is hashed, and a step pins the hashes of the steps whose output it reads.
4. Delivered in three parts: step 1 (the move to Aurora, no model), the cache step, and the DynamoDB steps. DocumentDB and OpenSearch contracts are not designed yet.

| Part | What it covers | Issues |
| --- | --- | --- |
| Shared | operation models, the step contract, the compiler, synthetic rows, the join-path catalog, the catalog view, rebase onto step 1 | #1762, #1763, #1765, #1766, #1767, #1768 |
| Step 1 | whole-table mirror rules into Aurora | #1764 |
| Cache step | ElastiCache design 1.2, the cache key writer rule and memory budget, the builder | #1769, #1770, #1771 |
| DynamoDB steps | DynamoDB design 1.3 and the translator for older designs, the builder, transaction scope (follow-up) | #1772, #1773, #1774 |

## 2. The boundary

The contract's input is relational rows per source table, as change events (insert, update, delete, with the row before and after) read from Kafka. Its output is target items (for DynamoDB, keys and attributes; for the cache, keys and values) and what each input change does to them. Where the rows come from, which engine is read, who writes after a cutover, ordering, buffering and delivery belong to the data movement layer.

The runtime never sees or classifies queries: change events carry the resulting rows, not the statements. Every query-specific decision is made at design time and compiled into rules: the access-pattern class (analysis and routing), the target shape and its lineage (schema design), and above all its joins.

The only upstream fact a rule needs is its input schema: the collector's schema, or the schema step 1 produces when step 1 converted types. With no step 1 (a scope without an Aurora engine), it is the collector's.

## 3. A step's rules come from its queries

A step's move units are sets of queries. The design maps each query to an access pattern (its `query_ids`, table and index), and each access pattern to the attributes it returns. A rule is in a step only when it serves a query of that step, and each rule lists the `query_ids` it serves.

- Different queries on one table can need different target shapes (an entity, an index, a cache key), each its own rule with its own denormalisation.
- A table read by queries in several steps is an input in each of those steps' contracts, each with only its own rules.
- A rule that serves queries in two steps belongs to the first; the later step references it through `depends_on`.
- A step that moves only some of a table's queries (a read model fed from the table while its writes stay, stage a of the slice plan) has rules for those queries only.

## 4. The operation catalog

Operations are a Pydantic discriminated union on `op`. Tables and columns are referenced by collector id (`schema.table`, column name); no operation carries SQL or free text.

| Operation | What it builds |
| --- | --- |
| `copy` | one source column, unchanged |
| `transform` | a source column converted by one function from a closed list (`cast` first) |
| `compose_key` | a key from typed parts, for example `POST#` and a column |
| `lookup` | a parent row's column through a many-to-one join |
| `chain_lookup` | a column several many-to-one hops away |
| `polymorphic_lookup` | a parent whose table depends on a type column |
| `embed_many` | child rows as a list, a map, or a set of one column, with order and limit |
| `aggregate` | count, sum, min, max, average, or exists over child rows |
| `union` | several tables into one target, with a type field |
| `filter` | a typed predicate (`{column, operator, value}`); a row leaving it removes its item |
| `conditional` | value-to-label mapping, like `CASE` |
| `constant` | a fixed value |
| `unsupported` | a field no operation can express: a plain-language description that becomes a named customer hook |

### 4.1 Joins

The join is part of the lineage, and the most important part. Every operation that reads a table other than the item's own carries its join as a join-path id, never as SQL:

- many-to-one: `lookup` (a self-join included), `chain_lookup`, `polymorphic_lookup`;
- one-to-many: `embed_many`, `aggregate`;
- many-to-many through a junction table: `embed_many` or `aggregate` over a two-hop path;
- a composite foreign key: a path with several column pairs.

An earlier proof of concept had a model write the joins, and it failed on exactly that. So before schema design, our code builds a catalog of join paths from the collector's foreign keys and from the joins the source queries perform (parsed with sqlglot in the source dialect; procedure and trigger statements join once they are extracted, #1653 and #1654). Each path has a stable id, its hops and its cardinality from collector statistics. Each hop is the join condition written as data: the pairs of columns that must be equal on both sides, with an optional typed predicate on the joined table for a join that also requires a constant condition. A `polymorphic_lookup` branch is a path plus the type value that selects it. A join on anything other than equality between columns is unsupported in this release.

Designers pick a path id for every join. A join outside the catalog is allowed only as a declared join backed by a served query, with a reason that appears in the report.

Each path is compiled into the functions the data movement layer and the tests use: from a changed row to the item keys it affects, and item building.

## 5. Validation and compilation

Validation runs in two stages:

- **Stage A, inside the designer's finalize loop:** references exist; casts are allowed (`validate_aurora_type` and the Aurora type map for Aurora targets); coverage holds both ways (every target field has a rule, every rule points at the design); key parts render the design's templates byte for byte; key grain is unique; every query of a step reaches a rule and every rule serves a query; each join and filter agrees with the source queries it serves (#554); rules run on synthetic rows. Findings go back to the designer the way the PE review does, so a design whose rules do not produce the promised shape never leaves schema design.
- **Stage B, after synthesis:** the step's rules are assembled per step, rebased onto step 1, and checked for continuity and equivalence (section 7).

An error blocks only its own rule; a warning never blocks.

The compiler turns each operation into a backend-neutral plan. DuckDB SQL is the first backend, for tests and the dry run; the runtime's Flink SQL backend is generated from the same plan (epic #1813), and golden fixtures, run on both, keep them equal. Identifiers come only from validated ids and are quoted; every value is a bound parameter. The DuckDB type map follows the target engine's semantics, not DuckDB's: on Aurora MySQL, `BOOLEAN` is `TINYINT(1)` and accepts 2.

Synthetic rows come from collector metadata only (types, lengths, precision, nullability, keys), never sample rows, and always include the values that break conversions: MySQL zero dates, unsigned integers above the signed range, `tinyint(1)` values other than 0 and 1, the largest decimal for its precision, and multi-byte text at the column's limit, with PostgreSQL equivalents.

## 6. Step 1

Step 1's rules need no model. One rule per wave 1 table mirrors every column into Aurora, with a `transform` only where the Aurora design delta or the Aurora type map changes a type. Target names and types come from the saved Aurora design, or from the Aurora DDL generator when there is none, and the validator checks them against the generated DDL. Mirroring whole tables keeps Aurora a complete copy for the later steps to read.

- Views are listed, never ruled.
- A table with no primary key or unique index is a warning: DMS cannot validate it, and Debezium needs declared key columns.
- A conversion Aurora would reject on a synthetic edge value blocks that rule.
- Homogeneous sources only (MySQL or MariaDB to Aurora MySQL, PostgreSQL to Aurora PostgreSQL). A cross-engine source gets no rules and the reason "staged relational move pending (#1702)"; a scope without Aurora has no step 1.

## 7. Later steps build on proven earlier steps

A later step's rules are rebased from source columns to the columns step 1 produced. A column that a later rule reads and that step 1 drops, renames without a mapping or converts lossily is a finding that blocks that rule. On the same synthetic rows, step 1 followed by the rebased rule must equal the designer's lineage on the source rows. Each step pins the hashes of the steps it depends on.

## 8. The cache step

The cache always comes after step 1 and reads Aurora; moving to a cache is never offered as a modernization step of its own. With no cached reads, or no relational engine to front, there is no cache step.

- **ElastiCache design 1.2 (additive):** typed key parts that render `key_pattern` and `example_key`; one operation per field with table-qualified columns; join-path ids. `key_pattern`, `fields_mapped` and `example_key` stay as they are.
- **One writer per key**, decided by a deterministic rule in synthesis before step planning: a key is pipeline-maintained when every value derives from source rows through catalog operations and it fits the memory budget; otherwise it is application-maintained (cache-aside with its TTL), with the reason: an unsupported field, not derived from rows, a data type the pipeline cannot feed (streams, HyperLogLog, Bloom filters, bitmaps, geo), size unknown, or over budget. The budget goes first to the keys that serve the most reads per GB of mirror.
- **Canonical encoding:** string keys hold JSON (one object per row, arrays for child rows, ISO-8601 UTC dates, decimals as strings, NULL as null); hashes hold one field per column, and a NULL column is an absent field.
- **TTL:** pipeline keys have no TTL in the first delivery; a TTL with refill by the pipeline follows (#1775, #1776).
- **Checks:** placeholders resolve; a key built from copied fields needs a primary or unique key, and grouping rows into one key only goes through `embed_many`; the data type fits the lineage; the encoding survives the edge values.

## 9. The DynamoDB steps

- **DynamoDB design 1.3 (additive):** one typed operation per attribute with join-path ids for joins; an optional entity filter; typed key parts. `pk_template` and `sk_template` stay required and byte-identical. Three internal consumers that read the old lineage fields treat the typed lineage as derived (the finalize gate, key reconciliation, the data movement plan's attribute gaps).
- **Older designs translate:** the ten legacy join types map onto operations over catalog paths (foreign key, self-join and multi-column to `lookup`; chain to `chain_lookup`; polymorphic to `polymorphic_lookup`; a conditional join to a `lookup` with a predicate; JSON construction, lookup table and aggregated list to `embed_many`; exists check to `aggregate` exists), `case` to `conditional`, transforms to `transform`; anything else is `unsupported` with a finding.
- **Checks:** every join is a catalog path (or a backed declared join) with the right cardinality; key grain in single-entity tables and item collections; every index key attribute has an operation; key parts render the templates byte for byte; types fit, with numbers within 38 digits.
- **Transaction scope:** a rule that embeds or aggregates rows the source writes in one transaction is marked transaction-scoped, from captured transaction groups, code constructs or procedure units, once those land (#1774). Until then the flag is false and items are built from each event and corrected by the next.

Two saved designs show why the join-path catalog matters even when a design records few joins: the wordpress design (MySQL) records no join lineage, yet 11 of its 17 tables are item collections or identifying relationships; the discourse design (PostgreSQL) records 3 foreign-key lookups and has 43 tables that are not one-to-one.

## 10. The catalog for the data movement layer

Per input table, a step's contract states what it produces (the rules whose items it is the grain of, with the target table, entity and key functions), what it feeds (the rules it is a dependency of, each with its path id and compiled function to the affected items), and per rule its required inputs, its delete semantics (an item delete, a parent delete, a row leaving the filter) and whether it is transaction-scoped. The data movement layer never re-derives a join from the designs.

## 11. `assess()`

The design contracts change additively (DynamoDB 1.3, ElastiCache 1.2); only `artifacts.schema_designs`, which has no compatibility promise, gains fields. The stable `designs` block does not change. Values in `designs` can change between releases once designers pick join paths, which the contract 1.4 proposal mentions. The library caller agrees before #1769 and #1772 merge. The inventory is #1796.

## 12. Release limits

This release is one-way (nothing is written back to the source or to Aurora) and does not support source schema changes during the migration window: the contract is built from the snapshot, the deliverables require a schema freeze while steps run, and a detected change stops the rules it touches and asks for a re-assessment.
