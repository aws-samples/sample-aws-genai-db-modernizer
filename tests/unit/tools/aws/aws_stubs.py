"""Botocore-stubbed clients for the AWS tool tests (no network, no credentials)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.stub import Stubber

from src.tools.aws.credentials import AWSCredentialManager

REGION = "us-east-1"


def make_client(service: str):
    """A real botocore client with dummy keys: parameters are validated, nothing is sent."""
    return boto3.client(
        service,
        region_name=REGION,
        aws_access_key_id="testing",  # pragma: allowlist secret
        aws_secret_access_key="testing",  # pragma: allowlist secret  # nosec B106
    )


class StubbedCredMgr(AWSCredentialManager):
    """Stands in for ``AWSCredentialManager``: hands out one stubbed client per service.

    ``asked_for`` records every service a tool requested, so a test can show a tool touches
    only its own AWS API.
    """

    def __init__(self) -> None:
        super().__init__(region=REGION)
        self.clients: dict[str, Any] = {}
        self.stubbers: dict[str, Stubber] = {}
        self.asked_for: list[str] = []

    def stub(self, service: str) -> Stubber:
        if service not in self.clients:
            client = make_client(service)
            self.clients[service] = client
            self.stubbers[service] = Stubber(client)
            self.stubbers[service].activate()
        return self.stubbers[service]

    def client(self, service: str):
        self.asked_for.append(service)
        self.stub(service)
        return self.clients[service]


def capture_params(cred_mgr: StubbedCredMgr, service: str, operation: str) -> list[dict]:
    """Record the parameters of every ``service.operation`` call (before they are sent)."""
    seen: list[dict] = []
    cred_mgr.stub(service)
    cred_mgr.clients[service].meta.events.register(
        f"provide-client-params.{service}.{operation}",
        lambda params, **_kw: seen.append(dict(params)),
    )
    return seen


def hourly_datapoints(days: int, value: float = 10.0) -> list[dict]:
    """One CloudWatch datapoint per hour for ``days`` days."""
    start = datetime.now(UTC) - timedelta(days=days)
    return [
        {
            "Timestamp": start + timedelta(hours=h),
            "Average": value + (h % 5),
            "Maximum": value + 20,
            "Minimum": value - 5,
            "Unit": "Percent",
        }
        for h in range(days * 24)
    ]
