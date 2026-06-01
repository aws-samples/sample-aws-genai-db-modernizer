"""S3-backed artifact store for cloud deployment."""

from __future__ import annotations

import json

import boto3
import botocore.exceptions

from src.storage.artifact_store import ArtifactStore


class S3ArtifactStore(ArtifactStore):
    """S3-backed artifact store for cloud deployment."""

    def __init__(self, bucket: str, s3_client=None):
        self.bucket = bucket
        self.s3 = s3_client or boto3.client("s3")

    def read_json(self, path: str) -> dict:
        response = self.s3.get_object(Bucket=self.bucket, Key=path)
        return json.loads(response["Body"].read())  # type: ignore[no-any-return]

    def write_json(self, path: str, data: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=path,
            Body=json.dumps(data, indent=2, default=str),
            ContentType="application/json",
        )

    def exists(self, path: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=path)
            return True
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise  # re-raise non-404 errors

    def list_prefix(self, prefix: str) -> list[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys
