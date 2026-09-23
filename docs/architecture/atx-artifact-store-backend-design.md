# ATX Artifact Store Backend — Design

**Date:** 2026-09-18
**Status:** Proposed (pending review)
**Branch:** `feat/graphdb-nl-query` (becomes a larger storage-layer branch)

## Summary

Add a new storage backend, `AtxArtifactStore`, that implements the existing
`ArtifactStore` abstraction over the ATX (Elastic Gumby) Agentic Artifact Store
via `agent_builder_sdk`. When an assessment runs inside the ATX runtime, all
pipeline state is read from and written to the ATX artifact store instead of our
own S3 bucket. Customer-uploaded input is read **in place** from its
`CUSTOMER_INPUT` artifact rather than copied into our S3.

**Goal:** ATX executions store nothing in our S3 bucket.

This branch has two phases: **(1)** the `AtxArtifactStore` backend (the enabler),
then **(2)** an orchestrator `query_context_graph` tool that answers graph
queries in-process from the artifact-store JSON — no S3, no HTTP hop. Phase 2
depends on phase 1 and lands after it in the same branch.

## Goals

- ATX runs use the ATX artifact store as the storage backend for all pipeline
  state. Our `S3_BUCKET` is no longer injected/used for ATX.
- Stop copying the customer's uploaded input into our S3 (`core.py:191`).
- Keep Step Functions (S3) and local (filesystem) execution unchanged.
- Keep the graph layer working with **zero** S3 writes (rebuild-on-demand).
- (Phase 2) Expose an orchestrator tool that runs curated/Cypher graph queries
  in-process against the artifact-store-backed graph — no S3, no API hop.

## Non-Goals

- Changing the `ArtifactStore` abstraction's public method set.
- Migrating Step Functions or local execution off their current backends.
- Offering the LadybugDB graph binary as a customer download (deferred to a
  follow-up branch — see Deferred Work).
- Changing how customer-facing deliverables are produced beyond routing them
  through the existing `publish()` path on the ATX code path.

## Current State (as-is)

- **Abstraction** — `src/storage/artifact_store.py`, `ArtifactStore(ABC)`: six
  methods keyed by a store-relative `path`: `read_json`, `write_json`,
  `read_bytes`, `write_bytes`, `exists`, `list_prefix`. Transform subclasses in
  `src/atx_orchestrator/runtime/store.py` add `write_text`.
- **Backends** — `S3ArtifactStore` (`src/storage/s3_store.py`),
  `LocalArtifactStore` (`src/storage/local_store.py`). No ATX backend exists.
- **Factory** — `src/storage/__init__.py::create_artifact_store()`: `S3_BUCKET`
  set → S3, else `LocalArtifactStore(ARTIFACT_DIR)`. `make_store()` in
  `src/atx_orchestrator/core.py:24` wraps it via `upgrade_store()`.
- **ATX artifact store today** — used only out-of-band via `agent_builder_sdk`:
  `runtime/artifacts.py` (`publish()` deliverables), `runtime/hitl.py` (HITL
  up/down), and `core.py::_discover_uploaded_input` (discover + download the
  customer upload, then **`store.write_json(seed_key, …)` — the copy we remove**
  at `core.py:191`).
- **Graph** — `src/api/routes/graph.py::_get_graph` uses `GraphPersistence`
  (`src/graph/persistence.py`) to `read_bytes`/`write_bytes` the `.lbug` binary
  through the store (self-healing download→build→upload).

### Relevant SDK surface (`agent_builder_sdk.agentic_framework.artifact_store.ArtifactStore`)

- `upload_artifact(content, digest, artifact_id=None, category_type=None, file_type=None, plan_step_id=None, label=None) -> str`
  - Pass `artifact_id` to **overwrite in place**; omit it (with
    `category_type` + `file_type`) to **create**. Uploads are `INTERNAL`
    visibility. `label` is free-form.
- `download_artifact(artifact_id, destination_file_path) -> None` — by id, to a
  local file; honors `storedInAtxBucket`.
- `list_artifacts(agent_instance_id, category=None)` — returns the artifact set
  (filter only by agent + category); each entry carries `artifactId`, `label`,
  `fileMetadata`, `artifactType.fileType`.
- `CategoryType` includes `STATE`, `INTERNAL`, `AGENT_OUTPUT`, `CUSTOMER_INPUT`,
  `CUSTOMER_OUTPUT`, `PLAN_STEP_OUTPUT`, `HITL_*`.
- `FileType` supports only `JSON` and `ZIP`.
- Agent context (`get_agent_context_from_env()`): `workspace_id`, `job_id`,
  `agent_instance_id`, `authorization_token`.

## Decisions

1. **Backend selection** — explicit env var `STORAGE_BACKEND=atx` (set in the ATX
   container / AgentCore runtime env). Predictable and testable. `S3_BUCKET`
   stops being injected for ATX.
2. **Internal backend is JSON-only**, `CategoryType.STATE`, `FileType.JSON`.
   All 62 `write_json` / 98 `read_json` call sites work unchanged.
3. **Addressing** — the store-relative `path` is stored as the artifact `label`.
   An in-memory `path → artifactId` index (built from one
   `list_artifacts(agent_instance_id, STATE)` call, updated on every write)
   powers `exists`, reads, `list_prefix`, and idempotent overwrite via
   `upload_artifact(artifact_id=…)`.
4. **Customer input** — `_discover_uploaded_input` no longer copies into our S3.
   It returns an `artifact://<artifact_id>` input key. The backend resolves an
   `artifact://` key by downloading that artifact id directly. The `CUSTOMER_INPUT`
   artifact is read in place.
5. **Deliverables** (HTML report, PPTX, PDF, provenance/summary markdown+HTML,
   mermaid) go through the existing `publish()` path to `EXTERNAL`/customer
   artifacts — not through the storage abstraction.
6. **Graph binary** — rebuild-on-demand. The API builds the `.lbug` graph on the
   task's local ephemeral disk from `STATE` JSON artifacts; it is never
   persisted (no S3, not in the artifact store). Restart → rebuild (<2s per
   ADR-023).
7. **Non-JSON methods** — `read_bytes`/`write_bytes`/`write_text` on the ATX
   backend **raise `NotImplementedError`** pointing at `publish()`. Every
   non-JSON caller is audited and rerouted so the ATX path never hits them.

## Architecture

```
                      create_artifact_store()  (src/storage/__init__.py)
                                 │
        STORAGE_BACKEND=atx ─────┼───── S3_BUCKET set ───── else
                                 │              │              │
                        AtxArtifactStore   S3ArtifactStore  LocalArtifactStore
                                 │
                  agent_builder_sdk.ArtifactStore (SDK)
                                 │
                     ATX Agentic Artifact Store (STATE, JSON)

ATX run data flow:
  customer upload ──(CUSTOMER_INPUT artifact)──► collector reads via artifact://<id>
  pipeline stages ──write_json(STATE)──► ATX artifact store  (label = path, overwrite by id)
  deliverables    ──publish()──► EXTERNAL customer artifacts
  graph           ──rebuild from STATE JSON──► local ephemeral .lbug (never persisted)
  our S3 bucket:  UNUSED
```

## Components

### 1. `AtxArtifactStore(ArtifactStore)` — new, `src/storage/` or `src/atx_orchestrator/runtime/`

Wraps an SDK `ArtifactStore` + agent context. Implements:

- `write_json(path, data)` — `content = json.dumps(...).encode()`;
  `digest = sha256`; `artifact_id = index.get(path)`;
  `upload_artifact(content, digest, artifact_id=artifact_id, category_type=STATE,
  file_type=JSON, label=path)` (category/file_type only when creating);
  update index with returned id.
- `read_json(path)` — resolve id (index; `artifact://<id>` short-circuits to the
  id); `download_artifact(id, tmp)`; parse JSON. `FileNotFoundError` if unknown.
- `exists(path)` — `path in index` (or valid `artifact://` id).
- `list_prefix(prefix)` — index keys starting with `prefix`.
- `read_bytes` / `write_bytes` — raise `NotImplementedError("ATX backend is
  JSON-only; use publish() for binary deliverables")`.

Index: `_load_index()` calls `list_artifacts(agent_instance_id, STATE)` once and
maps `label → artifactId` (newest wins on duplicate labels). Rebuilt per store
instance; each ATX process (orchestrator, subagents) builds its own from server
truth. Writes update the local index so intra-process reads are consistent.

Digest + presigned helpers reuse `agent_builder_sdk.agentic_framework.common`
(`calculate_digest`, `download_from_presigned_url`) — same seam as
`runtime/hitl.py`.

### 2. `TransformAtxStore(AtxArtifactStore)` — `runtime/store.py`

Adds `write_text(path, content, content_type=...)` that **raises
`NotImplementedError`** (deliverables use `publish()`). Registered in
`upgrade_store()` so it re-homes the ATX backend and no longer raises `TypeError`
on an unknown backend.

### 3. Factory — `src/storage/__init__.py::create_artifact_store()`

```
if os.environ.get("STORAGE_BACKEND") == "atx":
    return AtxArtifactStore(...)          # from agent context
if os.environ.get("S3_BUCKET"):
    return S3ArtifactStore(bucket)
return LocalArtifactStore(os.environ.get("ARTIFACT_DIR", "./artifacts"))
```

`upgrade_store()` gains an `AtxArtifactStore → TransformAtxStore` branch.

### 4. Customer input — `core.py::_discover_uploaded_input` + `_resolve_collector_input`

- Discovery finds the single `CUSTOMER_INPUT` candidate as today, but instead of
  `download_artifact` + `store.write_json(seed_key, …)`, returns
  `f"artifact://{artifact_id}"`.
- `_resolve_collector_input`: if `input_key` starts with `artifact://` →
  `store.exists`/`read_json` resolve it directly (backend handles the scheme).
  The S3/local seed-key fallback path is unchanged for non-ATX runs.
- `run_collect_core` is unchanged: `store.read_json(key)` transparently reads the
  customer artifact on ATX.

### 5. Graph — `src/api/routes/graph.py` + `src/graph/persistence.py`

`GraphPersistence` becomes a no-op when the backend does not support bytes
(ATX): `download_if_exists` returns `False`, `upload` is skipped. `_get_graph`
then always builds fresh into local ephemeral disk from `STATE` JSON artifacts.
Guard by capability (a `supports_bytes`/`persistent` flag on the store, or
`isinstance` check), not by catching `NotImplementedError`.

### 6. Orchestrator graph-query tool — `query_context_graph` (Phase 2)

A new orchestrator tool (registered like the other A2A/orchestrator tools in
`src/atx_orchestrator/tools.py`) that answers graph questions in-process, reusing
the phase-1 backend so nothing touches S3:

- Build/load the context graph on the orchestrator task's local ephemeral disk
  from the `STATE` JSON artifacts (the same `rebuild_graph` path `_get_graph`
  uses), cached per `job_id` for the process lifetime.
- Execute a **curated** query from `src/graph/queries.py` (e.g. table impact,
  provenance, risks, engine detail) or a raw Cypher string, and return rows.
- Signature sketch: `query_context_graph(job_id, query)` where `query` is either
  a curated-query name + params or a Cypher string. Reuses the existing Pydantic
  response models in `src/api/models/graph_responses.py`.
- No HTTP call to the API `/graph/query` route and no persisted `.lbug` — the
  orchestrator already holds the agent context and reads the artifact store
  directly.

This is strictly additive to phase 1 and cannot skip S3 until the phase-1 backend
is in place. A natural-language → Cypher layer on top of this tool is out of
scope here (possible future step).

## Non-JSON Caller Audit (must reroute for ATX)

`write_text` (19) / `write_bytes` (5) / `read_bytes` (4) sites, e.g.:

- `atx_orchestrator/tools.py` — summary/review markdown, provenance HTML/MD,
  analysis HTML, PPTX/PDF decks (→ `publish()`).
- `atx_orchestrator/core.py:638` — mermaid diagram (→ `publish()` or drop if
  internal-only).
- `graph/persistence.py:32,35,41` — graph binary (→ removed on ATX path).
- `agents/load_test/*/script_generator.py` — these `write_text` to a local
  scripts dir (`Path.write_text`), **not** the store; unaffected.
- `storage/local_store.py`, `api/services/local_s3.py` — non-ATX backends;
  unaffected.

Each store-backed non-JSON caller is inventoried in the implementation plan with
its ATX routing (publish vs drop).

## Execution-Mode Selection

No mode enum. `STORAGE_BACKEND=atx` (deploy-time env) selects the ATX backend.
The ATX Dockerfile / AgentCore runtime env sets `STORAGE_BACKEND=atx` and stops
setting `S3_BUCKET`. Local/dev/tests leave it unset → local store.

## Error Handling

- Outside the ATX runtime (no agent context) constructing `AtxArtifactStore`
  raises with a clear message; the factory only builds it when
  `STORAGE_BACKEND=atx`, so this surfaces as a config error, not a silent
  fallback.
- Non-JSON methods raise `NotImplementedError` with remediation text.
- `read_json` of an unknown path raises `FileNotFoundError` (matches local/S3).
- SDK/customer-config errors propagate via the SDK's
  `_raise_for_customer_config_error` (already used by the SDK methods).

## Testing

- **Fake backend** — an in-memory `FakeAtxArtifactStore` (dict `path→bytes`) for
  unit tests, mirroring the SDK contract (label/id, overwrite, list).
- **Backend unit tests** — write/read/exists/list_prefix/overwrite;
  `artifact://` resolution; non-JSON methods raise.
- **Factory tests** — `STORAGE_BACKEND=atx` selects ATX; precedence over
  `S3_BUCKET`; `upgrade_store` re-homes ATX.
- **Customer-input tests** — discovery returns `artifact://<id>`; collector reads
  it; no `write_json(seed_key)` occurs.
- **Graph tests** — on ATX, persistence is skipped and the graph builds fresh;
  no `write_bytes` call.
- **Caller-audit regression** — assert the ATX backend's `write_bytes`/
  `write_text` are never called on a full pipeline run (raise would fail tests).
- **(Phase 2) Graph-query tool** — with the fake backend seeded with `STATE`
  JSON, `query_context_graph` builds the graph and returns expected rows for a
  curated query and a raw Cypher query; no S3/HTTP access.

## Rollout / Migration

1. Land backend + factory + tests (no behavior change until env var is set).
2. Reroute non-JSON callers; make graph persistence capability-aware.
3. Switch the ATX Dockerfile/runtime env to `STORAGE_BACKEND=atx`, remove
   `S3_BUCKET` from the ATX path.
4. Verify an end-to-end ATX run writes nothing to our S3.
5. (Phase 2) Add the `query_context_graph` orchestrator tool on top of the
   landed backend.

Rollback: unset `STORAGE_BACKEND` / restore `S3_BUCKET` — reverts ATX to S3.

## Deferred Work

- Offer the LadybugDB graph binary as an `EXTERNAL` ZIP customer download
  ("consume at your leisure") — a new deliverable + consume surface; its own
  branch.
- Optional server-side artifact pagination if `list_artifacts` volume grows
  (current per-assessment node counts are low-thousands).

## Risks

| Risk | Mitigation |
|------|-----------|
| A non-JSON caller is missed and hits the ATX backend in prod | Backend raises; caller-audit regression test on a full run catches it pre-merge. |
| Index staleness across processes | Each process builds its index from `list_artifacts` server truth; writes update the local index. Reads are download-by-id (always current bytes). |
| Duplicate labels (write without index hit) | Overwrite by `artifact_id`; index "newest wins"; writes always consult the index first. |
| `list_artifacts` returns very large sets | Low node counts today; pagination deferred with a clear upgrade path. |
| Graph rebuild latency on every API cold read | <2s per ADR-023 for the largest workloads; acceptable, and only on cache miss. |

## References

- ADR-016 (S3 artifact path conventions), ADR-019 (query journey), ADR-023
  (context graph layer / rebuildable-from-artifacts).
- `agent_builder_sdk.agentic_framework.artifact_store` (`ATXAgentBuilderToolkit`).
- `src/storage/`, `src/atx_orchestrator/runtime/{store,artifacts,hitl}.py`,
  `src/atx_orchestrator/core.py`, `src/graph/persistence.py`,
  `src/api/routes/graph.py`.
