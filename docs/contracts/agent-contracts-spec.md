# Agent contracts specification

The contracts are the typed interfaces between the pipeline's phases and between the pipeline and its callers. Each one is a Pydantic model in `src/contracts/`. The models are the source of truth; this page explains how they are organised and versioned, and where the rules are enforced. It does not repeat field lists, because a copy of a field list goes stale: read the model.

Related decisions:

- [ADR-002: Structured output and validation](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-002-structured-output-and-validation.md)
- [ADR-008: Contract versioning](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-008-contract-versioning.md)

## Table of contents

1. [Contract overview](#1-contract-overview)
2. [Versioning strategy](#2-versioning-strategy)
3. [Contracts by phase](#3-contracts-by-phase)
4. [Contract testing](#4-contract-testing)
5. [Changing a contract](#5-changing-a-contract)

---

## 1. Contract overview

### 1.1 Where contracts sit in the pipeline

`LocalOrchestrator` calls each phase's handler in-process over the local `ArtifactStore`; there is no state machine and no per-agent task. The phases are the `Phase` enum in `src/contracts/phase_models.py`, in this order:

1. `collect_triage`: collect, then deterministic triage.
2. `analysis`: one deterministic analysis per selected engine.
3. `assignment`: query-to-engine assignment.
4. `reality_check`: the consolidation review.
5. `assignment_review`: the one human gate.
6. `schema_design`: one designer per assigned engine.
7. `load_test`: optional.
8. `synthesis`: the report and the deliverables.

A phase reads the previous phases' artifacts and writes its own. Every artifact is a contract, model input or output, or a declared internal file; [artifacts.md](artifacts.md) lists them all.

### 1.2 Where contracts live

```text
src/contracts/             Pydantic models (source of truth)
docs/contracts/
├── agent-contracts-spec.md   this page
├── artifacts.md              every JSON file a job writes, by kind
├── assess.md                 the assess() library contract
├── assess-design-block.md    the stable design block of an assess() result
└── schemas/                  published JSON Schemas of the assess() contract
tests/contract/            contract tests and the committed snapshots
```

`docs/contracts/schemas/` holds the JSON Schemas generated for the public `assess()` request and result (see [schemas/README.md](schemas/README.md)). Internal contracts are checked against the Pydantic models and the snapshots in `tests/contract/`, not against JSON Schema files.

---

## 2. Versioning strategy

### 2.1 MAJOR.MINOR

Every contract carries `contract_version` in the form `MAJOR.MINOR`, declared as `contract_version: str = Field(default="X.Y", pattern=r"^\d+\.\d+$", ...)`. There is no PATCH part.

- **MAJOR**: a breaking change, incompatible with the previous version.
- **MINOR**: a backward-compatible addition.

See [ADR-008](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-008-contract-versioning.md), Decision 3.

### 2.2 Agent version and contract version

The version of an agent's code (for example `collector_version`) is a separate field and never stands in for the contract version. Several agent versions can write the same contract version. A writer takes the contract version from the model's default and never from a literal of its own (`tests/contract/test_writers_take_version_from_model.py`).

### 2.3 Versioning rules

| Change type | Version increment | Example |
|-------------|-------------------|---------|
| Add optional field | MINOR | 1.0 to 1.1 |
| Add new enum value | MINOR | 1.0 to 1.1 |
| Remove field | MAJOR | 1.2 to 2.0 |
| Rename field | MAJOR | 1.2 to 2.0 |
| Change field type | MAJOR | 1.2 to 2.0 |
| Make optional field required | MAJOR | 1.2 to 2.0 |
| Tighten a constraint (`ge`/`le`/`gt`/`lt`/`min_length`/`max_length`/`pattern`) | MAJOR | 1.2 to 2.0 |
| Loosen a constraint | MINOR | 1.0 to 1.1 |
| `contract_version` adopts exactly `^\d+\.\d+$` (from an unpatterned string or a MAJOR.MINOR `Literal`, #1006) | MINOR | 1.0 to 1.1 |

The published enum and literal members, engine ids, capability ids and access-pattern id prefixes count as part of the shape: adding one is MINOR, removing or renaming one is MAJOR.

### 2.4 Declaring a version

Each contract model declares its own version and a Version History in its module docstring. For example, the collector output (`src/contracts/collector_output.py`):

```python
class CollectorOutputContract(BaseModel):
    contract_version: str = Field(default="X.Y", pattern=r"^\d+\.\d+$")
    collector_version: str  # the agent version, a separate field
```

The current number of every contract is in `tests/contract/schema_snapshot.json`; read it there or call `current_version(Model)` from `src/shared/contract_versioning.py`. Do not copy a number into a document or a test: the number is assigned after a pull request's last rebase (section 5).

### 2.5 Compatibility policy

- A reader accepts any MINOR of the MAJOR it was written for and refuses a different MAJOR, with an error that names the contract, the stored version and the supported MAJOR. Read a stored contract with `load_contract` from `src/shared/contract_versioning.py`; the artifact store checks raw-dict reads of contract files itself (ADR-008, Decision 4).
- A MAJOR bump ships its adapter in the same change. Adapters live in `src/shared/contract_versioning.py` (`_ADAPTERS`).
- A new field is optional with a default, so older artifacts still load (Decision 5).
- Each model's `extra` setting is a recorded choice. A model's answer from a language model keeps `extra="forbid"`; an artifact written to disk and read back across versions accepts unknown fields. `tests/contract/test_extra_policy.py` pins every model that is not the default.
- A field is marked deprecated for at least one MAJOR version before the next MAJOR removes it (Decision 6).

---

## 3. Contracts by phase

| Phase | Contract (model file in `src/contracts/`) | Artifact |
|---|---|---|
| Collect | `collector_input.py`, `collector_output.py` (`CollectorOutputContract`) | `collector/output.json` |
| Triage | `triage_output.py` (`TriageOutputContract`) | `referee-triage/triage.json` |
| Analysis | `analysis_input.py`, `analysis_output.py` (`AnalysisOutputContract`) and a decision trace per engine | `analysis-<engine>/analysis.json` |
| Assignment | `assignment_models.py` (`Assignment`), `move_ledger.py` | `assignment/v<N>/assignment.json` |
| Reality check | `reality_check_output.py` (`RealityCheckOutputContract`), `reality_check_model_response.py`, `model_flag.py` | `reality-check/output.json` |
| Assignment review | `decision_record.py`, `engine_scope_decision.py` | `decisions/` |
| Schema design | `schema_design_input.py`, `schema_design_output.py` and one `<engine>_model_output.py` per engine | `schema-<engine>/v<N>/schema_output.json` |
| Load test | `load_test_models.py` | `load-test-<engine>/v<N>/` |
| Synthesis | `synthesis_output.py` (`SynthesisOutputContract`) | `synthesis/v<N>/report.json`, `deliverables/assessment-data.json` |

Phase state is `phase_models.py` (`PhaseProgression`, under `.meta/`). The full list of paths, with the model that checks each, is [artifacts.md](artifacts.md). `referee_input.py` and `referee_output.py` are the legacy single-referee contracts from before the split into triage and synthesis; nothing in the pipeline reads them as one object.

Synthesis computes every customer-facing figure once into the report's `facts` (ADR-032); the renderers only format it.

The routing contracts (`Assignment`, the move ledger, the reality-check output) follow [ADR-031](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-031-query-first-routing-and-engine-rule-catalog.md): the query is the unit, and only `move_ledger.apply` writes `assigned_engine`.

The library entry point `assess()` has its own request and result, versioned apart from the artifacts: [assess.md](assess.md).

---

## 4. Contract testing

| Test | What it guards |
|---|---|
| `tests/contract/test_schema_snapshot.py` | The JSON Schema of every versioned contract against `schema_snapshot.json`; a shape change needs its version bump |
| `tests/contract/test_value_set_snapshot.py` | Enum and literal members, engine ids, capability ids and id prefixes against `value_set_snapshot.json` |
| `tests/contract/test_declared_artifacts.py` | Every JSON file a run writes is a contract, model I/O or a declared internal file |
| `tests/contract/test_written_contracts_versioned.py` | A written contract declares its own version |
| `tests/contract/test_reads_check_contract_version.py` | A raw read of a contract file checks the version |
| `tests/contract/test_extra_policy.py` | Each model's `extra` setting |
| `tests/contract/test_assess_*` | The `assess()` contract as a caller sees it, and against the last release |

`make test` and `ci/test.sh` run these. The `validate-contracts` CI job runs `scripts/validate_contracts.py` and the version-pin check.

---

## 5. Changing a contract

CONTRIBUTING.md, "Contract Changes", owns the procedure. In short:

1. Declare the bump kind with `uv run python scripts/bump_contract.py <contract> minor|major --reason "..." --issue N`. This writes one file under `src/contracts/pending/`.
2. After your last rebase onto main, run `uv run python scripts/resolve_contract_versions.py`. It assigns the number and regenerates both snapshots.
3. In tests, compare a written contract's version with `current_version(Model)`, never a literal.
4. A MAJOR bump ships its adapter in the same change.
