# ADR-008: Contract Versioning Strategy

**Status:** Accepted
**Date:** 2026-02-02
**Deciders:** Architecture Team
**Related ADRs:** ADR-002 (Pydantic Output)

---

## Context

Database Modernizer Assessment uses contracts (Pydantic models) to define input/output between agents. As the system evolves:

- **New fields** may be added to contracts (new analysis types, additional metadata)
- **Field types** may change (string → enum, optional → required)
- **Breaking changes** may be necessary (rename fields, restructure data)
- **Multiple versions** may coexist (old jobs resuming, new jobs starting)

### Key Distinction: Agent Version vs Contract Version

**Agent Version** (e.g., `collector_version: "2.3.1"`):

- Version of the agent code itself
- Can change frequently (bug fixes, optimizations)
- Multiple agent versions can produce the same contract version

**Contract Version** (e.g., `contract_version: "1.2"`):

- Version of the input/output data structure
- Changes only when contract structure changes
- Decoupled from agent version

**Example:**

```python
# Agent v2.3.1 produces contract v1.2 (the model's contract_version default)
output = CollectorOutput(
    collector_version="2.3.1",  # Agent version
    ...                         # contract_version: the model's default, "1.2"
)

# Agent v2.4.0 also produces contract v1.2 (no contract change)
output = CollectorOutput(
    collector_version="2.4.0",  # Different agent version
    ...                         # contract_version: still the model's default, "1.2"
)
```

### Audience: Internal Development

**Contract versioning is primarily for internal development:**

- ✅ **Developers**: Understand contract compatibility when making changes
- ✅ **Testing**: Test contract migrations and compatibility
- ✅ **Debugging**: Identify which contract version caused issues
- ❌ **End users**: Don't need to know contract versions

**Users see:**

- Deployment version (e.g., "Database Modernizer Assessment v1.5.0")
- Release notes (features, bug fixes)

**Users don't see:**

- Contract versions (internal implementation detail)
- Agent versions (internal implementation detail)

**Why internal?**

- Contract versions are implementation details
- Users care about features, not data structure versions
- Simplifies user experience (one version number to track)

### Deployment Model: Single Version at a Time

**Important:** Database Modernizer Assessment deploys as a **single version** (not multiple versions simultaneously).

- ✅ One agent version deployed at a time
- ✅ All agents in deployment use same codebase version
- ✅ User doesn't choose agent versions (deployment is atomic)
- ❌ No mixing agent versions in same deployment

**Why single version?**

- Simpler deployment (no version routing)
- Easier testing (one version to test)
- Clearer support (one version to debug)

### Version Mismatch Scenarios

**Scenario 1: Job Resume After Upgrade (Common)**

```
1. Job starts with deployment v1.0 (contract v1.0)
2. Job checkpoints at collector stage
3. Deployment upgraded to v1.1 (contract v1.1)
4. Job resumes with v1.1 code
   → Contract adapter handles v1.0 → v1.1 migration
```

**Scenario 2: Rollback (Rare)**

```
1. Job starts with deployment v1.1 (contract v1.1)
2. Job checkpoints at collector stage
3. Deployment rolled back to v1.0 (contract v1.0)
4. Job resumes with v1.0 code
   → Pydantic ignores unknown fields (forward compatibility)
```

**Scenario 3: No Mismatch (Most Common)**

```
1. Job starts with deployment v1.1
2. Job completes with deployment v1.1
   → No version mismatch
```

### Requirements

- **Backward compatibility**: Old checkpoints can resume with new code (Scenario 1)
- **Forward compatibility**: New checkpoints can be read by old code (Scenario 2)
- **Version detection**: System knows which contract version it's working with
- **Migration path**: Clear upgrade path for breaking changes
- **Minimal overhead**: Versioning shouldn't complicate simple changes

---

## Decision

We will implement **Semantic Versioning for Contracts** with eight decisions.

### 1. Every contract carries a `contract_version`

Every contract an agent or script writes or reads carries `contract_version` in MAJOR.MINOR form (for example `"1.0"`), so a reader can tell which structure it holds.

*Amended (2026-10-20, #1192):* every JSON file a run writes under a job is one of three kinds, and nothing else: a registered contract (`_ARTIFACT_CONTRACTS` and the schema outputs in `src/shared/contract_versioning.py`), versioned and written through its model; model I/O (`MODEL_IO_PATTERNS`), a model session's own request, input or answer; or a declared internal artifact (`INTERNAL_ARTIFACTS`), each listed with a one-line reason. Callers may rely on the shape of contracts only. An internal file that other phases or callers read back across runs is typed as a contract in its own issue and then leaves the internal list: the schema design trace (#1258) and the analysis decision traces of every engine but DynamoDB (#1188, #1189, #1190, #1191). The local API runner's `_meta.json` stays internal: only the local API and its stored-input redaction read it, never a pipeline phase or a library caller. `docs/contracts/artifacts.md` lists the three kinds. `tests/contract/test_declared_artifacts.py` enforces it: it fails on a JSON path that the `write_json` scan renders, or that a run writes, in none of the lists, and on an entry of any of the three lists that no write or run produces (one load-test pattern no run here reaches is listed with its writer). The runs are `run_assessment.py --all -y --llm-mode none` on each bundled sample and a wordpress run in `--llm-mode external` with the test answering for the model. The scan sees only `.write_json` calls: a JSON file written with `write_text`, `write_bytes` or `json.dump` (as `collector/sql_shadow.json` is), or one whose file name the scan cannot render, is checked only when one of those runs writes it, so such a file written only on a path no run takes (the API, AWS Transform, a load test) is not checked at all.

*Added (2026-10-20, #1187):* the analysis engines whose decision traces share the core sections (`trace_version`, `agent`, `summary`, `query_matches`, `pattern_summaries`, `recommendation_derivations`) type them on one base, `DecisionTraceBase` (`src/contracts/decision_trace_base.py`); a trace contract that shares none of them is its own model and is listed, with its reason, in the test's exemption set (`OFF_BASE_TRACE_CONTRACTS`). `write_decision_trace` writes a trace through the contract registered for its path and refuses a trace with none. While engines were being typed, a pending set (`UNTYPED_TRACE_ENGINES`) let the rest keep writing plain dicts; every analysis engine's trace is now typed (#973, #1188, #1189, #1190, #1191), so the set and the plain-dict branch of the writer are gone. The guard test pins the set of typed engines exactly and requires it to equal the analysis engines, so dropping a registration, or adding an engine without one, fails unless the test is edited too. The ElastiCache trace was typed as it is, with none of the core, and since 1.1 (#1276) declares the shared sections as optional fields (a saved 1.0 trace has none and must still load), so it stays in the exemption set rather than extending the base, whose core sections are required; the Aurora PostgreSQL and MySQL traces share one model, `AuroraDecisionTrace`.

*Added (2026-10-21, #1258):* the schema design trace is a registered contract. `schema-<engine>/[v<N>/]design_trace.json` is `DesignTrace` and DynamoDB's `design_trace_group_<G>.json` is `DesignTraceGroup` (`src/contracts/schema_design_trace.py`, both 1.0); both leave the internal list. They share one body (`DesignTraceRun`: the model-call entries, the PE reviewer's routing flags, token usage, the Aurora delta merge and an external PE review), every section optional because a trace holds what the run recorded, and the merged trace adds `total_groups`, `groups` and `merge_reconciliation`. The designers' handler, the group merge, the external PE review and the AWS Transform carry-forward write through `write_design_trace`; the post-schema router (`routing_flags_from_trace`), the local orchestrator and synthesis read through the model, so a `routing_flags` of the wrong type or a version this code refuses fails instead of reading as no flags: every reader raises `ContractVersionError` for a version and `DesignTraceShapeError` for a shape (the router, the orchestrator, the AWS Transform carry-forward and synthesis alike). The one lenient path is the external PE review, which writes after the paid model call: what the contract refuses is dropped and listed in the trace's `dropped_fields` (paths and reasons, never values), so the written trace always validates; a bad `routing_flags` is never repaired and the review step reports the error so the command's retry rule applies. The fields a model client or answer can shape (`iteration`, `elapsed_seconds`, `change_requests`, `token_usage` values) are not type-checked. Unknown keys are kept (`extra="allow"`), so the contract does not catch a writer that renames `routing_flags`; the writers' tests pin the key. A trace saved before the contract has no `contract_version` and loads as 1.0. `tests/unit/contracts/test_schema_design_trace.py` guards the writers and readers, and `tests/contract/test_design_trace_writers.py` fails on a direct write of a design-trace path and on `routing_flags` leaving the model; a copy of an engine's design to the next assignment version by the local orchestrator stays a verbatim file copy, as before.

### 2. The agent version is a separate field

The version of the agent code (for example `collector_version: "2.3.1"`) is its own field and never stands in for the contract version. A writer takes the contract version from the model's default, not from a literal of its own.

### 3. Contract versions are MAJOR.MINOR, and a shape change moves the version

MAJOR means a breaking change (incompatible). MINOR means a backward-compatible addition. A change to a contract's shape, including the values of its published enums and ids, moves the version by the bump the Versioning Rules below require.

### 4. Readers accept any MINOR of the same MAJOR and refuse a different MAJOR

*Amended (2026-10-16, #970; corrected 2026-10-17, #971):* the original decision called for version adapters and a version detector; none of that was built. A reader that loads a stored artifact accepts any MINOR of the MAJOR it was written for and refuses a different MAJOR with a clear error naming the contract, the stored version and the supported MAJOR. A different MAJOR is read only through an adapter, and an adapter should ship in the same change as the bump. That did not happen once: `RealityCheckOutputContract` went from 1.13 to 2.0 in #790 (it removed the always-empty `lightweight_recommendations`) with no adapter, the same day this amendment said no contract had shipped a MAJOR bump. Its 1.x adapter was added afterwards, in #971, so reality checks saved before 2.0 still load; it is the only adapter.

### 5. Optional fields by default

Prefer additions over changes: a new field is optional with a default, so older artifacts still load and the bump is MINOR. Forward compatibility (a newer artifact read by older code) relies on unknown fields being ignored, so a contract read back across versions does not set `extra="forbid"` without a decision recorded here.

*Recorded (2026-10-17, #976):* a model's answer (`ExecutiveSummaryResponse`, `RealityCheckModelResponse`, `ModelFlag`, the Aurora design delta) keeps `extra="forbid"`: an unknown key there is the model saying something the contract does not allow, such as a flag naming a destination engine (ADR-031 Decision 11), and the answer is refused. An artifact written to disk and read back across versions accepts unknown fields. Where the convention is to keep them on a round trip (the move ledger, feasibility findings, the groups manifest, design metrics, the DynamoDB decision trace, the synthesis report) it sets `extra="allow"`; every other contract model uses pydantic's default, `"ignore"`. `tests/contract/test_extra_policy.py` pins every model's setting and fails when a model reachable from a registered contract file forbids unknown fields. It checks the `extra` setting only; a validator that rejects a field by name is not seen. `ModelFlag` keeps `forbid` although flags are also re-read from saved model answers (the reality-check response, a DynamoDB design trace's routing flags): a newer flag with a field this code does not know is dropped whole when re-read, the cost of keeping a flag from ever naming a destination engine (the maintainer is to confirm this exception on #976). Approving an assignment as-is (`mark_assignment_customer_approved`) patches the stored dict (status, then `stamp_artifact_version`) instead of dumping the model, so the top-level and per-query unknown fields of a newer MINOR are kept as stored, although `Assignment` and `QueryAssignment` use `ignore` (#1056).

### 6. Deprecate before removing

A field is marked deprecated, with a warning in code, for at least one MAJOR version before the next MAJOR removes it.

### 7. A single version is deployed at a time

There are no multi-version deployments. A version mismatch only arises when a job resumes after an upgrade or a rollback.

### 8. A pull request declares the bump kind; the number is assigned after its last rebase

*Added (2026-10-19, #1158):* parallel pull requests used to claim the same number (four held `synthesis_output` 1.40 at once) and each renumbered its version, its Version History entry and both snapshots on every rebase. A pull request that changes a contract now runs `uv run python scripts/bump_contract.py <contract> minor|major --reason "..." --issue N`, which writes one file, `src/contracts/pending/<contract>.<slug>.toml`, and nothing else. The slug defaults to the branch name. Two pull requests can share a file name (two forks' `patch-1` branches): that is harmless, because each pull request's tree holds only its own pending files and the merge queue refuses any pending file, so two of them never meet on main. `bump_contract.py` refuses, when the bump is declared, a contract the resolver could not edit, and says how to bump it by hand.

While a bump is pending, the contract keeps main's `contract_version`, Version History and snapshots. The schema and value-set snapshot checks accept a change that the declared kind covers; a kind smaller than the change requires (a MINOR declared for a removed field) still fails.

After the last rebase, before the merge queue, `uv run python scripts/resolve_contract_versions.py` resolves the pending bumps. Its `--base` defaults to the public repository's remote (`upstream` on a fork, else `origin`), the same remote `ci/pre_merge.sh` picks and names in the command it prints. For each contract with a pending file it:

- takes main's version of the contract;
- moves it by the strongest declared kind (several pending files for one contract make one bump);
- writes the new default and a Version History entry built from the files' reasons and issues;
- deletes those files;
- regenerates both snapshots.

It refuses when HEAD does not contain main. A contract it cannot resolve is reported with what to do, and the PR's other bumps are still resolved. There are three such cases:

- a contract the resolver cannot edit;
- a contract the branch also moved by hand;
- a pending file whose bump already landed: its reason is already a Version History entry, as when a stacked branch is rebased onto the resolved commit of the branch below it and re-adds that branch's pending file.

The `validate-contracts` job, a required check, runs `resolve_contract_versions.py --check --against <main's tip>`. On a pull request, main's tip is HEAD^1 of the merge commit; in the merge queue, the merge group's base; on a push, the commit before it. The check does three things:

- it validates every pending file: a known contract, a kind and a reason, a contract the resolver could edit, and a bump that has not already landed;
- it compares the contracts with main's committed snapshots, so a shape or value change with no version move and no pending file fails even when the branch regenerated its own snapshot (`--against`, which `ci/pre_merge.sh` also runs against the merge base);
- on the merge queue and on main it adds `--require-resolved`, so main never holds a pending file.

Decisions 1 and 3 therefore hold on main as before: every shape change moves the version, and older artifacts load under Decision 4.

*Added (2026-10-20, #1226):* the resolver moves a number after review, so a test must never compare a written contract with a literal version; it compares with `current_version(Model)` (`src/shared/contract_versioning.py`), the same default Decision 2 has writers use. `scripts/check_version_pins.py` enforces it: in a temporary worktree of HEAD it bumps every contract the resolver can edit by one MINOR, as the resolver does (new default, Version History entry, both snapshots regenerated), and runs the test files that mention a contract version (`tests/e2e`, `tests/integration` and `tests/unit/atx_orchestrator` aside). A test that fails there pins a version; a collection or setup error is reported as an error, not a pin. The `validate-contracts` job runs it on every pull request, merge group and push, and `ci/pre_merge.sh` runs it without `--quick`. It does not simulate the contracts the resolver cannot edit, which are bumped by hand, and it only runs files whose text names `contract_version`, a `*_VERSION` constant or `current_version`.

What this does not do: two pull requests that both resolve before either merges still claim the same number. The merge queue then fails the second, because its Version History entry and snapshot lines conflict with the first's. Its author rebases, takes main's side of those hunks, declares the bump again and resolves. Resolving is the last step before the queue to keep that window short. The resolver edits a contract whose `contract_version` default is a literal in its own class and whose Version History (in the class docstring, else the module docstring) has the current version as its newest entry, or a module versioned by its Version History alone with no version constant. For the rest (today the two Aurora design outputs; the DocumentDB design output gained a Version History in #1281, so the resolver edits it now; `assignment_models.ValidationResult` and `model_flag`; `schema_design_input.SchemaDesignGroupsManifest` gained a Version History in #1243, so the resolver edits it now), `bump_contract.py` says what is missing and the bump is made by hand. PATCH stays out (Alternative 2): the kinds are `minor` and `major`.

### Agent Version Support Requirements

**Every agent MUST:**

- ✅ Include `contract_version` field in output
- ✅ Include agent-specific version field (e.g., `collector_version`)
- ✅ Read any MINOR of the MAJOR it supports and refuse another MAJOR (Decision 4)
- ✅ Produce current contract version in output

**Example:**

```python
class MySQLCollectorAgent:
    """MySQL Collector Agent"""

    AGENT_VERSION = "2.3.1"      # Agent code version

    def collect(self, input_contract: dict) -> CollectorOutput:
        """
        Collect database metadata.

        Returns CollectorOutput with version fields.
        """
        output = CollectorOutput(
            collector_version=self.AGENT_VERSION,    # Agent version
            # contract_version is not passed: the model's default applies (Decision 2)
            job_id=input_contract['job_id'],
            ...
        )
        return output
```

---

## Deployment Model

### Single Version Deployment

**Database Modernizer Assessment deploys as a single version:**

```
┌─────────────────────────────────────────────────────────┐
│         Database Modernizer Assessment Deployment v1.1             │
│                                                         │
│  All agents use same codebase version:                  │
│  - Collector agents: v1.1 code                          │
│  - Analysis agents: v1.1 code                           │
│  - Referee agent: v1.1 code                             │
│                                                         │
│  All produce same contract versions:                    │
│  - CollectorOutput: contract v1.2                       │
│  - AnalysisOutput: contract v1.0                        │
│  - ModernizationReport: contract v1.0                   │
└─────────────────────────────────────────────────────────┘
```

**User does NOT choose agent versions:**

- Deployment is atomic (all agents upgraded together)
- No version routing or selection
- Simpler deployment and testing

### Version Mismatch Scenarios

**When do versions mismatch?**

Only during **job resume after deployment upgrade/rollback**.

#### Scenario 1: Job Resume After Upgrade (Common)

```
Timeline:
─────────────────────────────────────────────────────────
Day 1: Deployment v1.0 (contract v1.0)
  ├─ Job-123 starts
  ├─ Collector completes → checkpoint saved (contract v1.0)
  └─ Job paused (waiting for user input)

Day 2: Deployment upgraded to v1.1 (contract v1.1)
  ├─ Job-123 resumes
  ├─ Load checkpoint (contract v1.0)
  ├─ Adapter migrates v1.0 → v1.1
  └─ Analysis continues with v1.1 code
```

**How handled:**

- Checkpoint loader detects contract v1.0
- Adapter migrates v1.0 → v1.1
- Job continues with v1.1 code

#### Scenario 2: Rollback (Rare)

```
Timeline:
─────────────────────────────────────────────────────────
Day 1: Deployment v1.1 (contract v1.1)
  ├─ Job-456 starts
  ├─ Collector completes → checkpoint saved (contract v1.1)
  └─ Job paused

Day 2: Deployment rolled back to v1.0 (contract v1.0)
  ├─ Job-456 resumes
  ├─ Load checkpoint (contract v1.1)
  ├─ Pydantic ignores unknown fields (forward compatibility)
  └─ Job continues with v1.0 code (graceful degradation)
```

**How handled:**

- Checkpoint loader reads contract v1.1
- Pydantic ignores unknown fields (v1.1 additions)
- Job continues with v1.0 code (some data lost, but functional)

#### Scenario 3: No Mismatch (Most Common)

```
Timeline:
─────────────────────────────────────────────────────────
Day 1: Deployment v1.1 (contract v1.1)
  ├─ Job-789 starts
  ├─ Collector completes → checkpoint saved (contract v1.1)
  ├─ Analysis completes → checkpoint saved (contract v1.1)
  └─ Job completes (same deployment version)
```

**No version handling needed** (same version throughout).

### Preventing Version Mismatches

**Best practices:**

1. **Minimize breaking changes** (use optional fields)
2. **Test upgrades** with in-progress jobs
3. **Graceful degradation** (forward compatibility)
4. **Clear migration path** (adapters for MAJOR versions)

---

## Architecture

### Contract Versioning Pattern

```python
# src/contracts/models/collector_output.py

from pydantic import BaseModel, Field
from typing import Optional

class CollectorOutput(BaseModel):
    """
    Collector output contract.

    Version history:
    - 1.0: Initial version
    - 1.1: Added query_patterns field (optional)
    - 1.2: Added aws_metadata field (optional)
    """
    # Version fields (REQUIRED)
    contract_version: str = Field(default="1.2", description="Contract version")
    collector_version: str = Field(description="Agent version that produced this output")

    # Core fields (v1.0)
    job_id: str
    database_metadata: dict
    schema: dict

    # Added in v1.1 (optional for backward compatibility)
    query_patterns: Optional[dict] = Field(default=None)

    # Added in v1.2 (optional for backward compatibility)
    aws_metadata: Optional[dict] = Field(default=None)
```

### Agent Implementation Pattern

```python
# src/agents/collector/mysql_collector.py

from strands import Agent
from contracts.models.collector_output import CollectorOutput

class MySQLCollectorAgent:
    """
    MySQL Collector Agent.

    Agent version: 2.3.1
    Produces contract version: 1.2
    Supports reading contract versions: 1.0, 1.1, 1.2
    """

    AGENT_VERSION = "2.3.1"      # This agent's code version

    def __init__(self, input_contract: dict):
        self.input_contract = input_contract
        self.job_id = input_contract['job_id']

    def collect(self) -> CollectorOutput:
        """
        Collect database metadata.

        Returns:
            CollectorOutput with version fields populated
        """
        # ... collection logic ...

        output = CollectorOutput(
            # Version fields (REQUIRED)
            collector_version=self.AGENT_VERSION,    # Agent version
            # contract_version: the model's default, never passed (Decision 2)

            # Data fields
            job_id=self.job_id,
            database_metadata={...},
            schema={...},
            query_patterns={...},  # v1.1+ field
            aws_metadata={...}     # v1.2+ field
        )

        return output
```

---

## Versioning Rules

### Rule 1: Increment MINOR for Backward-Compatible Changes

**Backward-compatible changes (MINOR bump):**

- ✅ Add optional field
- ✅ Add new enum value
- ✅ Expand field type (int → float)
- ✅ Add new nested object (optional)

**Example:**

```python
# v1.0 → v1.1: Add optional field
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.1")
    job_id: str
    schema: dict
    query_patterns: Optional[dict] = None  # NEW (optional)
```

### Rule 2: Increment MAJOR for Breaking Changes

**Breaking changes (MAJOR bump):**

- ❌ Remove field
- ❌ Rename field
- ❌ Change field type (incompatible)
- ❌ Make optional field required
- ❌ Change field semantics

**Example:**

```python
# v1.2 → v2.0: Rename field (breaking)
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="2.0")
    job_id: str
    database_schema: dict  # RENAMED from 'schema' (breaking)
```

### Rule 3: Use Optional Fields for Additions

**Prefer optional fields for new additions:**

```python
# Good: Optional field (backward compatible)
new_field: Optional[str] = None

# Avoid: Required field (breaking change)
new_field: str  # Forces MAJOR version bump
```

### Rule 4: Deprecate Before Removing

**Deprecation process:**

1. Mark field as deprecated (add comment)
2. Add deprecation warning in code
3. Wait 1+ major version
4. Remove in next MAJOR version

```python
# v1.3: Deprecate field
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.3")

    # Deprecated in v1.3, will be removed in v2.0
    # Use 'database_schema' instead
    schema: Optional[dict] = Field(
        default=None,
        deprecated=True,
        description="DEPRECATED: Use database_schema instead"
    )

    database_schema: Optional[dict] = None  # Replacement field
```

---

## Reading Older Artifacts

The first version of this ADR described `src/contracts/adapters/`, a `src/contracts/version_detector.py` module and a `load_checkpoint` that adapted old data. None of them exist, and Decision 4 replaces them: a reader accepts any MINOR of the same MAJOR and refuses a different MAJOR. When a contract first ships a MAJOR bump, its migration (for example renaming a field) is written as an adapter in the same change, with a test that loads an artifact saved under the old MAJOR.

The check lives in `src/shared/contract_versioning.py` (#971):

- `load_contract(Model, data, artifact=...)` checks the stored version and parses the model. The loaded model says the model's current version, so a model loaded at an older MINOR and written back (the `.meta` progression, an approved assignment) stores today's version.
- `check_artifact(path, data)` checks a raw dict by its path against a registry of contract files (`collector/output.json`, `analysis-<engine>/analysis.json`, `assignment/v<N>/assignment.json`, ...). Every `ArtifactStore.read_json` returns what it returns, so the raw-dict reads of contract files are checked in the store.
- `carries_contract(data, Model, artifact=...)` tells an uploaded collector output (it has a `contract_version`) from a raw collection (it has none), and refuses a collector output at another MAJOR rather than parsing it as a collection.
- A stored file with no `contract_version` (written before #972) is read as legacy, with a debug log.
- A skipped placeholder is a contract of its own, stored at the path of the output it stands in for: `SkippedSchemaOutput` at `schema-<engine>/v<N>/schema_output.json` and `SkippedSynthesisReport` at the synthesis report paths (`src/contracts/skipped_placeholders.py`, #1023). The registry reads the payload's `status` first (`contract_for_payload`): a `status: "skipped"` file is checked against the placeholder's version, for any engine, and anything else against the path's contract. A placeholder saved before #1023 has no version and loads as legacy; the empty synthesis report then said `status: "completed"`, so it is still read as a legacy report. The lookup trusts the `status`: no design or report contract writes `status: "skipped"`, and the placeholders share their MAJOR (1) with every design contract and the report today, so a file that says `skipped` at another MAJOR is refused. Once a design contract moves to MAJOR 2, a stale 1.x design that said `skipped` would be read as a placeholder; that bump has to check for it.
- A writer that patches a contract file as a raw dict (the synthesis cache-overlay patch of an assignment, a scope rewrite of a schema output, an uploaded collector output written as `collector/output.json`, ...) writes it through `stamp_artifact_version(path, data)`, so the file says the registered contract's current version, never the one it was read with (#1018). `tests/contract/test_contract_writes_go_through_model.py` scans every `.write_json(path, data)` call in `src/` and `scripts/` (positional or keyword arguments) and fails when the path renders to a registered contract file, or ends in a contract file's name after a caller's prefix (`f"{base}/assignment.json"`), and the data is neither a model dump nor `stamp_artifact_version` of that same path. Its exceptions are keyed on the path written: the user's uploads, kept as sent. A write whose path it cannot render must be listed with what it writes. It does not see writes other than `.write_json` (a `json.dump` to a file), it follows a name to its last assignment by line whatever the branch, and the stamp does not validate the dict against the model.
- Model I/O (`MODEL_IO_PATTERNS`: `llm_requests/`, `llm_responses/`, `llm_input.json`, `llm_response.json`, `partial_output.json`, group drafts and their inputs) is a model session's own file, not a stored contract, and is never checked: a model may echo any version. The writer that turns a response into a stored contract validates it and writes the contract's own version.

A different MAJOR raises `ContractVersionError`, which names the artifact, the contract, the stored version and the current one in the server log; the API answers it with 409 and a plain message ("This assessment was saved by an incompatible version of the tool (contract 3.0, expected 2.*).") that names no path or code, and a job whose `.meta` progression is refused shows as FAILED with that message. The exception is a MAJOR listed in the module's `_ADAPTERS`, whose payload is turned into the current shape before it is read. One contract has shipped a MAJOR bump so far: `RealityCheckOutputContract` 2.0 (#790) removed the always-empty `lightweight_recommendations`, and its 1.x adapter drops that key, so a reality check saved before 2.0 still loads.

The amendment's guards prove only that, with no adapter, a different MAJOR is refused and never read as the current one, and that the one adapter there is reads its saved files. That a MAJOR bump ships its adapter in the same change is checked in review, not by a test.

---

## Checkpoint Compatibility

### Backward Compatibility (Old Checkpoints, New Code)

**Scenario:** Job started with v1.0, resumed with v1.2 code

```python
# Checkpoint saved with v1.0 (no query_patterns field)
checkpoint_data = {
    "contract_version": "1.0",
    "job_id": "job-123",
    "schema": {...}
    # No query_patterns field
}

# Load with v1.2 code (has optional query_patterns field)
output = CollectorOutput(**checkpoint_data)
# Works! query_patterns defaults to None
```

### Forward Compatibility (New Checkpoints, Old Code)

**Scenario:** Job started with v1.2, resumed with v1.0 code

```python
# Checkpoint saved with v1.2 (has query_patterns field)
checkpoint_data = {
    "contract_version": "1.2",
    "job_id": "job-123",
    "schema": {...},
    "query_patterns": {...}  # New field
}

# Load with v1.0 code (doesn't know about query_patterns)
# Pydantic ignores unknown fields by default
output = CollectorOutput(**checkpoint_data)
# Works! query_patterns ignored (graceful degradation)
```

**Note:** Pydantic's `extra="ignore"` (or `"allow"`) allows forward compatibility; only a model's answer sets `extra="forbid"` (Decision 5, #976).

---

## Implementation Guidelines

### Adding New Field (MINOR Version Bump)

```python
# Step 1: Add optional field
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.3")  # Bump MINOR

    # Existing fields
    job_id: str
    schema: dict

    # New field (optional)
    performance_metrics: Optional[dict] = None  # NEW in v1.3
```

### Breaking Change (MAJOR Version Bump)

```python
# Step 1: Deprecate old field in v1.x
class CollectorOutput(BaseModel):
    contract_version: str = Field(default="1.4")

    schema: Optional[dict] = Field(
        default=None,
        deprecated=True,
        description="DEPRECATED: Use database_schema"
    )
    database_schema: Optional[dict] = None  # Replacement

# Step 2: Create v2.0 with breaking change
class CollectorOutputV2(BaseModel):
    contract_version: str = Field(default="2.0")

    # 'schema' removed, only 'database_schema' remains
    database_schema: dict  # Now required

# Step 3: Create adapter
class CollectorOutputAdapter:
    @staticmethod
    def _migrate_v1_to_v2(data: Dict) -> Dict:
        if "schema" in data:
            data["database_schema"] = data.pop("schema")
        data["contract_version"] = "2.0"
        return data
```

---

## Contract Version Matrix

The current version of every contract is in `tests/contract/schema_snapshot.json` (the `version` key of each entry), which `tests/contract/test_schema_snapshot.py` keeps equal to the code. This ADR does not repeat it, so it cannot go stale.

**Key Points:**

- Multiple agent versions can produce same contract version
- Agent version changes frequently (bug fixes, optimizations)
- Contract version changes only when structure changes
- Deployment uses single agent version (all agents same version)

---

## Enforcement

Each decision names the guard test that fails when the code stops following it. `tests/unit/docs/test_adr_enforcement.py` fails when a decision has no row here, or when a row names a test file, class or function that does not exist. A decision with no guard yet says `none yet` and the issue that will add one; A decision no code can guard says `by design` and the reason. It is never left blank.

| Decision | Enforced by |
|---|---|
| 1. Every contract carries a `contract_version` | `tests/contract/test_written_contracts_versioned.py::test_every_written_model_carries_contract_version`<br>`tests/contract/test_written_contracts_legacy_load.py::test_a_file_written_before_the_version_still_loads`<br>`tests/unit/agents/referee/test_derived_versions_carry_contract_version.py`<br>`tests/contract/test_written_contracts_versioned.py::test_every_versioned_contract_declares_the_one_version_style`<br>`tests/unit/contracts/test_dynamodb_decision_trace.py::test_the_builder_writes_the_current_contract_version`<br>`tests/unit/contracts/test_dynamodb_decision_trace.py::test_the_analysis_handler_writes_the_trace_through_its_model`<br>`tests/unit/contracts/test_dynamodb_decision_trace.py::test_a_saved_pre_973_trace_still_loads`<br>`tests/unit/contracts/test_skipped_placeholders.py::test_the_schema_design_placeholder_is_written_through_its_model`<br>`tests/unit/contracts/test_skipped_placeholders.py::test_the_empty_synthesis_report_is_written_through_its_model`<br>`tests/unit/contracts/test_skipped_placeholders.py::test_a_schema_placeholder_saved_before_still_loads`<br>`tests/unit/contracts/test_decision_trace_base.py::test_every_analysis_trace_is_a_registered_contract`<br>`tests/unit/contracts/test_decision_trace_base.py::test_the_typed_engines_are_exactly_the_registered_traces`<br>`tests/unit/contracts/test_decision_trace_base.py::test_every_trace_contract_extends_the_base_unless_exempt`<br>`tests/unit/contracts/test_decision_trace_base.py::test_a_trace_with_no_registered_contract_is_refused`<br>`tests/unit/contracts/test_decision_trace_base.py::test_the_dynamodb_trace_extends_the_shared_base`<br>`tests/unit/contracts/test_documentdb_decision_trace.py::test_the_builder_writes_the_current_contract_version`<br>`tests/unit/contracts/test_documentdb_decision_trace.py::test_a_saved_pre_1188_trace_still_loads`<br>`tests/unit/contracts/test_elasticache_decision_trace.py::test_the_agent_builds_the_model`<br>`tests/unit/contracts/test_elasticache_decision_trace.py::test_a_saved_pre_1191_trace_still_loads`<br>`tests/unit/contracts/test_opensearch_decision_trace.py::test_the_builder_writes_the_current_contract_version`<br>`tests/unit/contracts/test_opensearch_decision_trace.py::test_a_saved_pre_1189_trace_still_loads`<br>`tests/unit/contracts/test_opensearch_decision_trace.py::test_a_trace_built_from_each_bundled_sample_validates_and_round_trips`<br>`tests/unit/contracts/test_awaiting_llm_marker.py::test_a_marker_saved_before_1267_still_loads`<br>`tests/unit/agents/referee/test_reality_check_versioning.py::TestHandlerVersions::test_external_mode_marker_is_a_versioned_contract`<br>`tests/unit/contracts/test_decision_record.py::test_a_record_saved_before_1257_still_loads`<br>`tests/unit/contracts/test_decision_record.py::test_the_review_gate_record_is_written_through_the_model`<br>`tests/unit/contracts/test_aurora_decision_trace.py::test_the_builder_writes_the_current_contract_version`<br>`tests/unit/contracts/test_aurora_decision_trace.py::test_a_saved_pre_1190_trace_still_loads`<br>`tests/unit/contracts/test_aurora_decision_trace.py::test_a_trace_built_from_each_bundled_sample_validates_and_round_trips` |
| 2. The agent version is a separate field | `tests/contract/test_writers_take_version_from_model.py::test_no_writer_passes_a_contract_version_literal`<br>`tests/unit/agents/analysis/test_analysis_writes_the_model_version.py`<br>`tests/unit/tools/schema/aurora_common/test_delta_merge.py::test_the_output_version_is_the_engine_contract_default`<br>`tests/contract/test_contract_writes_go_through_model.py::test_every_contract_file_write_goes_through_its_model`<br>`tests/contract/test_contract_writes_go_through_model.py::test_every_write_with_an_unknown_path_is_listed`<br>`tests/unit/contracts/test_rewrites_stamp_current_version.py::test_the_synthesis_cache_overlay_patch_writes_the_current_version`<br>`tests/unit/contracts/test_rewrites_stamp_current_version.py::test_the_dynamodb_finalize_scope_rewrite_writes_the_current_version`<br>`tests/unit/contracts/test_dynamodb_decision_trace.py::test_the_handler_stamps_the_current_version_over_a_stale_one` |
| 3. Contract versions are MAJOR.MINOR, and a shape change moves the version | `tests/contract/test_schema_snapshot.py::test_schema_snapshot_file_matches_a_fresh_rebuild`<br>`tests/unit/scripts/test_contract_schema_snapshot.py`<br>`tests/contract/test_value_set_snapshot.py::test_value_set_snapshot_matches_a_fresh_rebuild`<br>`tests/unit/scripts/test_contract_schema_snapshot.py::test_adding_the_version_pattern_to_contract_version_is_minor`<br>`tests/unit/scripts/test_contract_schema_snapshot.py::test_another_pattern_on_contract_version_is_still_major` |
| 4. Readers accept any MINOR of the same MAJOR and refuse a different MAJOR | `tests/unit/contracts/test_contract_versioning.py::test_the_same_major_at_a_higher_minor_is_accepted`<br>`tests/unit/contracts/test_contract_versioning.py::test_a_different_major_is_refused_naming_artifact_and_both_versions`<br>`tests/unit/contracts/test_contract_versioning.py::test_a_missing_version_is_accepted_as_legacy_with_a_debug_log`<br>`tests/unit/contracts/test_contract_versioning.py::test_a_progression_loaded_at_an_older_minor_is_written_back_at_the_current_one`<br>`tests/unit/contracts/test_contract_versioning.py::test_the_local_store_refuses_a_contract_file_at_another_major`<br>`tests/contract/test_reads_check_contract_version.py::test_no_artifact_read_validates_a_versioned_contract_directly`<br>`tests/contract/test_reads_check_contract_version.py::test_every_store_read_json_checks_the_artifact`<br>`tests/contract/test_reads_check_contract_version.py::test_the_registry_covers_every_written_contract`<br>`tests/contract/test_reads_check_contract_version.py::test_every_direct_json_file_read_checks_its_version`<br>`tests/e2e/test_pipeline.py::test_every_versioned_artifact_is_a_registered_contract_file`<br>`tests/unit/contracts/test_dynamodb_decision_trace.py::test_the_store_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_dynamodb_decision_trace.py::test_the_finalize_resume_refuses_a_partial_trace_at_another_major`<br>`tests/unit/contracts/test_documentdb_decision_trace.py::test_the_store_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_documentdb_decision_trace.py::test_the_schema_designer_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_documentdb_decision_trace.py::test_the_finalize_resume_refuses_a_partial_trace_at_another_major`<br>`tests/unit/contracts/test_elasticache_decision_trace.py::test_the_store_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_elasticache_decision_trace.py::test_the_schema_designer_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_opensearch_decision_trace.py::test_the_store_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_opensearch_decision_trace.py::test_the_schema_designer_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_opensearch_decision_trace.py::test_the_handler_stops_on_a_trace_at_another_major`<br>`tests/unit/contracts/test_documentdb_decision_trace.py::test_the_store_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_documentdb_decision_trace.py::test_the_schema_designer_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_awaiting_llm_marker.py::test_the_store_refuses_a_marker_at_another_major`<br>`tests/unit/contracts/test_decision_record.py::test_the_store_refuses_a_record_at_another_major`<br>`tests/unit/contracts/test_decision_record.py::test_check_decision_refuses_a_record_at_another_major`<br>`tests/unit/contracts/test_decision_record.py::test_check_decision_refuses_a_record_that_is_not_one`<br>`tests/unit/contracts/test_decision_record.py::test_a_bad_record_does_not_count_as_an_approval`<br>`tests/unit/contracts/test_aurora_decision_trace.py::test_the_store_refuses_a_trace_at_another_major`<br>`tests/unit/contracts/test_aurora_decision_trace.py::test_the_schema_designer_refuses_a_trace_at_another_major` |
| 5. Optional fields by default | `tests/unit/scripts/test_contract_schema_snapshot.py`<br>`tests/contract/test_extra_policy.py::test_every_model_has_its_recorded_extra_setting`<br>`tests/contract/test_extra_policy.py::test_no_read_back_artifact_refuses_unknown_fields`<br>`tests/unit/contracts/test_newer_files_load.py::test_an_assignment_with_a_newer_move_and_finding_loads`<br>`tests/unit/test_assignment_overrides.py::TestApproveAsIsKeepsNewerFields::test_unknown_top_level_and_query_fields_survive`<br>`tests/e2e/test_pipeline.py::test_every_read_back_artifact_loads_a_newer_file_with_unknown_fields` |
| 6. Deprecate before removing | by design -- removing a field already needs a MAJOR bump under Decision 3, which the schema snapshot guard (`tests/contract/test_schema_snapshot.py`) enforces |
| 7. A single version is deployed at a time | by design -- a deployment property, not something code can check |
| 8. A pull request declares the bump kind; the number is assigned after its last rebase | `tests/unit/scripts/test_contract_bumps.py::test_resolution_takes_the_next_number_above_mains`<br>`tests/unit/scripts/test_contract_bumps.py::test_resolution_counts_from_main_after_another_pr_took_the_number`<br>`tests/unit/scripts/test_contract_bumps.py::test_several_pending_files_for_one_contract_make_one_bump`<br>`tests/unit/scripts/test_contract_bumps.py::test_a_hand_bump_with_a_pending_file_is_refused`<br>`tests/unit/scripts/test_contract_bumps.py::test_check_accepts_pending_files_and_require_resolved_refuses_them`<br>`tests/unit/scripts/test_contract_bumps.py::test_the_merge_queue_refuses_a_pending_bump`<br>`tests/unit/scripts/test_contract_schema_snapshot.py::test_a_pending_minor_bump_covers_an_added_optional_field`<br>`tests/unit/scripts/test_contract_schema_snapshot.py::test_a_pending_minor_bump_does_not_cover_a_removed_field`<br>`tests/unit/scripts/test_contract_value_snapshot.py::test_a_pending_minor_bump_does_not_cover_a_removed_value`<br>`tests/unit/ci/test_pre_merge.py::test_check_contract_with_a_pending_bump`<br>`tests/unit/scripts/test_contract_bumps.py::test_a_contract_it_cannot_edit_does_not_block_the_others`<br>`tests/unit/scripts/test_contract_bumps.py::test_bump_contract_refuses_a_contract_it_cannot_resolve`<br>`tests/unit/scripts/test_contract_bumps.py::test_check_refuses_a_pending_file_it_could_not_resolve`<br>`tests/unit/scripts/test_contract_bumps.py::test_a_pending_file_whose_bump_already_landed_is_refused`<br>`tests/unit/scripts/test_contract_bumps.py::test_the_default_base_is_the_public_repos_remote`<br>`tests/unit/scripts/test_contract_schema_snapshot.py::test_a_change_with_a_regenerated_snapshot_but_no_bump_fails_against_main`<br>`tests/unit/scripts/test_contract_value_snapshot.py::test_a_value_change_with_a_regenerated_snapshot_but_no_bump_fails_against_main`<br>`tests/unit/scripts/test_check_version_pins.py::test_every_resolvable_contract_moves_one_minor_with_its_history`<br>`tests/unit/scripts/test_check_version_pins.py::test_validate_contracts_runs_it_as_a_blocking_step`<br>`tests/unit/scripts/test_check_version_pins.py::test_a_collection_error_is_not_reported_as_a_pin` |
| Amendment 2026-10-20 (Decision 1, #1192): every written JSON file is a contract, model I/O or a declared internal artifact | `tests/contract/test_declared_artifacts.py::test_every_written_json_path_is_declared`<br>`tests/contract/test_declared_artifacts.py::test_every_json_file_a_run_writes_is_declared`<br>`tests/contract/test_declared_artifacts.py::test_no_stale_entries_in_any_list`<br>`tests/unit/docs/test_contract_artifacts_doc.py::test_the_doc_lists_every_registered_and_internal_artifact` |
| Amendment 2026-10-16 (Decision 4, #970): adapters only when a MAJOR bump ships | `tests/unit/contracts/test_contract_versioning.py::test_a_different_major_is_refused_naming_artifact_and_both_versions`<br>`tests/unit/contracts/test_contract_versioning.py::test_a_reality_check_saved_at_1_x_loads_through_its_adapter`<br>`tests/unit/contracts/test_contract_versioning.py::test_a_reality_check_at_a_major_with_no_adapter_is_refused` |

---

## Consequences

### Positive

✅ **Backward compatible**: Old checkpoints work with new code
✅ **Forward compatible**: New checkpoints degrade gracefully with old code
✅ **Clear versioning**: Semantic versioning easy to understand
✅ **Migration path**: Adapters handle MAJOR version changes
✅ **Minimal overhead**: Optional fields keep changes simple
✅ **Single deployment**: No multi-version complexity
✅ **Agent version tracking**: Know which agent produced output

### Negative

⚠️ **Adapter maintenance**: Need to maintain migration code
⚠️ **Version sprawl**: Multiple versions coexist (during job resume)
⚠️ **Testing complexity**: Test across versions
⚠️ **Rollback limitations**: Forward compatibility may lose data

### Neutral

🔶 **MAJOR versions rare**: Most changes are MINOR (optional fields)
🔶 **Deprecation period**: 1+ major version before removal
🔶 **Version detection**: Automatic from contract_version field
🔶 **Single deployment**: User doesn't choose agent versions
🔶 **Version mismatch**: Only during job resume after upgrade/rollback

---

## Alternatives Considered

### Alternative 1: No Versioning (Rejected)

**Rejected because:**

- ❌ Breaking changes break old checkpoints
- ❌ No migration path
- ❌ Can't evolve contracts safely

### Alternative 2: Full Semantic Versioning (MAJOR.MINOR.PATCH) (Rejected)

**Rejected because:**

- ❌ PATCH not needed (Pydantic handles bug fixes)
- ❌ Unnecessary complexity
- ❌ MAJOR.MINOR sufficient

### Alternative 3: Separate Contract Versions (v1/, v2/) (Rejected)

**Rejected because:**

- ❌ Code duplication (v1 and v2 contracts)
- ❌ Hard to maintain multiple versions
- ❌ Adapters simpler than parallel versions

---

## Release Management (Out of Scope)

**This ADR does NOT cover:**

- ❌ Release versioning (e.g., v1.5.0, v2.0.0)
- ❌ Release tagging strategy (git tags)
- ❌ Cherry-picking process
- ❌ Hotfix procedures
- ❌ Release notes format

**Why out of scope?**

- Contract versioning is internal (data structure compatibility)
- Release management is external (user-facing versions)
- Different concerns, different audiences

**Future ADR needed:**

- ADR-0XX: Release Management and Versioning Strategy
  - Release version format (semantic versioning for releases)
  - Git tagging strategy (e.g., `v1.5.0`, `v2.0.0`)
  - Branch strategy (mainline, release branches, hotfix branches)
  - Cherry-picking policy (when and how)
  - Release notes format
  - Upgrade path for users

**Relationship to contract versioning:**

```
Release v1.5.0 (user-facing)
  ├─ Agent versions: 2.3.1, 1.1.0, 1.0.5 (internal)
  └─ Contract versions: 1.2, 1.0, 1.0 (internal)

Release v2.0.0 (user-facing, breaking changes)
  ├─ Agent versions: 3.0.0, 2.0.0, 2.0.0 (internal)
  └─ Contract versions: 2.0, 2.0, 2.0 (internal, breaking)
```

**Key point:** Release versions are user-facing, contract versions are internal.

---

## Related Documents

- [ADR-002: Structured Output with Pydantic](ADR-002-structured-output-and-validation.md)
- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-02 | Architecture Team | Initial decision |
| 1.1 | 2026-10-16 | Architecture Team | Numbered the decisions, added the Enforcement table, replaced the adapter decision with accept-MINOR/refuse-MAJOR readers (#970), replaced the version matrix with a pointer |
| 1.2 | 2026-10-17 | Architecture Team | Decision 1 guarded: every contract written to disk carries a `contract_version` (#972) |
| 1.3 | 2026-10-17 | Architecture Team | Decision 4 guarded: every artifact read checks the stored `contract_version` through `src/shared/contract_versioning.py` (#971). Corrected the Decision 4 amendment: `RealityCheckOutputContract` 2.0 (#790) had shipped a MAJOR bump without an adapter; its 1.x adapter is a retrofit in #971 |
| 1.5 | 2026-10-17 | Architecture Team | Decision 2 guarded on rewrites: every write of a registered contract file is a model dump or stamped with the contract's current version (#1018) |
| 1.6 | 2026-10-17 | Architecture Team | Decision 1: the DynamoDB decision trace is a versioned contract, `DynamoDBDecisionTrace`, written through its model and checked on read (#973); the other engines' traces stay plain dicts |
| 1.8 | 2026-10-17 | Architecture Team | Decision 5 recorded and guarded: a model's answer forbids unknown fields, an artifact read back across versions accepts them; the move ledger, feasibility findings, groups manifest and design metrics move from `forbid` to `allow` (#976) |
| 1.9 | 2026-10-17 | Architecture Team | Decision 1: the skipped placeholders (schema output, synthesis report) are contracts of their own, written through their models and checked by their `status` first (#1023) |
| 1.10 | 2026-10-18 | Architecture Team | Decision 5: approving an assignment as-is patches the stored file, so a newer MINOR's top-level and per-query fields are kept (#1056) |
| 1.11 | 2026-10-18 | Architecture Team | Decision 1: the last five contracts declare `contract_version` with the MAJOR.MINOR pattern (#1006); Decision 3: adopting exactly that pattern on `contract_version` (from an unpatterned string or a MAJOR.MINOR `Literal`) is a MINOR change |
| 1.12 | 2026-10-19 | Architecture Team | Decision 8 added and guarded: a pull request declares the bump kind in a pending file and `scripts/resolve_contract_versions.py` assigns the number above main's after the last rebase; the merge queue refuses a pending file (#1158) |
| 1.13 | 2026-10-20 | Architecture Team | Decision 8: tests compare with `current_version(Model)`, never a literal version; `scripts/check_version_pins.py` bumps every resolvable contract in a scratch worktree and runs the version tests, in `validate-contracts` and `ci/pre_merge.sh` (#1226) |
| 1.14 | 2026-10-20 | Architecture Team | Decision 1 amended: every JSON file a run writes is a registered contract, model I/O or a declared internal artifact (`INTERNAL_ARTIFACTS`), guarded by `tests/contract/test_declared_artifacts.py` (#1192) |
| 1.15 | 2026-10-20 | Architecture Team | Decision 1: the decision traces share `DecisionTraceBase`; `write_decision_trace` refuses a trace with no registered contract, except for a pending set of engines that only shrinks (#1187) |
| 1.16 | 2026-10-20 | Architecture Team | Decision 1: the DocumentDB decision trace is a versioned contract, `DocumentDBDecisionTrace` on `DecisionTraceBase`, written through its model and checked on read; DocumentDB leaves the pending set (#1188) |
| 1.17 | 2026-10-20 | Architecture Team | Decision 1: the ElastiCache decision trace is a versioned contract, `ElastiCacheDecisionTrace`, typed as it is (exempt from the base), written through its model and checked on read; ElastiCache leaves the pending set (#1191) |
| 1.18 | 2026-10-21 | Architecture Team | Decision 1: the reality check's `awaiting_llm.json` marker is a versioned contract, `RealityCheckMarker`, written through its model and loaded through it by the assignment lineage and the local API; it leaves the internal list (#1267) |
| 1.19 | 2026-10-22 | Architecture Team | Decision 1: the ElastiCache decision trace gains the shared trace sections as optional fields (MINOR), and stays off `DecisionTraceBase` so saved 1.0 traces load (#1276) |
| 1.20 | 2026-10-21 | Architecture Team | Decision 1: the `decisions/` records are a versioned contract, `DecisionRecord`, written through its model and checked on read by `scripts/check_decision.py`; they leave the internal list (#1257) |
| 1.21 | 2026-10-21 | Architecture Team | Decision 1: the OpenSearch decision trace is a versioned contract, `OpenSearchDecisionTrace` on `DecisionTraceBase`, written through its model and checked on read; OpenSearch leaves the pending set (#1189) |
| 1.22 | 2026-10-21 | Architecture Team | Decision 1: the Aurora PostgreSQL and MySQL decision traces are one versioned contract, `AuroraDecisionTrace` on `DecisionTraceBase`, written through its model and checked on read; every analysis trace is typed, so the pending set `UNTYPED_TRACE_ENGINES` and the writer's plain-dict branch are removed (#1190)
| 1.23 | 2026-10-22 | Architecture Team | Decision 1: the schema design trace and a DynamoDB group's trace are registered contracts, `DesignTrace` and `DesignTraceGroup` 1.0, written through one writer and read through the model; they leave the internal list (#1258) |

---

**Status: Accepted and Ready for Implementation**
