# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Credentials in text, found by the repository's own rules (#1463, #2053, #1933).

The one definition of ``secret_findings``, shared by the CI private scan
(``ci/llm/private_scan.py``, the e2e-llm upload and the replay fixtures) and the
collector, which scans each routine body it carries (``routine_sources``). It needs
nothing beyond the standard library, so it runs in every deployed image:

- the repository's own rules, always: every credential key-value pair (``password``,
  ``secret``, ``token`` and ``api key`` keys, ``_PAIR_RES``), a value under a Markdown
  column that names one, an AWS access key id, a private key header, a Google API key,
  a Slack webhook, a bearer token, an AWS secret access key, a bare AWS secret key, and
  public IP addresses (``RULE_SET`` ``"regex"``);
- detect-secrets' shape plugins on top, when the package is installed (a dev checkout
  and CI: ``RULE_SET`` ``"regex+detect-secrets"``); the deployed images run without it.

A finding names its kind and line, never the value. A designer's example values are
allowed exactly as ``src/shared/example_values.py`` defines them (#2053).
"""

from __future__ import annotations

import contextlib
import importlib
import re
from typing import Any

from src.shared.example_values import (
    is_documentation_address,
    is_example_domain,
    is_placeholder,
    public_ipv4,
    public_ipv6,
)


def _load_detect_secrets() -> tuple[Any, Any]:
    """detect-secrets' line scan and settings, or ``(None, None)`` where the package is
    not installed (a dev dependency, absent from the deployed images)."""
    try:
        scan = importlib.import_module("detect_secrets.core.scan")
        settings = importlib.import_module("detect_secrets.settings")
    except ImportError:
        return None, None
    return scan.scan_line, settings.transient_settings


_scan_line, _transient_settings = _load_detect_secrets()


def detect_secrets_available() -> bool:
    """Whether detect-secrets' plugins run on top of the repository's own rules."""
    return _scan_line is not None


def rule_set() -> str:
    """Which rules :func:`secret_findings` runs: ``"regex"`` (the repository's own,
    always) or ``"regex+detect-secrets"``."""
    return "regex+detect-secrets" if detect_secrets_available() else "regex"


def _detect_secrets_line(line: str) -> list[Any]:
    return list(_scan_line(line)) if _scan_line is not None else []


def _detect_secrets_settings() -> contextlib.AbstractContextManager[Any]:
    if _transient_settings is None:
        return contextlib.nullcontext()
    settings: contextlib.AbstractContextManager[Any] = _transient_settings(
        {"plugins_used": _PLUGINS, "filters_used": []}
    )
    return settings


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
    ("bearer token", re.compile(r"\bBearer\s+(?P<v>[A-Za-z0-9._~+/=-]{20,})", re.IGNORECASE)),
    (
        "aws secret access key",
        re.compile(
            r"secret[_-]?access[_-]?key[\"']?\s*[:=]\s*[\"']?(?P<v>[A-Za-z0-9/+=]{20,})", re.I
        ),
    ),
)
# The repository's own shapes for what a detect-secrets plugin would also find, so the
# scan finds them without the package (#1933); a line found by both counts once, by kind.
_OWN_SHAPE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS Access Key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Private Key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")),
)
# Every key-value pair whose key names a credential (#2053), found with finditer so a
# line's second pair is never hidden by its first. This pass stands in for
# detect-secrets' keyword plugin, which reports one pair per line; it covers that
# plugin's denylist plus ``token`` keys. The key must END in the credential word
# (``user_password``, ``dbPassword``, ``x-api-key``, ``token_hash``), so a key that only
# contains one (``apiKeySource``, ``wp_api_keys``, ``token_expires_at``) is not a pair.
_PASSWORD_WORDS = (  # nosec B105 -- key-name words to look for, not a password
    r"password|passwd|pwd|passphrase|secret|db[_-]?pass|database[_-]?pass|key[_-]?pass"
    r"|contrase(?:ñ|n)a"
)
_KEY_WORDS = (
    r"api[_-]?key|auth[_-]?key|service[_-]?key|account[_-]?key|db[_-]?key|database[_-]?key"
    r"|priv[_-]?key|private[_-]?key|client[_-]?key|access[_-]?key|secret[_-]?key"
)
_KEY_NAME = (
    r"(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z0-9_.-]*?"
    rf"(?:(?P<pw>{_PASSWORD_WORDS})|(?P<tok>token)|(?P<ak>{_KEY_WORDS}))"
    r"(?:[_-]?(?:hash|digest|value|plain|raw|text))?)"
)
# A value in quotes, at any escape level: up to the (escaped) closing quote.
_QUOTED = r"(?P<qv>\\*[\"'])(?P<v>.*?)(?=\\*[\"'])"
_PAIR_RES = (
    # "key": "value" / \"key\": \"value\" / "key": 123 (JSON, any escape level)
    # (a SQL cast, 'name'::type, is no pair)
    re.compile(
        rf"\\*[\"']{_KEY_NAME}\\*[\"']\s*:(?!:)\s*(?:{_QUOTED}|(?P<u>\[REDACTED:[a-z0-9-]+\]|[^\s,}}\]\"'\\]+))",
        re.I,
    ),
    # key=value, key: value (YAML or prose), **key**: value, `key`=value
    re.compile(
        rf"(?:\*\*|`)?{_KEY_NAME}(?:\*\*|`)?\s*(?P<sep>=|:(?=\s))\s*`?"
        rf"(?:{_QUOTED}|(?P<u>[^\s\"'`\\]+))",
        re.I,
    ),
    # | key | value |
    re.compile(rf"\|\s*{_KEY_NAME}\s*\|\s*(?P<u>[^|\s][^|]*?)\s*(?=\|)", re.I),
)
_JSON_LITERALS = frozenset({"null", "true", "false"})
# A short integer (a count or a size in a design), never a long number or a float.
_COUNT_RE = re.compile(r"\d{1,6}")
# A SQL bind parameter after ``=`` (``post_password = ?``) stands for a value.
_BIND_RE = re.compile(r"\?|%s|\$\d+")
_NAMED_BIND_RE = re.compile(r"[:@](?P<name>[a-z_][a-z0-9_]*)")
# A column note in prose: `` `table.column`: varchar(255) ``.
_SQL_TYPE_RE = re.compile(
    r"(?:var)?char|(?:tiny|medium|long)?text|(?:tiny|small|medium|big)?int(?:eger)?|decimal"
    r"|numeric|float|double|real|bool(?:ean)?|date(?:time)?|time(?:stamp(?:tz)?)?|"
    r"(?:long)?blob|(?:var)?binary|jsonb?|uuid|(?:big)?serial|bytea|enum",
    re.I,
)
_SQL_TYPE_VALUE_RE = re.compile(rf"(?:{_SQL_TYPE_RE.pattern})(?:\(\d+(?:,\s*\d+)?\))?[,:]?", re.I)


def _bind_parameter(m: re.Match[str], value: str) -> bool:
    """``key = ?`` / ``%s`` / ``$N`` / ``:name`` / ``@name``, unquoted after ``=``, where a
    named parameter is a lower-case identifier ending in the key's own word
    (``post_password = :post_password``)."""
    if m.groupdict().get("sep") != "=":
        return False
    if _BIND_RE.fullmatch(value):
        return True
    named = _NAMED_BIND_RE.fullmatch(value)
    word = next(m.group(g) for g in ("pw", "tok", "ak") if m.group(g))
    return named is not None and named.group("name").endswith(word.lower())


# The first word of the pipeline's own key note (``src/shared/key_refusals.py``
# ``candidate_explanation``): `` `table.column`: accepted: ... `` / ``refused for ...``.
_KEY_NOTE_WORDS = frozenset({"accepted:", "refused"})


def _column_note(m: re.Match[str], line: str, value: str) -> bool:
    """A backtick-quoted ``table.column`` key followed by ``:`` and a SQL type, or by
    the first word of the pipeline's key note (``_KEY_NOTE_WORDS``)."""
    key_end = m.end("key")
    return (
        m.group(0).startswith("`")
        and "." in m.group("key").strip(".")
        and line[key_end : key_end + 1] == "`"
        and m.groupdict().get("sep") == ":"
        and (_SQL_TYPE_VALUE_RE.fullmatch(value) is not None or value in _KEY_NOTE_WORDS)
    )


def _pair_kind(m: re.Match[str]) -> str:
    return (
        "password value" if m.group("pw") else "token value" if m.group("tok") else "api key value"
    )


# A rate or a unit, not a credential: ``credits_per_token``, ``cost_per_api_key``.
_PER_UNIT_RE = re.compile(r"(?:^|[_-])per[_-]?(?:token|api[_-]?key)$", re.I)


def _pair_matches(line: str) -> list[tuple[str, tuple[int, int], str]]:
    """Each credential key's value on ``line``: its kind, span and key name. Every
    pair is checked; an allowed value (a placeholder, a marker, a bare JSON literal or
    short count, a bind parameter, a column note) is left out, and never exempts the
    rest of the line."""
    found: dict[tuple[int, int], tuple[str, str]] = {}
    for rx in _PAIR_RES:
        for m in rx.finditer(line):
            if _PER_UNIT_RE.search(m.group("key")):
                continue
            group = "v" if m.groupdict().get("v") is not None else "u"
            value = m.group(group) or ""
            if group == "u":
                # Not a value: an unquoted JSON literal or short count, a SQL bind
                # parameter after "=", a `table.column`: <SQL type> column note.
                if rx is _PAIR_RES[0] and (value in _JSON_LITERALS or _COUNT_RE.fullmatch(value)):
                    continue
                if rx is _PAIR_RES[1] and _column_note(m, line, value):
                    continue
                value = value.rstrip("),;")
                if rx is _PAIR_RES[1] and _bind_parameter(m, value):
                    continue
            if not value or _allowed_value(value):
                continue
            found.setdefault(m.span(group), (_pair_kind(m), m.group("key")))
    return [(kind, span, key) for span, (kind, key) in found.items()]


def _pair_hits(line: str) -> list[tuple[str, tuple[int, int]]]:
    """``_pair_matches`` without the key names."""
    return [(kind, span) for kind, span, _ in _pair_matches(line)]


def _table_hits(lines: list[str]) -> dict[int, list[tuple[str, tuple[int, int], str]]]:
    """The cells under a Markdown table column whose header names a credential
    (``| user | password |`` then ``| app | <value> |``), by line index."""
    found: dict[int, list[tuple[str, tuple[int, int], str]]] = {}
    columns: dict[int, tuple[str, str]] = {}
    key_cell = re.compile(rf"\s*{_KEY_NAME}\s*", re.I)
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            columns = {}
            continue
        bars = [i for i, c in enumerate(line) if c == "|"]
        cells = [(bars[i] + 1, bars[i + 1]) for i in range(len(bars) - 1)]
        header: dict[int, tuple[str, str]] = {}
        for col, (a, b) in enumerate(cells):
            m = key_cell.fullmatch(line[a:b])
            if m and not _PER_UNIT_RE.search(m.group("key")):
                header[col] = (_pair_kind(m), m.group("key"))
        if header:
            columns = header
            continue
        if re.fullmatch(r"[\s|:-]*", line):
            continue
        for col, (kind, key) in columns.items():
            if col < len(cells):
                a, b = cells[col]
                cell = line[a:b]
                value = cell.strip()
                if value and not _allowed_value(value):
                    start = a + cell.index(value)
                    found.setdefault(index, []).append((kind, (start, start + len(value)), key))
    return found


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
KEY_NAME_KINDS = frozenset({"password value", "token value", "api key value"})
IP_KINDS = frozenset({"Public IP (ipv4)", "Public IP (ipv6)"})
EMAIL_KIND = "email"
#: The kinds whose value sits on one line and can be replaced on its own, so a file
#: with only these findings can be kept redacted. A private key, a cloud credential
#: (an AWS, Azure, IBM, Google or other provider key) and every other shape cannot.
REDACTABLE_KINDS = (
    IP_KINDS
    | KEY_NAME_KINDS
    | {EMAIL_KIND, "bearer token", "JSON Web Token", "Basic Auth Credentials"}
)

#: What ``redact`` writes in place of a flagged value: ``[REDACTED:<kind>]``, the kind
#: lower-cased with dashes (``[REDACTED:public-ip-ipv4]``).
REDACTION_RE = re.compile(r"\[REDACTED:[a-z0-9-]+\]")
# Not started inside a JSON escape: after ``\\n`` the address starts after the ``n``.
_EMAIL_RE = re.compile(
    r"(?:(?<![\w.+\\-])|(?<=\\[ntr])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\\\))"
    r"[\w.+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)
_PORT_RE = re.compile(r":\d{1,5}$")
_PREFIX_RE = re.compile(r"/\d{1,3}")


def redaction_marker(kind: str) -> str:
    """The marker that replaces a value of finding ``kind``."""
    return "[REDACTED:" + "-".join(re.findall(r"[a-z0-9]+", kind.lower())) + "]"


def _allowed_value(value: str) -> bool:
    """A key-name value that is no credential: a placeholder or a redaction marker."""
    value = value.strip("\"'` ")
    return is_placeholder(value) or REDACTION_RE.fullmatch(value) is not None


def _allowed(kind: str, line: str, span: tuple[int, int]) -> bool:
    """The allow rules (the module docstring), each for its own kinds only."""
    value = line[span[0] : span[1]].strip("\"'")
    if kind in IP_KINDS:
        prefix = _PREFIX_RE.match(line, span[1])
        return is_documentation_address(
            _PORT_RE.sub("", value) + (prefix.group(0) if prefix else "")
        )
    if kind in KEY_NAME_KINDS:
        return _allowed_value(value)
    return False


Hit = tuple[str, "tuple[int, int] | None"]


def _line_hits(line: str, emails: bool) -> list[Hit]:
    """Each finding on ``line``: its kind and the span of its value (``None`` when the
    value cannot be placed on the line). Allowed values are left out."""
    found: list[Hit] = []
    for secret in _detect_secrets_line(line):
        value = secret.secret_value or ""
        spans = [(m.start(), m.end()) for m in re.finditer(re.escape(value), line)] if value else []
        if not spans:
            found.append((secret.type, None))
        found += [(secret.type, sp) for sp in spans if not _allowed(secret.type, line, sp)]
    for kind, rx in (*_OWN_SHAPE_RES, *_EXTRA_SECRET_RES):
        for m in rx.finditer(line):
            group: int | str = next((g for g in ("v", "m") if g in rx.groupindex and m.group(g)), 0)
            if not _allowed(kind, line, m.span(group)):
                found.append((kind, m.span(group)))
    found += _pair_hits(line)
    found += [
        ("bare secret key", m.span())
        for m in _BARE_KEY_RE.finditer(line)
        if _bare_secret_key(m.group(0))
    ]
    found += [
        ("Public IP (ipv4)", m.span())
        for m in public_ipv4(line)
        if not _allowed("Public IP (ipv4)", line, m.span())
    ]
    found += [
        ("Public IP (ipv6)", m.span())
        for m in public_ipv6(line)
        if not _allowed("Public IP (ipv6)", line, m.span())
    ]
    if emails:
        found += [
            (EMAIL_KIND, m.span())
            for m in _EMAIL_RE.finditer(line)
            if not is_example_domain(m.group(1))
        ]
    return found


def _scan_lines(text: str, key_names: bool, emails: bool = False):
    lines = text.splitlines(keepends=True)
    tables = _table_hits([line.rstrip("\r\n") for line in lines])
    with _detect_secrets_settings():
        for number, line in enumerate(lines, start=1):
            body = line.rstrip("\r\n")
            table = tables.get(number - 1, [])
            hits = _line_hits(body, emails) + [(kind, span) for kind, span, _ in table]
            if not key_names:
                hits = [hit for hit in hits if hit[0] not in KEY_NAME_KINDS]
            yield number, line, body, hits, table


def secret_findings(text: str, key_names: bool = True) -> list[str]:
    """``"<kind> at line N"`` for every credential in ``text``; no value is repeated.
    ``key_names=False`` leaves out the findings that go by a key's name (``KEY_NAME_KINDS``)."""
    found: list[str] = []
    for number, _line, _body, hits, _table in _scan_lines(text, key_names):
        found += [f"{kind} at line {number}" for kind in sorted({kind for kind, _ in hits})]
    return found
