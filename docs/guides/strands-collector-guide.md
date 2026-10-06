# Collector Agent Implementation Guide

**Document Type:** Implementation Guide
**Status:** Current

---

## Overview

Collector agents connect to (or parse already-collected output from) a
source database and produce a `CollectorOutputContract` — schema, query
patterns, and (optionally) AWS metrics — for the rest of the pipeline.
Collection is **entirely deterministic**: there is no LLM and no Strands
agent anywhere in the collector path, despite the name of this guide (kept
for continuity with the other agent guides, which do use Strands for the
phases that need a model — see
[strands-agent-development-guide.md](strands-agent-development-guide.md)).

Five engine-specific collectors live under `src/agents/collector/`:
`mysql_collector.py`, `mariadb_collector.py`, `postgres_collector.py`,
`sqlserver_collector.py`, `oracle_collector.py`. `handler.py` dispatches to
the right one based on an `ENGINE` env var (or, for the common local path,
`run_assessment.py` reads the file directly — see "How collection actually
happens" below).

---

## Three Collection Modes

Each engine-specific collector supports three modes
(`src/contracts/collector_input.py`'s `CollectionMode`):

| Mode | What it does | Used by |
|------|--------------|---------|
| `offline` | Parses the output of the read-only SQL scripts (`scripts/collect-mysql.sql`, `collect-postgresql.sql`, `collect-sqlserver.sql`, `collect-oracle.sql`) that a user runs themselves against their own database | `run_assessment.py --file <collection.json>` ([Analyzing your own database](../use-claude-code.md#analyzing-your-own-database)) |
| `ddl` | Parses schema DDL fetched from S3 — no live connection | The AWS Transform integration |
| `live` | Connects to a real database through an SSM Run Command on a per-VPC automation instance; credentials are resolved on the automation instance and never appear in SSM command history | The opt-in `automation.yaml` path, gated behind `MODERNIZER_ENABLE_AUTOMATION=1` (checked in `src/api/routes/assessments.py`, returns 501 when unset); tracked further in [#342](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/342) |

There is no direct-database-connection path anywhere in this codebase —
`live` mode always runs its SQL through SSM on the automation instance
(`src/tools/aws/ssm_executor.py`), never a credential handed to the local
process.

### How collection actually happens for the common case

The fastest path — what the sample fixtures and most real usage go
through — doesn't call an engine collector at all. `scripts/run_assessment.py`
(`phase_collect`) reads the `--file` argument and classifies it into one of
three cases:

- Already a `CollectorOutputContract` (`contract_version` starts with
  `"3"`) — writes it directly as `collector/output.json`. This is what the
  bundled sample fixtures, `docs/examples/wordpress/` and
  `docs/examples/discourse/`, provide.
- A raw collection file (`collection_version` present, or a `queries` list)
  — calls `parse_offline_collection` plus the engine collector's offline
  `_build_*` helpers to build the contract from it. This is the output of
  `collect-mysql.sql` / `collect-postgresql.sql` run against a real
  database.
- Neither — the script errors out with "Unrecognized file format."

```python
# scripts/run_assessment.py (phase_collect, simplified from the real code)
is_contract = isinstance(input_data.get("contract_version"), str) and input_data[
    "contract_version"
].startswith("3")
is_raw = "collection_version" in input_data or isinstance(input_data.get("queries"), list)

if is_contract:
    collector_data = input_data
elif is_raw:
    parsed = parse_offline_collection(input_data)
    # ... build a CollectorInput, call the mysql_collector _build_* helpers
else:
    _error("collect", "Unrecognized file format.")
```

---

## Checkpointed Collection (`offline`/`live` modes)

`collect()` in each engine collector is checkpoint-based and idempotent:

```python
# src/agents/collector/mysql_collector.py (real code)
def collect(input_contract: CollectorInput) -> CollectorOutputContract:
    """Entry point — init storage, dispatch to live or ddl mode with checkpoints."""
    ckpt = _init_checkpoint_store(input_contract)

    # If final output already exists, return it directly
    if ckpt.exists("output"):
        logger.info("Job %s: final output already exists, returning cached", input_contract.job_id)
        result: CollectorOutputContract = CollectorOutputContract.model_validate(
            ckpt.load("output")
        )
        return result

    if input_contract.mode == CollectionMode.live:
        result = _collect_live(input_contract, ckpt)
    elif input_contract.mode == CollectionMode.ddl:
        result = _collect_ddl(input_contract, ckpt)
    else:
        result = _collect_offline(input_contract, ckpt)

    # Save final output
    ckpt.save("output", result.model_dump(mode="json"))
    return result
```

Checkpoints are stored in S3 (`CheckpointStore`) when AWS credentials and a
cluster endpoint are available; otherwise collection runs through a
`NoopCheckpointStore` (no checkpointing — the normal case for local, offline
runs). Each collection stage saves its result as it completes, so a retried
run skips stages that already succeeded. **LIVE mode stages:** metadata,
schema, queries, AWS metrics, final output. **DDL mode stages:** DDL schema
(parsed from S3), AWS metrics, final output.

---

## Output Contract

`CollectorOutputContract` (`src/contracts/collector_output.py`) carries:
database metadata, normalized schema (tables, columns, indexes, foreign
keys, views, procedures, triggers), query patterns, and — when `live`
mode's AWS metrics stage succeeds — RDS instance metadata, CloudWatch
metrics, Performance Insights and Database Insights data. Collectors
normalize engine-specific types into a shared `NormalizedDataType` enum and
a shared `IndexType` enum (see the `_TYPE_MAP` / `_INDEX_TYPE_NORMALIZE`
tables at the top of `mysql_collector.py` for the full cross-engine
mapping) so downstream analysis agents don't need to know which source
engine produced the data.

The handler writes the result to the artifact store at
`{database_name}/{job_id}/collector/output.json`
(`src/agents/collector/handler.py`):

```python
# src/agents/collector/handler.py (real code, trimmed)
result = _dispatch_collect(engine, input_contract)
key = f"{database_name}/{job_id}/collector/output.json"
store.write_json(key, json.loads(result.model_dump_json()))
```

---

## Error Handling

Each stage degrades independently: a required stage (schema) failing is
fatal; an optional stage (AWS metrics, query patterns on an engine without
`performance_schema`/`pg_stat_statements` enabled) failing logs a warning
and the pipeline continues with that section empty. The collection scripts
(`scripts/collect-*.sql`) are read-only and never require write access to
the source database.

---

## Testing

Collector unit tests call the pure `_build_*` helpers on fixture rows and assert
on the parsed `CollectorOutputContract`; there is no model to mock since
collection never calls an LLM. See [testing-guide.md](testing-guide.md) and
`tests/unit/agents/test_mysql_collector.py`, `test_mariadb_collector.py`,
`test_oracle_collector.py`, `test_sqlserver_collector.py` and
`test_collector_handler.py`.

---

## Related Documentation

- [Orchestrator README](../../src/orchestrator/README.md)
- [Strands Agent Development Guide](strands-agent-development-guide.md)
- [Referee Agent Guide](referee-agent-guide.md)
- [Contract Specifications](../contracts/agent-contracts-spec.md)
