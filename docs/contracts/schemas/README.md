# DEPRECATED: JSON Schema Files

**Status:** DEPRECATED as of v2.0
**Date:** February 2, 2026

---

## Migration Notice

These JSON Schema files are **deprecated** and should not be used for new development.

**Use Pydantic models instead:** `src/contracts/`

---

## Why Deprecated?

As of Database Modernizer v2.0, we use **Pydantic models** instead of JSON Schema for contracts.

**Reasons:**

- ✅ Pydantic provides better type safety
- ✅ LLMs understand Pydantic type hints better (no format retries needed)
- ✅ Easier to maintain and evolve
- ✅ Better IDE support and developer experience
- ✅ Automatic validation without retry loops

**See:**

- [ADR-002: Structured Output with Pydantic](../../architecture/decisions/ADR-002-structured-output-and-validation.md)
- [Pydantic Contracts](../../../src/contracts/)

---

## Migration Path

**Old (JSON Schema):**

```python
import json
import jsonschema

# Load schema
with open('schemas/collector-output.json') as f:
    schema = json.load(f)

# Validate
jsonschema.validate(instance=data, schema=schema)
```

**New (Pydantic):**

```python
from contracts.models.collector_output import CollectorOutput

# Validate and create
output = CollectorOutput(**data)  # Automatic validation

# Or from JSON
output = CollectorOutput.model_validate_json(json_str)
```

---

## Timeline

- **v1.x**: JSON Schema (deprecated)
- **v2.0+**: Pydantic models (current)

These files are kept for reference only. Do not use for new development.
