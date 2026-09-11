# ADR-028: Aurora PostgreSQL & MySQL Schema-Design Agents (Script-First)

**Status:** Accepted
**Date:** 2026-09-11
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-027 (Consolidate the Schema-Design Fleet into One Agent), ADR-025 (Consolidate the Deterministic Core Agent)

---

## Context

The modernizer ships schema-design agents for four targets (DynamoDB,
DocumentDB, ElastiCache, OpenSearch). The two relational Aurora targets
(`aurora_postgresql`, `aurora_mysql`) are wired at the orchestrator/tools layer
but have no downstream implementation: they fall through to a `not_implemented`
placeholder in the handler, or — for same-family sources — emit a "no redesign
needed" note via `_SAME_FAMILY` and never run an agent (see ADR-027).

We are adding real Aurora PG and Aurora MySQL schema-design agents. Two facts
shape the decision:

1. **The collector already normalizes source column types** into a canonical
   13-value enum (`NormalizedDataType`) and provides fully structured
   tables/columns/indexes/foreign-keys via `project_schema_design_input(...)`.
   Deterministic translation can therefore operate on normalized, structured
   input rather than parsing vendor DDL.
2. **The internal `DatabaseAgentSkills` reference package** offloads all
   mechanical DDL work (type mapping, DDL rewriting, constraint/index
   translation, validation guards, batching) to the LLM as markdown prose
   tables — while its *pricing* side already uses deterministic scripts with a
   "MUST NOT hand-calculate" rule. The schema side never received the same
   treatment.

## Decision

### 1. Script-first, LLM-augments

A deterministic translation core builds a *draft target schema* from the
normalized collector data. The LLM handles only judgment: ambiguous type intent,
Aurora-specific optimizations, heterogeneous residuals, and trade-offs. The
existing `SchemaDesignRunner` PE-review loop then runs. This inverts the
LLM-first pattern used by DynamoDB/DocumentDB.

The core lives in `src/tools/schema/aurora_common/` (shared by both engines):
`type_map.py` (machine-readable `NormalizedDataType → Aurora type` dicts),
`ddl_generator.py` (`AgentTable[] → CREATE TABLE/INDEX/FK/PK`),
`constraint_translator.py`, and `source_family.py`. Every input the core cannot
prove becomes a **typed "needs judgment" marker** for the LLM — never a silent
guess. This keeps the script/LLM boundary explicit and auditable.

### 2. Target-selection rule

- source PostgreSQL → Aurora PostgreSQL (homogeneous: carry-over + optimize)
- source MySQL → Aurora MySQL (homogeneous: carry-over + optimize)
- source anything else (Oracle, SQL Server, …) → Aurora PostgreSQL (heterogeneous: translate)

The heterogeneous translation work concentrates in the PostgreSQL agent.

### 3. `_SAME_FAMILY` override

Same-family sources no longer emit "no redesign needed." The orchestrator routes
PG→Aurora-PG / MySQL→Aurora-MySQL into the agent with
`migration_strategy="carry_over"`, whose value-add is the LLM's Aurora
optimization pass (partitioning, hot-query indexes, read-replica / I/O-Optimized
hints). Aurora is added to `IMPLEMENTED_SCHEMA_DESIGNERS` and the handler
dispatch.

### 4. Single-pass (no split→merge)

Aurora runs single-pass like OpenSearch. Relational carry-over does not need the
per-aggregate split→merge path used by DynamoDB, so `group_splitter` /
`group_merger` are left untouched.

### 5. Phasing

Phase 1 delivers Aurora PostgreSQL in full (builds the shared core + the whole
heterogeneous path). Phase 2 adds Aurora MySQL, reusing the core and adding only
the MySQL type-map dict, dialect quirks, contract, skills, and command.

## Consequences

- **Positive:** mechanical translation is deterministic and unit-testable
  (exhaustive type-map tests, golden-file DDL tests); the LLM's job is bounded;
  DDL correctness no longer rides on a prompt; type provenance (script- vs
  LLM-derived) is recorded on every column.
- **Negative:** diverges from the existing LLM-first engine pattern, so the two
  Aurora agents look different from the other four; a machine-readable type map
  must be maintained per source family.
- **Follow-up:** data migration/ETL execution, Aurora load-testing subpackages,
  and Oracle/SQL-Server → Aurora MySQL remain out of scope.
