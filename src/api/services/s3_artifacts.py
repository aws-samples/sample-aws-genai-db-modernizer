"""S3 artifacts service — reads agent contract outputs from S3.

Path convention per ADR-016:
  s3://<bucket>/<database-name>/<job-id>/<agent-name>/artifact.json
"""

import json

import boto3
from botocore.exceptions import ClientError


class S3ArtifactsService:
    def __init__(self, bucket: str):
        self.bucket = bucket
        self.client = boto3.client("s3")

    def read_artifact(
        self, database_name: str, job_id: str, agent_name: str, filename: str
    ) -> dict | None:
        """Read a single agent artifact from S3."""
        key = f"{database_name}/{job_id}/{agent_name}/{filename}"
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            result: dict = json.loads(response["Body"].read())
            return result
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def artifact_exists(
        self, database_name: str, job_id: str, agent_name: str, filename: str
    ) -> bool:
        """Check if an artifact exists without reading it."""
        key = f"{database_name}/{job_id}/{agent_name}/{filename}"
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def artifact_size(
        self, database_name: str, job_id: str, agent_name: str, filename: str
    ) -> int | None:
        """Get artifact size in bytes."""
        key = f"{database_name}/{job_id}/{agent_name}/{filename}"
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
            size: int = response["ContentLength"]
            return size
        except ClientError:
            return None

    def list_agent_artifacts(self, database_name: str, job_id: str) -> list[str]:
        """List all agent directories for a job."""
        prefix = f"{database_name}/{job_id}/"
        response = self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=prefix,
            Delimiter="/",
        )
        return [p["Prefix"].rstrip("/").split("/")[-1] for p in response.get("CommonPrefixes", [])]

    def read_collector(self, database_name: str, job_id: str) -> dict | None:
        return self.read_artifact(database_name, job_id, "collector", "output.json")

    def read_triage(self, database_name: str, job_id: str) -> dict | None:
        return self.read_artifact(database_name, job_id, "referee-triage", "triage.json")

    def read_analysis(self, database_name: str, job_id: str, agent_type: str) -> dict | None:
        return self.read_artifact(database_name, job_id, f"analysis-{agent_type}", "analysis.json")

    def read_synthesis(self, database_name: str, job_id: str) -> dict | None:
        return self.read_artifact(database_name, job_id, "referee-synthesis", "report.json")

    def read_reality_check(self, database_name: str, job_id: str) -> dict | None:
        return self.read_artifact(database_name, job_id, "reality-check", "output.json")

    def read_schema_design(self, database_name: str, job_id: str, target_type: str) -> dict | None:
        return self.read_artifact(
            database_name, job_id, f"schema-{target_type}", "schema_output.json"
        )

    def read_all_schema_designs(self, database_name: str, job_id: str) -> list[dict]:
        """Read all schema design artifacts for a job.

        Handles both path formats written by the schema design agent:
        - Versioned: schema-{type}/v{N}/schema_output.json  (assignment_version > 0)
        - Legacy:    schema-{type}/schema_output.json        (assignment_version == 0)

        For versioned paths, returns only the latest version per engine.
        """
        prefix = f"{database_name}/{job_id}/"
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            all_keys: list[str] = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    all_keys.append(obj["Key"])
        except Exception:
            return []

        # Collect schema design keys: schema-{type}/... paths only
        # Track latest version per engine: engine → (version, key)
        # version=0 means legacy (no version prefix)
        latest: dict[str, tuple[int, str]] = {}

        for key in all_keys:
            relative = key.removeprefix(prefix)
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
                    latest[engine] = (version, key)

            # Legacy: schema-{type}/schema_output.json
            elif len(parts) == 2 and parts[1] == "schema_output.json":
                if engine not in latest:
                    latest[engine] = (0, key)

        designs = []
        for engine, (_, key) in sorted(latest.items()):
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                data = json.loads(response["Body"].read())
                designs.append({"target_type": engine, "artifact_path": key, "content": data})
            except ClientError as e:
                # Skip artifacts that can't be read (e.g. deleted mid-request)
                print(f"[schema-designs] Skipping {key}: {e}")  # nosec B112

        return designs
