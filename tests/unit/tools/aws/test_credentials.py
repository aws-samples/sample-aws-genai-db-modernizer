"""ADR-004 Decision 2: one credential manager for same-account and cross-account (#702).

Same-account uses the task role as is (no STS call); cross-account calls AssumeRole with the
customer's ExternalId and hands back a session built from the temporary credentials.
"""

from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from botocore.stub import Stubber

from src.tools.aws.credentials import AWSCredentialManager
from tests.unit.tools.aws.aws_stubs import REGION, make_client

ROLE_ARN = "arn:aws:iam::987654321098:role/ModernizerAccessRole"
EXTERNAL_ID = "unique-customer-id-12345"
TEMPORARY = {
    "AccessKeyId": "ASIATEMPORARYKEY",  # pragma: allowlist secret
    "SecretAccessKey": "temporary-secret",  # pragma: allowlist secret  # nosec B105
    "SessionToken": "temporary-token",
    "Expiration": "2030-01-01T00:00:00Z",
}


def _sts_returning_credentials(**expected: object) -> tuple[object, Stubber]:
    sts = make_client("sts")
    stubber = Stubber(sts)
    stubber.add_response("assume_role", {"Credentials": TEMPORARY}, expected)
    stubber.activate()
    return sts, stubber


class TestSameAccount:
    def test_uses_the_task_role_without_calling_sts(self) -> None:
        mgr = AWSCredentialManager(region=REGION)

        with patch.object(boto3, "client", side_effect=AssertionError("STS must not be called")):
            session = mgr.get_session()

        assert mgr.mode == "same-account"
        assert session.region_name == REGION

    def test_an_external_id_without_a_role_does_not_make_it_cross_account(self) -> None:
        assert AWSCredentialManager(REGION, external_id=EXTERNAL_ID).mode == "same-account"


class TestCrossAccount:
    def test_assumes_the_role_with_the_external_id(self) -> None:
        sts, stubber = _sts_returning_credentials(
            RoleArn=ROLE_ARN,
            RoleSessionName="ModernizerCollector",
            DurationSeconds=3600,
            ExternalId=EXTERNAL_ID,
        )
        mgr = AWSCredentialManager(REGION, role_arn=ROLE_ARN, external_id=EXTERNAL_ID)

        with patch.object(boto3, "client", return_value=sts):
            session = mgr.get_session()

        stubber.assert_no_pending_responses()
        assert mgr.mode == "cross-account"
        creds = session.get_credentials()
        assert creds is not None
        assert creds.access_key == TEMPORARY["AccessKeyId"]
        assert creds.token == TEMPORARY["SessionToken"]
        assert session.region_name == REGION

    def test_the_assumed_session_is_reused(self) -> None:
        sts, stubber = _sts_returning_credentials(
            RoleArn=ROLE_ARN, RoleSessionName="ModernizerCollector", DurationSeconds=3600
        )
        mgr = AWSCredentialManager(REGION, role_arn=ROLE_ARN)

        with patch.object(boto3, "client", return_value=sts):
            first = mgr.get_session()
            second = mgr.get_session()  # a second AssumeRole would find no stubbed response

        assert first is second
        stubber.assert_no_pending_responses()

    def test_a_failed_assume_role_is_not_hidden(self) -> None:
        sts = make_client("sts")
        stubber = Stubber(sts)
        stubber.add_client_error("assume_role", service_error_code="AccessDenied")
        stubber.activate()
        mgr = AWSCredentialManager(REGION, role_arn=ROLE_ARN, external_id=EXTERNAL_ID)

        with patch.object(boto3, "client", return_value=sts), pytest.raises(Exception) as err:
            mgr.get_session()

        assert "AccessDenied" in str(err.value)
