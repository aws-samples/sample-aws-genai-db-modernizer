# Referee Agent Implementation Guide

**Document Type:** Implementation Guide
**Status:** Current

---

## Overview

"The referee" is three agents, run in this order by `LocalOrchestrator`:

- **Referee-Triage** (`src/agents/referee/triage.py`) — reads collector output,
  detects workload signals, and selects which analysis agents run. Pure
  Python, deterministic, no LLM.
- **Referee-Reality-Check** (`src/agents/referee/reality_check.py` +
  `reality_check_handler.py`) — a deterministic CTO-level consolidation pass
  over the initial query-to-engine assignment, with an LLM validation step
  (`--llm-mode bedrock`/`external`) that can be skipped (`--llm-mode none`).
- **Referee-Synthesis** (`src/agents/referee/synthesis_handler.py`) — reads
  all analysis, schema design and load test outputs, produces the weighted
  ranking, TCO, risk assessment and executive summary.

All three run as direct Python function calls under `LocalOrchestrator`, over
the local `ArtifactStore` — there is no Step Functions state machine and no
per-agent ECS task. That hosted mechanism was retired in
[#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175).
See [the orchestrator README](../../src/orchestrator/README.md) for the full
pipeline diagram.

---

## 1. Referee-Triage

### Purpose

Read collector output, classify the workload, and select which analysis
agents should run. Not every engine runs for every workload — a key-value
PostgreSQL workload doesn't need an OpenSearch analysis pass.

### How it actually works

Triage is pure pattern detection — regex and structural checks over the
collector's schema and query patterns, with **no LLM call at all**. It
matches ten query-pattern groups (key-value lookups, range queries,
status filters, writes, complex joins, aggregations, large scans,
time-series, session store, metadata/config) and five schema-level signals
(JSON columns, junction tables, high FK density, self-referential FKs, EAV
pattern), each mapping to one or more candidate target engines:

```python
# src/agents/referee/triage.py (real code, trimmed)
def triage(collector_output: dict) -> TriageResult:
    """Analyze collector output and decide which analysis agents to run."""
    signals = _detect_schema_signals(collector_output)
    signals.extend(_detect_query_signals(collector_output))

    # Determine which Aurora agent matches the source engine
    source_engine = (
        collector_output.get("metadata", {}).get("source_database", {}).get("engine", "")
    ).lower()
    aurora_agent = SOURCE_ENGINE_TO_AURORA.get(source_engine)

    result = _select_agents(signals, aurora_agent=aurora_agent)

    # Detect hard capability requirements per query
    result.query_capabilities = _detect_query_capabilities(collector_output, signals)

    return result
```

Aurora (the engine matching the source database — `aurora_mysql` or
`aurora_postgresql`) is always selected as the relational baseline; it
competes alongside the NoSQL/search/cache candidates for every query rather
than being an automatic default. `keyspaces` and `neptune` are tracked as
`deferred` signals for the synthesis report but are not dispatched as
analysis agents today (Phase 1 scope).

### Output (`triage.json`, `TriageOutputContract` in `src/contracts/triage_output.py`)

`triage()` itself returns an internal `TriageResult` dataclass (`selected`,
`skipped`, `baseline`, `deferred`, `signals`, `query_capabilities` — plain
dicts keyed by engine name). `triage_handler.py` converts that into the
contract actually written to `triage.json`, which has different field
names and list-of-object shapes:

- `selected_agents` — list of `{agent_type, reasons}`. `LocalOrchestrator`
  dispatches exactly these engines (plus the Aurora baseline) to the
  analysis phase.
- `skipped_agents` — list of `{agent_type, reason}`.
- `baseline` — dict of the Aurora signals accumulated for
  Referee-Synthesis (this one keeps the same shape as the internal result).
- `deferred_agents` — list of `{agent_type, reasons}` for Phase 1 signals
  (`keyspaces`, `neptune`), reporting only.
- `signals` — every detected `TriageSignalRecord` (signal, targets,
  evidence, query IDs, table IDs, query count), for the decision trace.
- `query_capabilities` — per-query hard-capability requirements (detected via
  `src/agents/referee/capability_registry.py`), used later to catch an engine
  that cannot actually serve a query it was assigned.
- `confidence_score` — 0-100, computed from signal and selection counts (see
  `_compute_triage_confidence` in `triage_handler.py`).
- `job_id`, `database_name`, `contract_version`, `agent_type`, `timestamp` —
  envelope fields.

There is no confidence-threshold fallback to "run everything" — triage
always selects at least the matching Aurora engine, and any workload signal
adds its target engines deterministically.

---

## 2. Referee-Reality-Check

### Purpose

Run after the initial assignment, before schema design. Default posture: at
most 2 committed engines; a third must provide a genuinely unique capability
no other committed engine can serve. Five deterministic passes
(`src/agents/referee/reality_check.py`):

0. **Unique value assessment** — per engine, which assigned queries does it
   serve meaningfully better than the next-best alternative (`unique`) vs.
   nearly-as-well (`redundant`)?
1. **Aurora absorption** — pull orphan queries from low-count engines into
   an already-committed Aurora engine when it can serve them.
2. **Consolidation** — absorb a redundant engine's queries into a committed
   engine (duplicate/already-assigned/can't-serve checks).
3. **Architectural pattern detection** — CQRS, materialized views, event
   sourcing.
4. **Integration topology** — specific sync mechanisms between the
   surviving engines.

The deterministic pass writes a **new assignment version**
(`src/storage/assignment_versioning.py` — reality check and customer edits
always write a new version, never overwrite one) plus a
`RealityCheckOutputContract` (`reality-check/output.json`).

The LLM seam (`run_reality_check_deterministic` /
`prepare_reality_check_llm_input` / `apply_reality_check_llm_output`, in
`reality_check_handler.py`) writes the executive summary. In `bedrock` mode it
also runs `validate_consolidations` from
`src/agents/referee/consolidation_validator.py` — an LLM validation pass
over pass 2's consolidation decisions ("can the target engine actually
serve these query patterns, or will this fail during schema design?",
avoiding a flip-flop where schema design later fails on a query
consolidation moved). This is not a pure rubber stamp: `apply_corrections`
can redirect a query a consolidation moved to Aurora or back to its
original engine when the model flags it as unserviceable —
`corrections_for_moved_queries` restricts this to exactly the queries
pass 2 touched, so the model can undo or redirect a consolidation's move
but cannot make a fresh ownership decision outside of it. In `--llm-mode
none`, reality check keeps its deterministic result and skips this
validation pass entirely (no corrections possible).

In `external` mode Claude Code answers through `prepare_reality_check_llm_input`
and `apply_reality_check_llm_output` instead. In every mode, the handler then runs
`sanity_sweep` (also from `consolidation_validator.py`) as a deterministic final
step over the consolidations.

```python
# src/agents/referee/reality_check.py (docstring, real code)
"""
Reality Check — CTO-level optimization of query-to-engine assignments.
...
Default posture: 2 engines max. A third engine must provide genuinely unique
capabilities that no other committed engine can serve.
"""
```

---

## 3. Referee-Synthesis

### Purpose

Read every analysis, schema design and load test artifact for the job and
produce the final report: a weighted ranking per engine, the recommended
architecture, table mappings, query groups, TCO analysis, risk assessment,
the migration wave plan, and (via the LLM seam) the executive summary.

```python
# src/agents/referee/synthesis_handler.py (real docstring)
"""Referee-Synthesis agent handler — produces the modernization report.

Reads all pipeline artifacts (triage, collector, analysis, schema design)
via ArtifactStore, builds a comprehensive report with architecture
recommendations, table mappings, query groups, TCO analysis, and risk
assessment.

LLM seam functions (for Skill Sync / external LLM integration):
- run_synthesis_deterministic — full report without any LLM call
- prepare_synthesis_llm_input — formats the LLM request payload
- apply_synthesis_llm_output  — merges LLM output into deterministic result
"""
```

Everything except the executive summary narrative is deterministic:
`src/agents/referee/synthesis_report.py` builds the ranking, TCO analysis,
risk assessment, table mappings, query groups and cache-overlay accounting;
`src/agents/referee/migration_waves.py` derives the incremental wave plan
from the assignment; `src/agents/referee/synthesis_grounding.py` checks that
any LLM-written summary text is actually grounded in those deterministic
numbers before accepting it (falling back to a deterministic summary if
not). With `--llm-mode none`, synthesis keeps the deterministic executive
summary.

The output carries a `needs_deeper_analysis` flag (left over from the
retired Step Functions flow — the `synthesis_handler.py` module docstring
still lists it as feeding "Step Functions ... for the analysis loop").
Nothing in `LocalOrchestrator` or anywhere else in the local pipeline reads
it today; the deterministic builder sets it, and that is where it stops.

### Output (`SynthesisOutputContract`)

Written to `{database_name}/{job_id}/synthesis/v{assignment_version}/report.json`
for the normal case (any job that went through assignment/reality check, so
`assignment_version > 0` — this is what `/modernize` always produces). Falls
back to `{database_name}/{job_id}/referee-synthesis/report.json` only when
`assignment_version` is 0.

Key fields: `ranking` (per-engine, ordered by workload share, with
`routed_confidence` — the mean fit of the queries actually routed to that
engine, labelled `signal_only` when no rated source table backs it),
`architecture_recommendation`, `table_mappings`, `query_groups`,
`tco_analysis`, `risk_assessment`, `migration_waves`, and the optional
`cache_overlay` (the cache layer's queries and share of calls, which the
owner distribution never counts — see `src/agents/referee/cache_overlay.py`
and `build_cache_overlay` in `synthesis_report.py`).

---

## Testing

See [testing-guide.md](testing-guide.md). The core triage logic is tested
in `tests/unit/agents/test_triage_handler.py`, and the core reality-check
consolidation logic in `tests/unit/test_reality_check.py`. Most of the
synthesis and reality-check *handler* behavior — the LLM seam, grounding,
versioning, cache overlay, migration waves — lives under
`tests/unit/agents/referee/` (for example `test_synthesis_llm_seam.py`,
`test_reality_check_llm_seam.py`, `test_reality_check_determinism.py`).
Every deterministic builder is tested with no model call; the LLM seam
functions are tested by patching the model entry point and feeding a
canned `llm_output.json`.

---

## Related Documentation

- [Orchestrator README](../../src/orchestrator/README.md)
- [Strands Agent Development Guide](strands-agent-development-guide.md) — the LLM seam pattern in detail
- [Contract Specifications](../contracts/agent-contracts-spec.md)
- `AGENTS.md` — "Key invariants" (assignment, deliverable consistency, untrusted text)
