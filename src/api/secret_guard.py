"""Refuse a request that carries a database password or other secret (ADR-004 Decision 5, #723).

Database credentials live in a secrets manager (for example AWS Secrets Manager) and a
request names them by reference: ``connection.secret_arn`` or ``connection.secret_name``.
The live collector fetches the secret through the IAM role (``secret_arn`` in
``src/contracts/collector_input.py``), so a password in a request is never needed and is
never stored or forwarded.

Two layers. The request fields that are forwarded are allowlisted in
``src/api/models/requests.py`` (host name or IP, plain names, ARN and secret-name
character sets, table-pattern characters), so most credential forms cannot even be
written into them. Everything else is checked here, by :func:`scan`:

- a key that names a secret (``password``, ``db_password``, ``MYSQL_PWD``, ``PassWord``,
  ``client_secret``, ``auth_token``, ``api_key``, ``aws_secret_access_key``,
  ``credentials``, ``SecretString``, ...) with a non-empty value, at any depth. The key's
  words decide, so ``max_tokens``, ``token_budget`` or ``secret_rotation_days`` are not
  secrets;
- a string shaped like a credential: user info with a password (``user:pw@host``, with or
  without a scheme, ``scott/pw@db``, JDBC thin), a secret-named assignment
  (``Password=...;``, ``Password = ...``, ``?password=...``, ``PGPASSWORD=...``,
  ``password: ...``), a client flag (``mysql -p<pw>``, ``--password <pw>``,
  ``sqlcmd -P <pw>``, ``sqlplus scott/<pw>``) or ``IDENTIFIED BY <pw>``; also inside a
  percent-, base64- or JSON-encoded string, up to :data:`MAX_DECODE_DEPTH` levels.

Every check is plain parsing over bounded input, so the cost is linear in the capped
input: a string longer than :data:`MAX_SCANNED_CHARS`, a key longer than
:data:`MAX_KEY_CHARS`, nesting deeper than :data:`MAX_DEPTH`, a body larger than
:data:`MAX_GUARDED_BODY_BYTES`, or more than :data:`MAX_DECODED_CHARS` of decoded text in
one request is refused; decoded variants are de-duplicated and decoding stops after
:data:`MAX_DECODE_DEPTH` levels; no regular expression has a nested or overlapping
quantifier. The dependencies run the scan in a worker thread, off the event loop. Results
are field paths, never values, so a refusal can name the field without echoing the secret.

``broad=False`` is the free-text reading, used for the agent-answer routes and to redact
stored files (``src.api.stored_input_redaction``). Free text describes schemas, so it only
counts a value with a credential shape: an assignment of a literal (``password=...``,
``Password = '...'``), a quoted ``PASSWORD '...'`` or ``IDENTIFIED BY ...``, a client flag
with a value (``mysql -p<pw>``, ``--password <pw>``), or user info in a connection string
(with a scheme, ``jdbc:``, a bare host such as ``admin:pw@db`` or a port as in
``app:pw@db.example.com:5432``). The value is a placeholder only when it is empty, ``?``,
``%s``, ``none``, ``null``, ``n/a``, ``-``, ``*``, ``...``, ``$1``, ``:name`` or ``@name``.
So ``users.password: bcrypt hash``, ``token: rotates daily``, an e-mail address after a
label (``Owner:dba@example.com``) and an npm scope (``@scope/pkg@1.2.3``) are accepted and
kept. The request fields get the broad reading, which also counts ``key: value`` for a
secret-named key.

#1001 adds two structural forms, refused only when the value is a plain literal
(:func:`_plain_literal`: after one pair of matching outer quotes is removed, no ``$ { } %
` < > ( ) [ ] ! #`` and no quote in it, not starting with ``@`` or ``:``, and not ``?``,
``...``, ``***``, ``xxxx``, a placeholder :func:`_is_value` knows (``none``, ``null``,
``n/a``, ``-``), a masked value (``••••••``, ``REDACTED``) or a documentation word such as
``password``, ``pass``, ``changeme``, ``replace_me``, ``auth_string`` or
``YOUR_TOKEN_HERE``, in any case). :func:`_has_credential_shape` is shared, so they also
apply in the broad reading (``PUT /settings``, the assessment routes):

- a quoted literal as the password of a ``SET PASSWORD [FOR <user>] = '<pw>'`` (or
  ``= PASSWORD('<pw>')``) statement, read only from the statement's first word (the start
  of the text, a line, or after ``;``);
- ``curl -u|--user|--user=|-u<v> user:<pw>`` inside one curl command, which ends at a
  newline (a trailing ``\\`` continues it), ``;``, ``&&``, ``||``, ``|``, ``&``, a trailing
  ``,`` or ``.``, or a bare word that is no flag, flag value or URL.

A variable, template or command substitution there (``"$user:$pass"``, ``{{ .Values.pw
}}``, ``$(cat ~/.token)``) is not refused, and neither is a real password that holds one
of those characters (a known limit). No other rule changed.

Forms that may state a credential but may as well describe one are saved with
:data:`WARNING_MESSAGE` instead (:func:`credential_warnings`, #1001), and the stored-file
redaction never removes them: prose (``the password is <pw>``, where the value reads as a
password, so ``the password is hashed`` or ``... is SHA256`` is not), user info before a
dotted host with no port (``admin:pw@db.example.com``, with or without a path), and an
unquoted plain literal in ``SET PASSWORD FOR app = <pw>`` or ``CREATE [OR
REPLACE]|ALTER USER|ROLE|LOGIN <name> [WITH|LOGIN|ENCRYPTED ...] PASSWORD <pw>``, read
from the statement's first word (all-letter keywords such as ``EXPIRE`` or ``NULL`` are
not values).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

MAX_SCANNED_CHARS = 8192
MAX_KEY_CHARS = 256
MAX_DEPTH = 32
MAX_GUARDED_BODY_BYTES = 64 * 1024
MAX_DECODE_DEPTH = 3
MAX_DECODED_CHARS = 256 * 1024

# Last word of a key that names a secret. "tokens", "keys" etc. are deliberately absent.
_SECRET_LAST_WORDS = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "pwd",
        "pass",
        "pw",
        "secret",
        "secrets",
        "token",
        "credential",
        "credentials",
        "apikey",
        "accesskey",
        "privatekey",
        "secretkey",
    }
)
# ... or its last two words.
_SECRET_LAST_PAIRS = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("private", "key"),
        ("secret", "key"),
        ("auth", "key"),
        ("signing", "key"),
    }
)
# The key's words run together end like a secret: pgpassword, PassWord, pass_word, authtoken.
_SECRET_SUFFIXES = (
    "password",
    "passwd",
    "passphrase",
    "pwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "privatekey",
    "secretkey",
)
# Known names that hold a secret without ending like one.
_SECRET_NAMES = frozenset({"passwordhash", "secretvalue", "secretstring", "secretbinary"})
_MYSQL_CLIENTS = frozenset({"mysql", "mariadb", "mysqldump", "mysqladmin", "mysqlsh"})
# Clients whose short password flag takes the next word: mongosh -p <pw>, redis-cli -a <pw>.
_SHORT_PASSWORD_FLAGS = {
    "mongosh": "-p",
    "mongo": "-p",
    "redis-cli": "-a",
    "sqlcmd": "-P",
    "bcp": "-P",
    "osql": "-P",
    "isql": "-P",
}
# Clients whose -u / --user takes user:password.
_CURL_USER_FLAGS = frozenset({"-u", "--user", "--proxy-user", "-U"})
# What CREATE/ALTER makes in a statement whose PASSWORD is read.
_PRINCIPALS = frozenset({"user", "role", "login"})
# Words that may stand between the principal's name and PASSWORD.
_PASSWORD_LEAD_INS = frozenset({"with", "login", "encrypted", "unencrypted"})
# A value with one of these is no plain literal in the two #1001 forms (a variable, a
# template, a command substitution or quoting), so it is never refused there.
_NOT_PLAIN = frozenset("${}%`<>()[]!#'\"")
# ... and the documentation words the two forms never refuse (curl -u user:pass, the MySQL
# manual's 'auth_string'), compared in lower case.
_MASK_CHARS = frozenset("•*·….")
_DOCUMENTATION_WORDS = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "pw",
        "pwd",
        "secret",
        "token",
        "changeme",
        "change_me",
        "replace_me",
        "your-password",
        "your_password",
        "auth_string",
        "new_password",
        "mypassword",
        "example",
    }
)
# Hash, cipher and encoding names: "the password is SHA256" describes it, it is not it.
_NOT_PASSWORDS = frozenset(
    {
        "md5",
        "sha1",
        "sha2",
        "sha256",
        "sha512",
        "argon2",
        "argon2id",
        "argon2i",
        "pbkdf2",
        "bcrypt",
        "scrypt",
        "base64",
        "base64url",
        "base32",
        "uuidv4",
        "uuid4",
        "aes256",
        "rsa2048",
        "utf8",
    }
)
# Prose: "the password is <pw>", "the password for app is <pw>".
_PROSE_VERBS = frozenset({"is", "was", "are", "were"})
# A placeholder value, not a credential: "token = ?", "auth token: none".
_PLACEHOLDERS = frozenset({"?", "%s", "''", '""', "none", "null", "n/a", "na", "-", "*", "..."})

# Single character classes only: linear, no backtracking.
_KEY_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_PAIR_SEPARATORS = re.compile(r"[;&?,\s]+")
_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_QUOTES = " \t'\"`"

REFUSAL_MESSAGE = (
    "This request includes a password or secret ({fields}). The API never stores or "
    "forwards credentials, so it was refused and nothing was saved. Store the database "
    "credentials in a secrets manager such as AWS Secrets Manager, then send a reference "
    "instead: connection.secret_arn (the secret's ARN) or connection.secret_name (its name)."
)
WARNING_MESSAGE = (
    "Answers look like they contain a credential ({fields}); use a secrets manager "
    "reference (the secret's name or ARN) instead of the credential itself."
)
TOO_LARGE_MESSAGE = (
    "This request is too large to check for credentials ({fields}): a field is longer than "
    f"{MAX_SCANNED_CHARS} characters, a key longer than {MAX_KEY_CHARS}, the body nested "
    f"deeper than {MAX_DEPTH} levels or larger than {MAX_GUARDED_BODY_BYTES} bytes, or it "
    f"decodes to more than {MAX_DECODED_CHARS} characters. Nothing was saved."
)

_SAFE_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{0,63}")

logger = logging.getLogger(__name__)


@dataclass
class Findings:
    """Paths of the fields that carry a secret, and of those too large to check."""

    secrets: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.secrets or self.too_large)

    def paths(self) -> list[str]:
        return list(dict.fromkeys(self.secrets + self.too_large))


@dataclass
class _Budget:
    """Decoded characters left for one scan; exhausted means "too large to check"."""

    remaining: int = MAX_DECODED_CHARS
    exhausted: bool = False

    def spend(self, text: str) -> bool:
        self.remaining -= len(text)
        if self.remaining < 0:
            self.exhausted = True
        return not self.exhausted


# --------------------------------------------------------------------------- keys


def is_secret_key(key: str) -> bool:
    """True when ``key`` names a secret, e.g. ``db_password``; False for ``max_tokens``.

    A key longer than :data:`MAX_KEY_CHARS` is not judged here (the scan reports it as
    too large to check)."""
    if len(key) > MAX_KEY_CHARS:
        return False
    words = [w.lower() for w in _KEY_WORDS.findall(key)]
    if not words:
        return False
    if words[-1] in _SECRET_LAST_WORDS or tuple(words[-2:]) in _SECRET_LAST_PAIRS:
        return True
    joined = "".join(words)
    return joined.endswith(_SECRET_SUFFIXES) or joined in _SECRET_NAMES


# --------------------------------------------------------------------------- strings


def _is_value(value: str) -> bool:
    """A non-empty value that is not a placeholder (``?``, ``none``, ``$1``, ``:name``)."""
    v = value.strip(_QUOTES)
    if not v or v.lower() in _PLACEHOLDERS:
        return False
    if v[0] == "$" and v[1:].isdigit():
        return False  # bind parameter: $1
    if v[0] in ":@" and v[1:].replace("_", "").isalnum() and len(v) <= 64:
        return False  # bind parameters: :name, @p1
    return True


def _strip_one_quote_pair(value: str) -> str:
    v = value.strip(" \t")
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _plain_literal(v: str) -> bool:
    """A value the two #1001 forms refuse (its outer quotes already removed): no ``$ { } %
    ` < > ( ) [ ] ! #`` and no quote in it, not starting with ``@`` or ``:``, and not
    ``?``, ``...``, ``***``, ``xxxx``, a placeholder main's :func:`_is_value` knows
    (``none``, ``null``, ``n/a``, ``-``, ...), a masked value (``••••••``, ``…``,
    ``REDACTED``, ``**redacted**``) or a documentation word (:data:`_DOCUMENTATION_WORDS`,
    ``<letters, _ and ->_here`` or ``-here``, such as ``YOUR_TOKEN_HERE``; any case).
    Anything else (a variable, a template, a command substitution, adjacent quoting) is
    not read as a credential there. Main's own rules never use this."""
    if not v or v[0] in "@:" or any(c in _NOT_PLAIN for c in v):
        return False
    lower = v.lower()
    if not _is_value(v) or lower in ("?", "...") or (len(v) >= 4 and set(lower) <= {"x"}):
        return False
    if set(v) <= _MASK_CHARS or lower.strip("*") == "redacted":
        return False  # ••••••, …, ***, REDACTED, **redacted**
    if lower in _DOCUMENTATION_WORDS:
        return False
    word = lower[:-5]
    return not (
        lower.endswith(("_here", "-here"))
        and bool(word)
        and word.isascii()
        and word.replace("_", "").replace("-", "").isalpha()
    )


def _userinfo_has_password(token: str, broad: bool) -> bool:
    """User info with a password, with or without a scheme, or Oracle/JDBC thin user/pw."""  # pragma: allowlist secret
    before, at, _ = token.rpartition("@")
    if not at or not before:
        return False
    _, scheme_sep, after_scheme = before.rpartition("://")
    if scheme_sep:
        before = after_scheme
    elif not broad and not before.lower().startswith("jdbc:") and not _bare_host(token):
        # Free text: "Owner:dba@example.com" is an e-mail address after a label and
        # "@scope/pkg@1.2.3" a package; "admin:pw@db" or "app:pw@db:5432" is user info.
        return False
    if "/" in before:
        user, _, password = before.partition("/")
        if user.rpartition(":")[2] and password:  # jdbc:oracle:thin:scott/pw
            return True
    if ":" in before:
        password = before.rpartition(":")[2]
        return bool(password.strip(_QUOTES))
    return False


def _bare_host(token: str) -> bool:
    """After the last ``@``: a host with no dot (``db``) or with a port (``db.x:5432``),
    which an e-mail domain never is; and the user part has a letter (not ``10:30@UTC``)."""
    before, _, host = token.rpartition("@")
    user = before.lstrip("@").partition(":")[0].partition("/")[0]
    if not any(c.isalpha() for c in user):
        return False
    name, colon, port = host.partition(":")
    if colon:
        return port[:5].isdigit()
    return bool(name) and "." not in name and name.replace("-", "").isalnum()


def _assignment_is_secret(key: str, value: str, broad: bool) -> bool:
    key = key.strip(_QUOTES).lstrip("-")
    if len(key) > MAX_KEY_CHARS or not _is_value(value):
        return False
    return is_secret_key(key)


def _tighten(text: str, sep: str) -> str:
    """``a = b`` -> ``a=b`` without a regex (split and strip are linear)."""
    if sep not in text:
        return text
    parts = text.split(sep)
    last = len(parts) - 1
    return sep.join(
        p.rstrip() if i == 0 else p.lstrip() if i == last else p.strip()
        for i, p in enumerate(parts)
    )


def _flag_takes_secret(word: str, following: str | None) -> bool:
    """``--password <pw>``, ``-Password <pw>`` (a ``--password=<pw>`` is an assignment)."""
    if not word.startswith("-") or "=" in word or following is None:
        return False
    name = word.lstrip("-")
    return len(name) > 1 and is_secret_key(name) and not following.startswith("-")


def _client_flags_hold_secret(words: list[str], lower: list[str]) -> bool:
    mysql = any(w in _MYSQL_CLIENTS for w in lower)
    short_flags = {_SHORT_PASSWORD_FLAGS[w] for w in lower if w in _SHORT_PASSWORD_FLAGS}
    sqlplus = "sqlplus" in lower
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if _flag_takes_secret(word, following) and _is_value(following or ""):
            return True
        if mysql and word.startswith("-p") and len(word) > 2:
            return True
        for flag in short_flags:
            if word == flag and following is not None and _is_value(following):
                return True
            if word.startswith(flag) and len(word) > 2 and flag != "-p":
                return True  # sqlcmd -Ppw, redis-cli -apw (mongosh -p needs a space)
        if sqlplus and "/" in word and "@" not in word:
            user, _, password = word.partition("/")
            if user and password:  # sqlplus scott/tiger
                return True
        if lower[index] == "identified" and index + 2 < len(words) and lower[index + 1] == "by":
            if _is_value(words[index + 2]):
                return True
        # PostgreSQL CREATE/ALTER ROLE|USER ... PASSWORD '<pw>': a quoted literal.
        if lower[index] == "password" and following is not None and following[:1] in "'\"":
            if _is_value(following):
                return True
    return False


# --------------------------------------------------------------------------- statements
#
# #1001: only a quoted literal in a statement that really is one is refused. The parser
# reads one statement at a time (a line, split at ";") and only from its first word, so
# prose that mentions "create user ... password ..." is never read as SQL.


def _statements(text: str) -> list[list[str]]:
    """The words of each statement: lines (a trailing ``\\`` continues one) split at ``;``."""
    joined = text.replace("\\\n", " ") if "\\\n" in text else text
    return [part.split() for line in joined.splitlines() for part in line.split(";")]


def _unwrap(value: str) -> str:
    """The value without a trailing ``,``/``;`` and outside MySQL 5's ``PASSWORD(...)``."""
    v = value.rstrip(",;")
    if v.lower().startswith("password(") and v.endswith(")"):
        v = v[len("password(") : -1]  # PASSWORD('<pw>'), PASSWORD(?)
    return v


def _quoted_literal(value: str) -> bool:
    """``'<pw>'``, ``"<pw>"``, ``PASSWORD('<pw>')``: quoted, and a plain literal inside."""
    v = _unwrap(value)
    if not (v[:1] in "'\"" and v[-1:] == v[:1] and len(v) > 1):
        return False
    return _plain_literal(v[1:-1])


def _value_after(words: list[str], index: int) -> str | None:
    """The value after ``words[index]`` (``PASSWORD`` or the user of ``SET PASSWORD FOR``),
    skipping one ``=`` whether it stands alone or is glued to either side."""
    word = words[index]
    if "=" in word:
        return word.partition("=")[2] or (words[index + 1] if index + 1 < len(words) else None)
    if index + 1 >= len(words):
        return None
    following = words[index + 1]
    if following == "=":
        return words[index + 2] if index + 2 < len(words) else None
    return following[1:] if following.startswith("=") else following


def _set_password_value(words: list[str]) -> str | None:
    """``SET PASSWORD [FOR <user>] = <value>`` as a whole statement: its value."""
    lower = [w.lower() for w in words[:3]]
    if len(words) < 2 or lower[0] != "set":
        return None
    if lower[1] != "password" and not lower[1].startswith("password="):
        return None
    if "=" not in lower[1] and len(words) > 3 and lower[2] == "for":
        user = words[3]
        if "=" in user or (len(words) > 4 and words[4].startswith("=")):
            return _value_after(words, 3)
        return None
    if "=" in lower[1] or (len(words) > 2 and words[2].startswith("=")):
        return _value_after(words, 1)
    return None


def _principal_password_value(words: list[str]) -> str | None:
    """``CREATE [OR REPLACE]|ALTER USER|ROLE|LOGIN [IF [NOT] EXISTS] <name>
    [WITH|LOGIN|ENCRYPTED|UNENCRYPTED ...] PASSWORD <value>`` as a whole statement: its
    value. ``PASSWORD`` must follow the name or those words directly."""
    lower = [w.lower() for w in words[:12]]
    if not lower or lower[0] not in ("create", "alter"):
        return None
    i = 1
    if lower[i : i + 2] == ["or", "replace"]:
        i += 2
    if i >= len(lower) or lower[i] not in _PRINCIPALS:
        return None
    i += 1
    if lower[i : i + 3] == ["if", "not", "exists"]:
        i += 3
    elif lower[i : i + 2] == ["if", "exists"]:
        i += 2
    i += 1  # the name
    while i < len(lower) and lower[i] in _PASSWORD_LEAD_INS:
        i += 1
    if i >= len(lower) or not (lower[i] == "password" or lower[i].startswith("password=")):
        return None
    return _value_after(words, i)


def _statement_holds_secret(text: str) -> bool:
    """A quoted plain literal as the password of a ``SET PASSWORD [FOR <user>]`` statement
    (refused). A quoted ``CREATE|ALTER USER ... PASSWORD '...'`` is refused by main's own
    rule already."""
    for words in _statements(text):
        value = _set_password_value(words)
        if value is not None and _quoted_literal(value):
            return True
    return False


def _statement_may_hold_secret(text: str) -> bool:
    """An unquoted plain literal in ``SET PASSWORD`` or ``CREATE|ALTER USER ... PASSWORD``
    (warned about): not a keyword (``EXPIRE``, ``NULL``, ``HISTORY``: all letters), a hash
    name, or a value with a variable or template character."""
    for words in _statements(text):
        value = _set_password_value(words) or _principal_password_value(words)
        if value is None:
            continue
        v = _unwrap(value)
        if _is_value(v) and _plain_literal(v) and not v.isalpha():
            if v.lower() not in _NOT_PASSWORDS:
                return True
    return False


# --------------------------------------------------------------------------- curl


def _curl_user_value(word: str, following: str | None) -> str | None:
    """The value of ``-u``/``--user``/``--user=``/``-u<v>`` (``None`` for another word)."""
    if word in _CURL_USER_FLAGS:
        return following
    if word.startswith("--") and "=" in word:
        flag, _, value = word.partition("=")
        return value if flag in _CURL_USER_FLAGS else None
    if word.startswith("-u") and len(word) > 2 and not word.startswith("--"):
        return word[2:]
    return None


def _curl_password_is_literal(value: str) -> bool:
    """``user:<pw>`` (one pair of outer quotes removed from the whole token) whose
    password is a plain literal; ``"$user:$pass"``, ``"$USER":"$PASS"`` and
    ``user:$(cat pw)`` are not."""
    user, colon, password = _strip_one_quote_pair(value).partition(":")
    return bool(user and colon) and _plain_literal(password)


def _command_end(word: str) -> tuple[str, bool]:
    """``word`` without a glued separator, and whether the command ends with it: at
    ``;``, ``&``, ``|`` (so ``&&``, ``||``) or a trailing ``,`` or ``.``."""
    for index, char in enumerate(word):
        if char in ";&|":
            return word[:index], True
    if word[-1:] in (",", "."):
        return word.rstrip(",."), True
    return word, False


def _curl_holds_secret(text: str) -> bool:
    """``curl -u user:<pw>`` within one curl command (refused). The command ends at a
    newline (a trailing ``\\`` continues it), at ``;``, ``&&``, ``||``, ``|``, ``&``, a
    trailing ``,`` or ``.``, or at a bare word that is no flag, flag value or URL (``curl
    and docker run -u 1000:1000``). Only a plain literal password counts
    (:func:`_curl_password_is_literal`)."""
    joined = text.replace("\\\n", " ") if "\\\n" in text else text
    for line in joined.splitlines():
        words = line.split()
        in_curl = after_flag = False
        for index, raw in enumerate(words):
            word, ends = _command_end(raw)
            if not in_curl:
                in_curl = word.rpartition("/")[2].lower() == "curl" and not ends
                after_flag = False
                continue
            if word.startswith("-"):
                following = None
                if not ends and index + 1 < len(words):
                    following = _command_end(words[index + 1])[0]
                value = _curl_user_value(word, following)
                if value is not None and _curl_password_is_literal(value):
                    return True
                after_flag = "=" not in word
            elif after_flag or word[:1] in "'\"" or any(c in word for c in "./:"):
                after_flag = False  # a flag's value, a quoted value or a URL: still curl's
            else:
                in_curl = False  # a bare word: another command or prose
            if ends:
                in_curl = False
    return False


def _has_credential_shape(text: str, broad: bool = True) -> bool:
    words = text.split()
    if "@" in text and any(_userinfo_has_password(w, broad) for w in words if "@" in w):
        return True
    lower = [w.lower() for w in words]
    if _client_flags_hold_secret(words, lower):
        return True
    if "=" in text:
        for pair in _PAIR_SEPARATORS.split(_tighten(text, "=")):
            key, eq, value = pair.partition("=")
            if eq and _assignment_is_secret(key, value, broad):
                return True
    if ":" in text and broad:
        # Request fields only. In free text "users.password: bcrypt hash" or "token:
        # rotates daily" describes a column or a mechanism, not a value.
        tight = _tighten(text, ":").split()
        for index, word in enumerate(tight):
            key, colon, value = word.partition(":")
            if not colon or value.startswith("//"):
                continue
            if not value and index + 1 < len(tight):
                value = tight[index + 1]
            if is_secret_key(key.strip(_QUOTES)) and _is_value(value):
                return True
    # #1001: the two structural forms a parser can delimit exactly.
    if "password" in text.lower() and _statement_holds_secret(text):
        return True
    return "curl" in text.lower() and _curl_holds_secret(text)


# --------------------------------------------------------------------------- warnings
#
# #1001: forms that may state a credential but may as well describe one. The answers
# routes save them with a warning; the stored-file redaction never removes them.


def _looks_like_password(value: str) -> bool:
    """A word that reads as a password: letters with a digit or a symbol.

    Not a placeholder, a hash, cipher or encoding name (``SHA256``, ``base64url``), a
    number with a unit (``86400s``), hyphenated words and numbers (``base64-encoded``,
    ``AES-256-encrypted``, ``15-minutes``), or a type, call, ARN or URL (``VARCHAR(255)``,
    ``bcrypt(cost=12)``, anything with ``(`` or ``:``). A value with ``/`` or an
    identifier with ``_`` counts only with upper and lower case and a digit, 8 or more
    characters (``S3cr3t_pw9``, not ``pbkdf2_sha256`` or ``/var/lib/mysql``)."""
    v = value.strip(_QUOTES).rstrip(".,;:)")
    if not _is_value(v) or not _plain_literal(v) or not any(c.isalpha() for c in v):
        return False
    if v.isalpha():
        return False
    if v.lstrip("0123456789").isalpha():
        return False  # 86400s, 30d
    if any(c in v for c in "(:"):
        return False
    if "/" in v or ("_" in v and v.replace("_", "").isalnum()):
        return _mixed_case_with_digit(v)
    parts = v.split("-")
    return not all(p.isalpha() or p.isdigit() or p.lower() in _NOT_PASSWORDS for p in parts)


def _mixed_case_with_digit(value: str) -> bool:
    return (
        len(value) >= 8
        and any(c.isupper() for c in value)
        and any(c.islower() for c in value)
        and any(c.isdigit() for c in value)
    )


def _is_secret_word(words: list[str], index: int) -> bool:
    """``password``, ``db_password``, ``token``; or ``key`` after ``api``/``access``/..."""
    word = words[index].strip(_QUOTES + ".,;:!?()").lower()
    if word == "key" and index > 0:
        return (words[index - 1].strip(_QUOTES).lower(), "key") in _SECRET_LAST_PAIRS
    return is_secret_key(word)


def _prose_holds_secret(words: list[str]) -> bool:
    """``the password is <pw>``, ``the password for app is '<pw>'``, ``api key is <pw>``.

    Only a value that reads as a password counts (:func:`_looks_like_password`, and eight
    or more characters unless quoted). So ``the password is hashed``, ``... is 'hashed'``,
    ``... is SHA256``, ``... is VARCHAR(255)`` and ``the password policy is 90 days`` are
    descriptions. Looks at most four words ahead."""
    for index in range(len(words)):
        if not _is_secret_word(words, index):
            continue
        verb = index + 1
        if verb + 1 < len(words) and words[verb].lower() == "for":
            verb += 2  # the password for app is ...
        if verb + 1 >= len(words) or words[verb].lower() not in _PROSE_VERBS:
            continue
        value = words[verb + 1].rstrip(".,;:)")
        quoted = value[:1] in "'\"`"
        if (quoted or len(value) >= 8) and _looks_like_password(value):
            return True
    return False


def _email_shaped_userinfo(word: str) -> bool:
    """``admin:pw@db.example.com``: user info with a password before a dotted host and no
    port, which reads the same as an e-mail address after a label
    (``Owner:dba@example.com``); with a path (``.../app``) too."""
    before, at, host = word.strip(_QUOTES + ".,;)").rpartition("@")
    if not at or "://" in before or not host or "." not in host:
        return False
    user, colon, password = before.lstrip("@").partition(":")
    return bool(colon and _is_value(password) and any(c.isalpha() for c in user))


def _has_warning_shape(text: str) -> bool:
    """Free text that may hold a credential but may as well describe one (#1001): prose
    (``the password is <pw>``), user info before a dotted host (``admin:pw@db.example.com``,
    with or without a path), and an unquoted password in a ``SET PASSWORD`` or
    ``CREATE|ALTER USER`` statement. Saved with a warning, never redacted."""
    if len(text) > MAX_SCANNED_CHARS:
        return False
    words = text.split()
    if _prose_holds_secret(words):
        return True
    if "@" in text and any(_email_shaped_userinfo(w) for w in words if "@" in w):
        return True
    return "password" in text.lower() and _statement_may_hold_secret(text)


def find_credential_warnings(payload: Any) -> list[str]:
    """The paths of the strings in ``payload`` that :func:`_has_warning_shape` flags
    (paths only, never values). Strings over :data:`MAX_SCANNED_CHARS` and anything nested
    deeper than :data:`MAX_DEPTH` are not checked."""
    found: list[str] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, _join(path, str(key)[:MAX_KEY_CHARS]), depth + 1)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]", depth + 1)
        elif isinstance(node, str) and _has_warning_shape(node):
            found.append(path or "<body>")

    walk(payload, "", 0)
    return found


def credential_warnings(payload: Any) -> list[str]:
    """The warning to return with a saved answer: one message naming the flagged fields,
    or none. Logs the field paths, never the values."""
    paths = find_credential_warnings(payload)
    if not paths:
        return []
    fields = ", ".join(dict.fromkeys(paths))
    logger.warning("answer field(s) %s look like a credential; saved with a warning", fields)
    return [WARNING_MESSAGE.format(fields=fields)]


def _decoded_variants(text: str, budget: _Budget) -> list[str]:
    """The string plus its percent- and base64-decoded forms, one level each (callers
    recurse), de-duplicated and charged to ``budget``."""
    variants = [text]
    unquoted = unquote(text)
    if unquoted != text and budget.spend(unquoted):
        variants.append(unquoted)
    for candidate in list(variants):
        stripped = candidate.strip()
        if len(stripped) >= 12 and len(stripped) % 4 == 0 and set(stripped) <= _B64_CHARS:
            try:
                decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if decoded.isprintable() and budget.spend(decoded):
                variants.append(decoded)
    return list(dict.fromkeys(variants))


def _string_holds_secret(
    text: str, decode_depth: int = 0, budget: _Budget | None = None, broad: bool = True
) -> bool:
    budget = budget if budget is not None else _Budget()
    if _has_credential_shape(text, broad):
        return True
    if decode_depth >= MAX_DECODE_DEPTH or budget.exhausted:
        return False
    variants = _decoded_variants(text, budget)
    for variant in variants[1:]:
        if _string_holds_secret(variant, decode_depth + 1, budget, broad):
            return True
    for variant in variants:
        stripped = variant.strip()
        if stripped[:1] not in ("{", "["):
            continue
        try:
            inner = json.loads(stripped)
        except (ValueError, RecursionError):
            continue
        nested = Findings()
        _walk(inner, "", 0, nested, True, budget, broad, decode_depth + 1)
        if nested.secrets:
            return True
    return False


# --------------------------------------------------------------------------- walk


def _join(path: str, segment: str) -> str:
    shown = segment if _SAFE_SEGMENT.fullmatch(segment) else "<field>"
    return f"{path}.{shown}" if path else shown


def _is_empty(value: Any) -> bool:
    return value is None or value is False or value == "" or value == {} or value == []


def _walk(
    node: Any,
    path: str,
    depth: int,
    out: Findings,
    check_keys: bool,
    budget: _Budget,
    broad: bool = True,
    decode_depth: int = 0,
) -> None:
    if depth > MAX_DEPTH:
        out.too_large.append(path or "<body>")
        return
    if isinstance(node, dict):
        for key, value in node.items():
            key = str(key)
            child = _join(path, key)
            if len(key) > MAX_KEY_CHARS:
                out.too_large.append(child)
                continue
            if check_keys and is_secret_key(key):
                if not _is_empty(value):
                    out.secrets.append(child)
                continue
            _walk(value, child, depth + 1, out, check_keys, budget, broad, decode_depth)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(
                value, f"{path}[{index}]", depth + 1, out, check_keys, budget, broad, decode_depth
            )
    elif isinstance(node, str):
        if len(node) > MAX_SCANNED_CHARS:
            out.too_large.append(path or "<body>")
        elif decode_depth <= MAX_DECODE_DEPTH and _string_holds_secret(
            node, decode_depth, budget, broad
        ):
            out.secrets.append(path or "<body>")
        if budget.exhausted:
            out.too_large.append(path or "<body>")


def scan(payload: Any, check_keys: bool = True, broad: bool = True) -> Findings:
    """Where ``payload`` carries a secret, or is too large to check (paths, never values).

    ``check_keys=False`` checks string values for credential shapes only, for bodies whose
    keys are data (question ids) rather than field names. ``broad=False`` is the
    free-text reading (see the module docstring).
    """
    out = Findings()
    _walk(payload, "", 0, out, check_keys, _Budget(), broad)
    out.too_large = list(dict.fromkeys(out.too_large))
    return out


def find_submitted_secrets(payload: Any) -> list[str]:
    """The paths of every field in ``payload`` that carries a secret or is too large to
    check. Empty means the payload is safe to store."""
    return scan(payload).paths()


REDACTED = "<redacted>"


def _redact_string(node: str, budget: _Budget) -> str:
    # Over-long strings are not secrets: they are left exactly as they are.
    if len(node) > MAX_SCANNED_CHARS:
        return node
    # Only the forms the routes refuse. What they only warn about (prose, e-mail-shaped
    # user info, #1001) may be a description, so it is never removed from disk.
    return REDACTED if _string_holds_secret(node, 0, budget, broad=False) else node


def strip_secrets(node: Any, depth: int = 0, budget: _Budget | None = None) -> Any:
    """A copy of ``node`` without credentials, for files stored before #723.

    Secret-named keys with a value are removed and strings that hold an unambiguous
    credential become ``<redacted>``. Nothing else changes: an over-long string, anything
    nested deeper than :data:`MAX_DEPTH`, and text that only looks like one (an e-mail
    address after a label) are kept as they are."""
    budget = budget if budget is not None else _Budget()
    if depth > MAX_DEPTH:
        return node
    if isinstance(node, dict):
        return {
            k: strip_secrets(v, depth + 1, budget)
            for k, v in node.items()
            if not (is_secret_key(str(k)) and not _is_empty(v))
        }
    if isinstance(node, list):
        return [strip_secrets(v, depth + 1, budget) for v in node]
    if isinstance(node, str):
        return _redact_string(node, budget)
    return node


def redact_credential_shapes(node: Any, depth: int = 0, budget: _Budget | None = None) -> Any:
    """Like :func:`strip_secrets`, but keys are kept: only string values are checked."""
    budget = budget if budget is not None else _Budget()
    if depth > MAX_DEPTH:
        return node
    if isinstance(node, dict):
        return {k: redact_credential_shapes(v, depth + 1, budget) for k, v in node.items()}
    if isinstance(node, list):
        return [redact_credential_shapes(v, depth + 1, budget) for v in node]
    if isinstance(node, str):
        return _redact_string(node, budget)
    return node


# --------------------------------------------------------------------------- requests


def _query_findings(query: str) -> Findings:
    out = Findings()
    if len(query) > MAX_SCANNED_CHARS:
        out.too_large.append("query")
        return out
    for key, value in parse_qsl(query, keep_blank_values=True):  # repeated keys too
        found = scan({key: value})
        out.secrets += [f"query.{p}" for p in found.secrets]
        out.too_large += [f"query.{p}" for p in found.too_large]
    return out


def inspect_request(query: str, body: bytes, check_keys: bool = True) -> Findings:
    """Everything the guard checks in one request: query, body, ``credentials_type``."""
    out = _query_findings(query)
    if len(body) > MAX_GUARDED_BODY_BYTES:
        out.too_large.append("<body>")
        return out
    if not body:
        return out
    try:
        payload = json.loads(body)
    except RecursionError:
        out.too_large.append("<body>")
        return out
    except ValueError:
        return out  # not JSON: the body model's 422 handles it, without echoing input
    # Free-text bodies (agent answers) get the free-text reading.
    found = scan(payload, check_keys=check_keys, broad=check_keys)
    out.secrets += found.secrets
    out.too_large += found.too_large
    connection = payload.get("connection") if isinstance(payload, dict) else None
    if isinstance(connection, dict):
        kind = connection.get("credentials_type")
        if isinstance(kind, str) and kind.strip().lower() == "direct":
            out.secrets.append("connection.credentials_type=direct")
    return out


def _raise_for(found: Findings) -> None:
    if found.secrets:
        fields = ", ".join(dict.fromkeys(found.secrets))
        # Paths only: the value never reaches a log line.
        logger.warning("refused request: field(s) %s must be sent by reference", fields)
        raise HTTPException(status_code=400, detail=REFUSAL_MESSAGE.format(fields=fields))
    if found.too_large:
        fields = ", ".join(dict.fromkeys(found.too_large))
        logger.warning("refused request: field(s) %s too large to check", fields)
        raise HTTPException(status_code=413, detail=TOO_LARGE_MESSAGE.format(fields=fields))


async def refuse_submitted_secrets(request: Request) -> None:
    """FastAPI dependency: 400 with secrets-manager guidance when the request carries a
    secret, 413 when it is too large to check. Runs before the body model is validated."""
    body = await request.body()
    found = await run_in_threadpool(inspect_request, request.url.query, body)
    _raise_for(found)


async def refuse_credential_shapes(request: Request) -> None:
    """Like :func:`refuse_submitted_secrets`, for free-text bodies whose keys are data
    (agent answers keyed by question id): only string values are checked."""
    body = await request.body()
    found = await run_in_threadpool(inspect_request, request.url.query, body, False)
    _raise_for(found)


class RedactSecretQueryFilter(logging.Filter):
    """Drop the query string from an access-log line whose query carries a secret.

    The request itself is refused by :func:`refuse_submitted_secrets`; this keeps the
    server's own access log from recording the secret. ``uvicorn.access`` logs with
    ``args = (client_addr, method, full_path, http_version, status_code)`` (uvicorn's
    ``httptools_impl``/``h11_impl``), so the path with its query is ``args[2]``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            parts = urlsplit(args[2])
            if parts.query and _query_findings(parts.query):
                redacted = urlunsplit(parts._replace(query=REDACTED))
                record.args = (*args[:2], redacted, *args[3:])
        return True
