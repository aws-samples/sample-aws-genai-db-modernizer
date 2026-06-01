"""Local S3 artifacts service — replaces S3ArtifactsService for local development."""

from __future__ import annotations

from pathlib import Path


class _MockS3Client:
    """Minimal S3 client mock for presigned URLs, head_object, list_objects, delete_object."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def generate_presigned_url(
        self, operation: str, Params: dict | None = None, **kwargs
    ) -> str:  # noqa: N803
        """Return a file:// URL — not a real upload, but won't crash callers."""
        if Params is None:
            Params = {}
        key = Params.get("Key", "")
        local_path = self._root / key
        return f"file://{local_path}"

    def head_object(self, Bucket: str, Key: str, **kwargs) -> dict:  # noqa: N803
        """Simulate HEAD — raises ClientError-like exception if not found."""
        local_path = self._root / Key
        if local_path.exists():
            return {"ContentLength": local_path.stat().st_size}
        # Raise a botocore-style ClientError without importing botocore
        raise _FakeClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    def list_objects_v2(
        self, Bucket: str, Prefix: str = "", Delimiter: str = "", **kwargs
    ) -> dict:  # noqa: N803
        """Return S3-like listing of files/prefixes under Prefix."""
        prefix_path = self._root / Prefix.rstrip("/")
        if not prefix_path.exists():
            return {"Contents": [], "CommonPrefixes": []}

        contents = []
        common_prefixes = []

        if Delimiter == "/":
            # Return immediate subdirectories as CommonPrefixes
            for child in sorted(prefix_path.iterdir()):
                if child.is_dir():
                    rel = str(child.relative_to(self._root)) + "/"
                    common_prefixes.append({"Prefix": rel})
                else:
                    rel = str(child.relative_to(self._root))
                    contents.append({"Key": rel, "Size": child.stat().st_size})
        else:
            # Return all files recursively
            for child in sorted(prefix_path.rglob("*")):
                if child.is_file():
                    rel = str(child.relative_to(self._root))
                    contents.append({"Key": rel, "Size": child.stat().st_size})

        return {"Contents": contents, "CommonPrefixes": common_prefixes}

    def get_paginator(self, operation: str):
        """Return a simple paginator that wraps list_objects_v2."""
        return _FakePaginator(self)

    def delete_object(self, Bucket: str, Key: str, **kwargs) -> dict:  # noqa: N803
        """Delete the local file corresponding to Key."""
        local_path = self._root / Key
        if local_path.exists():
            local_path.unlink()
        return {}

    def get_object(self, Bucket: str, Key: str, **kwargs):  # noqa: N803
        """Read and return a file body as a dict with a Body-like object."""
        local_path = self._root / Key
        if not local_path.exists():
            raise _FakeClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "No Such Key"}}, "GetObject"
            )
        data = local_path.read_bytes()

        class _Body:
            def read(self_inner) -> bytes:  # noqa: N805
                return data

        return {"Body": _Body()}


class _FakePaginator:
    def __init__(self, client: _MockS3Client) -> None:
        self._client = client

    def paginate(self, Bucket: str, Prefix: str = "", **kwargs):  # noqa: N803
        page = self._client.list_objects_v2(Bucket=Bucket, Prefix=Prefix)
        return [page]


class _FakeClientError(Exception):
    """Minimal stand-in for botocore.exceptions.ClientError."""

    def __init__(self, response: dict, operation_name: str) -> None:
        self.response = response
        self.operation_name = operation_name
        super().__init__(
            f"An error occurred ({response['Error']['Code']}) when calling {operation_name}"
        )


class LocalS3Service:
    """Filesystem-backed artifact service for local development.

    Implements the same interface as S3ArtifactsService.
    """

    def __init__(self, artifact_store, artifact_root: str = ".artifacts") -> None:
        self._store = artifact_store
        self._root: Path = artifact_store.base_dir
        self.bucket = "local"
        self.client = _MockS3Client(self._root)

    # ------------------------------------------------------------------
    # Core read/exists/size helpers
    # ------------------------------------------------------------------

    def read_artifact(
        self, database_name: str, job_id: str, agent_name: str, filename: str
    ) -> dict | None:
        """Read a single agent artifact from the local store."""
        path = f"{database_name}/{job_id}/{agent_name}/{filename}"
        if not self._store.exists(path):
            return None
        try:
            result: dict = self._store.read_json(path)
            return result
        except Exception:
            return None

    def artifact_exists(
        self, database_name: str, job_id: str, agent_name: str, filename: str
    ) -> bool:
        path = f"{database_name}/{job_id}/{agent_name}/{filename}"
        exists: bool = self._store.exists(path)
        return exists

    def artifact_size(
        self, database_name: str, job_id: str, agent_name: str, filename: str
    ) -> int | None:
        path = f"{database_name}/{job_id}/{agent_name}/{filename}"
        full_path = self._root / path
        if full_path.exists():
            return full_path.stat().st_size
        return None

    def list_agent_artifacts(self, database_name: str, job_id: str) -> list[str]:
        """List subdirectory names (agent names) for a job."""
        job_dir = self._root / database_name / job_id
        if not job_dir.exists():
            return []
        return [
            child.name
            for child in sorted(job_dir.iterdir())
            if child.is_dir() and not child.name.startswith("_")
        ]

    # ------------------------------------------------------------------
    # Named convenience readers — mirrors S3ArtifactsService
    # ------------------------------------------------------------------

    def read_collector(self, database_name: str, job_id: str) -> dict | None:
        return self.read_artifact(database_name, job_id, "collector", "output.json")

    def read_triage(self, database_name: str, job_id: str) -> dict | None:
        return self.read_artifact(database_name, job_id, "referee-triage", "triage.json")

    def read_analysis(self, database_name: str, job_id: str, agent_type: str) -> dict | None:
        return self.read_artifact(database_name, job_id, f"analysis-{agent_type}", "analysis.json")

    def read_synthesis(self, database_name: str, job_id: str) -> dict | None:
        return self.read_artifact(database_name, job_id, "referee-synthesis", "report.json")

    def read_reality_check(self, database_name: str, job_id: str) -> dict | None:
        data = self.read_artifact(database_name, job_id, "reality-check", "output.json")
        if data is not None:
            return data

        # Fallback: synthesize distribution from latest assignment artifact
        # so the UI can render the assignment gate before reality-check runs.
        return self._synthesize_reality_check_from_assignments(database_name, job_id)

    def _synthesize_reality_check_from_assignments(
        self, database_name: str, job_id: str
    ) -> dict | None:
        """Build a minimal reality-check response from assignment data."""
        # Find latest assignment version
        prefix = f"{database_name}/{job_id}/assignment/"
        keys = self._store.list_prefix(prefix)
        versions: list[int] = []
        for key in keys:
            parts = key.replace(prefix, "").split("/")
            if parts and parts[0].startswith("v"):
                try:
                    versions.append(int(parts[0][1:]))
                except ValueError:
                    continue
        if not versions:
            return None

        latest = max(versions)
        path = f"{database_name}/{job_id}/assignment/v{latest}/assignment.json"
        try:
            assignment = self._store.read_json(path)
        except Exception:
            return None

        # Compute distribution
        dist: dict[str, int] = {}
        for qa in assignment.get("query_assignments", []):
            if qa.get("in_scope", True):
                engine = qa.get("assigned_engine", "unknown")
                dist[engine] = dist.get(engine, 0) + 1

        return {
            "consolidations": [],
            "before_distribution": dist,
            "after_distribution": dist,
            "architectural_patterns": [],
            "recommendations": [],
        }

    def read_schema_design(self, database_name: str, job_id: str, target_type: str) -> dict | None:
        # Try versioned path first, then legacy flat path
        result = self.read_artifact(
            database_name, job_id, f"schema-{target_type}", "v1/schema_output.json"
        )
        if result is not None:
            return result
        return self.read_artifact(
            database_name, job_id, f"schema-{target_type}", "schema_output.json"
        )

    def read_all_schema_designs(self, database_name: str, job_id: str) -> list[dict]:
        """Read all schema design artifacts for a job.

        Handles both path formats:
        - Versioned: schema-{type}/v{N}/schema_output.json
        - Legacy:    schema-{type}/schema_output.json

        For versioned paths, returns only the latest version per engine.
        """
        prefix = f"{database_name}/{job_id}/"
        all_paths = self._store.list_prefix(prefix.rstrip("/"))

        latest: dict[str, tuple[int, str]] = {}

        for full_path in all_paths:
            # full_path is relative to base_dir e.g. "mydb/job123/schema-ddb/v1/schema_output.json"
            relative = full_path.removeprefix(prefix)
            if not relative.startswith("schema-"):
                continue
            parts = relative.split("/")
            if len(parts) < 2:
                continue

            engine = parts[0].removeprefix("schema-")

            # Versioned: schema-{type}/v{N}/schema_output.json
            if len(parts) == 3 and parts[1].startswith("v") and parts[2] == "schema_output.json":
                try:
                    version = int(parts[1][1:])
                except ValueError:
                    continue
                current = latest.get(engine, (-1, ""))
                if version > current[0]:
                    latest[engine] = (version, full_path)

            # Legacy: schema-{type}/schema_output.json
            elif len(parts) == 2 and parts[1] == "schema_output.json":
                if engine not in latest:
                    latest[engine] = (0, full_path)

        designs = []
        for engine, (_, rel_path) in sorted(latest.items()):
            try:
                data = self._store.read_json(rel_path)
                designs.append(
                    {
                        "target_type": engine,
                        "artifact_path": rel_path,
                        "content": data,
                    }
                )
            except Exception as exc:
                print(f"[schema-designs] Skipping {rel_path}: {exc}")  # nosec B112

        return designs
