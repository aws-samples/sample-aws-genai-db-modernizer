"""The retired settings API is gone (#437).

``/api/v1/settings`` held an in-memory copy of the retired hosted deployment's
configuration (S3 bucket, DynamoDB table, IAM role) that nothing in the local
pipeline read, and the Settings page never called it. Both are removed.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


def test_the_settings_route_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.api.routes.settings")


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/settings"),
        ("put", "/api/v1/settings"),
        ("post", "/api/v1/settings/test-connection"),
    ],
)
def test_the_settings_endpoints_are_gone(method: str, path: str) -> None:
    resp = getattr(client, method)(path, **({"json": {}} if method != "get" else {}))
    assert resp.status_code == 404, resp.text
    assert not any(getattr(r, "path", "").startswith("/api/v1/settings") for r in app.routes)
