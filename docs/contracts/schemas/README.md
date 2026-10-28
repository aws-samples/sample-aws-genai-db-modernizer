# JSON Schemas for the `assess()` contract

This folder holds the JSON Schemas of the public library entry point `assess()` ([assess.md](../assess.md)), for callers in other languages or callers that validate payloads outside Python.

| File | Model | What it describes |
|---|---|---|
| `assess_request.v1.schema.json` | `AssessmentRequest` | The request a caller sends |
| `assess_result.v1.schema.json` | `AssessmentResult` | The result `assess()` returns, as JSON (`model_dump_json`) |

## Versions

Each file is named after the MAJOR version of the contract it describes: `v1` covers every `contract_version` `1.x`. A MINOR version (a new optional field, a new enum value, a new engine in the result's `designs`) regenerates the same file; a MAJOR version adds a `v2` file next to it. Each file states the exact `contract_version` it was rendered from in `x-contract-version`, and the payloads carry it in `contract_version`. The current files are `1.3`, and they describe `1.3` only: this release supports contract `1.3` only, so `contract_version` is the constant `"1.3"` in both. Versions `1.0`, `1.1` and `1.2` were beta development versions, removed in v0.4.0-beta.3: their payloads no longer validate against these files, and the library refuses them ([assess.md, Removed: 1.0, 1.1 and 1.2](../assess.md#removed-10-11-and-12)). A MINOR version can add a `designs` engine, and `designs` is a closed `oneOf` on `engine`: read `designs` with a default branch on `engine` in your own code.

The schemas use JSON Schema draft 2020-12 (`$schema`). Each file's `$id` is its raw URL on this repository's `main` branch.

In the result schema every field is required: the library always writes it, and a field with nothing to say is `null` or empty, never left out. In the request schema only `database_name` and `collection` are required; the other fields have defaults.

## Validating a payload

Any draft 2020-12 validator works. In Python, with `jsonschema`:

```python
import json
from jsonschema import Draft202012Validator

schema = json.load(open("docs/contracts/schemas/assess_result.v1.schema.json"))
Draft202012Validator(schema).validate(json.load(open("result.json")))
```

From Python, the models themselves are the stricter check: `AssessmentRequest.model_validate(payload)` and `AssessmentResult.model_validate_json(text)` (`src.assessment`). The schemas describe the shape; the library applies a few rules a schema does not state:

- `options.target_engines`: ids are normalised, and an unknown, legacy or planned engine id is refused;
- `options.run_through`: `"schema_design"` and `"synthesis"` need `deterministic: false` (the request schema states it too, with `if`/`then`); the library refuses them otherwise, with a message;
- `database_name`: surrounding whitespace is stripped before the name is checked (the schema's `pattern` checks the name as sent);
- `label` follows `summary.run_through_reached` (`analysis_only`, `recommendation`, `designed`); a `"schema_design"` or `"synthesis"` result with an empty `summary.designed_engines` is a `recommendation`;
- `contract_version`: a retired version (`"1.0"`, `"1.1"` or `"1.2"`) raises `RetiredContractVersionError` (a `ValueError`), which names the supported version; any other value but `"1.3"` is refused as an invalid field (`pydantic.ValidationError`);
- `designs` has one entry per engine of `summary.designed_engines`, in that order;
- every engine in a statement's `fit` has a `fit_breakdown` entry with no `gate` and the same `fit`, and an engine with a `gate` in `fit_breakdown` has no `fit` and is in `excluded`.

## Regenerating

The files are generated from the Pydantic models in `src/assessment/models.py`; never edit them by hand:

```bash
uv run python scripts/render_contract_schemas.py --write
```

`ci/lint.sh` runs `scripts/render_contract_schemas.py --check`, which fails when a committed schema differs from a fresh render, so a model change without a regeneration fails lint.
