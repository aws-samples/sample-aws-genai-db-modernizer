# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What must never reach a public artifact or fixture (#1463).

Two scans over text, used by ``ci/llm/run.py collect-job`` (the whole-job upload) and
``ci/llm/replay.py save`` (the public replay fixture):

- ``secret_findings``: credentials. Runs the detect-secrets plugins the repo's
  pre-commit hook uses, minus the two entropy plugins (a job is full of hex ids and
  hashes that are not secrets; the hook filters those by file), line by line, plus a
  few shapes detect-secrets has no plugin for (a Google API key, a Slack webhook, an
  AWS session token, a bare AWS secret key). A finding names the kind and the line,
  never the value (``identity_findings`` and ``replay.private_data`` too).
- ``identity_findings``: what names a machine, a network or a person: AWS and
  internal hostnames, private IPs, emails, Windows and UNC paths, a 12-digit AWS
  account id even inside a name (``bucket-123456789012-logs``), but not a ``group_key``
  value, which is a 12-hex hash that can be all digits.

A replay fixture file over 900 KB is committed gzip-compressed (``<name>.gz``, #1847),
which detect-secrets' own hook cannot read: it skips a binary file. ``read_text``
decompresses one, and the command line runs both scans over the decompressed text of
every ``.gz`` it is given (the ``scan-compressed-fixtures`` pre-commit hook and
``ci/lint.sh`` run it)::

    uv run python ci/llm/private_scan.py FILE_OR_DIR ...   # a folder: every .gz under it

It prints ``<path>: <finding>`` per finding and exits 1 when there is one, 2 when a
file cannot be read. A transcript (``transcript.jsonl.gz``) gets the credential-shape
scan only, as ``ci/llm/replay.py save`` gives it (``secret_findings(key_names=False)``).
"""

from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

# The plugins that find a credential by its shape or its key name. The entropy
# plugins (Base64HighEntropyString, HexHighEntropyString) are left out on purpose.
_PLUGINS = [
    {"name": name}
    for name in (
        "ArtifactoryDetector",
        "AWSKeyDetector",
        "AzureStorageKeyDetector",
        "BasicAuthDetector",
        "CloudantDetector",
        "DiscordBotTokenDetector",
        "GitHubTokenDetector",
        "GitLabTokenDetector",
        "IbmCloudIamDetector",
        "IbmCosHmacDetector",
        "IPPublicDetector",
        "JwtTokenDetector",
        "KeywordDetector",
        "MailchimpDetector",
        "NpmDetector",
        "OpenAIDetector",
        "PrivateKeyDetector",
        "PypiTokenDetector",
        "SendGridDetector",
        "SlackDetector",
        "SoftlayerDetector",
        "SquareOAuthDetector",
        "StripeDetector",
        "TelegramBotTokenDetector",
        "TwilioKeyDetector",
    )
]

_EXTRA_SECRET_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack webhook", re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/\w+")),
    (
        "password assignment",
        re.compile(r"\b\w*(?:password|passwd|pwd|secret)\w*\s*=\s*[\"']?[^\s\"',;]{4,}", re.I),
    ),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    (
        "token value",
        re.compile(
            r"(?:session|access|auth|refresh|api|secret)[_-]?token[\"']?\s*[:=]\s*[\"']?"
            r"[A-Za-z0-9/+=_.-]{16,}",
            re.IGNORECASE,
        ),
    ),
    (
        "aws secret access key",
        re.compile(r"secret[_-]?access[_-]?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{20,}", re.I),
    ),
)
# A bare AWS secret key: 40 base64 characters with upper, lower and a digit (a hex
# digest has no upper-case mix; an identifier has underscores).
_BARE_KEY_RE = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")


def _bare_secret_key(token: str) -> bool:
    return (
        any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
        and not re.fullmatch(r"[0-9a-fA-F]+", token)
    )


# Findings that go by a key's name, not a credential's shape: prose and tool output
# (a transcript) trip them, so a caller can leave them out.
KEY_NAME_KINDS = frozenset({"Secret Keyword", "password assignment", "token value"})


def secret_findings(text: str, key_names: bool = True) -> list[str]:
    """``"<kind> at line N"`` for every credential in ``text``; no value is repeated.
    ``key_names=False`` leaves out the findings that go by a key's name (``KEY_NAME_KINDS``)."""
    found: list[str] = []
    with transient_settings({"plugins_used": _PLUGINS, "filters_used": []}):
        for number, line in enumerate(text.splitlines(), start=1):
            if len(line) < 8:
                continue
            kinds = {secret.type for secret in scan_line(line)}
            kinds |= {kind for kind, rx in _EXTRA_SECRET_RES if rx.search(line)}
            if any(_bare_secret_key(m.group(0)) for m in _BARE_KEY_RE.finditer(line)):
                kinds.add("bare secret key")
            if not key_names:
                kinds -= KEY_NAME_KINDS
            found += [f"{kind} at line {number}" for kind in sorted(kinds)]
    return found


# A hex id of 16 or more characters (a query id, a hash) can hold a run of 12 digits.
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
# So can a uuid's last group.
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_PRIVATE_IP_RE = re.compile(
    r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3})(?![\d])"
)
_IDENTITY_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "aws hostname",
        re.compile(
            r"[\w.-]+\.(?:amazonaws\.com(?:\.cn)?|ec2\.internal|compute\.internal"
            r"|a2z\.com|amazon\.dev|amazon\.com)\b",
            re.IGNORECASE,
        ),
    ),
    ("internal hostname", re.compile(r"\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}\b")),
    ("private ip", _PRIVATE_IP_RE),
    (
        "email",
        re.compile(
            r"[\w.+-]+@(?!(?:[\w-]+\.)*(?:example\.(?:com|org|net)|test|invalid|localhost)\b)"
            r"[\w-]+(?:\.[\w-]+)+"
        ),
    ),
    # C:\Users\x or C:\\Users\\x (JSON-escaped); the drive letter stands alone.
    ("windows path", re.compile(r"(?<!\w)[A-Za-z]:\\{1,4}[\w$. -]+\\{1,4}[\w$.-]")),
    ("unc path", re.compile(r"(?<![\w\\])\\{2,4}[\w.-]{2,}\\{1,2}[\w$.-]+")),
)
_ACCOUNT_ID_RE = re.compile(r"(?<![\d.])\d{12}(?!\d|\.\d)")
# A group key is the first 12 hex characters of a sha256 (move_ledger.group_key), all
# decimal digits about one time in 450: a discourse job has hundreds (#1847). Its
# value, JSON-escaped in a transcript too, is a hash, never an account id.
_GROUP_KEY_RE = re.compile(r'"group_key\\*"\s*:\s*\\*"[0-9a-f]{12}\\*"')


def line_of(text: str, match: re.Match[str]) -> int:
    """The 1-based line of ``match`` in the text it was found in."""
    return text.count("\n", 0, match.start()) + 1


def identity_findings(text: str) -> list[str]:
    """``"<kind> at line N"`` for what in ``text`` names a host, network, person, share
    or AWS account; the matched value is never repeated."""
    found = [
        f"{kind} at line {line_of(text, m)}"
        for kind, rx in _IDENTITY_RES
        for m in rx.finditer(text)
    ]
    # Blanking keeps newlines, so the lines still count.
    blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))  # noqa: E731
    without_hex = _LONG_HEX_RE.sub(blank, _UUID_RE.sub(blank, _GROUP_KEY_RE.sub(blank, text)))
    found += [
        f"aws account id at line {line_of(without_hex, m)}"
        for m in _ACCOUNT_ID_RE.finditer(without_hex)
    ]
    return found


# A compressed fixture file is never larger than this once decompressed: a discourse
# collector output is about 14 MB. A larger one (a crafted archive) is refused, not read.
MAX_DECOMPRESSED_BYTES = 64_000_000
COMPRESSED_SUFFIX = ".gz"


def read_text(path: Path) -> str:
    """The UTF-8 text of ``path``, decompressed first when its name ends in ``.gz``."""
    if path.name.endswith(COMPRESSED_SUFFIX):
        with gzip.open(path, "rb") as f:
            raw = f.read(MAX_DECOMPRESSED_BYTES + 1)
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise ValueError(f"{path}: over {MAX_DECOMPRESSED_BYTES} bytes decompressed")
        return raw.decode("utf-8")
    return path.read_text(encoding="utf-8")


def compressed_file_findings(path: Path) -> list[str]:
    """Both scans over the decompressed text of ``path``; a transcript gets the
    credential-shape scan only (no key-name findings, which prose trips)."""
    text = read_text(path)
    key_names = not path.name.startswith("transcript.")
    return secret_findings(text, key_names=key_names) + identity_findings(text)


def _compressed_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out += sorted(p for p in path.rglob(f"*{COMPRESSED_SUFFIX}") if p.is_file())
        elif path.name.endswith(COMPRESSED_SUFFIX):
            out.append(path)
    return out


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (sys.argv[1:] if argv is None else argv)]
    status = 0
    for path in _compressed_files(paths):
        try:
            found = compressed_file_findings(path)
        except (OSError, ValueError, EOFError) as exc:
            print(f"{path}: cannot be read: {exc}")
            return 2
        for hit in found:
            print(f"{path}: {hit}")
        status = 1 if found else status
    return status


if __name__ == "__main__":
    sys.exit(main())
