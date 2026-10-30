# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/llm/private_scan.py: what must not reach a public artifact or fixture (#1463)."""

from __future__ import annotations

import pytest

from ci.llm import private_scan, replay

# Built at run time so no scanner reads a credential-shaped literal in this file.
_SLACK = "xoxb-" + "123456789012-1234567890123-" + "abcdefghijklmnopqrstuvwx"
_GOOGLE = "AIza" + "A" * 35
_AZURE = "AccountKey=" + "A" * 86 + "=="
_JWT = (
    "eyJ"
    + "hbGciOiJIUzI1NiJ9"
    + ".eyJ"
    + "zdWIiOiIxMjM0NTY3ODkwIn0"
    + "."
    + "abcdefghijklmnopqrstuv"
)
_BARE = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5nP7r"  # pragma: allowlist secret
_SESSION = (
    '"aws_session_token": "' + "FwoGZXIvYXdzEBYaDabcdefghijklmnop" + '"'  # pragma: allowlist secret
)  # pragma: allowlist secret


@pytest.mark.parametrize(
    "text",
    [
        "dsn = postgres://user:pw12345@host/db",  # pragma: allowlist secret
        "mysql://root:pw12345@10.0.3.4/wp",  # pragma: allowlist secret
        '"password": "hunter2xyz"',  # pragma: allowlist secret
        "DB_PASSWORD=hunter2xyz",  # pragma: allowlist secret
        "api_key = '" + "abcd1234" + "efgh5678'",  # pragma: allowlist secret
        _SLACK,  # pragma: allowlist secret
        f"key {_GOOGLE}",  # pragma: allowlist secret
        f"DefaultEndpointsProtocol=https;AccountName=a;{_AZURE};",  # pragma: allowlist secret
        _JWT,  # pragma: allowlist secret
        f"secret {_BARE}",  # pragma: allowlist secret
        _SESSION,  # pragma: allowlist secret
        "Authorization: Bearer " + "a" * 30,  # pragma: allowlist secret
        "AKIA" + "QWERTYUIOPASDFGH",  # pragma: allowlist secret
        "-----BEGIN RSA PRIVATE" + " KEY-----",  # pragma: allowlist secret
    ],
    # Named ids: test ids are written to .test_durations and CI logs, and the raw
    # values would reassemble the credential shapes this file splits (#1718).
    ids=[
        "postgres-dsn",
        "mysql-url",
        "json-password",
        "env-password",
        "api-key",
        "slack-token",
        "google-key",
        "azure-connection-string",
        "jwt",
        "bare-secret",
        "aws-session-token",
        "bearer-token",
        "aws-access-key",
        "private-key-header",
    ],
)
def test_secret_findings_catch_each_credential_shape(text: str) -> None:
    findings = private_scan.secret_findings(f"line one\n{text}\n")
    assert findings, text
    assert all(f.endswith("at line 2") for f in findings)


def test_a_secret_finding_never_repeats_the_value() -> None:
    findings = private_scan.secret_findings("DB_PASSWORD=hunter2xyz")
    assert "hunter2xyz" not in " ".join(findings)


@pytest.mark.parametrize(
    "text",
    [
        '{"query_id": "' + "a1b2c3d4" * 8 + '", "group_key": "a9549108e34f"}',
        '{"text_sha256": "5b5f6e02116cd224"}',  # pragma: allowlist secret
        '{"table": "wp_users", "column": "user_pass", "type": "varchar(255)"}',
        "A plain sentence about the password column of the users table.",
    ],
)
def test_ids_hashes_and_names_are_not_secrets(text: str) -> None:
    assert private_scan.secret_findings(text) == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("x.cluster-abc.us-east-1.rds.amazonaws.com", "aws hostname"),
        ("db.abc.eu-west-1.rds.amazonaws.com.cn", "aws hostname"),
        ("ip-10-0-1-5.ec2.internal", "internal hostname"),
        ("host 10.12.34.56 up", "private ip"),
        ("host 192.168.1.20 up", "private ip"),
        ("host 172.20.0.9 up", "private ip"),
        ("dba@customer-corp.com", "email"),
        ("C:\\Users\\jdoe\\work", "windows path"),
        ("C:\\\\Users\\\\jdoe\\\\work", "windows path"),
        ("\\\\fileserver01\\share", "unc path"),
        ("bucket-123456789012-logs", "aws account id"),
        ("Admin-123456789012", "aws account id"),
        ("123456789012", "aws account id"),
    ],
)
def test_identity_findings_catch_each_shape(text: str, kind: str) -> None:
    assert any(f.startswith(kind) for f in private_scan.identity_findings(text)), text


@pytest.mark.parametrize(
    "text",
    [
        "0.208333333334 of the calls",
        "172.32.0.1 and 11.0.0.1 are public ranges",
        "mail admin@example.com",
        "line one\\n\\nDecision text",
        "query " + "ab12" * 4 + "123456789012" + "ab12" * 4,
        "see calculator.aws for prices",
        "version 8.0.35",
    ],
)
def test_identity_findings_leave_ordinary_text_alone(text: str) -> None:
    assert private_scan.identity_findings(text) == []


@pytest.mark.parametrize(
    "text",
    ["dba@customer-corp.com", "bucket-123456789012-logs", _SESSION],
    ids=["email", "account-id-in-name", "aws-session-token"],
)
def test_save_scans_job_files_with_both_scans(text: str) -> None:
    assert replay.job_private_data(text)


def test_every_committed_file_of_a_saved_job_passes_the_scan() -> None:
    """The pre-commit hook skips real_runs/*/job/ by path (hex keys and hashes), so this
    is the check that keeps a hand-edited or stale job file from carrying a credential,
    host, address or person into the repo."""
    files = sorted(replay.DEFAULT_FIXTURE.parent.glob("*/job/**/*"))
    files = [f for f in files if f.is_file()]
    assert files
    for path in files:
        text = path.read_text()
        name = path.relative_to(replay.DEFAULT_FIXTURE.parent).as_posix()
        assert replay.job_private_data(text) == [], name
