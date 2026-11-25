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

Every scan is strict by default: any value under a credential key name but an allowed
one is a finding (the collector's routine bodies, #1933). The CI scans of model-written
text (``ci/llm/private_scan.py``: the e2e-llm upload, the replay fixtures) pass
``narrow_key_names=True`` (#2179): an unquoted value after a ``token`` or ``api_key``
name with ``key:`` or in a Markdown table then counts only when it looks like a secret
(``_looks_like_secret``), so design prose ("Validate a token: GetItem ...") is no
finding. Every other key name (password, secret, ``secret_key``, ``private_key``,
``access_key`` ...), quoted values, ``key=value`` assignments and JSON keys stay strict.

A finding names its kind and line, never the value. A designer's example values are
allowed exactly as ``src/shared/example_values.py`` defines them (#2053).

The key-value scan runs in two passes, and every result is their union (#2595): main's
rules as they were (``src/shared/secret_scan_baseline.py``), then this module's rules,
which read a value whole where main's cut it short (#2247, #2260), YAML block scalars,
values inside arrays and objects and unclosed quotes (#2263), and SQL quoting in the
source dialect and dynamic SQL (#2262). A baseline hit is added wherever a hit of this
module's rules of the same kind does not already cover it, and ``credential_spans``
merges main's spans in, so the newer rules can only add masking or findings.
``tests/property/test_secret_scan_differential.py`` checks it.
"""

from __future__ import annotations

import contextlib
import importlib
import re
import threading
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator
from typing import Any, NamedTuple

from src.shared import secret_scan_baseline as _baseline
from src.shared.example_values import (
    is_documentation_address,
    is_example_domain,
    is_placeholder,
    public_ipv4,
    public_ipv6,
)
from src.shared.sql_spans import span_dialect


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
    if _scan_line is None:
        return []
    if _denylist is not None and not any(rx.search(line) for rx in _denylist):
        return []
    return list(_scan_line(line))


# One entry per open scan session: detect-secrets' settings are entered by the
# outermost one and kept for every scan inside it (#2098).
_open_sessions: list[None] = []
# detect-secrets' settings are process-wide: the thread in a session holds this lock
# (re-entrant, so its nested sessions and scans go on), and a scan in any other thread
# waits for the session to end instead of running on settings it may lose mid-scan.
_session_lock = threading.RLock()
# Every pattern the session's plugins look for, or ``None`` when a plugin may find a
# credential some other way: a line none of them matches holds nothing the plugins
# report, so detect-secrets' per-plugin dispatch is skipped for it (#2098).
_denylist: tuple[re.Pattern[str], ...] | None = None


def _plugin_denylist() -> tuple[re.Pattern[str], ...] | None:
    """The denylist patterns of the session's plugins, when every plugin reports only
    what its patterns match: a ``RegexBasedDetector`` with the inherited
    ``analyze_line``, and the inherited ``analyze_string`` or the JWT plugin's, which
    only filters those matches. ``None`` (no skip, the full scan) otherwise, and on
    any failure: the guard never turns a plugin off."""
    try:
        base = importlib.import_module("detect_secrets.plugins.base")
        jwt = importlib.import_module("detect_secrets.plugins.jwt")
        plugins = importlib.import_module("detect_secrets.settings").get_plugins()
        own_matches_only = (
            base.RegexBasedDetector.analyze_string,
            jwt.JwtTokenDetector.analyze_string,
        )
        patterns: list[re.Pattern[str]] = []
        for plugin in plugins:
            cls = type(plugin)
            if not (
                isinstance(plugin, base.RegexBasedDetector)
                and cls.analyze_line is base.BasePlugin.analyze_line
                and any(cls.analyze_string is method for method in own_matches_only)
            ):
                return None
            patterns += plugin.denylist
        return tuple(patterns)
    except Exception:  # noqa: BLE001 - a failed guard means the full scan, never less
        return None


@contextlib.contextmanager
def scan_session() -> Iterator[None]:
    """Keep detect-secrets' settings for every scan inside: building them costs more
    than scanning a short text, so a caller that scans many texts (the collector, one
    scan per routine body) opens one session around them all (#2098). Nested
    sessions and the scans inside reuse the outermost one's settings; a scan in
    another thread waits until the session ends (every scan opens a session)."""
    global _denylist
    with _session_lock:
        if _open_sessions or _transient_settings is None:
            _open_sessions.append(None)
            try:
                yield
            finally:
                _open_sessions.pop()
            return
        with _transient_settings({"plugins_used": _PLUGINS, "filters_used": []}):
            denylist = _plugin_denylist()
            _open_sessions.append(None)
            try:
                _denylist = denylist
                yield
            finally:
                _denylist = None
                _open_sessions.pop()


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
# A value in quotes, at any escape level: its first piece, up to the next quote or the
# end of the line (``_quoted_end`` finds where it really ends; an opening quote that
# never closes runs to the end of the line, #2263). A quote run only starts where a
# backslash run starts (``(?<!\\)``), so a long backslash run is read once, never once
# per backslash (#2263).
_QUOTED = r"(?P<qv>(?<!\\)\\*[\"'])(?P<v>.*?)(?=(?<!\\)\\*[\"']|$)"
_PAIR_RES = (
    # "key": "value" / \"key\": \"value\" / "key": 123 (JSON, any escape level)
    # (a SQL cast, 'name'::type, is no pair; nor is a key whose value is an object or
    # an array, ``"DynamoDBKey": {``: an array's scalars are values, ``_array_hits``)
    re.compile(
        rf"(?<!\\)\\*[\"']{_KEY_NAME}\\*[\"']\s*:(?!:)\s*(?:{_QUOTED}"
        rf"|(?P<u>\[REDACTED:[a-z0-9-]+\]|(?![\[{{])[^\s,}}\]\"'\\]+))",
        re.I,
    ),
    # key=value, key: value (YAML or prose), **key**: value, `key`=value
    re.compile(
        rf"(?:\*\*|`)?{_KEY_NAME}(?:\*\*|`)?\s*(?P<sep>=|:(?=\s))\s*`?"
        rf"(?:(?:(?P<sp>[Qq](?='[^\w\s'])|[NnEeBbXx]|[Uu]&|_[A-Za-z0-9]+)(?=[\"']))?{_QUOTED}"
        rf"|(?P<u>[^\s\"'`\\]+))",
        re.I,
    ),
    # | key | value |
    re.compile(rf"\|\s*{_KEY_NAME}\s*\|\s*(?P<u>[^|\s][^|]*?)\s*(?=\|)", re.I),
)
# Every quote with the backslashes right before it (from the start of the run only).
_QUOTE_RUN_RE = re.compile(r"(?<!\\)\\*[\"']")
# A run of quotes, backslashes and backticks inside an unquoted value; the piece after.
_VALUE_RUN_RE = re.compile(r"[\\\"'`]+")
_VALUE_PIECE_RE = re.compile(r"[^\s\"'`\\]+")
# A quote token in a run: its backslashes and the quote; a backtick; trailing backslashes.
_RUN_TOKEN_RE = re.compile(r"\\*[\"']|`|\\+")


def _unquoted_end(line: str, m: re.Match[str], end: int, index: _LineIndex) -> tuple[int, bool]:
    """Where the unquoted value the pair rule ended at ``end`` really ends, and whether
    the scan can tell (#2247, #2260). A run of quotes, backslashes and backticks inside
    it is part of it, except that the value ends before:

    - a quote that closes a string opened before the key, at that string's escape
      level (``conn("password=...")``, ``{"a":"password=...","b":1}``,
      ``EXEC('...PASSWORD=...'+@x+'')``), followed by what may follow a closed string
      (``_CLOSE_CONTEXT_RE``);
    - a backtick that closes a Markdown code span opened before the value;
    - a JSON escape of a line break (``\\n``, ``\\r``, ``\\u000a``, ``\\u000d``, at any
      escape level) when a string is open around the key: a ``.env`` file read into a
      transcript keeps its other settings. Outside any string it is a backslash in
      the value.

    A run that closes nothing and is followed by whitespace may end the value or be
    part of it (``Zq9" Xv7...``): the scan cannot tell, so the value is placed up to
    the run and marked uncertain, and the file is withheld."""
    key_start, value_start = m.start(), m.start("u")
    in_string = index.any_string_open(key_start)
    code_span = index.backticks_before(value_start) % 2 == 1
    while end < len(line) and not line[end].isspace():
        run = _VALUE_RUN_RE.match(line, end)
        if run is None:
            end = _VALUE_PIECE_RE.match(line, end).end()  # type: ignore[union-attr]
            continue
        for token in _RUN_TOKEN_RE.finditer(line, run.start(), run.end()):
            text = token.group(0)
            if text == "`":
                if code_span:
                    return token.start(), True
            elif text[-1] in "\"'":
                closes = _CLOSE_CONTEXT_RE.match(line, token.end()) is not None
                # ``\\'`` closes a different string in each backslash reading, so it never
                # ends a value where a setting moves that reading (#2595).
                escaped = text[-1] == "'" and len(text) > 1 and not index.literal_backslash
                if closes and not escaped and index.string_open(key_start, text[-1], len(text) - 1):
                    # A ``'`` keeps its backslashes in the value (the longer reading).
                    level = 0 if text[-1] == "'" else _level(len(text) - 1)
                    return token.end() - 1 - ((1 << level) - 1), True
            elif in_string and _LINE_BREAK_ESCAPE_RE.match(line, token.start()):
                return token.start(), True
        end = run.end()
        if end < len(line) and line[end].isspace():
            return end, False
    return end, True


def _quoted_end(line: str, m: re.Match[str], index: _LineIndex) -> int:
    """Where the quoted value of ``m`` really ends (#2247, #2260): before its own closing
    quote, the opening quote's character at the opening's escape level (``_level``: at
    level 0 an even run of backslashes, ``"Zq9\\"Xv7"`` holds ``\\"``; at level 1, a
    JSON string inside a JSON string, one, five, ...), followed by what may follow a
    closed string (``_CLOSE_CONTEXT_RE``): ``'Zq9'Xv7'`` holds its middle quote, and so
    does SQL's doubled ``''``. A quote of the other kind, or of its own kind at another
    level, is part of the value, unless it closes a string opened before the key: then
    the value ends there, and never runs past the string around it. A value no quote
    closes runs to the end of the line (#2263)."""
    prefix = (m.groupdict().get("sp") or "").lower()
    if prefix == "q":
        # Oracle's ``q'[...]'``: it ends at its closing delimiter and a quote.
        delim = line[m.end("qv") : m.end("qv") + 1]
        closer = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(delim, delim) + "'"
        at = line.find(closer, m.end("qv") + 1)
        return len(line) if at == -1 else at + 1
    escapes = prefix == "e"
    if escapes:
        return _quoted_end_reading(line, m, index, "")
    if index.literal_backslash:
        return _quoted_end_reading(line, m, index, "'")
    # Where a setting moves a backslash's meaning, or no dialect is known, both
    # readings, and the value is the longer one (#2262).
    return max(_quoted_end_reading(line, m, index, ""), _quoted_end_reading(line, m, index, "'"))


def _quoted_end_reading(line: str, m: re.Match[str], index: _LineIndex, literal: str) -> int:
    """``_quoted_end`` in one reading: a backslash before a quote in ``literal`` is an
    ordinary character (SQL Server, Oracle, MySQL with ``NO_BACKSLASH_ESCAPES``,
    PostgreSQL's standard strings), and an escape before any other quote."""

    def level_of(char: str, backslashes: int) -> int:
        return 0 if char in literal else _level(backslashes)

    opener = m.group("qv")
    quote = opener[-1]
    level = level_of(quote, len(opener) - 1)
    key_start = m.start()
    pos = m.start("v")
    # A search that reached the end of the line from an earlier start reaches it from
    # here too: repeated unclosed openers cost linear time (#2595).
    memo = (literal, quote, level, index.state_key(key_start))
    if index.unclosed_from.get(memo, len(line) + 1) <= pos:
        return len(line)
    first = pos
    while (run := _QUOTE_RUN_RE.search(line, pos)) is not None:
        backslashes, char = run.end() - run.start() - 1, line[run.end() - 1]
        found = level_of(char, backslashes)
        if char == quote and found == level:
            doubled = line[run.end() : run.end() + 1] == quote
            if doubled and (not backslashes or char in literal):
                pos = run.end() + 1  # doubled: SQL's ``''``
                continue
            if _CLOSE_CONTEXT_RE.match(line, run.end()):
                return run.end() - 1 - ((1 << level) - 1)
        elif (
            not (char == "'" and backslashes and not index.literal_backslash)
            and index.string_open(key_start, char, backslashes)
        ) and _CLOSE_CONTEXT_RE.match(line, run.end()):
            return run.end() - 1 - ((1 << found) - 1)
        pos = run.end()
    index.unclosed_from[memo] = min(first, index.unclosed_from.get(memo, first))
    return len(line)


# A closing bracket and the bracket it closes.
_OPENERS = {")": "(", "]": "[", "}": "{"}


class _LineIndex:
    """What the value-end rules count on one line, found on first use, so a line with
    many pairs is read once (#2263): where each bracket is, and which strings are open
    before an offset. Each quote toggles the string of its kind at its escape level
    (``_level``), except an apostrophe between two letters (``don't``)."""

    def __init__(self, line: str, literal_backslash: bool = False) -> None:
        self.line = line
        #: The dialect reads a backslash before ``'`` as an ordinary character (SQL
        #: Server, Oracle): such a quote is at level 0 whatever precedes it.
        self.literal_backslash = literal_backslash
        self._quote_ends: list[int] | None = None
        self._quote_states: list[tuple[int, ...]] = []
        # Each reading of an unclosed quoted value: the earliest start from which the
        # search reached the end of the line (``_quoted_end_reading``).
        self.unclosed_from: dict[tuple[object, ...], int] = {}
        self._backticks: list[int] | None = None

    def _readings(self) -> tuple[tuple[str, bool], ...]:
        """The readings of a line: of a backslash before ``'`` (``literal`` where the
        dialect fixes it, SQL Server and Oracle, else both, since a setting moves it,
        #2262), each with and without a one-letter word before a quote read as a string
        prefix (``N'``: SQL, or ``B's``: prose; nothing tells them apart, #2595)."""
        backslash = ("literal",) if self.literal_backslash else ("escape", "literal")
        return tuple((reading, prefixes) for reading in backslash for prefixes in (False, True))

    def _open_strings(self, offset: int) -> tuple[int, ...]:
        """The strings open before ``offset`` in each reading (``_readings``)."""
        if self._quote_ends is None:
            self._quote_ends = []
            line = self.line
            states = [0 for _ in self._readings()]
            for run in _QUOTE_RUN_RE.finditer(line):
                at, backslashes = run.end() - 1, run.end() - run.start() - 1
                char = line[at]
                can_open: dict[bool, bool] = {}
                for i, (reading, prefixes) in enumerate(self._readings()):
                    bit = _string_bit(char, _reading_level(reading, char, backslashes))
                    if not states[i] & bit:
                        if prefixes not in can_open:
                            can_open[prefixes] = _can_open(line, run.start(), prefixes)
                        if not can_open[prefixes]:
                            continue  # ``admins'``, ``5"``: no string starts here (#2595)
                    elif char == "'" and line[at - 1 : at].isalpha():
                        if line[at + 1 : at + 2].isalpha() and not backslashes:
                            continue  # ``don't`` inside a string
                    states[i] ^= bit
                self._quote_ends.append(run.end())
                self._quote_states.append(tuple(states))
        i = bisect_right(self._quote_ends, offset)
        return self._quote_states[i - 1] if i else tuple(0 for _ in self._readings())

    def string_open(self, offset: int, char: str, backslashes: int) -> bool:
        """Whether the string a ``char`` quote after ``backslashes`` backslashes would
        close is open before ``offset`` in every reading: where the readings differ, no
        string is taken as open, so a value never ends early at that quote."""
        return all(
            state & _string_bit(char, _reading_level(reading, char, backslashes))
            for (reading, _), state in zip(
                self._readings(), self._open_strings(offset), strict=True
            )
        )

    def string_open_in_some_reading(self, offset: int, char: str, backslashes: int) -> bool:
        """``string_open`` in at least one reading: what adds a value (a nested
        doubled-quote value) asks this, so it finds the most."""
        return any(
            state & _string_bit(char, _reading_level(reading, char, backslashes))
            for (reading, _), state in zip(
                self._readings(), self._open_strings(offset), strict=True
            )
        )

    def any_string_open(self, offset: int) -> bool:
        """Whether some string is open before ``offset`` in every reading."""
        return all(self._open_strings(offset))

    def state_key(self, offset: int) -> tuple[int, ...]:
        return self._open_strings(offset)

    def backticks_before(self, offset: int) -> int:
        if self._backticks is None:
            self._backticks = [i for i, c in enumerate(self.line) if c == "`"]
        return bisect_left(self._backticks, offset)


# The one-letter string prefixes: ``N'``, ``E'``, ``B'``, ``X'``, ``Q'`` (``U&'`` follows
# ``&``, which opens anyway).
_PREFIX_LETTERS = frozenset("NnEeBbXxQq")


def _word_char(char: str) -> bool:
    return bool(char) and (char.isalnum() or char in "_$")


def _can_open(line: str, start: int, prefixes: bool = True) -> bool:
    """Whether a quote run starting at ``start`` can open a string: not right after a
    letter, a digit or ``_`` (a possessive ``admins'``, an inch mark ``5"``), unless,
    with ``prefixes``, that is a string prefix (``N'...``, MySQL's ``_utf8mb4'...``).
    The letters and digits before the quote are read once, as no other quote lies
    inside them."""
    if not _word_char(line[start - 1 : start]):
        return True
    if not prefixes:
        return False
    at = start
    while at > 0 and line[at - 1].isalnum():
        at -= 1
    word, before = line[at:start], line[at - 1 : at]
    if len(word) == 1 and word in _PREFIX_LETTERS and not _word_char(before) and before != "&":
        return True
    # ``_utf8mb4'``: an underscore that starts a word, then letters and digits.
    return bool(word) and before == "_" and not _word_char(line[at - 2 : at - 1])


def _reading_level(reading: str, char: str, backslashes: int) -> int:
    """The escape level of a quote in one reading: in ``literal``, a backslash before
    ``'`` is an ordinary character."""
    return 0 if reading == "literal" and char == "'" else _level(backslashes)


def _string_bit(char: str, level: int) -> int:
    return 1 << (2 * level + (char == "'"))


# What may follow a quote that closes a string: the end, whitespace, an operator or a
# separator, another quote or a backslash. A letter or a digit continues a value.
_CLOSE_CONTEXT_RE = re.compile(r"$|[\s,;:)\]}|+.`=<>&!\\\"']")


def _opens_no_value(line: str, m: re.Match[str], index: _LineIndex) -> bool:
    """The quote after ``key=`` closes a string open before the key
    (``'password=' + pw``, ``CONCAT('password=', @p)``), so it opens no value."""
    opener = m.group("qv")
    if opener[-1] == "'" and len(opener) > 1 and not index.literal_backslash:
        return False  # ``\\'``: see ``_unquoted_end``
    return index.string_open(m.start(), opener[-1], len(opener) - 1) and (
        _CLOSE_CONTEXT_RE.match(line, m.end("qv")) is not None
    )


# A YAML block scalar's indicator (``|``, ``>``, with a chomping and an indentation
# indicator in either order, after an optional tag such as ``!!str``), and what may
# follow it on its line: a comment.
_BLOCK_INDICATOR_RE = re.compile(r"(?:![^\s]*[ \t]+)?[|>](?:[1-9][-+]?|[-+][1-9]?)?(?=[\s\\]|$)")
_BLOCK_REST_RE = re.compile(r"\s*(?:#.*)?")
# A JSON escape of a line break, at any escape level.
_LINE_BREAK_ESCAPE_RE = re.compile(r"\\+(?:[nr]|u000[aAdD])")


def _block_scalar_hits(lines: list[str]) -> dict[int, list[tuple[str, tuple[int, int], str]]]:
    """The lines of each YAML block scalar under a credential key (``password: |``,
    ``"password": |``, ``password: !!str >``, then its more indented lines), by line
    index: each non-blank line is a value, unless it is a placeholder (#2263). The scan
    reads one line at a time, so the indicator alone was the value before. A block
    inside an earlier one is part of it, so it is not read again."""
    found: dict[int, list[tuple[str, tuple[int, int], str]]] = {}
    covered = -1
    for index, line in enumerate(lines):
        if index <= covered or ("|" not in line and ">" not in line):
            continue
        if _KEY_WORD_RE.search(line) is None:
            continue
        for m in (*_PAIR_RES[0].finditer(line), *_PAIR_RES[1].finditer(line)):
            if not _raw_block_indicator(m) or _PER_UNIT_RE.search(m.group("key")):
                continue
            indent = len(line) - len(line.lstrip())
            for at in range(index + 1, len(lines)):
                content = lines[at]
                if not content.strip():
                    continue
                start = len(content) - len(content.lstrip())
                if start <= indent:
                    break
                covered = max(covered, at)
                end = len(content.rstrip())
                if not _allowed_value(content[start:end]):
                    found.setdefault(at, []).append((_pair_kind(m), (start, end), m.group("key")))
    return found


def _block_end(m: re.Match[str]) -> int | None:
    """Where the block scalar indicator that is the value of ``m`` ends, or ``None``."""
    if m.re is _PAIR_RES[2] or m.groupdict().get("u") is None:
        return None
    if m.re is _PAIR_RES[1] and m.group("sep") != ":":
        return None
    found = _BLOCK_INDICATOR_RE.match(m.string, m.start("u"))
    return found.end() if found else None


def _block_indicator(m: re.Match[str]) -> bool:
    return _block_end(m) is not None


def _raw_block_indicator(m: re.Match[str]) -> bool:
    """``key: |`` with nothing but a comment after it: its value is on the lines
    below (``_block_scalar_hits``)."""
    end = _block_end(m)
    return end is not None and _BLOCK_REST_RE.fullmatch(m.string, end) is not None


def _flow_span(line: str, start: int) -> tuple[tuple[int, int], bool]:
    """The span of a YAML flow sequence or mapping that opens at ``start``
    (``[a, b]``, ``{current: b}``), and whether the scan can tell where it ends. A
    bracket inside quotes does not count. It ends at its closing bracket only when
    nothing but a comment follows on the line; otherwise, and when it does not close,
    it runs to the end of the line, so text after a bracket is never kept
    (``password: []<value>``, #2595). One that does not close is uncertain."""
    depth, at, quote = 0, start, ""
    while at < len(line):
        char = line[at]
        if quote:
            if char == "\\" and quote == '"':
                at += 2
                continue
            if char == quote:
                if quote == "'" and line[at + 1 : at + 2] == "'":
                    at += 2
                    continue
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if not depth:
                if _BLOCK_REST_RE.fullmatch(line, at + 1) is not None:
                    return (start, at + 1), True
                return (start, len(line)), True
        at += 1
    return (start, len(line)), False


# A credential key whose value is an array or an object (``"password": [``,
# ``"password": {``), at any escape level.
_JSON_CONTAINER_RE = re.compile(
    rf"(?<!\\)(?P<lead>\\*)[\"']{_KEY_NAME}\\*[\"']\s*:\s*(?P<open>[\[{{])", re.I
)
# Inside a container: whitespace (an escaped line break or tab too), a scalar.
_ARRAY_SPACE_RE = re.compile(r"(?:\s|\\+[nrt])+")
_ARRAY_SCALAR_RE = re.compile(r"[^\s,:\[\]{}\"'\\]+")


def _level(backslashes: int) -> int:
    """The escape level a quote after ``backslashes`` backslashes closes: the number of
    trailing one bits (``"`` 0, ``\\"`` 1, ``\\\\"`` 0, ``\\\\\\"`` 2)."""
    level = 0
    while backslashes & 1:
        backslashes >>= 1
        level += 1
    return level


def _string_close(line: str, pos: int, level: int, quote: str) -> tuple[int, int, bool]:
    """Where a JSON string opened before ``pos`` at ``level`` ends: the start and end
    of its closing quote, and ``True``. ``False`` when it does not close on the line:
    the end of the line, or the start of a ``"`` that closes an outer string."""
    while (run := _QUOTE_RUN_RE.search(line, pos)) is not None:
        char, found = line[run.end() - 1], _level(run.end() - run.start() - 1)
        if char == quote and found == level:
            return run.end() - 1 - ((1 << level) - 1), run.end(), True
        if char == '"' and found < level:
            return run.end() - 1 - ((1 << found) - 1), run.end(), False
        pos = run.end()
    return len(line), len(line), False


class _Container(NamedTuple):
    """What ``_container_values`` read: each value (line, start, end), where the walk
    stopped, and whether it read the whole container: ``False`` when it stopped at
    something it cannot read (a quote of another escape level, a stray backslash, a
    bracket that does not match, the end of an escaped line or of the string around
    it), so the rest of the container is unknown."""

    values: list[tuple[int, int, int]]
    stop: tuple[int, int]
    whole: bool


def _container_values(
    lines: list[str], index: int, pos: int, level: int, opener: str
) -> _Container:
    """Each string and scalar in the array or object that opens right before ``pos`` on
    line ``index``, at escape ``level``, at any depth: an array's items and an object's
    member values, never a member's key (#2263). A raw container (level 0) runs on over
    the lines below; an escaped one ends with its line."""
    values: list[tuple[int, int, int]] = []
    # Each open container: its bracket, and whether a member's key comes next.
    stack: list[list] = [[opener, opener == "{"]]  # type: ignore[type-arg]
    line = lines[index]
    whole = False
    while True:
        if not stack:
            whole = True
            break
        if pos >= len(line):
            if level:
                break
            if index + 1 >= len(lines):
                whole = True  # the text ends: nothing more to read
                break
            index += 1
            line, pos = lines[index], 0
            continue
        space = _ARRAY_SPACE_RE.match(line, pos)
        if space is not None:
            pos = space.end()
            continue
        char = line[pos]
        frame = stack[-1]
        if char in "\\\"'":
            quote = _QUOTE_RUN_RE.match(line, pos)
            if quote is None or _level(quote.end() - quote.start() - 1) != level:
                break
            start, after, closed = _string_close(line, quote.end(), level, line[quote.end() - 1])
            if frame[0] == "{" and frame[1]:
                frame[1] = False
            elif start > quote.end():
                values.append((index, quote.end(), start))
            if not closed:
                if start < len(line):
                    pos = start
                break
            pos = after
        elif char in "[{":
            stack.append([char, char == "{"])
            pos += 1
        elif char in "]}":
            if frame[0] != _OPENERS[char]:
                break
            stack.pop()
            pos += 1
        elif char == ",":
            frame[1] = frame[0] == "{"
            pos += 1
        elif char == ":":
            pos += 1
        else:
            scalar = _ARRAY_SCALAR_RE.match(line, pos)
            if scalar is None:
                break
            if frame[0] == "{" and frame[1]:
                frame[1] = False
            else:
                values.append((index, scalar.start(), scalar.end()))
            pos = scalar.end()
    return _Container(values, (index, pos), whole)


def _array_hits(lines: list[str]) -> dict[int, list[tuple[str, tuple[int, int] | None, str]]]:
    """Each string and scalar in an array or an object under a credential key
    (``"password": ["..."]``, ``"password": {"current": "..."}``), by line index
    (#2263): the key is no pair, its values are, at any depth. A JSON literal or short
    count is no value, as after a key; a placeholder is allowed. A walk that stops
    before its container closes (``_Container.whole``) masks the rest of that line and
    is uncertain (an unplaced hit, ``None``), so the file is withheld. A key inside a
    container an earlier key's walk read is not walked again, so the scan stays
    linear."""
    found: dict[int, list[tuple[str, tuple[int, int] | None, str]]] = {}
    walked = (-1, -1)
    for index, line in enumerate(lines):
        if ("[" not in line and "{" not in line) or _KEY_WORD_RE.search(line) is None:
            continue
        for m in _JSON_CONTAINER_RE.finditer(line):
            if (index, m.start()) < walked or _PER_UNIT_RE.search(m.group("key")):
                continue
            level = _level(len(m.group("lead")))
            container = _container_values(lines, index, m.end(), level, m.group("open"))
            walked = max(walked, container.stop)
            kind, key = _pair_kind(m), m.group("key")
            for at, start, end in container.values:
                value = lines[at][start:end]
                if value in _JSON_LITERALS or _COUNT_RE.fullmatch(value) or _allowed_value(value):
                    continue
                found.setdefault(at, []).append((kind, (start, end), key))
            if not container.whole:
                at, stop = container.stop
                rest = (stop, len(lines[at])) if stop < len(lines[at]) else None
                if rest is not None:
                    found.setdefault(at, []).append((kind, rest, key))
                found.setdefault(at, []).append((kind, None, key))
    return found


# A word every credential key name holds (each ``_PASSWORD_WORDS`` word holds ``pass``,
# ``pwd``, ``secret`` or ``contrase``; each ``_KEY_WORDS`` word ends in ``key``), with the
# same flags: a line without one has no pair, so the pair patterns are not run on it.
_KEY_WORD_RE = re.compile(r"pass|pwd|secret|contrase|token|key", re.I)
# A Markdown table cell that is a credential key name; a separator row.
_KEY_CELL_RE = re.compile(rf"\s*{_KEY_NAME}\s*", re.I)
# A whole credential key name, in any case (a named bind's name, #2248).
_KEY_NAME_RE = re.compile(_KEY_NAME, re.I)
_SEPARATOR_ROW_RE = re.compile(r"[\s|:-]*")
_JSON_LITERALS = frozenset({"null", "true", "false"})
# A short integer (a count or a size in a design), never a long number or a float.
_COUNT_RE = re.compile(r"\d{1,6}")
# A SQL bind parameter after ``=`` (``post_password = ?``) stands for a value: a known
# positional shape (``?``, ``%s``, ``$N``; ``:N`` / ``@N``, SQL Server's ``@P1`` and
# Oracle's ``:B1`` / ``:SYS_B_0`` with at most three digits), or a named one
# (``:name``, ``@name``, ``%(name)s``) whose name is a credential key name (#2248).
_BIND_RE = re.compile(r"\?|%s|\$\d+|[:@]\d{1,3}|@P\d{1,3}|:B\d{1,3}|:SYS_B_\d{1,3}", re.I)
_NAMED_BIND_RE = re.compile(
    r"[:@](?P<name>[A-Za-z_][A-Za-z0-9_]*)|%\((?P<py>[A-Za-z_][A-Za-z0-9_]*)\)s"
)
# A bare ``key`` (``token = :key``) or ``pw`` names a bind too.
_BARE_BIND_NAMES = frozenset({"key", "pw"})
# A ``.env`` line (``DB_PASSWORD=...``, ``export ...``) or a connection string's
# ``;Password=...``: an assignment with no space around ``=``, at column 0 of a line
# or after ``;``. Its value is a literal, never a bind. An indented one is SQL (a
# ``SET`` list written one column per line: ``\t\tPasswordResetToken=@token,``).
_ENV_START_RE = re.compile(r"(?:export\s+)?")
# A column note in prose: `` `table.column`: varchar(255) ``.
_SQL_TYPE_RE = re.compile(
    r"(?:var)?char|(?:tiny|medium|long)?text|(?:tiny|small|medium|big)?int(?:eger)?|decimal"
    r"|numeric|float|double|real|bool(?:ean)?|date(?:time)?|time(?:stamp(?:tz)?)?|"
    r"(?:long)?blob|(?:var)?binary|jsonb?|uuid|(?:big)?serial|bytea|enum",
    re.I,
)
_SQL_TYPE_VALUE_RE = re.compile(rf"(?:{_SQL_TYPE_RE.pattern})(?:\(\d+(?:,\s*\d+)?\))?[,:]?", re.I)


def _key_value_assignment(m: re.Match[str]) -> bool:
    """``key=value`` with no space around ``=``, starting a ``.env`` line or following a
    connection string's ``;`` (``_ENV_START_RE``, column 0 only): not SQL, so no bind."""
    line, key, sep = m.string, m.start("key"), m.start("sep")
    if line[sep - 1 : sep].isspace() or line[sep + 1 : sep + 2].isspace():
        return False
    return line[key - 1 : key] == ";" or _ENV_START_RE.fullmatch(line, 0, key) is not None


def _bind_parameter(m: re.Match[str], value: str) -> bool:
    """``key = <bind>``, unquoted after ``=`` in SQL, the bind as the whole value: a
    known positional shape (``_BIND_RE``), or a named ``:name`` / ``@name`` /
    ``%(name)s`` whose name, in any case, is itself a credential key name (a full match
    of ``_KEY_NAME``: ``@ApiKey``, ``:NEW_PASSWORD``, ``@passwordResetToken``) or a bare
    ``key`` / ``pw``, with no digit in it (#2248). ``@@name`` (a system variable), a quoted string, a
    ``key: value`` line, a ``.env`` line and a connection string stay values."""
    if m.groupdict().get("sep") != "=" or _key_value_assignment(m):
        return False
    if _BIND_RE.fullmatch(value):
        return True
    named = _NAMED_BIND_RE.fullmatch(value)
    if named is None:
        return False
    name = named.group("name") or named.group("py")
    if any(c.isdigit() for c in name):
        return False  # ``@Summer2024Password``, ``@hunter2_pwd``: a value, not a name
    return name.lower() in _BARE_BIND_NAMES or _KEY_NAME_RE.fullmatch(name) is not None


# A column reference that is a whole value: one to four dot-joined parts, each a bare
# identifier, a ``[bracketed]`` one or a ``"double-quoted"`` one (#2253).
_IDENT_PART = r'[A-Za-z_]\w*|\[[A-Za-z_]\w*\]|"[A-Za-z_]\w*"'
_COLUMN_REF_RE = re.compile(rf"(?:(?:{_IDENT_PART})\.){{0,3}}(?:{_IDENT_PART})(?=[\s,;)]|$)", re.A)
# A short table alias: up to three letters, or one letter and a digit (``u``, ``t1``).
_ALIAS_RE = re.compile(r"[A-Za-z]{1,3}|[A-Za-z]\d")
# The rest of a value after a column reference: up to the next whitespace.
_VALUE_REST_RE = re.compile(r"\S*")


def _words(name: str) -> bool:
    """Each ``_``-joined piece of ``name`` is word-shaped, in any case (``USER_PASSWORD``,
    ``api_key``, ``PasswordHash``): no digit, no random run of letters."""
    pieces = [piece for piece in name.split("_") if piece]
    return bool(pieces) and all(
        _word_shaped(piece) or _word_shaped(piece.capitalize()) for piece in pieces
    )


def _variable_from_column(m: re.Match[str]) -> bool:
    """``@variable = <column>`` in a T-SQL ``SELECT`` list (#2253): a key written as one
    ``@variable`` (not ``@@``) with ``=``, whose whole unquoted value is a column
    reference (``_COLUMN_REF_RE``: ``Password``, ``u.Password``, ``[u].[Password]``,
    ``"Password"``), with nothing but ``),;`` after it before whitespace, whose column
    is itself a credential key name with no digit, made of words (``_words``), and
    whose qualifiers are words or a short alias (``_ALIAS_RE``). A ``.env`` line
    and a connection string (the ``_key_value_assignment`` rule, read from the ``@``)
    stay values."""
    if m.groupdict().get("sep") != "=":
        return False
    line, at, sep = m.string, m.start("key") - 1, m.start("sep")
    if at < 0 or line[at] != "@" or line[at - 1 : at] in ("@", ";"):
        return False
    spaced = line[sep - 1 : sep].isspace() or line[sep + 1 : sep + 2].isspace()
    if not spaced and _ENV_START_RE.fullmatch(line, 0, at) is not None:
        return False
    start = m.start("qv") if m.group("v") is not None else m.start("u")
    if line[m.end("sep") : start].strip():
        return False  # a backtick before the value: Markdown, not SQL
    ref = _COLUMN_REF_RE.match(line, start)
    if ref is None:
        return False
    # The reference must be the whole value, up to a closing ``)``, ``,`` or ``;``:
    # ``Password,@Token=...`` or ``Password;pwd=...`` hides a value glued after it.
    rest = _VALUE_REST_RE.match(line, ref.end())
    if rest is None or rest.group(0).strip("),;"):
        return False
    *qualifiers, column = (part.strip('[]"') for part in ref.group(0).split("."))
    return (
        _KEY_NAME_RE.fullmatch(column) is not None
        and _words(column)
        and all(_ALIAS_RE.fullmatch(q) or _words(q) for q in qualifiers)
    )


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

# With ``narrow_key_names`` (the CI scans of model-written text, #2179), an unquoted
# value after a ``token`` or ``api_key`` name with ``key:`` (YAML or prose) or in a
# Markdown table counts only when it looks like a secret: design prose such as
# "Validate a token: GetItem ... by token_hash" is no credential. Every other key name
# (a password, ``secret_key``, ``private_key``, ``access_key`` ...), quoted values,
# ``key=value`` assignments and JSON keys are not narrowed: they fail closed as before.
#
# A known token format: an AWS access key id, a Google API key, a GitHub, GitLab,
# Slack, npm, OpenAI-style or Stripe token, a JSON Web Token. A value in one of these
# formats is a secret even when it is letters only.
_TOKEN_FORMAT_RE = re.compile(
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_\w{20,}|glpat-[\w-]{20,}|xox[abposr]-[\w-]{10,}|npm_[A-Za-z0-9]{20,}"
    r"|sk-[\w-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{20,}|eyJ[\w-]+\.eyJ[\w-]+\.[\w-]*"
)
# Whitespace and the punctuation that joins words and identifiers in prose
# (``token_hash``, ``email-tokens``, ``GetItem(...)``, ``read/write``): a value is split
# into pieces at these.
_PIECE_SEPARATOR_RE = re.compile(r"[\s_.\-/()\[\]{},;]+")
# The longest piece that can still be a word or an identifier.
_MAX_WORD_LENGTH = 24
# A word-shaped piece: an acronym (``TTL``, ``JSONB``), or a lower-case or CamelCase
# word of at most five humps, with an optional leading acronym and a two-letter
# trailing one (``GetItem``, ``TransactWriteItems``, ``HTTPHeader``, ``DynamoDB``),
# where no hump (``_HUMP_RE``) is unpronounceable (``_UNPRONOUNCEABLE_RE``: five
# consonants in a row, or four or more letters with no vowel; ``TxnId`` still passes).
# A random run of letters is practically never one: about 99.7% of random 20-letter
# mixed-case values and practically every random 16-letter single-case one are not.
_WORD_RE = re.compile(
    r"[A-Z]{2,6}|(?:[A-Z]{2,4}(?=[A-Z][a-z]))?[A-Z]?[a-z]{1,15}(?:[A-Z][a-z]{1,15}){0,4}"
    r"(?:[A-Z]{2})?"
)
_HUMP_RE = re.compile(r"[A-Z]?[a-z]+")
_UNPRONOUNCEABLE_RE = re.compile(r"[b-df-hj-np-tv-xz]{5}|^[^aeiouy]{4,}$", re.I)
# The key words the narrowing applies to: ``token`` (the ``tok`` group) and the literal
# ``api key`` names. Every other ``_KEY_WORDS`` name stays strict.
_NARROWED_KEY_WORD_RE = re.compile(r"api[_-]?key", re.I)


def _word_shaped(piece: str) -> bool:
    return (
        len(piece) <= _MAX_WORD_LENGTH
        and _WORD_RE.fullmatch(piece) is not None
        and not any(_UNPRONOUNCEABLE_RE.search(hump) for hump in _HUMP_RE.findall(piece))
    )


def _looks_like_secret(value: str) -> bool:
    """A known token format, or a value with any piece (split at whitespace and word
    punctuation) that is not word-shaped (``_word_shaped``): a digit, a symbol or a
    random run of letters. Prose made only of words and identifiers (``GetItem``,
    ``token_hash``, ``GetItem(token_hash)``) is not one."""
    if _TOKEN_FORMAT_RE.match(value):
        return True
    return any(not _word_shaped(piece) for piece in _PIECE_SEPARATOR_RE.split(value) if piece)


def _narrowable(m: re.Match[str]) -> bool:
    """A ``token`` key or a literal ``api key`` key (``api_key``, ``x-api-key``,
    ``apiKey``), never a password or another key name (#2179)."""
    if m.group("tok"):
        return True
    return bool(m.group("ak")) and _NARROWED_KEY_WORD_RE.fullmatch(m.group("ak")) is not None


def _narrowed(rx: re.Pattern[str], m: re.Match[str], value: str) -> bool:
    """An unquoted value under a token or API key name, after ``key:`` or in a
    ``| key | value |`` row, that does not look like a secret: no finding (#2179)."""
    narrowed_form = rx is _PAIR_RES[2] or (rx is _PAIR_RES[1] and m.groupdict().get("sep") == ":")
    return narrowed_form and _narrowed_value(_narrowable(m), value)


def _narrowed_value(narrowable: bool, value: str) -> bool:
    """A value under a narrowable key that is unquoted and does not look like a secret
    (#2179)."""
    return narrowable and not _quoted(value) and not _looks_like_secret(value)


def _quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] in "\"'`" and value[-1] == value[0]


class _Pair(NamedTuple):
    """A credential key's value on a line: its kind, span and key name, and whether
    the scan can tell where it ends (``certain``). Where it cannot, the redaction
    is incomplete, so the file is withheld (``_pair_hits``), and comment masking
    masks to the end of the line (#2263)."""

    kind: str
    span: tuple[int, int]
    key: str
    certain: bool = True


# The span dialects whose strings read a backslash as an ordinary character, whatever
# the session sets. MySQL's and PostgreSQL's readings move with a setting
# (``NO_BACKSLASH_ESCAPES``, ``standard_conforming_strings``), so a scan in those, or in
# no known dialect, reads a backslash as an escape: the reading that holds more of the
# value, the rule the literal redaction follows (``src.tools.sql.redact``, #1482).
_LITERAL_BACKSLASH_DIALECTS = frozenset({"sqlserver", "oracle"})


def _nested_value(line: str, m: re.Match[str], index: _LineIndex) -> tuple[int, int] | None:
    """The span of a value quoted by SQL's doubled quote inside a string open around the
    key (``EXEC('... PASSWORD=''...''')``, #2262): from after the doubled quote to the
    next doubled one, to a single quote that closes the string around it, or to the end
    of the line. ``None`` when the opening quote is not doubled or no string is open."""
    opener = m.group("qv")
    quote, start = opener[-1], m.end("qv") + 1
    if len(opener) != 1 or line[m.end("qv") : start] != quote:
        return None
    if not index.string_open_in_some_reading(m.start(), quote, 0):
        return None
    pos = start
    while (at := line.find(quote, pos)) != -1:
        run_end = at
        while line[run_end : run_end + 1] == quote:
            run_end += 1
        # Four quotes are a quote in the nested string; two (or three, the string
        # around it closing too) close it; one closes the string around it.
        at += (run_end - at) // 4 * 4
        if run_end - at >= 2 or (run_end - at == 1 and _CLOSE_CONTEXT_RE.match(line, run_end)):
            return start, at
        pos = run_end
    return start, len(line)


def _pairs(line: str, narrow_key_names: bool = False, dialect: str = "") -> list[_Pair]:
    """Each credential key's value on ``line`` (``_Pair``). Every pair is checked; an
    allowed value (a placeholder, a marker, a bare JSON literal or short count, a bind
    parameter, a ``@variable`` set from a column, a column note, and with
    ``narrow_key_names`` prose under a token or API key name) is left out, and never
    exempts the rest of the line."""
    found: dict[tuple[int, int], _Pair] = {}
    if _KEY_WORD_RE.search(line) is None:
        return []
    index = _LineIndex(line, dialect in _LITERAL_BACKSLASH_DIALECTS)
    flow_cover = -1
    for rx in _PAIR_RES:
        for m in rx.finditer(line):
            if _PER_UNIT_RE.search(m.group("key")):
                continue
            group = "v" if m.groupdict().get("v") is not None else "u"
            value = m.group(group) or ""
            certain = True
            # A T-SQL variable set from a credential-named column (#2253).
            if rx is _PAIR_RES[1] and _variable_from_column(m):
                continue
            if rx is not _PAIR_RES[2] and _block_indicator(m):
                # A YAML block scalar: its value is on the lines below
                # (``_block_scalar_hits``). One inside a JSON string
                # (``password: |\\n  ...``) cannot be placed (#2263).
                if _raw_block_indicator(m):
                    continue
                if _LINE_BREAK_ESCAPE_RE.match(line, _block_end(m) or 0) is not None:
                    certain = False
            flow_value = rx is _PAIR_RES[1] and group == "u" and value[:1] in "[{"
            flow_value = flow_value and not _allowed_value(value)
            if flow_value and m.group("sep") == ":" and certain:
                # A YAML flow sequence or mapping (``password: [a, b]``): the whole of
                # it is the value; one that does not close on the line is uncertain.
                # Inside an earlier one that runs to the end of the line it adds
                # nothing (``_outermost``), so it is not read again.
                if m.start() < flow_cover:
                    continue
                span, closed = _flow_span(line, m.start("u"))
                if span[1] == len(line):
                    flow_cover = len(line)
                flow = line[span[0] : span[1]]
                if flow.strip("[]{}, ") and not _allowed_value(flow):
                    found.setdefault(span, _Pair(_pair_kind(m), span, m.group("key"), closed))
                continue
            nested = _nested_value(line, m, index) if group == "v" else None
            if nested is not None:
                value = line[nested[0] : nested[1]]
                if value and not _allowed_value(value):
                    found.setdefault(nested, _Pair(_pair_kind(m), nested, m.group("key")))
                continue
            if group == "v" and _opens_no_value(line, m, index):
                continue
            if group == "u" and certain:
                # Not a value: an unquoted JSON literal or short count, a SQL bind
                # parameter after "=", a `table.column`: <SQL type> column note.
                if rx is _PAIR_RES[0] and (value in _JSON_LITERALS or _COUNT_RE.fullmatch(value)):
                    continue
                if rx is _PAIR_RES[1] and _column_note(m, line, value):
                    continue
                value = value.rstrip("),;")
                if rx is _PAIR_RES[1] and _bind_parameter(m, value):
                    continue
                # Prose after "key:" or in a table row, not a secret (#2179).
                if narrow_key_names and _narrowed(rx, m, value):
                    continue
            if not value or _allowed_value(value):
                continue
            # The value up to its first quote or backslash decides the finding, as
            # before; the span then runs to the value's real end, so neither the
            # redaction nor the recorded value stops at that character (#2247).
            start, end = m.span(group)
            if group == "v":
                end = _quoted_end(line, m, index)
            elif rx is _PAIR_RES[1] and certain:
                end, certain = _unquoted_end(line, m, end, index)
            pair = found.get((start, end))
            if pair is None or (pair.certain and not certain):
                found[(start, end)] = _Pair(_pair_kind(m), (start, end), m.group("key"), certain)
    return _outermost(found.values())


def _outermost(pairs: Iterable[_Pair]) -> list[_Pair]:
    """``pairs`` less each one inside another's span, whose uncertainty the outer one
    takes on: a line of unclosed values (a key name and an opening quote, repeated with
    no closing quote) records each stretch once, so what the redaction keeps stays
    linear in the line (#2595)."""
    kept: list[_Pair] = []
    cover = -1
    for pair in sorted(pairs, key=lambda p: (p.span[0], -p.span[1])):
        if cover >= 0 and pair.span[1] <= kept[cover].span[1]:
            if not pair.certain:
                kept[cover] = kept[cover]._replace(certain=False)
            continue
        kept.append(pair)
        if cover < 0 or pair.span[1] > kept[cover].span[1]:
            cover = len(kept) - 1
    return kept


def _covered(kind: str, span: tuple[int, int] | None, by: Iterable[tuple[str, object]]) -> bool:
    """Whether a hit of ``kind`` at ``span`` lies inside one of the same kind in ``by``
    (an unplaced hit, ``None``, only by another unplaced one)."""
    for other_kind, other in by:
        if other_kind != kind:
            continue
        if span is None or other is None:
            if span is None and other is None:
                return True
            continue
        if other[0] <= span[0] and span[1] <= other[1]:  # type: ignore[index]
            return True
    return False


def _with_baseline(
    rules: list[tuple[str, tuple[int, int] | None]],
    baseline: Iterable[tuple[str, tuple[int, int] | None]],
) -> list[tuple[str, tuple[int, int] | None]]:
    """``rules`` and each ``baseline`` hit they do not already cover: the union of the
    two passes (#2595). What the baseline masks stays masked and what it finds stays
    found, so the newer rules only ever add."""
    return rules + [hit for hit in baseline if not _covered(hit[0], hit[1], rules)]


def _pair_matches(
    line: str, narrow_key_names: bool = False, dialect: str = ""
) -> list[tuple[str, tuple[int, int], str]]:
    """Each credential key's value on ``line``: its kind, span and key name, from both
    passes (``_pairs``, and main's rules in ``secret_scan_baseline``)."""
    found = [(p.kind, p.span, p.key) for p in _pairs(line, narrow_key_names, dialect)]
    rules = [(kind, span) for kind, span, _ in found]
    return found + [
        (kind, span, key)
        for kind, span, key in _baseline._pair_matches(line, narrow_key_names)
        if not _covered(kind, span, rules)
    ]


def _pair_hits(
    line: str, narrow_key_names: bool = False, to_line_end: bool = False, dialect: str = ""
) -> list[Hit]:
    """``_pairs`` without the key names. A value whose end the scan cannot tell is
    placed as far as it is known and also counted unplaced, so a redaction of it is
    incomplete; with ``to_line_end`` (comment masking, which has no file to withhold)
    it runs to the end of the line instead."""
    hits: list[Hit] = []
    for pair in _pairs(line, narrow_key_names, dialect):
        if pair.certain:
            hits.append((pair.kind, pair.span))
        elif to_line_end:
            hits.append((pair.kind, (pair.span[0], len(line))))
        else:
            hits += [(pair.kind, pair.span), (pair.kind, None)]
    return hits


def _multiline_hits(
    lines: list[str], narrow_key_names: bool = False
) -> dict[int, list[tuple[str, tuple[int, int] | None, str]]]:
    """The values a line-by-line scan cannot see, by line index: a Markdown table
    column's cells, a YAML block scalar's lines and an array's values under a
    credential key. A ``None`` span is a value that cannot be placed (the redaction is
    incomplete)."""
    found: dict[int, list[tuple[str, tuple[int, int] | None, str]]] = {
        index: list(hits) for index, hits in _table_hits(lines, narrow_key_names).items()
    }
    for extra in (_block_scalar_hits(lines), _array_hits(lines)):
        for index, hits in extra.items():
            found.setdefault(index, []).extend(hits)
    return found


def _table_hits(
    lines: list[str], narrow_key_names: bool = False
) -> dict[int, list[tuple[str, tuple[int, int], str]]]:
    """The cells under a Markdown table column whose header names a credential
    (``| user | password |`` then ``| app | <value> |``), by line index."""
    found: dict[int, list[tuple[str, tuple[int, int], str]]] = {}
    columns: dict[int, tuple[str, str, bool]] = {}
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            columns = {}
            continue
        bars = [i for i, c in enumerate(line) if c == "|"]
        cells = [(bars[i] + 1, bars[i + 1]) for i in range(len(bars) - 1)]
        header: dict[int, tuple[str, str, bool]] = {}
        for col, (a, b) in enumerate(cells):
            m = _KEY_CELL_RE.fullmatch(line[a:b])
            if m and not _PER_UNIT_RE.search(m.group("key")):
                header[col] = (_pair_kind(m), m.group("key"), narrow_key_names and _narrowable(m))
        if header:
            columns = header
            continue
        if _SEPARATOR_ROW_RE.fullmatch(line):
            continue
        for col, (kind, key, narrowable) in columns.items():
            if col < len(cells):
                a, b = cells[col]
                cell = line[a:b]
                value = cell.strip()
                # A cell of prose under a token or API key column is no secret (#2179).
                if _narrowed_value(narrowable, value):
                    continue
                if value and not _allowed_value(value):
                    start = a + cell.index(value)
                    found.setdefault(index, []).append((kind, (start, start + len(value)), key))
    return found


# A bare AWS secret key: 40 base64 characters with upper, lower and a digit (a hex
# digest has no upper-case mix; an identifier has underscores).
_BARE_KEY_RE = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")


def _bare_secret_key(token: str) -> bool:
    return (
        any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
        and not _HEX_RE.fullmatch(token)
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


def _line_hits(
    line: str,
    emails: bool,
    narrow_key_names: bool = False,
    plugins: bool = True,
    to_line_end: bool = False,
    dialect: str = "",
) -> list[Hit]:
    """Each finding on ``line`` (``_rule_line_hits``), with each key-value hit of main's
    rules (``secret_scan_baseline``) they do not cover: the union of the two passes, so
    nothing main masks or finds is lost (#2595)."""
    rules = _rule_line_hits(line, emails, narrow_key_names, plugins, to_line_end, dialect)
    return _with_baseline(rules, _baseline._pair_hits(line, narrow_key_names))


def _rule_line_hits(
    line: str,
    emails: bool,
    narrow_key_names: bool = False,
    plugins: bool = True,
    to_line_end: bool = False,
    dialect: str = "",
) -> list[Hit]:
    """Each finding on ``line`` by this module's rules: its kind and the span of its
    value (``None`` when the value cannot be placed on the line). Allowed values are left
    out. ``plugins=False`` runs the repository's own rules only (``RULE_SET``
    ``"regex"``); ``to_line_end`` as in ``_pair_hits``; ``dialect`` is the span dialect
    SQL quotes are read in."""
    found: list[Hit] = []
    for secret in _detect_secrets_line(line) if plugins else ():
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
    found += _pair_hits(line, narrow_key_names, to_line_end, dialect)
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


def _scan_lines(
    text: str,
    key_names: bool,
    emails: bool = False,
    narrow_key_names: bool = False,
    dialect: str = "",
):
    lines = text.splitlines(keepends=True)
    bodies = [line.rstrip("\r\n") for line in lines]
    tables = _multiline_hits(bodies, narrow_key_names)
    # Main's Markdown column rule too, so the union holds for tables as well (#2595).
    for index, entries in _baseline._table_hits(bodies, narrow_key_names).items():
        rules = [(kind, span) for kind, span, _ in tables.get(index, [])]
        extra = [entry for entry in entries if not _covered(entry[0], entry[1], rules)]
        if extra:
            tables.setdefault(index, []).extend(extra)
    with scan_session():
        for number, line in enumerate(lines, start=1):
            body = line.rstrip("\r\n")
            table = tables.get(number - 1, [])
            hits = _line_hits(body, emails, narrow_key_names, dialect=dialect) + [
                (kind, span) for kind, span, _ in table
            ]
            if not key_names:
                hits = [hit for hit in hits if hit[0] not in KEY_NAME_KINDS]
            yield number, line, body, hits, table


def secret_findings(
    text: str,
    key_names: bool = True,
    narrow_key_names: bool = False,
    source_engine: str | None = None,
) -> list[str]:
    """``"<kind> at line N"`` for every credential in ``text``; no value is repeated.
    ``key_names=False`` leaves out the findings that go by a key's name (``KEY_NAME_KINDS``);
    ``narrow_key_names=True`` leaves out prose under a token or API key name (#2179, the
    CI scans of model-written text only; the default is strict). ``source_engine``: the
    SQL dialect the text's quotes are read in (``_LITERAL_BACKSLASH_DIALECTS``, #2262)."""
    found: list[str] = []
    for number, _line, _body, hits, _table in _scan_lines(
        text, key_names, narrow_key_names=narrow_key_names, dialect=span_dialect(source_engine)
    ):
        found += [f"{kind} at line {number}" for kind in sorted({kind for kind, _ in hits})]
    return found


def secret_kinds(text: str, key_names: bool = True, source_engine: str | None = None) -> set[str]:
    """The kinds :func:`secret_findings` finds in ``text``, without their lines: what a
    caller that scans many texts keeps of each (the collector, #2098), read in
    ``source_engine``'s SQL dialect (#2262). Always strict: a routine body is SQL, not
    design prose (#2179)."""
    kinds: set[str] = set()
    dialect = span_dialect(source_engine)
    for _number, _line, _body, hits, _table in _scan_lines(text, key_names, dialect=dialect):
        kinds.update(kind for kind, _ in hits)
    return kinds


# A URL's userinfo password (``mysql://user:<password>@host``, ``jdbc:mysql://...``,
# Redis's empty user ``redis://:<password>@host``): the value detect-secrets' Basic Auth
# plugin reports. The user is read as that plugin reads it, or is empty; the password
# with fewer characters left out, so every value it reports is found, and up to the
# last ``@`` before the host (an unencoded ``@`` in it, split as common parsers do).
_URL_USER = r"[^\s{}:/?#\[\]@!$&'()*+,;=]*"
_URL_PASSWORD_RE = re.compile(r"://" + _URL_USER + r":(?P<v>[^\s{}/]+)@")
# A known token format standing on its own (not the tail of a longer word).
_BOUNDED_TOKEN_RE = re.compile(r"(?<![\w-])(?:" + _TOKEN_FORMAT_RE.pattern + r")")


def token_format_spans(text: str) -> list[tuple[int, int]]:
    """The span of each known token format in ``text`` (``_TOKEN_FORMAT_RE``: an AWS
    access key id, a GitHub, GitLab, Slack, npm, Stripe or OpenAI-style token, a JSON
    Web Token) that is not the tail of a longer word: what valid SQL never holds
    outside a string, so it can be masked in code (a MySQL executable comment, #2234)."""
    return [m.span() for m in _BOUNDED_TOKEN_RE.finditer(text)]


def credential_spans(text: str, source_engine: str | None = None) -> list[tuple[int, int]]:
    """The span (offsets in ``text``) of each credential value the repository's own
    rules find, strictly, sorted and merged: every ``RULE_SET`` ``"regex"`` rule (key-name
    pairs, a Markdown credential column, the AWS, Google, Slack, bearer and private-key
    shapes, a bare secret key, public IP addresses), a URL's userinfo password (only the
    password: the scheme, user and host stay), and the known token formats of
    ``_TOKEN_FORMAT_RE`` (GitHub, GitLab, Slack, npm, Stripe and OpenAI-style tokens, a
    JSON Web Token), which stand in for detect-secrets' shape plugins. No plugin runs,
    so the spans are the same in every image, with or without detect-secrets: what the
    collector masks in a SQL comment (``src.tools.sql.redact.scrub_comment``, #2234)
    never depends on a dev dependency. ``source_engine``: the SQL dialect the text's
    quotes are read in (#2262)."""
    if not text:
        return []
    dialect = span_dialect(source_engine)
    spans: list[tuple[int, int]] = []
    offset = 0
    lines = text.splitlines(keepends=True)
    tables = _multiline_hits([line.rstrip("\r\n") for line in lines])
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        hits = _rule_line_hits(body, emails=False, plugins=False, to_line_end=True, dialect=dialect)
        hits += [(kind, span) for kind, span, _ in tables.get(index, [])]
        found = [span for _kind, span in hits if span is not None]
        found += [m.span() for m in _TOKEN_FORMAT_RE.finditer(body)]
        found += [m.span("v") for m in _URL_PASSWORD_RE.finditer(body)]
        spans += [(offset + a, offset + b) for a, b in found if b > a]
        offset += len(line)
    # Main's spans too: the union never masks less than main (#2595).
    spans += _baseline.credential_spans(text)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
