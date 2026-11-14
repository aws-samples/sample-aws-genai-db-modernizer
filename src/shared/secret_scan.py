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
"""

from __future__ import annotations

import contextlib
import importlib
import re
import threading
from collections.abc import Iterator
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
# A quote or backslash run inside an unquoted ``key=value`` / ``key: value`` value
# (``Zq9"Xv7``, ``Zq9\Xv7``) and the piece after it. A run followed by whitespace, a
# backtick, ``,;:)]}`` or the end closes an enclosing string instead
# (``conn("password=...")``, ``{"a":"password=...","b":1}``), so it ends the value (#2247).
# So does a run ending in a JSON escape of a line break (``\n``, ``\r``, ``\u000a``,
# ``\u000d``, at any escape level): a ``.env`` file read into a transcript keeps the
# settings after the value.
_EMBEDDED_RUN_RE = re.compile(
    r"(?![\\\"']*\\(?:[nr]|u000[aAdD]))[\\\"']+(?=[^\s\"'`\\,;:)\]}])[^\s\"'`\\]+"
)
# Every quote with the backslashes right before it.
_QUOTE_RUN_RE = re.compile(r"\\*[\"']")
# After a quote that closes an enclosing string: a JSON member or element ends.
_STRING_CLOSE_RE = re.compile(r"\s*(?:[,:}\]]|$)")


def _unquoted_end(line: str, end: int) -> int:
    """Where the unquoted value the pair rule ended at ``end`` really ends: past each
    embedded quote or backslash run (``_EMBEDDED_RUN_RE``)."""
    while (run := _EMBEDDED_RUN_RE.match(line, end)) is not None:
        end = run.end()
    return end


def _closes_a_lower_level(backslashes: int, level: int) -> bool:
    """A quote after ``backslashes`` backslashes is unescaped at an escape level below
    ``level``: at level ``j`` (``2**j - 1`` backslashes) the run is ``2**j - 1`` modulo
    ``2**(j + 1)`` (level 0: an even run, ``\\\\"`` included)."""
    j = 0
    while 2**j - 1 < level:
        if backslashes % 2 ** (j + 1) == 2**j - 1:
            return True
        j += 1
    return False


def _quoted_end(line: str, m: re.Match[str]) -> int:
    """Where the quoted value of ``m`` really ends: before its own closing quote, the
    opening quote's character at the opening's escape level. At level ``n`` (``n``
    backslashes before the quote) that is a quote after ``k`` backslashes with
    ``k % (2n + 2) == n``: at level 0 an even run (``"Zq9\\"Xv7"`` holds ``\\"``), at
    level 1 (a JSON string inside a JSON string) one, five, ... A quote of the other
    kind is part of the value, and so is SQL's doubled ``''``. Where no quote closes it
    before a quote that closes an enclosing string (one of the other kind, or of its
    own kind at a lower escape level, followed by ``,``, ``:``, ``}``, ``]`` or the end
    of the line), the value ends where the pair rule ended it: it never runs past
    the JSON string around it (#2259 review)."""
    opener = m.group("qv")
    level, quote = len(opener) - 1, opener[-1]
    pos = m.start("v")
    while (run := _QUOTE_RUN_RE.search(line, pos)) is not None:
        backslashes, char = run.end() - run.start() - 1, line[run.end() - 1]
        if char == quote:
            if (
                level == 0
                and quote == "'"
                and not backslashes
                and line[run.end() : run.end() + 1] == "'"
            ):
                pos = run.end() + 1
                continue
            if backslashes % (2 * level + 2) == level:
                return run.end() - 1 - level
        encloses = char != quote or _closes_a_lower_level(backslashes, level)
        if encloses and _STRING_CLOSE_RE.match(line, run.end()):
            break
        pos = run.end()
    return m.end("v")


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


def _pair_matches(
    line: str, narrow_key_names: bool = False
) -> list[tuple[str, tuple[int, int], str]]:
    """Each credential key's value on ``line``: its kind, span and key name. Every
    pair is checked; an allowed value (a placeholder, a marker, a bare JSON literal or
    short count, a bind parameter, a column note, and with ``narrow_key_names`` prose
    under a token or API key name) is left out, and never exempts the rest of the
    line."""
    found: dict[tuple[int, int], tuple[str, str]] = {}
    if _KEY_WORD_RE.search(line) is None:
        return []
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
                end = _quoted_end(line, m)
            elif rx is _PAIR_RES[1]:
                end = _unquoted_end(line, end)
            found.setdefault((start, end), (_pair_kind(m), m.group("key")))
    return [(kind, span, key) for span, (kind, key) in found.items()]


def _pair_hits(line: str, narrow_key_names: bool = False) -> list[tuple[str, tuple[int, int]]]:
    """``_pair_matches`` without the key names."""
    return [(kind, span) for kind, span, _ in _pair_matches(line, narrow_key_names)]


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
    line: str, emails: bool, narrow_key_names: bool = False, plugins: bool = True
) -> list[Hit]:
    """Each finding on ``line``: its kind and the span of its value (``None`` when the
    value cannot be placed on the line). Allowed values are left out. ``plugins=False``
    runs the repository's own rules only (``RULE_SET`` ``"regex"``)."""
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
    found += _pair_hits(line, narrow_key_names)
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


def _scan_lines(text: str, key_names: bool, emails: bool = False, narrow_key_names: bool = False):
    lines = text.splitlines(keepends=True)
    tables = _table_hits([line.rstrip("\r\n") for line in lines], narrow_key_names)
    with scan_session():
        for number, line in enumerate(lines, start=1):
            body = line.rstrip("\r\n")
            table = tables.get(number - 1, [])
            hits = _line_hits(body, emails, narrow_key_names) + [
                (kind, span) for kind, span, _ in table
            ]
            if not key_names:
                hits = [hit for hit in hits if hit[0] not in KEY_NAME_KINDS]
            yield number, line, body, hits, table


def secret_findings(text: str, key_names: bool = True, narrow_key_names: bool = False) -> list[str]:
    """``"<kind> at line N"`` for every credential in ``text``; no value is repeated.
    ``key_names=False`` leaves out the findings that go by a key's name (``KEY_NAME_KINDS``);
    ``narrow_key_names=True`` leaves out prose under a token or API key name (#2179, the
    CI scans of model-written text only; the default is strict)."""
    found: list[str] = []
    for number, _line, _body, hits, _table in _scan_lines(
        text, key_names, narrow_key_names=narrow_key_names
    ):
        found += [f"{kind} at line {number}" for kind in sorted({kind for kind, _ in hits})]
    return found


def secret_kinds(text: str, key_names: bool = True) -> set[str]:
    """The kinds :func:`secret_findings` finds in ``text``, without their lines: what a
    caller that scans many texts keeps of each (the collector, #2098). Always strict:
    a routine body is SQL, not design prose (#2179)."""
    kinds: set[str] = set()
    for _number, _line, _body, hits, _table in _scan_lines(text, key_names):
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


def credential_spans(text: str) -> list[tuple[int, int]]:
    """The span (offsets in ``text``) of each credential value the repository's own
    rules find, strictly, sorted and merged: every ``RULE_SET`` ``"regex"`` rule (key-name
    pairs, a Markdown credential column, the AWS, Google, Slack, bearer and private-key
    shapes, a bare secret key, public IP addresses), a URL's userinfo password (only the
    password: the scheme, user and host stay), and the known token formats of
    ``_TOKEN_FORMAT_RE`` (GitHub, GitLab, Slack, npm, Stripe and OpenAI-style tokens, a
    JSON Web Token), which stand in for detect-secrets' shape plugins. No plugin runs,
    so the spans are the same in every image, with or without detect-secrets: what the
    collector masks in a SQL comment (``src.tools.sql.redact.scrub_comment``, #2234)
    never depends on a dev dependency."""
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    offset = 0
    lines = text.splitlines(keepends=True)
    tables = _table_hits([line.rstrip("\r\n") for line in lines])
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        hits = _line_hits(body, emails=False, plugins=False)
        hits += [(kind, span) for kind, span, _ in tables.get(index, [])]
        found = [span for _kind, span in hits if span is not None]
        found += [m.span() for m in _TOKEN_FORMAT_RE.finditer(body)]
        found += [m.span("v") for m in _URL_PASSWORD_RE.finditer(body)]
        spans += [(offset + a, offset + b) for a, b in found if b > a]
        offset += len(line)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
