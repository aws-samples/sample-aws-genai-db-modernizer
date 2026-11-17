# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2327: ``GET /health`` says whether uploads stay on this machine.

The new-assessment page opens on the upload form when ``local`` is true. The field is
one boolean: the response names no path, host, bucket or credential.
"""

from fastapi.testclient import TestClient

from src.api import main
from src.api.routes import assessments
from src.api.services.local_s3 import LocalS3Service

client = TestClient(main.app)


def test_health_says_local_when_the_local_file_service_is_wired(monkeypatch):
    # Other api tests reset the wired services on teardown; wire it here, whatever ran first.
    monkeypatch.setattr(assessments, "s3_service", main._s3)
    data = client.get("/health").json()
    assert data["local"] is True


def test_health_says_local_with_a_fresh_local_file_service(monkeypatch):
    monkeypatch.setattr(assessments, "s3_service", LocalS3Service(main._artifact_store))
    assert client.get("/health").json()["local"] is True


class _UploadServiceElsewhere:
    """An upload service that is not the local file service (a deployment's)."""

    bucket = "assessments"
    client = None


def test_health_says_not_local_with_another_upload_service(monkeypatch):
    monkeypatch.setattr(assessments, "s3_service", _UploadServiceElsewhere())
    data = client.get("/health").json()
    assert data["local"] is False
    assert "assessments" not in client.get("/health").text  # the bucket is never named


def test_health_says_not_local_with_no_upload_service(monkeypatch):
    monkeypatch.setattr(assessments, "s3_service", None)
    assert client.get("/health").json()["local"] is False


def test_health_carries_only_status_version_and_the_flag(monkeypatch):
    monkeypatch.setattr(assessments, "s3_service", main._s3)
    data = client.get("/health").json()
    assert set(data) == {"status", "version", "local"}
    root = str(main._artifact_store.base_dir)
    assert root not in client.get("/health").text
