# Storage Architecture Implementation Guide

**Document Type:** Implementation Guide
**Status:** Current

---

## Overview

This guide covers the `ArtifactStore` abstraction (`src/storage/`) that every
agent reads and writes through. All agent artifacts follow a shared path
convention:

```
<database-name>/<job_id>/<agent-name>/<filename>
```

There is no intra-step checkpointing above the collector (see
[strands-collector-guide.md](strands-collector-guide.md) for the collector's
own checkpoint stages). If a non-collector step fails, the user re-runs the
command or resumes the phase — `LocalOrchestrator` does not retry
automatically. See [the orchestrator README](../../src/orchestrator/README.md).

---

## The `ArtifactStore` Abstraction

```python
# src/storage/artifact_store.py (real code)
class ArtifactStore(ABC):
    """Storage-agnostic artifact read/write interface."""

    @abstractmethod
    def read_json(self, path: str) -> dict:
        """Read a JSON artifact and return it as a dict."""
        ...

    @abstractmethod
    def write_json(self, path: str, data: dict) -> None:
        """Write a dict as a JSON artifact."""
        ...

    @abstractmethod
    def read_bytes(self, path: str) -> bytes:
        """Read a binary artifact and return its raw bytes."""
        ...

    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None:
        """Write raw bytes as a binary artifact."""
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return True if the artifact at *path* exists."""
        ...

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Return all artifact keys under *prefix*."""
        ...
```

Two implementations, selected by a factory — no DynamoDB, no separate
metadata table:

```python
# src/storage/__init__.py (real code)
def create_artifact_store() -> ArtifactStore:
    """Factory: S3_BUCKET env var set → S3ArtifactStore, else LocalArtifactStore.

    Uses ARTIFACT_DIR env var for local store base directory (default: ./artifacts).
    """
    bucket = os.environ.get("S3_BUCKET")
    if bucket:
        from src.storage.s3_store import S3ArtifactStore

        return S3ArtifactStore(bucket)

    from src.storage.local_store import LocalArtifactStore

    return LocalArtifactStore(os.environ.get("ARTIFACT_DIR", "./artifacts"))
```

### `LocalArtifactStore` (`src/storage/local_store.py`)

The default for every local run — the CLI, Claude Code, and the local
API/UI. A thin wrapper over `pathlib.Path`: `read_json`/`write_json` do
`json.loads`/`json.dumps` against files under `ARTIFACT_DIR` (default
`./artifacts`), `list_prefix` globs `*.json` under a prefix directory.

### `S3ArtifactStore` (`src/storage/s3_store.py`)

Any process with `S3_BUCKET` set gets this instead of the local store — in
practice that's the separate AWS Transform integration
(`src/atx_orchestrator/`), which sets it so jobs started there read and
write the exact same artifact layout in an S3 bucket, and a job can be
inspected the same way regardless of which path produced it. `exists()`
treats a `404` from `head_object` as "does not exist" and re-raises
everything else.

```python
# src/storage/s3_store.py (real code, trimmed)
class S3ArtifactStore(ArtifactStore):
    def __init__(self, bucket: str, s3_client=None):
        self.bucket = bucket
        self.s3 = s3_client or boto3.client("s3")

    def read_json(self, path: str) -> dict:
        response = self.s3.get_object(Bucket=self.bucket, Key=path)
        return json.loads(response["Body"].read())

    def write_json(self, path: str, data: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket, Key=path,
            Body=json.dumps(data, indent=2, default=str),
            ContentType="application/json",
        )
```

Neither implementation uses DynamoDB. There is no hosted metadata table —
that was part of the retired hosted deployment
([#175](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/175)).

---

## Job Status, Without a Metadata Table

The local API's `LocalExecutionService`
(`src/api/services/local_execution.py`) derives job status entirely from the
artifact directory layout `LocalArtifactStore` already produces — it does
not maintain a separate status table:

```python
# src/api/services/local_execution.py (real docstring)
"""Local execution service — the API's only execution backend (hosted Step
Functions service retired, #175)."""

class LocalExecutionService:
    """Filesystem-backed execution service for local development.

    Derives all state from the artifact directory layout produced by
    LocalArtifactStore / LocalOrchestrator.
    """
```

`start_execution` writes a small `_meta.json` (job id, database name,
started-at timestamp) for jobs the local API itself starts; `describe_execution`
reconstructs status by looking at which artifact directories exist under the
job. Jobs run directly through the CLI scripts or Claude Code skills have no
`_meta.json` — `_read_source_engine` falls back to reading the collector
output for the source engine in that case (`local_execution.py`). `GET
/api/v1/assessments/{job_id}` polls this derived state — there is no event
bus or push channel.

---

## File Structure (Local)

```
./artifacts/
└── <database-name>/
    └── <job_id>/
        ├── _meta.json      # only for jobs the local API started
        ├── collector/
        │   └── output.json
        ├── referee-triage/
        │   └── triage.json
        ├── analysis-<engine>/
        │   ├── analysis.json
        │   ├── decision-trace.json
        │   └── er-diagram.mmd
        ├── assignment/
        │   └── v<N>/assignment.json
        ├── reality-check/
        │   ├── llm_input.json
        │   └── output.json
        ├── schema-<engine>/
        │   └── v<N>/schema_output.json
        ├── load-test/
        │   └── v<N>/results/summary.json
        └── synthesis/
            └── v<N>/report.json   # or referee-synthesis/report.json when assignment_version is 0
```

Job IDs are UUIDs, truncated to 8 hex characters by the local scripts — not
KSUIDs, despite what older drafts of this guide (and some ADRs) said.

---

## Related Documentation

- [Orchestrator README](../../src/orchestrator/README.md) — the full artifact path table per agent
- [Strands Collector Guide](strands-collector-guide.md) — the collector's own S3-backed checkpoint stages
- [Storage Architecture Diagram](../architecture/diagrams/07-storage-architecture.md)
