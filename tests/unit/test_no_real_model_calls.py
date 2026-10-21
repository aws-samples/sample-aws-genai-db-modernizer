"""The test session refuses every request to a real Bedrock endpoint (tests/conftest.py).

AGENTS.md: tests never call a real model. The guard sits at botocore's endpoint, so a
request is refused whatever built the client (boto3, a Strands BedrockModel), before
it is signed or sent, while a stubbed client still answers.
"""

from __future__ import annotations

from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber

from tests.conftest import RealModelCallBlocked

# These test tests/conftest.py itself, not src/: run against main's src/ with this
# branch's tests/ they pass, which is the point (the guard is in the test tree).


def _client(service: str = "bedrock-runtime"):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",  # pragma: allowlist secret
        aws_secret_access_key="testing",  # pragma: allowlist secret  # nosec B106
    )


@pytest.mark.passes_on_main
def test_a_real_bedrock_request_is_refused() -> None:
    with pytest.raises(RealModelCallBlocked):
        _client().converse(modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}])


@pytest.mark.passes_on_main
def test_a_stubbed_bedrock_client_still_answers() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("list_async_invokes", {"asyncInvokeSummaries": []})
        assert client.list_async_invokes()["asyncInvokeSummaries"] == []


@pytest.mark.passes_on_main
def test_other_services_are_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.conftest as conftest

    sent: list[tuple] = []

    def real(*args):
        sent.append(args)
        return "ok"

    monkeypatch.setattr(conftest, "_real_make_request", real)
    endpoint = SimpleNamespace(host="https://sts.us-east-1.amazonaws.com")
    operation = SimpleNamespace(name="GetCallerIdentity")
    assert conftest._refuse_bedrock_requests(endpoint, operation, {}) == "ok"
    assert len(sent) == 1
