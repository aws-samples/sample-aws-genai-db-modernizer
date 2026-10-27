# Agent contracts documentation

This directory holds the contract documentation for the Database Modernizer Assessment pipeline. The contracts themselves are the Pydantic models in `src/contracts/`.

## Index

| Document | Purpose | Audience |
|---|---|---|
| [agent-contracts-spec.md](agent-contracts-spec.md) | How contracts are organised and versioned, and where the rules are enforced | All developers |
| [artifacts.md](artifacts.md) | Every JSON file a job writes: registered contracts, model I/O and internal artifacts | Developers, library callers |
| [assess.md](assess.md) | The library entry point `assess()`: its typed request and result, the stable surface and the unversioned artifacts | Library callers |
| [assess-design-block.md](assess-design-block.md) | The published standard for the stable design block of a designed `assess()` result: the fields per engine, what is guaranteed, and an example per engine | Library callers |
| [../../src/contracts/](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/src/contracts/) | Pydantic model implementations (source code) | Developers, tools |
| [schemas/](schemas/README.md) | The published JSON Schemas of the `assess()` request and result | External tools, library callers |

## Getting started

1. Read [agent-contracts-spec.md](agent-contracts-spec.md).
2. Find the model for your phase in `src/contracts/` (the table in section 3 of the spec maps phases to files).
3. Validate with the Pydantic model; read a stored contract through `load_contract` (`src/shared/contract_versioning.py`) so the version check runs.
4. To change a contract, follow "Contract Changes" in [CONTRIBUTING.md](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/CONTRIBUTING.md).

## Contracts by phase

| Phase | Source of truth |
|---|---|
| Collector | [src/contracts/collector_output.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/collector_output.py) |
| Triage | [src/contracts/triage_output.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/triage_output.py) |
| Analysis | [src/contracts/analysis_output.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/analysis_output.py) |
| Assignment and move ledger | [src/contracts/assignment_models.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/assignment_models.py), [src/contracts/move_ledger.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/move_ledger.py) |
| Reality check | [src/contracts/reality_check_output.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/reality_check_output.py) |
| Schema design | [src/contracts/schema_design_output.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/schema_design_output.py) and one `<engine>_model_output.py` per engine |
| Load test | [src/contracts/load_test_models.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/load_test_models.py) |
| Synthesis | [src/contracts/synthesis_output.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/synthesis_output.py) |
| Phase progression | [src/contracts/phase_models.py](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/src/contracts/phase_models.py) |

## Key concepts

### Versioning

Contracts are versioned `MAJOR.MINOR`, with no PATCH ([ADR-008](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/architecture/decisions/ADR-008-contract-versioning.md)).

- **MAJOR**: a breaking change. It ships its adapter in the same change.
- **MINOR**: a backward-compatible addition.

A reader accepts any MINOR of its MAJOR and refuses a different MAJOR. The current numbers are in `tests/contract/schema_snapshot.json`; this page does not copy them.

### Validation

A contract is validated by its Pydantic model when it is written and when it is read. `tests/contract/` pins the shape of every versioned contract in a committed snapshot, so a change to a field fails until its version moves.

```bash
uv run python -c "from src.contracts.collector_output import CollectorOutputContract; CollectorOutputContract.model_validate_json(open('collector/output.json').read())"
```

## Changing a contract

The procedure (declare the bump with `scripts/bump_contract.py`, assign the number with `scripts/resolve_contract_versions.py` after the last rebase) is in CONTRIBUTING.md under "Contract Changes" and summarised in section 5 of the spec.

## Related documentation

- [High-level design](../architecture/high-level-design.md)
- [Data specifications](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/docs/data-specs/)
