"""Host-header allowlist for the local API."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import host_guard
from src.api.host_guard import HostAllowlistMiddleware, allowed_hosts_from_env, host_name


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    app.add_middleware(
        HostAllowlistMiddleware, allowed_hosts=frozenset(host_guard.DEFAULT_ALLOWED_HOSTS)
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000", "[::1]", "LOCALHOST:3000"],
)
def test_loopback_hosts_are_served(client, host):
    resp = client.get("/ping", headers={"host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "example.com:8000",
        "127.0.0.1.example.com",
        "localhost.example.com:8000",
        "",
        "[::2]:8000",
    ],
)
def test_other_hosts_are_rejected(client, host):
    resp = client.get("/ping", headers={"host": host})
    assert resp.status_code == 400
    assert resp.text == "Invalid host header."


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("LocalHost:8000", "localhost"),
        ("[::1]:80", "::1"),
        ("::1", "::1"),
        ("localhost.", "localhost"),
        ("[bad", ""),
    ],
)
def test_host_name(header, expected):
    assert host_name(header) == expected


def test_allowed_hosts_from_env(monkeypatch):
    monkeypatch.delenv(host_guard.ALLOWED_HOSTS_ENV, raising=False)
    assert allowed_hosts_from_env() == frozenset({"localhost", "127.0.0.1", "::1"})
    monkeypatch.setenv(host_guard.ALLOWED_HOSTS_ENV, " api.internal , [::1] ,")
    assert allowed_hosts_from_env() == frozenset({"api.internal", "::1"})
    monkeypatch.setenv(host_guard.ALLOWED_HOSTS_ENV, "*")
    assert allowed_hosts_from_env() is None


def test_local_api_installs_the_allowlist():
    from src.api import main

    assert any(m.cls is HostAllowlistMiddleware for m in main.app.user_middleware)
    client = TestClient(main.app)
    assert client.get("/health").status_code == 200
    assert client.get("/health", headers={"host": "example.com"}).status_code == 400
    assert client.get("/health", headers={"host": "localhost:8000"}).status_code == 200
