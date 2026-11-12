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
2606 reserves. Nothing else is allowed. The scans here read model-written text (the
upload, the replay fixtures), so they pass ``narrow_key_names=True`` (#2179): an unquoted
value after a ``token`` or ``api_key`` name with ``key:`` or in a Markdown table counts
only when it looks like a secret (a known token format, or any piece that is not a
word or an identifier: a digit, a symbol, a random run of letters), so design prose
such as "Validate a token: GetItem ..." is no finding. Every other key name (passwords,
``secret_key``, ``private_key`` ...) stays strict, and so does the collector's scan of
routine bodies, which keeps the default. A JSON escape (``\\n``, ``\\t``, ``\\u2014``, an
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

from src.shared.example_values import (
    is_example_domain,
)

# The one definition of the credential scan (#1933): shared with the collector, which
# scans each routine body it carries. Re-exported here for the callers and tests.
from src.shared.secret_scan import (  # noqa: E402,F401
    _EMAIL_RE,
    _PREFIX_RE,
    EMAIL_KIND,
    IP_KINDS,
    KEY_NAME_KINDS,
    REDACTABLE_KINDS,
    REDACTION_RE,
    Hit,
    _allowed,
    _allowed_value,
    _line_hits,
    _pair_hits,
    _pair_matches,
    _scan_lines,
    _table_hits,
    redaction_marker,
    secret_findings,
)

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
    text: str,
    key_names: bool = True,
    emails: bool = False,
    session: bool = False,
    narrow_key_names: bool = False,
) -> Redaction:
    """Replace every value ``secret_findings`` (with ``emails``, also each email at a
    domain that is not an example one) flags in ``text`` by its ``redaction_marker``.

    A private-key block goes whole first (BEGIN through END; a raw block keeps its line
    breaks, so later line numbers hold). Then, per line, the flagged spans are merged
    and replaced outermost first: only the values change, never a whole line, so JSON
    stays valid. A value that cannot be placed, or a line still flagged afterwards,
    makes the result ``complete=False``. ``narrow_key_names`` as in ``secret_findings``."""
    findings: list[tuple[str, int]] = key_material(text, session=session)
    complete = True

    def _block(m: re.Match[str]) -> str:
        findings.append(("Private Key", line_of(text, m)))
        return redaction_marker("Private Key") + "\n" * m.group(0).count("\n")

    unblocked = _KEY_BLOCK_RE.sub(_block, text)
    out: list[str] = []
    keys: dict[tuple[str, int], set[str]] = {}
    for number, line, body, hits, table in _scan_lines(
        unblocked, key_names, emails, narrow_key_names
    ):
        if not hits:
            out.append(line)
            continue
        findings += [(kind, number) for kind in sorted({kind for kind, _ in hits})]
        for kind, _span, key in _pair_matches(body, narrow_key_names) + table:
            if key_names or kind not in KEY_NAME_KINDS:
                keys.setdefault((kind, number), set()).add(key)
        placed = [(kind, span) for kind, span in hits if span is not None]
        if len(placed) < len(hits) or any(kind == "Private Key" for kind, _ in hits):
            complete = False
        redacted = body
        for kind, start, end in reversed(_merge(placed)):
            redacted = redacted[:start] + _marker_for(redacted, start, end, kind) + redacted[end:]
        rescan = _line_hits(redacted, emails, narrow_key_names)
        if any(h for h in rescan if key_names or h[0] not in KEY_NAME_KINDS):
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
    credential-shape scan only (no key-name findings, which prose trips). A fixture is a
    saved e2e-llm job, model-written text, so its key names are narrowed as on upload
    (#2179)."""
    text = read_text(path)
    key_names = not path.name.startswith("transcript.")
    findings = secret_findings(text, key_names=key_names, narrow_key_names=True)
    return findings + identity_findings(text)


def _compressed_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out += sorted(p for p in path.rglob(f"*{COMPRESSED_SUFFIX}") if p.is_file())
        elif path.name.endswith(COMPRESSED_SUFFIX):
            out.append(path)
    return out


# Every finding kind a scan here can report: the repository's own rules, detect-secrets'
# plugin types and the identity scan. The command line prints a finding only as one of
# these constants and its line number, so nothing the scan read (a matched value) can
# reach the output, whatever a finding string holds.
PRINTABLE_KINDS = frozenset(
    {
        *KEY_NAME_KINDS,
        *IP_KINDS,
        EMAIL_KIND,
        "AWS Access Key",
        "Private Key",
        "google api key",
        "slack webhook",
        "bearer token",
        "aws secret access key",
        "bare secret key",
        "Artifactory Credentials",
        "Azure Storage Account access key",
        "Basic Auth Credentials",
        "Cloudant Credentials",
        "Discord Bot Token",
        "GitHub Token",
        "GitLab Token",
        "IBM COS HMAC Credentials",
        "IBM Cloud IAM Key",
        "JSON Web Token",
        "Mailchimp Access Key",
        "NPM tokens",
        "OpenAI Token",
        "PyPI Token",
        "SendGrid API Key",
        "Slack Token",
        "SoftLayer Credentials",
        "Square OAuth Secret",
        "Stripe Access Key",
        "Telegram Bot Token",
        "Twilio API Key",
        *(kind for kind, _rx in _IDENTITY_RES),
        "aws account id",
    }
)
_UNLISTED_KIND = "unlisted finding kind"
_FINDING_RE = re.compile(r"(?P<kind>.+) at line (?P<line>\d+)")


def printable_finding(hit: str) -> str:
    """``"<kind> at line N"`` rebuilt from ``PRINTABLE_KINDS`` and the line number alone
    (an unlisted or malformed finding prints as ``_UNLISTED_KIND``), never the finding
    string itself."""
    m = _FINDING_RE.fullmatch(hit)
    raw = m.group("kind") if m else ""
    kind = next((k for k in sorted(PRINTABLE_KINDS) if k == raw), _UNLISTED_KIND)
    line = int(m.group("line")) if m else 0
    return f"{kind} at line {line}"


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
            print(f"{path}: {printable_finding(hit)}")
        status = 1 if found else status
    return status


if __name__ == "__main__":
    sys.exit(main())
