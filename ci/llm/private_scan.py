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
such as "Validate a token: GetItem ..." is no finding. The bare-key rule is never
narrowed, whatever field a value sits under (#2209). Every other key name (passwords,
``secret_key``, ``private_key`` ...) stays strict, and so does the collector's scan of
routine bodies, which keeps the default. A JSON escape (``\\n``, ``\\t``, ``\\u2014``, an
escaped backslash) right before an address does not hide it.

``redact`` also records each placed finding's safe context (#2209, ``Redaction.context``):
the field name right before the value, its JSON path (``document``) and its shape
(``value_shape``), each label a constant; a name or JSON key that shares a piece of
``VALUE_PIECE`` characters with a flagged value, or is not an identifier, is withheld.

``redact`` replaces each flagged value with that marker, outermost span first, so a
JSON file stays valid JSON, and a whole private-key block (BEGIN through END) with one
marker. ``redact_flagged_values`` then replaces, in any text, each occurrence of a value
flagged anywhere in the upload: in JSON each object key that contains one, numbering
keys of one object that would collide (#2213), and each one inside a string value; in
plain text each one anywhere (#2243); a value that does not look random matches only
as a whole token (#2266). It refuses a JSON file it cannot parse that holds one. ``key_material`` also finds a key's BEGIN or END line, two or more key body
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

import bisect
import gzip
import html
import json
import json.decoder
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


# The key or field name right before a value on its line: ``"name": "``, ``\\"name\\": \\"``
# (JSON at any escape level), ``name: ``, ``name = '``, ``**name**: ``, `` `name`= ``,
# ``| name | ``. Only this much of the line before the value is read.
_PRECEDING_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_.$@/\\-])(?P<key>[A-Za-z0-9_.$@-]+)(?:\\*[\"'`]|\*\*)?\s*[:=|]\s*"
    r"(?:\\*[\"'`])?\Z"
)
_PRECEDING_KEY_LOOKBACK = 128


def preceding_key(line: str, start: int) -> str | None:
    """The key or field name right before the value at ``line[start:]``, or ``None``
    when nothing names it (a bare line, an array item, prose), or when the name starts
    right after a ``/`` (a URL's user name: ``//user:password@``) or a backslash (a
    name cut at a JSON escape)."""
    m = _PRECEDING_KEY_RE.search(line[max(0, start - _PRECEDING_KEY_LOOKBACK) : start])
    return m.group("key") if m else None


# A finding's safe context (#2209): what lets a reviewer judge a withheld finding
# without its file. It is built from constants, a computed shape and names that are
# checked first, never from the value: no value, prefix or suffix of one.
#: The character classes ``value_shape`` names, in this order.
_CLASS_LABELS = (
    ("mixed case", "upper case", "lower case"),
    "digits",
    "symbols",
    "spaces",
)
_EMPTY_LABEL = "empty"
#: The alphabets ``value_shape`` names: hex, the base64 alphabet (``+/`` and ``=``
#: padding), the URL-safe one (``-_``), or neither.
_ALPHABETS = (
    ("hex", re.compile(r"[0-9a-fA-F]+")),
    ("base64", re.compile(r"[A-Za-z0-9+/]+={0,2}")),
    ("base64url", re.compile(r"[A-Za-z0-9_-]+={0,2}")),
)
_OTHER_ALPHABET = "other"
#: What a field name or JSON key that cannot be shown is replaced by.
WITHHELD_NAME = "[withheld]"
WITHHELD_SEGMENT = "[*]"
# A name that can be shown: an identifier of at most 64 characters.
_SAFE_NAME_RE = re.compile(r"[A-Za-z_$@][A-Za-z0-9_.$@-]{0,63}")
#: The shortest piece of a value that must never reach the context (a shorter value
#: is a piece by itself).
VALUE_PIECE = 4
#: A value shorter than this gets no shape but ``SHORT_SHAPE``: its length and
#: character classes would narrow it down too far.
SHORT_VALUE = 8
SHORT_SHAPE = "short"
#: The kinds whose value follows a user name (``user:password@host``): the name before
#: it is half the credential, so it is never reported as the field.
_USERINFO_KINDS = frozenset({"Basic Auth Credentials"})


def value_shape(value: str) -> dict[str, Any]:
    """A value's length, character classes (``"mixed case + digits"``) and alphabet
    (``hex``, ``base64``, ``base64url`` or ``other``), each label a constant; only
    ``{"shape": "short"}`` under ``SHORT_VALUE`` characters."""
    if len(value) < SHORT_VALUE:
        return {"shape": SHORT_SHAPE}
    upper = any(c.isupper() for c in value)
    lower = any(c.islower() for c in value)
    case_labels = _CLASS_LABELS[0]
    labels: list[str] = []
    if upper and lower:
        labels.append(case_labels[0])
    elif upper:
        labels.append(case_labels[1])
    elif lower:
        labels.append(case_labels[2])
    if any(c.isdigit() for c in value):
        labels.append(_CLASS_LABELS[1])
    if any(not c.isalnum() and not c.isspace() for c in value):
        labels.append(_CLASS_LABELS[2])
    if any(c.isspace() for c in value):
        labels.append(_CLASS_LABELS[3])
    alphabet = next((name for name, rx in _ALPHABETS if rx.fullmatch(value)), _OTHER_ALPHABET)
    return {
        "length": int(len(value)),
        "characters": " + ".join(labels) if labels else _EMPTY_LABEL,
        "alphabet": alphabet,
    }


def decoded_forms(raw: str) -> list[str]:
    """``raw`` and each JSON-unescaped form of it (``\\u`` escapes and every escape
    level, as in a transcript's tool output), the last the most decoded. Only JSON
    escapes are undone: a name holding the value percent-encoded, with YAML ``\\x``
    escapes or in base64 is not recognised (a known limit, #2211)."""
    forms = [raw]
    for _level in range(4):
        if "\\" not in forms[-1]:
            break
        try:
            decoded = json.loads('"' + forms[-1] + '"')
        except ValueError:
            break
        if not isinstance(decoded, str) or decoded == forms[-1]:
            break
        forms.append(decoded)
    return forms


def value_pieces(values: Iterable[str]) -> set[str]:
    """The case-folded pieces of ``VALUE_PIECE`` characters of each value in every
    decoded form (``decoded_forms``); a shorter value is one piece."""
    pieces: set[str] = set()
    for value in values:
        for form in decoded_forms(value):
            folded = form.lower()
            if len(folded) < VALUE_PIECE:
                if folded:
                    pieces.add(folded)
                continue
            pieces.update(folded[i : i + VALUE_PIECE] for i in range(len(folded) - VALUE_PIECE + 1))
    return pieces


def _shares_a_piece(name: str, pieces: set[str]) -> bool:
    folded = name.lower()
    windows = {folded[i : i + VALUE_PIECE] for i in range(len(folded) - VALUE_PIECE + 1)}
    return bool(windows & pieces) or any(
        len(piece) < VALUE_PIECE and piece in folded for piece in pieces
    )


def holds_a_piece(name: str, pieces: set[str]) -> bool:
    """Whether ``name`` (a file name, a path) shares a piece (``value_pieces``) with a
    flagged value, in any case: such a name is shown as ``WITHHELD_NAME`` (#2266)."""
    return _shares_a_piece(name, pieces)


def _safe_name(name: str, pieces: set[str]) -> bool:
    """A name (a field, a JSON key) that can be shown: an identifier of at most 64
    characters that shares no piece (``value_pieces``) with any flagged value and is
    no finding itself. (A name that short can't be key material: that takes two body
    lines or a 200-character run.)"""
    return (
        _SAFE_NAME_RE.fullmatch(name) is not None
        and not _shares_a_piece(name, pieces)
        and not _line_hits(name, emails=True)
    )


# A JSON number or literal, at the position ``_json_scalars`` reads it.
_JSON_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?|true|false|null")
_JSON_WS_RE = re.compile(r"[ \t\n\r]*")
JsonPath = tuple["str | int", ...]
# The standard library's JSON string reader: ``(text, end)`` for the string whose
# opening quote is right before ``start`` (untyped in the stubs).
_scanstring: Callable[[str, int], tuple[str, int]] = vars(json.decoder)["scanstring"]


#: One object key of a JSON document: its string's span (quotes included), its decoded
#: name and the offset of its object's ``{``.
JsonKey = tuple[int, int, str, int]


def _json_scalars(
    text: str, keys: list[JsonKey] | None = None
) -> list[tuple[int, int, JsonPath]] | None:
    """Every scalar of the JSON document ``text`` (strings, numbers, literals, and
    object keys) as ``(start, end, path)``, in order; a key's path ends in the key
    itself. ``keys``, when given, also gets every object key (``JsonKey``). ``None``
    when ``text`` is no JSON document."""
    out: list[tuple[int, int, JsonPath]] = []

    def ws(i: int) -> int:
        m = _JSON_WS_RE.match(text, i)
        return m.end() if m else i

    def value(i: int, path: JsonPath) -> int:
        i = ws(i)
        c = text[i : i + 1]
        if c == "{":
            opening = i
            i = ws(i + 1)
            if text[i : i + 1] == "}":
                return i + 1
            while True:
                if text[i : i + 1] != '"':
                    raise ValueError("key")
                name, end = _scanstring(text, i + 1)
                out.append((i, end, (*path, name)))
                if keys is not None:
                    keys.append((i, end, name, opening))
                i = ws(end)
                if text[i : i + 1] != ":":
                    raise ValueError("colon")
                i = ws(value(i + 1, (*path, name)))
                if text[i : i + 1] == ",":
                    i = ws(i + 1)
                    continue
                if text[i : i + 1] == "}":
                    return i + 1
                raise ValueError("object")
        if c == "[":
            i = ws(i + 1)
            if text[i : i + 1] == "]":
                return i + 1
            index = 0
            while True:
                i = ws(value(i, (*path, index)))
                index += 1
                if text[i : i + 1] == ",":
                    i += 1
                    continue
                if text[i : i + 1] == "]":
                    return i + 1
                raise ValueError("array")
        if c == '"':
            _text, end = _scanstring(text, i + 1)
            out.append((i, end, path))
            return end
        m = _JSON_NUMBER_RE.match(text, i)
        if not m:
            raise ValueError("scalar")
        out.append((m.start(), m.end(), path))
        return m.end()

    try:
        end = ws(value(0, ()))
    except (ValueError, IndexError, RecursionError):
        return None
    return out if end == len(text) else None


def _render_path(path: JsonPath, pieces: set[str]) -> str:
    """``$.a.b[0].c``: an index as itself, a key by name when it is safe to show
    (``_safe_name``), else ``WITHHELD_SEGMENT``."""
    parts = ["$"]
    for segment in path:
        if isinstance(segment, int):
            parts.append(f"[{int(segment)}]")
        elif _safe_name(segment, pieces) and "." not in segment:
            parts.append("." + segment)
        else:
            parts.append(WITHHELD_SEGMENT)
    return "".join(parts)


class _JsonIndex:
    """The JSON path at a line and column of a JSON document (``"json"``) or of each
    line of a JSON Lines file (``"jsonl"``). Lines are split once, as the scan splits
    them (``str.splitlines``, so a raw U+2028 is a break too); a document is parsed
    once and a JSON Lines line when first asked."""

    def __init__(self, text: str, document: str) -> None:
        self._document = document if document in ("json", "jsonl") else ""
        self._lines = text.splitlines(keepends=True) if self._document else []
        self._starts: list[int] = []
        self._parsed: dict[int, list[tuple[int, int, JsonPath]] | None] = {}
        self._text = text

    def _scalars(self, number: int) -> tuple[list[tuple[int, int, JsonPath]] | None, int]:
        if self._document == "jsonl":
            if number not in self._parsed:
                line = self._lines[number - 1] if number <= len(self._lines) else ""
                self._parsed[number] = _json_scalars(line.rstrip("\r\n"))
            return self._parsed[number], 0
        if 0 not in self._parsed:
            self._parsed[0] = _json_scalars(self._text)
            total = 0
            for line in self._lines:
                self._starts.append(total)
                total += len(line)
        offset = self._starts[number - 1] if number <= len(self._starts) else 0
        return self._parsed[0], offset

    def path(self, number: int, start: int) -> JsonPath | None:
        if not self._document:
            return None
        scalars, offset = self._scalars(number)
        if not scalars:
            return None
        at = offset + start
        index = bisect.bisect_right(scalars, at, key=lambda s: s[0]) - 1
        if index >= 0 and scalars[index][0] <= at < scalars[index][1]:
            return scalars[index][2]
        return None


#: One placed finding before its names are checked: kind, line, the field name before
#: the value, its JSON path and the value. Kept in memory only, never written.
PendingContext = tuple[str, int, "str | None", "JsonPath | None", str]


def _context_entry(
    kind: str, field_name: str | None, path: JsonPath | None, value: str, pieces: set[str]
) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if field_name is not None and kind not in _USERINFO_KINDS:
        entry["field"] = field_name if _safe_name(field_name, pieces) else WITHHELD_NAME
    if path is not None:
        entry["json_path"] = _render_path(path, pieces)
    entry.update(value_shape(decoded_forms(value)[-1]))
    return entry


def render_context(
    pending: list[PendingContext], keys: dict[tuple[str, int], set[str]], pieces: set[str]
) -> tuple[dict[tuple[str, int], list[str]], dict[tuple[str, int], list[dict[str, Any]]]]:
    """The key names and the safe context of each ``(kind, line)``, every name checked
    against ``pieces``: the pieces of every value flagged in the whole upload, so a
    value flagged in one file never shows as a name in another (#2209)."""
    context: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for kind, number, name, path, value in pending:
        entry = _context_entry(kind, name, path, value, pieces)
        listed = context.setdefault((kind, number), [])
        if entry not in listed:
            listed.append(entry)
    names = {
        where: sorted({n if _safe_name(n, pieces) else WITHHELD_NAME for n in found})
        for where, found in keys.items()
    }
    return names, context


@dataclass
class Redaction:
    """``redact``'s result: the text with every flagged value replaced, the findings as
    ``(kind, line)`` (never the value), and whether every value could be removed."""

    text: str
    findings: list[tuple[str, int]] = field(default_factory=list)
    complete: bool = True
    #: The key names of the key-value findings, by ``(kind, line)``: a key name is no
    #: secret, and a withheld run cannot be diagnosed without it. A name that shares a
    #: piece of a flagged value is ``WITHHELD_NAME``.
    keys: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    #: Each placed finding's safe context, by ``(kind, line)`` (#2209): the field name
    #: right before the value, its JSON path (with ``document``) and its shape
    #: (``value_shape``), never the value.
    context: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    #: What ``keys`` and ``context`` were built from, checked against this text's own
    #: values only; a caller that scans many files re-renders them with every file's
    #: values (``render_context``). In memory only: it holds the values.
    pending: list[PendingContext] = field(default_factory=list, repr=False)
    raw_keys: dict[tuple[str, int], set[str]] = field(default_factory=dict, repr=False)
    #: Each value under ``VALUE_PIECE`` characters the scan ended at a backslash or
    #: at a quote that does not close it, with what it was cut at (``password =
    #: Zq9",Xv7...`` gives ``Zq9"``, ``DB_PASSWORD=ab\\`` gives ``ab\\``; an embedded
    #: run, ``Zq9"Xv7...``, is part of the value since #2247): the rest of the value
    #: is unknown, so ``redact_flagged_values`` refuses a key, a string or a text that
    #: holds it (#2213, #2266). In memory only.
    cut: set[str] = field(default_factory=set, repr=False)

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
    document: str = "",
) -> Redaction:
    """Replace every value ``secret_findings`` (with ``emails``, also each email at a
    domain that is not an example one) flags in ``text`` by its ``redaction_marker``.

    A private-key block goes whole first (BEGIN through END; a raw block keeps its line
    breaks, so later line numbers hold). Then, per line, the flagged spans are merged
    and replaced outermost first: only the values change, never a whole line, so JSON
    stays valid. A value that cannot be placed, or a line still flagged afterwards,
    makes the result ``complete=False``. ``narrow_key_names`` as in ``secret_findings``.
    ``document`` (``"json"`` or ``"jsonl"``) adds each finding's JSON path to its context."""
    findings: list[tuple[str, int]] = key_material(text, session=session)
    complete = True

    def _block(m: re.Match[str]) -> str:
        findings.append(("Private Key", line_of(text, m)))
        return redaction_marker("Private Key") + "\n" * m.group(0).count("\n")

    unblocked = _KEY_BLOCK_RE.sub(_block, text)
    out: list[str] = []
    keys: dict[tuple[str, int], set[str]] = {}
    index = _JsonIndex(unblocked, document)
    # Each placed finding (``PendingContext``); the names are checked against every
    # flagged value once the scan is done.
    placed_all: list[PendingContext] = []
    cut: set[str] = set()
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
        columns = {(kind, span): key for kind, span, key in table}
        for kind, span in hits:
            if span is not None:
                # A value right before "@" is a URL's or a DSN's password: the name
                # before it is its user (``user:secret@host``), never shown.
                name = (
                    None
                    if body[span[1] : span[1] + 1] == "@"
                    else columns.get((kind, span)) or preceding_key(body, span[0])
                )
                value = body[span[0] : span[1]]
                placed_all.append((kind, number, name, index.path(number, span[0]), value))
                if len(value) < VALUE_PIECE and _cut_short(body, span):
                    cut.add(value + _cut_at(body, span))
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
    names, context = render_context(placed_all, keys, value_pieces(v for *_r, v in placed_all))
    return Redaction(
        "".join(out),
        sorted(set(findings), key=lambda f: (f[1], f[0])),
        complete,
        names,
        context,
        placed_all,
        keys,
        cut,
    )


def _cut_at(body: str, span: tuple[int, int]) -> str:
    """What the scan cut the value at ``span`` at: the escape after it (``\\"``, or a
    lone ``\\`` at the end), or the ``"``."""
    after = body[span[1] : span[1] + 2]
    return after if after.startswith("\\") else after[:1]


def _cut_short(body: str, span: tuple[int, int]) -> bool:
    """The value at ``span`` ends at a ``\\`` (an escape inside it), or at a ``"`` that
    does not close a quote opened right before it: the scan cut it short."""
    after = body[span[1] : span[1] + 1]
    before = body[span[0] - 1 : span[0]] if span[0] else ""
    return after == "\\" or (after == '"' and before not in ('"', "'"))


class _Automaton:
    """Every pattern a text contains, found in one pass over it (Aho-Corasick): the
    rewrite checks thousands of keys against thousands of flagged values in time
    linear in their total length (#2213). ``every``: also keep, at each node, every
    pattern that ends there (``matches``), not only the longest (``spans``)."""

    def __init__(self, patterns: dict[str, set[str]], every: bool = False) -> None:
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._out: list[set[str]] = [set()]
        # The length of the longest pattern that ends at each node (0: none).
        self._longest: list[int] = [0]
        # With ``every``: each pattern that ends at each node, as (length, tags).
        self._ending: list[list[tuple[int, frozenset[str]]]] = [[]]
        for pattern, tags in patterns.items():
            node = 0
            for char in pattern:
                nxt = self._goto[node].get(char)
                if nxt is None:
                    nxt = len(self._goto)
                    self._goto[node][char] = nxt
                    self._goto.append({})
                    self._fail.append(0)
                    self._out.append(set())
                    self._longest.append(0)
                    self._ending.append([])
                node = nxt
            self._out[node] |= tags
            self._longest[node] = len(pattern)
            if every:
                self._ending[node].append((len(pattern), frozenset(tags)))
        queue = list(self._goto[0].values())
        for node in queue:
            for char, nxt in self._goto[node].items():
                queue.append(nxt)
                back = self._fail[node]
                while back and char not in self._goto[back]:
                    back = self._fail[back]
                target = self._goto[back].get(char, 0)
                self._fail[nxt] = target if target != nxt else 0
                self._out[nxt] |= self._out[self._fail[nxt]]
                if not self._longest[nxt]:
                    self._longest[nxt] = self._longest[self._fail[nxt]]
                if every:
                    self._ending[nxt] = self._ending[nxt] + self._ending[self._fail[nxt]]

    def tags(self, text: str) -> set[str]:
        """The tags of every pattern ``text`` contains."""
        found: set[str] = set()
        node = 0
        for char in text:
            while node and char not in self._goto[node]:
                node = self._fail[node]
            node = self._goto[node].get(char, 0)
            if self._out[node]:
                found |= self._out[node]
        return found

    def spans(self, text: str) -> list[tuple[int, int, set[str]]]:
        """``(start, end, tags)`` of the longest pattern ending at each position of
        ``text`` that ends one, with the tags of every pattern ending there."""
        found: list[tuple[int, int, set[str]]] = []
        goto, fail, out, longest = self._goto, self._fail, self._out, self._longest
        node = 0
        for end, char in enumerate(text, start=1):
            while node and char not in goto[node]:
                node = fail[node]
            node = goto[node].get(char, 0)
            if longest[node]:
                found.append((end - longest[node], end, out[node]))
        return found

    def matches(self, text: str) -> list[tuple[int, int, frozenset[str]]]:
        """``(start, end, tags)`` of every pattern at every place in ``text`` (built
        with ``every``)."""
        found: list[tuple[int, int, frozenset[str]]] = []
        goto, fail, ending = self._goto, self._fail, self._ending
        node = 0
        for end, char in enumerate(text, start=1):
            while node and char not in goto[node]:
                node = fail[node]
            node = goto[node].get(char, 0)
            for length, tags in ending[node]:
                found.append((end - length, end, tags))
        return found


# One JSON escape: a surrogate pair, a ``\\u`` escape or a one-character escape.
_JSON_ESCAPE_RE = re.compile(
    r"\\(?:u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
    r"|u([0-9a-fA-F]{4})|([\"\\/bfnrt]))"
)
# An HTML character reference: named, decimal or hex (``&amp;``, ``&#87;``, ``&#x57;``).
_HTML_ENTITY = r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});"
# A Markdown backslash escape: a backslash before ASCII punctuation (``\\_``, ``\\*``).
_MARKDOWN_ESCAPE = r"\\[!-/:-@\[-`{-~]"
#: The escapes undone before matching, by ``document``: JSON escapes everywhere, and in
#: a Markdown or HTML file our renderers' own escaping too (#2266).
_ESCAPES_BY_DOCUMENT = {
    "markdown": re.compile(_JSON_ESCAPE_RE.pattern + "|" + _MARKDOWN_ESCAPE + "|" + _HTML_ENTITY),
    "html": re.compile(_JSON_ESCAPE_RE.pattern + "|" + _HTML_ENTITY),
}
_ONE_CHAR_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
#: How many escape levels the raw-text check undoes (a transcript nests tool output).
ESCAPE_LEVELS = 6


def _decode_escape(m: re.Match[str]) -> str:
    """The one character a ``_JSON_ESCAPE_RE`` match stands for."""
    if m.group(1):
        high, low = int(m.group(1), 16), int(m.group(2), 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))
    if m.group(3):
        return chr(int(m.group(3), 16))
    return _ONE_CHAR_ESCAPES[m.group(4)]


def _decode_any(m: re.Match[str]) -> str:
    """What a match of an ``_ESCAPES_BY_DOCUMENT`` pattern stands for: a JSON escape,
    an HTML character reference, or a Markdown escape's character."""
    raw = m.group(0)
    if raw.startswith("&"):
        return html.unescape(raw)
    json_escape = _JSON_ESCAPE_RE.fullmatch(raw)
    return _decode_escape(json_escape) if json_escape else raw[1:]


def _unescape_once(text: str) -> str:
    """``text`` with one level of JSON escapes undone (a quote, a backslash, a slash,
    ``b f n r t``, ``u`` escapes and surrogate pairs); an unknown escape is kept."""
    return _JSON_ESCAPE_RE.sub(_decode_escape, text)


@dataclass
class _Unescaped:
    """One level of escapes undone, and where each undone escape was: the offset of
    what it stands for in ``text`` (``at``, ``size`` characters) and its span in the
    text it was undone in (``starts``, ``ends``), in order."""

    text: str
    at: list[int]
    size: list[int]
    starts: list[int]
    ends: list[int]

    @classmethod
    def of(cls, text: str, document: str = "") -> _Unescaped:
        pattern = _ESCAPES_BY_DOCUMENT.get(document, _JSON_ESCAPE_RE)
        decode = _decode_escape if pattern is _JSON_ESCAPE_RE else _decode_any
        parts: list[str] = []
        at: list[int] = []
        size: list[int] = []
        starts: list[int] = []
        ends: list[int] = []
        cursor = length = 0
        for m in pattern.finditer(text):
            literal = text[cursor : m.start()]
            undone = decode(m)
            if undone == m.group(0):
                continue
            parts += [literal, undone]
            length += len(literal)
            at.append(length)
            size.append(len(undone))
            starts.append(m.start())
            ends.append(m.end())
            length += len(undone)
            cursor = m.end()
        parts.append(text[cursor:])
        return cls("".join(parts), at, size, starts, ends)

    def _escape_at(self, offset: int) -> int:
        """The index of the last escape that starts at or before ``offset``, or -1."""
        return bisect.bisect_right(self.at, offset) - 1

    def _outer_start(self, offset: int) -> int:
        k = self._escape_at(offset)
        if k < 0:
            return offset
        if offset < self.at[k] + self.size[k]:
            return self.starts[k]
        return self.ends[k] + offset - self.at[k] - self.size[k]

    def outer(self, start: int, end: int) -> tuple[int, int]:
        """The span, in the text this was undone from, of ``text[start:end]``: whole
        escapes only."""
        k = self._escape_at(end - 1)
        if k >= 0 and end - 1 < self.at[k] + self.size[k]:
            return self._outer_start(start), self.ends[k]
        return self._outer_start(start), self._outer_start(end - 1) + 1

    def widen(self, start: int, end: int) -> tuple[int, int]:
        """``start:end`` of the text this was undone from, widened so it cuts no
        escape in two."""
        k = bisect.bisect_right(self.starts, start) - 1
        if k >= 0 and start < self.ends[k]:
            start = self.starts[k]
        k = bisect.bisect_left(self.starts, end) - 1
        if k >= 0 and end < self.ends[k]:
            end = self.ends[k]
        return start, end


def _fold(text: str) -> str:
    """``text`` in lower case, one character for one, so offsets hold (a character
    whose lower case is longer is kept as it is)."""
    folded = text.lower()
    if len(folded) == len(text):
        return folded
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


def escape_levels(text: str) -> list[str]:
    """``text`` and each form of it with one more level of JSON escapes undone, up to
    ``ESCAPE_LEVELS``, until nothing changes."""
    forms = [text]
    for _level in range(ESCAPE_LEVELS):
        if "\\" not in forms[-1]:
            break
        undone = _unescape_once(forms[-1])
        if undone == forms[-1]:
            break
        forms.append(undone)
    return forms


def _levels(raw: str, document: str = "") -> Iterator[tuple[str, list[_Unescaped]]]:
    """``raw`` and each form of it with one more level of escapes undone (JSON escapes,
    and Markdown or HTML ones for such a ``document``), up to ``ESCAPE_LEVELS``, with
    the levels that lead back to ``raw``."""
    levels: list[_Unescaped] = []
    text = raw
    for depth in range(ESCAPE_LEVELS + 1):
        yield text, levels
        if depth == ESCAPE_LEVELS or ("\\" not in text and "&" not in text):
            return
        level = _Unescaped.of(text, document)
        if not level.at:
            return
        levels = [*levels, level]
        text = level.text


def _to_raw(levels: list[_Unescaped], start: int, end: int) -> tuple[int, int]:
    for level in reversed(levels):
        start, end = level.outer(start, end)
    return start, end


#: A run of this many characters a key shares with a flagged value is redacted when
#: it looks random (``_random_run``), #2213.
RANDOM_RUN = 8


def _random_run(run: str) -> bool:
    """A run that looks random rather than like a word: no whitespace, and a digit, a
    symbol, or a capital after its first character (#2266: ``" in the "`` is none)."""
    return not any(c.isspace() for c in run) and (
        any(not c.isalpha() for c in run) or any(c.isupper() for c in run[1:])
    )


def _looks_random(value: str) -> bool:
    """A value that looks random: ``RANDOM_RUN`` or more characters and a random run
    as a whole (``_random_run``). Only such a value is matched inside other text and
    gives pieces; any other is matched only as a whole token (#2266)."""
    return len(value) >= RANDOM_RUN and _random_run(value)


#: A value of this many characters or more with no whitespace is matched whole
#: anywhere, even when it looks like a word (``correcthorsebatterystaple``, #2266).
WHOLE_ANYWHERE = 12


def _random_by_case_only(run: str) -> bool:
    """A random run (``_random_run``) that is all letters: random only because of a
    capital (``ordPress``), so it is matched in its own case only (#2266)."""
    return all(c.isalpha() for c in run)


def _word_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _starts_a_token(text: str, start: int) -> bool:
    """``text[start:]`` has no letter, digit or ``_`` right before it."""
    return not (start > 0 and _word_char(text[start - 1]))


def _is_token(text: str, start: int, end: int) -> bool:
    """``text[start:end]`` has no letter, digit or ``_`` right before or after it."""
    return not (start > 0 and _word_char(text[start - 1])) and not (
        end < len(text) and _word_char(text[end])
    )


@dataclass
class _KeyMatcher:
    """The flagged values, prebuilt once per upload (``flagged_matcher``) for
    ``redact_flagged_values``. Each value is matched in every decoded form, in any
    case:

    - ``long``: a value that looks random (``_looks_random``) and each of its runs of
      ``RANDOM_RUN`` characters that looks random with a digit or a symbol, matched
      anywhere (inside a key, a string, an identifier); and a value of
      ``WHOLE_ANYWHERE`` or more characters with no whitespace, whole only.
    - ``exact``: each run, and each value under ``WHOLE_ANYWHERE`` characters, that
      looks random only because of a capital (``_random_by_case_only``: ``ordPress``
      of ``WordPress2024!``), matched anywhere but in its own case only.
    - ``tokens``: any other value of ``VALUE_PIECE`` or more characters (a word, a
      prose phrase, an all-digit value of any length), and a value random only by
      case in any case, matched only as a whole token.
    - ``short``: a value under ``VALUE_PIECE`` characters, matched only as a whole key
      or JSON string.
    - ``whole``: every form of every value, for a number, replaced only when it is one.
    - ``cut``: the short values the scan cut short, each with what it was cut at
      (``Redaction.cut``): a key that contains one, or a string or text that holds one
      with no letter, digit or ``_`` before it, is refused.
    - ``short_tokens``: the short values, as tokens, for text that cannot be parsed."""

    long: _Automaton
    exact: _Automaton
    tokens: _Automaton
    short: dict[str, set[str]]
    whole: dict[str, set[str]]
    cut: _Automaton
    short_tokens: _Automaton

    @classmethod
    def build(cls, flagged: Iterable[tuple[str, str]], cut: Iterable[str] = ()) -> _KeyMatcher:
        long: dict[str, set[str]] = {}
        exact: dict[str, set[str]] = {}
        tokens: dict[str, set[str]] = {}
        short: dict[str, set[str]] = {}
        whole: dict[str, set[str]] = {}
        for kind, value in set(flagged):
            for form in decoded_forms(value):
                folded = form.lower()
                if not folded:
                    continue
                whole.setdefault(folded, set()).add(kind)
                if len(folded) < VALUE_PIECE:
                    short.setdefault(folded, set()).add(kind)
                    continue
                spaced = any(c.isspace() for c in form)
                if form.isdigit() or (not _looks_random(form) and spaced):
                    tokens.setdefault(folded, set()).add(kind)
                    continue
                if len(form) >= WHOLE_ANYWHERE:
                    long.setdefault(folded, set()).add(kind)
                elif not _looks_random(form):
                    tokens.setdefault(folded, set()).add(kind)
                    continue
                elif _random_by_case_only(form):
                    exact.setdefault(form, set()).add(kind)
                    tokens.setdefault(folded, set()).add(kind)
                else:
                    long.setdefault(folded, set()).add(kind)
                if not _looks_random(form):
                    continue
                for i in range(len(form) - RANDOM_RUN + 1):
                    run = form[i : i + RANDOM_RUN]
                    if not _random_run(run):
                        continue
                    if _random_by_case_only(run):
                        exact.setdefault(run, set()).add(kind)
                    else:
                        long.setdefault(run.lower(), set()).add(kind)
        cuts = {form.lower(): {"cut"} for value in cut for form in decoded_forms(value) if form}
        return cls(
            _Automaton(long),
            _Automaton(exact),
            _Automaton(tokens, every=True),
            short,
            whole,
            _Automaton(cuts, every=True),
            _Automaton(short, every=True),
        )

    @staticmethod
    def _forms(raw_key: str) -> list[str]:
        return [form.lower() for form in decoded_forms(raw_key)]

    @staticmethod
    def _token_hits(automaton: _Automaton, folded: str) -> list[tuple[int, int, frozenset[str]]]:
        return [m for m in automaton.matches(folded) if _is_token(folded, m[0], m[1])]

    def kind_of(self, raw_key: str) -> str | None:
        """The kind of the flagged value the key holds (in its raw text or any decoded
        form of it, in any case), or ``None``. A key that holds values of several kinds
        is named by the first kind in sorted order, so the answer is stable."""
        found: set[str] = set()
        for form in decoded_forms(raw_key):
            folded = form.lower()
            found |= self.short.get(folded, set()) | self.long.tags(folded)
            found |= self.exact.tags(form)
            for _s, _e, tags in self._token_hits(self.tokens, folded):
                found |= tags
        return min(found) if found else None

    def number_kind(self, number: str) -> str | None:
        """The kind of the flagged value a JSON number is, whole, or ``None``."""
        kinds = self.whole.get(number.lower())
        return min(kinds) if kinds else None

    def holds_a_cut_value(self, raw_key: str) -> bool:
        """Whether the key holds a value the scan cut short, in any form and case."""
        return any(self.cut.tags(folded) for folded in self._forms(raw_key))

    def holds_a_cut_token(self, raw: str, document: str = "") -> bool:
        """Whether a string or a text holds a value the scan cut short with what it was
        cut at (``Zq9"``, ``ab\\``), with no letter, digit or ``_`` before it, in its
        raw text or with escapes undone at any level, in any case: a bare ``a`` or
        ``ab`` withholds nothing (#2266)."""
        for text, _levels_back in _levels(raw, document):
            folded = _fold(text)
            if any(_starts_a_token(folded, m[0]) for m in self.cut.matches(folded)):
                return True
        return False

    def short_kind(self, raw: str) -> str | None:
        """The kind of the value under ``VALUE_PIECE`` characters ``raw`` is whole, in
        its raw text or any decoded form and in any case, or ``None``."""
        found: set[str] = set()
        for folded in self._forms(raw):
            found |= self.short.get(folded, set())
        return min(found) if found else None

    def value_spans(self, raw: str, document: str = "") -> list[tuple[str, int, int]]:
        """``(kind, start, end)`` of each place ``raw`` holds a ``long`` pattern, or a
        ``tokens`` one as a whole token, in its raw text or with escapes undone at any
        level (``_levels``), in any case: overlapping places folded into one
        (``_merge``), each widened to whole escapes, so replacing it keeps every escape
        level valid. A place inside an existing ``[REDACTED:<kind>]`` marker is left
        alone."""
        hits: list[tuple[str, tuple[int, int]]] = []
        first: _Unescaped | None = None
        for text, levels in _levels(raw, document):
            folded = _fold(text)
            found = [(s, e, set(t)) for s, e, t in self._token_hits(self.tokens, folded)]
            found += self.long.spans(folded)
            found += self.exact.spans(text)
            for start, end, tags in found:
                hits.append((min(tags), _to_raw(levels, start, end)))
            first = first or (levels[0] if levels else None)
        if not hits:
            return []
        if first is not None:
            hits = [(kind, first.widen(*span)) for kind, span in hits]
        markers = [(m.start(), m.end()) for m in REDACTION_RE.finditer(raw)]
        if markers:
            starts = [m_start for m_start, _end in markers]

            def in_a_marker(start: int, end: int) -> bool:
                k = bisect.bisect_right(starts, start) - 1
                return k >= 0 and end <= markers[k][1]

            hits = [(kind, span) for kind, span in hits if not in_a_marker(*span)]
        return _merge(hits)

    def in_raw_text(self, text: str) -> bool:
        """Whether ``text`` holds a flagged value, in any case, as it is or with every
        JSON escape undone at every level (``escape_levels``): a ``long`` pattern
        anywhere, any other value as a token."""
        for form in escape_levels(text):
            folded = _fold(form)
            if self.long.tags(folded) or self.exact.tags(form):
                return True
            if self._token_hits(self.tokens, folded) or self._token_hits(self.short_tokens, folded):
                return True
        return False


def flagged_matcher(flagged: Iterable[tuple[str, str]], cut: Iterable[str] = ()) -> _KeyMatcher:
    """The matcher ``redact_flagged_values`` uses, built once for the whole upload:
    ``flagged`` is every ``(kind, value)`` the scan flagged, ``cut`` the short values it
    cut short (``Redaction.cut``)."""
    return _KeyMatcher.build(flagged, cut)


#: Why ``redact_flagged_values`` refused a file (#2213): the text its manifest finding names.
UNPARSED_FLAGGED = "flagged value in JSON that cannot be parsed"
CUT_FLAGGED = "holds a flagged value cut short"


@dataclass
class KeyRewrite:
    """``redact_flagged_values``'s result: the rewritten text and how many places it
    replaced, or ``None`` and the reason (``UNPARSED_FLAGGED``, ``CUT_FLAGGED``) when
    the file must be withheld."""

    text: str | None
    reason: str | None = None
    replaced: int = 0


def jsonl_lines(text: str) -> list[str]:
    """The lines of a JSON Lines file: split at ``\\n`` only, so a raw U+2028 inside a
    string is no break. The one rule the key rewrite and the validity check share."""
    return text.split("\n")


def _replaced(text: str, edits: Iterable[tuple[int, int, str]]) -> str:
    """``text`` with each ``(start, end, replacement)`` of ``edits`` (none overlapping)
    applied."""
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(edits):
        parts += [text[cursor:start], replacement]
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _value_edits(
    document: str,
    scalars: list[tuple[int, int, JsonPath]],
    keys: list[JsonKey],
    matcher: _KeyMatcher,
) -> list[tuple[int, int, str]] | None:
    """The edits that replace each flagged value inside the string values of
    ``document`` (in place, ``_KeyMatcher.value_spans``; a value under
    ``VALUE_PIECE`` characters only as the whole string) and each number that is one,
    whole (by a quoted marker). Keys are left to the key rewrite. ``None`` when a
    string holds a value the scan cut short."""
    key_starts = {key[0] for key in keys}
    edits: list[tuple[int, int, str]] = []
    for start, end, _path in scalars:
        if start in key_starts:
            continue
        if document[start] != '"':
            kind = matcher.number_kind(document[start:end])
            if kind is not None:
                edits.append((start, end, '"' + redaction_marker(kind) + '"'))
            continue
        content = document[start + 1 : end - 1]
        if matcher.holds_a_cut_token(content):
            return None
        kind = matcher.short_kind(content)
        if kind is not None:
            edits.append((start + 1, end - 1, redaction_marker(kind)))
            continue
        for kind, s, e in matcher.value_spans(content):
            edits.append((start + 1 + s, start + 1 + e, redaction_marker(kind)))
    return edits


def _redact_in(document: str, matcher: _KeyMatcher) -> KeyRewrite:
    """``document`` with each key that holds a flagged value replaced, and each flagged
    value inside its string values (``_value_edits``). Refused when it is no JSON this
    parser reads (a ``NaN``, nesting past its depth, a cut line, a byte-order mark) and
    holds a flagged value, or when a key or a string holds a value the scan cut short:
    it must not ship."""
    keys: list[JsonKey] = []
    scalars = _json_scalars(document, keys)
    if scalars is None:
        if matcher.in_raw_text(document):
            return KeyRewrite(None, UNPARSED_FLAGGED)
        return KeyRewrite(document)
    if any(matcher.holds_a_cut_value(document[key[0] + 1 : key[1] - 1]) for key in keys):
        return KeyRewrite(None, CUT_FLAGGED)
    edits = _value_edits(document, scalars, keys, matcher)
    if edits is None:
        return KeyRewrite(None, CUT_FLAGGED)
    by_object: dict[int, list[JsonKey]] = {}
    for key in keys:
        by_object.setdefault(key[3], []).append(key)
    for members in by_object.values():
        names = [name for _s, _e, name, _o in members]
        counts: dict[str, int] = {}
        for name in names:
            counts[name] = counts.get(name, 0) + 1
        bases: list[str | None] = []
        for start, end, name, _opening in members:
            kind = matcher.kind_of(document[start + 1 : end - 1])
            marker = REDACTION_RE.search(name)
            if kind is not None:
                bases.append(redaction_marker(kind))
            elif marker and counts[name] > 1:
                # Two values this file's own scan redacted inside keys of one object
                # left the same marker twice: each gets its own.
                bases.append(marker.group(0))
            else:
                bases.append(None)
        # A kept key, an existing marker among them, is never reused.
        taken = {name for name, base in zip(names, bases, strict=True) if base is None}
        numbers: dict[str, int] = {}
        for (start, end, _name, _opening), base in zip(members, bases, strict=True):
            if base is None:
                continue
            number = numbers.get(base, 1)
            marker_text = base if number == 1 else f"{base[:-1]}-{number}]"
            while marker_text in taken:
                number += 1
                marker_text = f"{base[:-1]}-{number}]"
            numbers[base] = number + 1
            taken.add(marker_text)
            edits.append((start, end, '"' + marker_text + '"'))
    if not edits:
        return KeyRewrite(document)
    return KeyRewrite(_replaced(document, edits), replaced=len(edits))


def redact_flagged_values(
    text: str,
    document: str,
    flagged: Iterable[tuple[str, str]] = (),
    cut: Iterable[str] = (),
    *,
    matcher: _KeyMatcher | None = None,
) -> KeyRewrite:
    """``text`` (a JSON document, ``document="json"``, JSON Lines, ``"jsonl"``, lines
    as ``jsonl_lines``, or plain text: ``"markdown"``, ``"html"`` or any other
    ``document``) with each flagged value it holds replaced: in JSON each object key
    that holds one (#2213) and each one inside a string value, in plain text each one
    anywhere (#2243). ``flagged`` is every ``(kind, value)`` the scan flagged anywhere
    in the upload, so a value redacted in one file is redacted wherever another file
    holds it; ``cut`` the short values it cut short (``Redaction.cut``). ``matcher``,
    built once per upload by ``flagged_matcher``, replaces both.

    What a value matches (#2266, ``_KeyMatcher``): a value that looks random
    (``_looks_random``: 8 or more characters, no whitespace, and a digit, a symbol or a
    capital after its first character), and each random run of ``RANDOM_RUN`` of its
    characters (``Wn6Rt9Kc``, ``EXAMPLEK``), anywhere; a run, or a value under
    ``WHOLE_ANYWHERE`` characters, random only because of a capital (``ordPress``) in
    its own case only; a value of ``WHOLE_ANYWHERE`` or more characters with no
    whitespace, whole, anywhere; any other value of ``VALUE_PIECE`` or more characters
    (``admin``, ``wordpress``, an all-digit value of any length, a phrase) only as a
    whole token, with no letter, digit or ``_`` next to it, so ``wp_wc_admin_notes``
    and ``root_table`` are kept; a shorter value only as a whole key or JSON string. A
    number is replaced, by a quoted marker, only when the whole number is the value.
    Matching is otherwise case-insensitive, in the raw text and with JSON
    escapes undone at any level, and in a Markdown or HTML file with its backslash
    escapes and character references undone too; percent-encoding and base64 are not
    undone.

    A string value or plain text has each place replaced in place by the marker,
    widened to whole escapes, an existing marker left alone, so the JSON stays valid
    (``_KeyMatcher.value_spans``). A key that holds a match becomes the value's
    ``redaction_marker``; the manifest's names keep the stricter ``VALUE_PIECE`` rule
    (``_safe_name``). Keys of one object that would end up alike, this file's own
    in-key markers included, are numbered in document order (``[REDACTED:<kind>-2]``),
    so the JSON stays valid and loses no member.

    Refused (``KeyRewrite(None, reason)``: withhold the file): a document or line this
    parser cannot read that holds a flagged value, as it is or with every JSON escape
    undone (``UNPARSED_FLAGGED``), and a key that holds, or a string or plain text that
    holds with no word character before it, a value the scan cut short together with
    what it was cut at (``CUT_FLAGGED``)."""
    if matcher is None:
        flagged = list(flagged)
        if not flagged:
            return KeyRewrite(text)
        matcher = flagged_matcher(flagged, cut)
    if document == "json":
        return _redact_in(text, matcher)
    if document != "jsonl":
        if matcher.holds_a_cut_token(text, document):
            return KeyRewrite(None, CUT_FLAGGED)
        spans = matcher.value_spans(text, document)
        edits = [(s, e, redaction_marker(k)) for k, s, e in spans]
        return KeyRewrite(_replaced(text, edits), replaced=len(edits))
    lines: list[str] = []
    replaced = 0
    for line in jsonl_lines(text):
        rewrite = _redact_in(line, matcher)
        if rewrite.text is None:
            return rewrite
        lines.append(rewrite.text)
        replaced += rewrite.replaced
    return KeyRewrite("\n".join(lines), replaced=replaced)


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
