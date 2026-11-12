# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/llm/private_scan.py: what must not reach a public artifact or fixture (#1463)."""

from __future__ import annotations

import gzip
import re
import shutil
import subprocess  # nosec B404 -- runs detect-secrets' own hook with fixed argv
from pathlib import Path

import pytest

from ci.llm import private_scan, replay

REPO = Path(__file__).resolve().parents[3]

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


def _committed_fixture_findings(root: Path) -> list[str]:
    """What the scans find in the saved runs under ``root``: every job file (plain or
    compressed) with both scans, and every other compressed file as the pre-commit hook
    scans it (``private_scan.compressed_file_findings``)."""
    found: list[str] = []
    for path in sorted(p for p in root.glob("*/**/*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel.split("/")[1] == replay.JOB_DIR:
            found += [
                f"{rel}: {hit}" for hit in replay.job_private_data(private_scan.read_text(path))
            ]
        elif path.name.endswith(".gz"):
            found += [f"{rel}: {hit}" for hit in private_scan.compressed_file_findings(path)]
    return found


def test_every_committed_file_of_a_saved_job_passes_the_scan() -> None:
    """The pre-commit hook skips real_runs/*/job/ by path (hex keys and hashes), and
    detect-secrets cannot read a compressed file (#1847), so this is the check that keeps
    a hand-edited or stale fixture file from carrying a credential, host, address or
    person into the repo."""
    root = replay.DEFAULT_FIXTURE.parent
    assert list(root.glob("*/job/**/*.json"))
    assert _committed_fixture_findings(root) == []


# A credential planted in a fixture file that is then gzip-compressed (#1847).
_PLANTED = (
    '{"dsn": "postgres://admin:' + "hunter2pw" + '@db.example.org/wp"}\n'
)  # pragma: allowlist secret


def _planted_fixture(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "real_runs" / "discourse-both" / (name + ".gz")
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(('{"ok": 1}\n' + _PLANTED).encode(), mtime=0))
    return path


@pytest.mark.passes_on_main  # the premise: detect-secrets' own behaviour, not this change
def test_detect_secrets_own_hook_reads_nothing_in_a_compressed_file(tmp_path: Path) -> None:
    """Why the compressed files need their own scan: the hook skips the binary file."""
    path = _planted_fixture(tmp_path, "report.json")
    plain = tmp_path / "report.json"
    plain.write_bytes(gzip.decompress(path.read_bytes()))
    hook = shutil.which("detect-secrets-hook")
    assert hook
    assert subprocess.run([hook, str(plain)], capture_output=True).returncode == 1  # nosec B603
    assert subprocess.run([hook, str(path)], capture_output=True).returncode == 0  # nosec B603


@pytest.mark.parametrize("name", ["job/collector/output.json", "report.json", "transcript.jsonl"])
def test_a_planted_secret_inside_a_compressed_fixture_file_is_caught(
    tmp_path: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _planted_fixture(tmp_path, name)
    # The pre-commit hook and ci/lint.sh: the file, or the folder it is in.
    for arg in (path, tmp_path / "real_runs"):
        assert private_scan.main([str(arg)]) == 1
        out = capsys.readouterr().out
        assert "Basic Auth Credentials at line 2" in out and "hunter2pw" not in out
    # The unit test over the committed fixtures.
    found = _committed_fixture_findings(tmp_path / "real_runs")
    assert any("Basic Auth Credentials at line 2" in f for f in found), found


def test_a_clean_compressed_file_passes_and_an_unreadable_one_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "report.json.gz"
    clean.write_bytes(gzip.compress(b'{"database_name": "discourse"}\n', mtime=0))
    assert private_scan.main([str(clean)]) == 0
    broken = tmp_path / "broken.json.gz"
    broken.write_bytes(b"not gzip at all")
    assert private_scan.main([str(broken)]) == 2
    assert "cannot be read" in capsys.readouterr().out
    # A plain file is not the compressed scan's: the detect-secrets hook reads it.
    plain = tmp_path / "plain.json"
    plain.write_text(_PLANTED)
    assert private_scan.main([str(plain)]) == 0


def test_a_compressed_file_too_large_once_decompressed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_scan, "MAX_DECOMPRESSED_BYTES", 100)
    path = tmp_path / "bomb.json.gz"
    path.write_bytes(gzip.compress(b"0" * 1000, mtime=0))
    with pytest.raises(ValueError, match="over 100 bytes decompressed"):
        private_scan.read_text(path)


def test_the_hook_and_lint_scan_every_compressed_fixture_file() -> None:
    config = (REPO / ".pre-commit-config.yaml").read_text()
    hook = config[config.index("id: scan-compressed-fixtures") :]
    hook = hook[: hook.index("\n\n")]
    assert "entry: uv run python ci/llm/private_scan.py" in hook
    files = re.search(r"files: (\S+)", hook).group(1)
    assert re.search(files, "tests/fixtures/real_runs/discourse-both/job/collector/output.json.gz")
    assert re.search(files, "tests/fixtures/real_runs/discourse-both/transcript.jsonl.gz")
    assert "pass_filenames: true" in hook
    lint = (REPO / "ci" / "lint.sh").read_text()
    assert "uv run python ci/llm/private_scan.py tests/fixtures/real_runs\n" in lint


@pytest.mark.parametrize(
    "text",
    [
        '{"group_key": "123456789012"}',
        '\\"group_key\\": \\"123456789012\\"',  # JSON-escaped, as in a transcript
    ],
    ids=["json", "escaped"],
)
def test_an_all_digit_group_key_is_a_hash_not_an_account(text: str) -> None:
    """move_ledger.group_key is 12 hex characters of a sha256; one in about 450 is all
    digits, and a discourse job has hundreds (#1847)."""
    assert private_scan.identity_findings(text) == []


@pytest.mark.parametrize(
    "text",
    [
        '{"account_id": "123456789012"}',
        '{"group_key": "a1b2c3d4e5f6", "owner": "123456789012"}',  # pragma: allowlist secret gitleaks:allow (fake group key fixture)
        '{"group_key": 123456789012}',  # not the quoted 12-hex string a group key is
        '{"group_key_owner": "123456789012"}',
    ],
    ids=["account-id", "next-to-a-group-key", "unquoted", "other-key"],
)
@pytest.mark.passes_on_main  # the group-key exception must not hide a real account id
def test_an_account_id_outside_a_group_key_is_still_found(text: str) -> None:
    assert private_scan.identity_findings(text) == ["aws account id at line 1"]


def test_every_kind_a_scan_reports_is_printable() -> None:
    """The command line prints a finding only as a ``PRINTABLE_KINDS`` constant: every
    kind the scans can report is listed, so a real finding is never printed as
    unlisted."""
    from src.shared import secret_scan

    own = {kind for kind, _ in (*secret_scan._OWN_SHAPE_RES, *secret_scan._EXTRA_SECRET_RES)}
    plugins = {p["name"] for p in secret_scan._PLUGINS}
    util = pytest.importorskip("detect_secrets.core.plugins.util")
    plugin_kinds = {
        kind for kind, cls in util.get_mapping_from_secret_type_to_class().items()
        if cls.__name__ in plugins
    }  # fmt: skip
    identity = {kind for kind, _ in private_scan._IDENTITY_RES} | {"aws account id"}
    reported = own | plugin_kinds | identity | secret_scan.KEY_NAME_KINDS | {"bare secret key"}
    assert (
        reported | secret_scan.IP_KINDS | {secret_scan.EMAIL_KIND} <= private_scan.PRINTABLE_KINDS
    )


def test_the_command_line_never_prints_text_a_finding_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding is printed as its listed kind and line number, rebuilt from constants:
    even a finding string that carried the matched value prints without it (#2180)."""
    leaked = "q9Zk3vLmP0aB7cXy" + "W2rT8nE5"  # pragma: allowlist secret
    path = tmp_path / "report.json.gz"
    path.write_bytes(gzip.compress(b"{}\n", mtime=0))
    monkeypatch.setattr(
        private_scan,
        "compressed_file_findings",
        lambda _path: [
            "token value at line 3",
            f"token value at line 4 {leaked}",
            f"{leaked} at line 5",
        ],
    )
    assert private_scan.main([str(path)]) == 1
    out, err = capsys.readouterr()
    assert leaked not in out + err
    assert out.splitlines() == [
        f"{path}: token value at line 3",
        f"{path}: unlisted finding kind at line 0",
        f"{path}: unlisted finding kind at line 5",
    ]
