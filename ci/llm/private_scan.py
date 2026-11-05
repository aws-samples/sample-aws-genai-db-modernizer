# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What must never reach a public artifact or fixture (#1463).

Two scans over text, used by ``ci/llm/run.py collect-job`` (the whole-job upload) and
``ci/llm/replay.py save`` (the public replay fixture):

- ``secret_findings``: credentials. Runs the detect-secrets plugins the repo's
  pre-commit hook uses, minus the two entropy plugins (a job is full of hex ids and
  hashes that are not secrets; the hook filters those by file) and the keyword plugin
  (it reports one pair per line, so a line's second pair went unchecked; ``_PAIR_RES``
  checks every pair instead, #2053), line by line, plus a few shapes detect-secrets has
  no plugin for (a Google API key, a Slack webhook, an AWS session token, a bare AWS
  secret key). A finding names the kind and the line, never the value
  (``identity_findings`` and ``replay.private_data`` too).
- ``identity_findings``: what names a machine, a network or a person: AWS and
  internal hostnames, private IPs, emails, Windows and UNC paths, a 12-digit AWS
  account id even inside a name (``bucket-123456789012-logs``), but not a ``group_key``
  value, which is a 12-hex hash that can be all digits.

A designer's example values are allowed (#2053), exactly as
``src/shared/example_values.py`` defines them: an IP finding passes when its address (or
``address/prefix``) lies in a documentation range, a key-name finding (``KEY_NAME_KINDS``)
when its value is one angle-bracket placeholder (``<token>``), a ``[REDACTED:<kind>]``
marker or a SQL bind parameter (``?``, ``$1``), and an email when its domain is one RFC
2606 reserves. Nothing else is allowed. A JSON escape (``\\n``, ``\\t``, ``\\u2014``, an
escaped backslash) right before an address does not hide it.

``redact`` replaces each flagged value with that marker, outermost span first, so a
JSON file stays valid JSON, and a whole private-key block (BEGIN through END) with one
marker. ``key_material`` also finds a key's BEGIN or END line, two or more key body
lines in a row without their header (a key split across files or transcript lines,
under a Read tool, ``grep -n``, list, table, timestamp or escape prefix) and a single
base64 run of 200 or more characters. Any finding
not in ``REDACTABLE_KINDS`` (a key, key material, a cloud credential) makes
``ci/llm/run.py collect-job`` withhold the file and the job archive.

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
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

from src.shared.example_values import (
    is_documentation_address,
    is_example_domain,
    is_placeholder,
    public_ipv4,
    public_ipv6,
)

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
# A private-key block, raw (multi-line) or JSON-escaped on one line: BEGIN through
# END, or through the end of the JSON string (or of the text) when END is missing.
_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|(?=(?<!\\)\")|\Z)",
    re.S,
)
# A key's BEGIN or END line, also without the rest of its block (a key split across
# files or transcript lines).
_KEY_EDGE_RE = re.compile(r"-{4,5} ?(?:BEGIN|END) [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?(?: ?-{4,5})")
# A key body line: a run of 40 or more base64 characters with upper, lower and a digit
# (a hex digest is not one), an optional "=" pad, that is most of its line once a known
# prefix is removed. Raw lines and segments of a line split at escaped breaks (any
# escape level) count.
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_ESCAPED_BREAK_RE = re.compile(r"\\+r\\+n|\\+n|\\+r|\\+u000[aAdD]|<br\s*/?>")
# Known line prefixes: the Read tool's "  12→" (raw or escaped), grep -n's "file:12:",
# a list marker, a table pipe, a timestamp and a log level, and JSON punctuation.
_LINE_PREFIX_RE = re.compile(
    r"^(?:\s+|\d+(?:→|\\+u2192)|[\w./-]*:\d+:|(?:[-*+>]|\d+[.)])\s|\|"
    r"|\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\]?"
    r"|(?:DEBUG|INFO|WARN(?:ING)?|ERROR)\b:?|[\"'{}\[\],:\\])"
)
_LINE_SUFFIX_RE = re.compile(r"[\s\"'|,\\}\]]+$")
#: Two body lines in a row are key material, with or without a header (fail closed:
#: a two-line chunk of a key read back in pieces).
KEY_BODY_RUN = 2
#: One base64 run this long is key material by itself (a key joined on one line).
KEY_BLOB_CHARS = 200


def _mixed(run: str) -> bool:
    return (
        any(c.isupper() for c in run)
        and any(c.islower() for c in run)
        and any(c.isdigit() for c in run)
    )


def _strip_prefixes(segment: str) -> str:
    while True:
        m = _LINE_PREFIX_RE.match(segment)
        if not m or not m.group(0):
            break
        segment = segment[m.end() :]
    return _LINE_SUFFIX_RE.sub("", segment)


def _key_body_line(segment: str) -> bool:
    segment = _strip_prefixes(segment)
    runs = [m.group(0) for m in _BASE64_RUN_RE.finditer(segment) if _mixed(m.group(0))]
    return bool(runs) and len(max(runs, key=len)) >= 0.8 * len(segment)


def _thinking_signatures(line: str) -> list[str]:
    """The ``signature`` of each ``"type": "thinking"`` content item on one line of a
    session transcript (a long base64 value, and no key)."""
    try:
        event = json.loads(line)
    except ValueError:
        return []
    found: list[str] = []
    stack = [event]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            signature = item.get("signature")
            if item.get("type") == "thinking" and isinstance(signature, str):
                found.append(signature)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def key_material(text: str, session: bool = False) -> list[tuple[str, int]]:
    """``(kind, line)`` for each line that holds part of a private key: a BEGIN or END
    line, a run of ``KEY_BODY_RUN`` or more key body lines (raw lines, or segments of a
    line split at escaped breaks; across lines too), with or without its header, and a
    single base64 run of ``KEY_BLOB_CHARS`` or more. A segment of only quotes, brackets
    or spaces does not end a run. ``session``: the text is a ``.jsonl`` session
    transcript, whose thinking items' signatures are left out."""
    found: set[tuple[str, int]] = set()
    run: list[int] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if session:
            # In a session transcript (``session=True``), a thinking item's signature
            # is blanked, its positions kept; every other field is searched as is.
            for signature in _thinking_signatures(line):
                if signature:
                    line = line.replace(signature, " " * len(signature))
        if _KEY_EDGE_RE.search(line):
            found.add(("Private Key", number))
        if any(
            len(m.group(0)) >= KEY_BLOB_CHARS and _mixed(m.group(0))
            for m in _BASE64_RUN_RE.finditer(line)
        ):
            found.add(("key material", number))
        for segment in _ESCAPED_BREAK_RE.split(line):
            if _key_body_line(segment):
                run.append(number)
                if len(run) >= KEY_BODY_RUN:
                    found |= {("key material", n) for n in run}
            elif segment.strip(" \t\"'{}[],:\\"):
                run = []
    return sorted(found, key=lambda f: (f[1], f[0]))


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
    for secret in scan_line(line):
        value = secret.secret_value or ""
        spans = [(m.start(), m.end()) for m in re.finditer(re.escape(value), line)] if value else []
        if not spans:
            found.append((secret.type, None))
        found += [(secret.type, sp) for sp in spans if not _allowed(secret.type, line, sp)]
    for kind, rx in _EXTRA_SECRET_RES:
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
    with transient_settings({"plugins_used": _PLUGINS, "filters_used": []}):
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


@dataclass
class Redaction:
    """``redact``'s result: the text with every flagged value replaced, the findings as
    ``(kind, line)`` (never the value), and whether every value could be removed."""

    text: str
    findings: list[tuple[str, int]] = field(default_factory=list)
    complete: bool = True
    #: The key names of the key-value findings, by ``(kind, line)``: a key name is no
    #: secret, and a withheld run cannot be diagnosed without it.
    keys: dict[tuple[str, int], list[str]] = field(default_factory=dict)

    @property
    def redactable(self) -> bool:
        """Every value was removed and every finding is of a ``REDACTABLE_KINDS`` kind."""
        return self.complete and all(kind in REDACTABLE_KINDS for kind, _ in self.findings)


def _merge(hits: list[tuple[str, tuple[int, int]]]) -> list[tuple[str, int, int]]:
    """Overlapping spans folded into one, named by the outermost (earliest, then
    longest) span's kind."""
    merged: list[tuple[str, int, int]] = []
    for kind, (start, end) in sorted(hits, key=lambda h: (h[1][0], -h[1][1])):
        if merged and start < merged[-1][2]:
            last = merged[-1]
            merged[-1] = (last[0], last[1], max(last[2], end))
        else:
            merged.append((kind, start, end))
    return merged


# A JSON key's colon right before a value (``"key": `` or ``\\"key\\": ``).
_AFTER_JSON_KEY_RE = re.compile(r"(\\*)[\"']\s*:\s*\Z")


def _marker_for(line: str, start: int, end: int, kind: str) -> str:
    """The marker for ``line[start:end]``. An unquoted JSON value (a number) gets a
    quoted marker, at the key's own escape level, so the JSON stays valid."""
    marker = redaction_marker(kind)
    after_key = _AFTER_JSON_KEY_RE.search(line[max(0, start - 64) : start])
    if after_key and line[start : start + 1] not in ('"', "'", "\\"):
        quote = after_key.group(1) + '"'
        return quote + marker + quote
    return marker


def redact(
    text: str, key_names: bool = True, emails: bool = False, session: bool = False
) -> Redaction:
    """Replace every value ``secret_findings`` (with ``emails``, also each email at a
    domain that is not an example one) flags in ``text`` by its ``redaction_marker``.

    A private-key block goes whole first (BEGIN through END; a raw block keeps its line
    breaks, so later line numbers hold). Then, per line, the flagged spans are merged
    and replaced outermost first: only the values change, never a whole line, so JSON
    stays valid. A value that cannot be placed, or a line still flagged afterwards,
    makes the result ``complete=False``."""
    findings: list[tuple[str, int]] = key_material(text, session=session)
    complete = True

    def _block(m: re.Match[str]) -> str:
        findings.append(("Private Key", line_of(text, m)))
        return redaction_marker("Private Key") + "\n" * m.group(0).count("\n")

    unblocked = _KEY_BLOCK_RE.sub(_block, text)
    out: list[str] = []
    keys: dict[tuple[str, int], set[str]] = {}
    for number, line, body, hits, table in _scan_lines(unblocked, key_names, emails):
        if not hits:
            out.append(line)
            continue
        findings += [(kind, number) for kind in sorted({kind for kind, _ in hits})]
        for kind, _span, key in _pair_matches(body) + table:
            if key_names or kind not in KEY_NAME_KINDS:
                keys.setdefault((kind, number), set()).add(key)
        placed = [(kind, span) for kind, span in hits if span is not None]
        if len(placed) < len(hits) or any(kind == "Private Key" for kind, _ in hits):
            complete = False
        redacted = body
        for kind, start, end in reversed(_merge(placed)):
            redacted = redacted[:start] + _marker_for(redacted, start, end, kind) + redacted[end:]
        if any(h for h in _line_hits(redacted, emails) if key_names or h[0] not in KEY_NAME_KINDS):
            complete = False
        out.append(redacted + line[len(body) :])
    return Redaction(
        "".join(out),
        sorted(set(findings), key=lambda f: (f[1], f[0])),
        complete,
        {where: sorted(names) for where, names in keys.items()},
    )


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
    found += [
        f"{EMAIL_KIND} at line {line_of(text, m)}"
        for m in _EMAIL_RE.finditer(text)
        if not is_example_domain(m.group(1))
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
