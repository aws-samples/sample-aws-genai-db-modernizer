# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991 review: the write routes that take no body need ``Content-Type:
application/json``. Without it, a page on another site could reach them with a simple
form post (no CORS preflight): a ``text/plain`` or form body, or none, gets 415 and the
handler never runs. The UI's own calls send JSON (ApiManager) and still work."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assessments, schema_revisions

client = TestClient(app)

ROUTES = [
    "/api/v1/assessments/job-1/schema/confirm-all?database_name=mydb",
    "/api/v1/assessments/job-1/schema/dynamodb/confirm?database_name=mydb",
    "/api/v1/assessments/job-1/uploads/confirm?database_name=mydb",
]
CROSS_SITE = {"origin": "https://evil.example"}


@pytest.fixture
def services(monkeypatch):
    store, orch, sfn, s3 = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    store.list_prefix.return_value = []
    store.exists.return_value = False
    sfn.is_cancelled.return_value = False
    s3.client.head_object.return_value = {"ContentLength": 1}
    monkeypatch.setattr(schema_revisions, "artifact_store", store)
    monkeypatch.setattr(schema_revisions, "orchestrator", orch)
    monkeypatch.setattr(schema_revisions, "sfn_service", sfn)
    monkeypatch.setattr(assessments, "sfn_service", sfn)
    monkeypatch.setattr(assessments, "s3_service", s3)
    return store, orch, s3


def _untouched(store, orch, s3) -> bool:
    return (
        not store.method_calls
        and not orch.confirm_schema_design.called
        and not s3.client.head_object.called
    )


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-type": "text/plain", **CROSS_SITE}, b"{}"),
        ({"content-type": "application/x-www-form-urlencoded", **CROSS_SITE}, b"a=1"),
        ({"content-type": "multipart/form-data; boundary=x", **CROSS_SITE}, b"--x--"),
        (CROSS_SITE, b""),
        ({}, b""),
    ],
)
def test_a_simple_cross_site_post_is_refused_before_the_handler(
    services, path, headers, body
) -> None:
    resp = client.post(path, content=body, headers=headers)
    assert resp.status_code == 415, resp.text
    assert _untouched(*services)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("path", ROUTES)
def test_the_uis_own_json_call_still_reaches_the_handler(services, path) -> None:
    """ApiManager sends ``Content-Type: application/json`` and ``{}``."""
    resp = client.post(path, json={})
    assert resp.status_code != 415
    store, orch, s3 = services
    assert store.method_calls or s3.client.head_object.called  # the handler ran
