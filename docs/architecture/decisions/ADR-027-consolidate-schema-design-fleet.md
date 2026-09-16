# ADR-027: Consolidate the Schema-Design Fleet into One Agent

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** Database Modernizer Assessment Architecture Team
**Related ADRs:** ADR-024 (Consolidate the Deterministic Analysis Fleet), ADR-025 (Consolidate the Deterministic Core Agent), ADR-026 (Reality Check in the Assessment Core), ADR-016 (Compute and Orchestration Strategy)

---

## Context

After ADR-024/025/026 the ATX fleet is nine runtimes: `orchestrator`,
`assessment-core` (the consolidated deterministic front-half plus the Reality
Check LLM pass), `synthesis`, and **six** `schema-{dynamodb, documentdb,
elasticache, opensearch, aurora-pg, aurora-mysql}` agents.

Unlike the analysis phases — which had six distinct core functions and so needed
ADR-024 to merge them — schema design **already** runs through a single
parameterised core. The six schema agents are the same code differing only by
one build-time value:

- One subagent module builds all six: `subagents/schema.py`
  (`make_schema_agent_factory`).
- One entrypoint branch dispatches all six: `atx_entrypoint.py`
  (`agent_type.startswith("schema-")`).
- One upstream core does the work for any engine:
  `core.run_schema_design_core(..., target_type=...)`.

The only thing that differs across the six is the `AGENT_TYPE` env var, baked at
build/deploy time, which `SCHEMA_TARGETS` maps to `target_type`. In other words,
six registry names and six AgentCore runtimes exist purely to carry one
parameter that changes per invocation. (Only four of the six even have real
designers today — `dynamodb`, `documentdb`, `opensearch`, `elasticache`;
`aurora-pg`/`aurora-mysql` take a same-family / not-implemented placeholder
path. This branch's orchestrator now recognises that: it gates dispatch on both
whether the engine has an implemented designer AND whether any in-scope query is
routed to it, so the two placeholder engines are skipped before the A2A call
rather than paying a cold-start to write a placeholder.)

### The instance-sizing exploration (and why it does not apply)

We explored serving all engines from one larger, multi-core instance that
parallelises the designs internally. The registry compute model rules this out
on the path we use. `ProvisionedComputeConfiguration` is a union of two options:

- `agentCoreConfiguration` — what the whole fleet uses. Its only fields are
  `{atxAccessRoleArn, runtimeArn, qualifier}`; there is **no** vCPU, memory, or
  core setting. Bedrock AgentCore Runtime is managed/serverless, subagents are
  stateless, and the runtime spawns a fresh instance per concurrent invoke.
  Parallelism comes from concurrent invocations, not from a bigger instance.
- `mdeConfiguration` — the only option with sizing knobs (`instanceType`,
  `storageSize`, `warmPoolArn`, `maxRuntimeMinutes`), but it is a different,
  heavier compute model (managed dev environments, warm pools) that we are not
  adopting for subagent serving.

So "one bigger instance running six designs in-process" is actually the weaker
option here: on AgentCore that single instance cannot be given more cores, so
concurrent in-process designs would contend on fixed compute. The parallelism we
already have — N engines dispatched concurrently, each its own stateless
instance — is the right model and needs no compute-model change.

## Decision

Consolidate the six `schema-{engine}` agents into **one `schema` agent** (one
registry name, one AgentCore runtime). Move `target_type` from the `AGENT_TYPE`
env var (baked per runtime at deploy time) into the **A2A invocation payload**,
alongside `job_id`, `database_name`, and `assignment_version` — where the version
already travels.

**"Same agent, invoked as required" — yes, exactly.** There is one `schema`
registry agent and one runtime definition. The orchestrator invokes it **once
per engine, concurrently**, exactly as it dispatches the six tools today; the
only change is that every call resolves to the same agent id and carries
`target_type` in the message. Because AgentCore spawns a fresh **stateless**
instance per invoke, dispatching five engines still yields five parallel
instances of the one `schema` agent — the same concurrency as six separate
agents, with one-sixth the fleet entries. No engine "waits" for another; the
runtime handles concurrent invocations of the same agent independently.

The fleet drops from nine runtimes to **four**: `orchestrator`,
`assessment-core`, `schema`, `synthesis`.

### Implementation surface

- **`pipeline/atx_deploy.py`** — replace the `*[Agent(f"schema-{e}", ...)]`
  comprehension with a single `Agent("schema", "schema")`.
- **`atx_entrypoint.py`** — one `_AGENTS` row (`"schema"`) instead of six; the
  dispatch branch resolves `target_type` from the invocation payload rather than
  from `AGENT_TYPE`.
- **`subagents/schema.py`** — `_work` reads `target_type` from `params` (the way
  it already reads `assignment_version`) and validates it against the known
  engine set, instead of the factory closing over a per-runtime target.
- **`tools.py`** — keep the per-engine orchestrator tools (so the LLM's
  proven parallel dispatch is unchanged), but resolve them all to the one
  `schema` agent id (`f"{_AGENT_PREFIX}-schema"`) and put `target_type` in the
  payload. The two pre-dispatch gates added in this branch (implemented-designer
  first, then query-routing) compose unchanged: both run before the invoke, so
  they short-circuit to the one `schema` agent id exactly as they do to the six
  today.
- **`orchestrator.py`** — the prompt keeps listing per-engine schema tools; no
  behavioural change is required.
- **`Dockerfile.atx`** — one `/tmp/schema_agent` directory instead of six.

The durable contract stays the artifact set (`schema-<engine>/v<N>/schema_output.json`),
so Synthesis and every fixture receive byte-identical inputs — the same property
that let ADR-024/025 ship without touching downstream.

## Rationale

- **Footprint.** Six registry entries, runtimes, deploys, and cold-start targets
  collapse to one, easing the AtxAgentRegistry per-account quota and the deploy
  path. Combined with ADR-024/025/026 the fleet reaches its minimal sensible
  size (four).
- **Fidelity to the code.** Schema design is already one parameterised core; a
  payload parameter expresses the per-engine difference directly, rather than
  encoding it as six deployed runtimes.
- **Parallelism preserved, no compute change.** Concurrent invokes of one agent
  spawn concurrent stateless instances. No instance sizing is needed (and none
  is available on AgentCore).
- **Naming.** The 48-char AgentCore runtime-name cap stops constraining schema
  (the longest was `dbmod-<env>-schema-aurora-mysql`).

This refines ADR-016 principle 4 ("agents own their parallelism"): the schema
phase is one agent whose parallelism is expressed by how the orchestrator invokes
it, not by how many runtimes it is split into.

## Alternatives considered

- **One agent invoked once with all engines + internal `ThreadPoolExecutor` on a
  larger instance — rejected.** AgentCore exposes no instance-size knob, so a
  single instance cannot be given more cores; six in-process LLM designs would
  contend on fixed compute and lose per-engine instance isolation. Getting a
  sizable instance would mean switching to the MDE compute model, a heavier
  change unwarranted for this phase.
- **Status quo (six agents) — rejected.** Pure overhead: six deployed runtimes to
  carry one per-invocation parameter.
- **A single tool taking a list of engines — deferred.** It would change the
  orchestrator's proven parallel per-engine dispatch and prompt. Keeping
  per-engine tools that resolve to one agent is the lower-risk shape and can be
  revisited later.

## Consequences

Positive:

- Fleet nine to four; fewer runtimes, registrations, cold starts, and deploy
  steps; simpler naming; identical parallelism and downstream contract.

Tradeoffs:

- **Payload-carried target.** `target_type` now arrives in the message, so a
  missing or unknown value must be validated and fail loudly, since a single
  runtime no longer encodes it. Mirror the existing `SCHEMA_TARGETS` validation
  against the payload.
- **Deploy and reap.** After deploying `dbmod-<env>-schema`, the six old
  `dbmod-<env>-schema-<engine>` registry entries and runtimes are orphaned and
  must be reaped (deregister + delete runtime), as with prior renames.
- **Retry and observability granularity — unchanged.** Each engine is still a
  separate invoke/instance of the one agent, so a single engine still fails and
  retries independently, and the plan's `schema_<engine>` sub-steps (independent
  of agent count) keep the WebApp panel's per-engine view.

## Future Work

- With schema consolidated, no further consolidation is sensible: `synthesis`
  must run last and once, `assessment-core` is already consolidated, and
  `orchestrator` is the chat entrypoint. Four is the floor.
- Settle the pending preview rename (dropping `-dev`) before the redeploy so the
  schema consolidation and the rename land in a single deploy rather than two.
- The schema-design table-qualifier match bug (tracked separately) is orthogonal
  to this change and should land regardless of consolidation.

---

## Amendment (2026-08-27): restore per-engine query grouping on the deployed path + co-dependency-aware clustering

Consolidating the fleet (above) did not change how a single engine's queries are
turned into a design. That path had **drifted** from its intended behavior, and
this amendment corrects it. The drift was not caught in review; a broader audit
follows once this and the assignment re-entry work (ADR-029) land.

### What drifted

1. **Grouping never runs on the deployed (A2A) path.** The intended design splits
   a large engine's queries into affinity groups, designs each group, then merges
   (`group_splitter` -> per-group design -> `group_merger`), so related queries are
   modeled together. That path exists only behind `run_schema_design_auto` /
   `run_schema_split`, which the docstrings describe as the Step Functions / local
   entrypoints. The deployed subagent path
   (`subagents/schema.py` -> `core.run_schema_design_core` -> `handler.run_schema_design`)
   calls the **single-shot** designer, which sends an engine's entire filtered query
   set to one LLM prompt. So on AWS Transform, grouping never happened. Earlier
   testing found grouped results materially better than the single all-queries
   prompt; the deployed path regressed to the worse behavior.

2. **The grouping ignored the JOIN-relatedness signal.** `group_splitter._build_table_clusters`
   clusters tables from foreign keys, aggregate recommendations, and co-access
   patterns, but never consults the assignment's `co_dependency_groups` — the
   union-find over significant JOINs that exists precisely to keep related queries
   together. So even where grouping did run, the signal most directly meant to drive
   it was unused.

3. **The merger was incomplete and partly wrong.** `group_merger.ENGINE_LIST_FIELDS`
   covered only dynamodb / opensearch / documentdb. Against the actual output
   contracts: documentdb used `collection_designs` (the contract field is
   `collections`), so merges kept only the first group's collections; the opensearch
   data-stream dedup keyed on `stream_name` (contract field is `data_stream_name`)
   and listed a `migration_notes` field the opensearch contract does not define;
   elasticache and the two Aurora engines had no entries at all. Routing those
   engines through grouping without this fix would silently drop all but the first
   group's design.

### The correction

- **A. Wire the deployed path through grouping.** `run_schema_design_core` calls
  `run_schema_design_auto` (split -> per-group design -> merge) instead of the
  single-shot `run_schema_design`, preserving the existing `<= MAX_GROUP_SIZE`
  single-pass fallback so small engines are unchanged.
- **B. Co-dependency-aware clustering.** Fold the assignment's `co_dependency_groups`
  into the table-affinity union in `_build_table_clusters`, alongside FK / aggregate
  / co-access. For each co-dependency group, the tables its queries touch are unioned
  into one cluster, so JOIN-related queries land in the same design group. The
  assignment's groups are threaded from `run_schema_split` -> `split_schema_input`
  -> `build_groups`.
- **C. Correct and complete the merger, and scope grouping to the remodeling
  engines.** Fix documentdb (`collections`), fix the opensearch data-stream dedup key
  (`data_stream_name`) and drop the non-existent `migration_notes`, and add
  elasticache. Grouping applies to **dynamodb, documentdb, opensearch, elasticache**;
  **Aurora (postgresql, mysql) stays single-pass**. Aurora's output is a single
  `generated_ddl` script plus table definitions that do not merge from independently
  designed groups, and a relational engine does not gain from query grouping the way
  a remodeling target does (JOINs execute within the engine regardless of how the
  design prompt was batched). `run_schema_design_auto` forces single-pass for the
  Aurora engines.

### Rationale

Grouping related queries yields a more coherent per-engine model — denormalization
and access-pattern decisions are made with the related queries in view — than one
prompt over every query. This matches what earlier testing showed and what the
consolidation ADR always assumed the per-engine core did. The tradeoff is more LLM
calls plus a merge step for large engines (slower), which is accepted for the
quality gain.

### Consequences

- On the deployed path, a large remodeling engine now designs per affinity group
  (FK + aggregate + co-access + co-dependency JOINs) and merges, instead of one
  all-queries prompt.
- The merger is correct for every engine it is now used for; Aurora is explicitly
  single-pass.
- This is a drift correction, so the durable artifact contract
  (`schema-<engine>/v<N>/schema_output.json`) is unchanged; synthesis and fixtures
  see the same shape.
