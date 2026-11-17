# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SCRAM-SHA-256 verifier of a PostgreSQL login role, computed here (#2352), so the
routine proof's runner role is created with ``CREATE ROLE ... LOGIN PASSWORD '<verifier>'``
and its password never appears in SQL text or in a parameter. PostgreSQL stores a password
given in this form as it is, without hashing it again.

The form is PostgreSQL's own (``scram_build_secret`` in ``src/common/scram-common.c``, RFC
5802 and RFC 7677): ``SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>``, each part
in standard base 64, where ``SaltedPassword = PBKDF2-HMAC-SHA-256(password, salt,
iterations)``, ``StoredKey = SHA-256(HMAC(SaltedPassword, "Client Key"))`` and ``ServerKey =
HMAC(SaltedPassword, "Server Key")``. PostgreSQL's defaults are kept: 4,096 iterations and a
16-byte salt. PostgreSQL runs the password through SASLprep first; that leaves printable
ASCII unchanged, and the run's password is printable ASCII (``secrets.token_urlsafe``), so
any other password is refused here rather than prepared. Pure Python, on :mod:`hashlib` only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

#: PostgreSQL's default ``scram_iterations``.
ITERATIONS = 4096
#: PostgreSQL's salt length, in bytes.
SALT_BYTES = 16
#: A verifier this module writes: base 64 only, so no quote and no backslash.
VERIFIER = re.compile(
    r"^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=$"
)
_PRINTABLE = re.compile(r"^[\x20-\x7e]+$")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def scram_keys(
    password: str, salt: bytes, iterations: int = ITERATIONS
) -> tuple[bytes, bytes, bytes]:
    """``(ClientKey, StoredKey, ServerKey)`` of ``password`` with ``salt``."""
    if not _PRINTABLE.fullmatch(password):
        raise ValueError("the runner's password is printable ASCII, which SASLprep leaves as it is")
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations)
    client = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return client, hashlib.sha256(client).digest(), server


def scram_verifier(password: str, salt: bytes | None = None) -> str:
    """The SCRAM-SHA-256 verifier of ``password``, the form ``CREATE ROLE ... PASSWORD``
    stores as it is."""
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    if len(salt) != SALT_BYTES:
        raise ValueError("a SCRAM-SHA-256 salt here is 16 bytes")
    _, stored, server = scram_keys(password, salt)
    verifier = f"SCRAM-SHA-256${ITERATIONS}:{_b64(salt)}${_b64(stored)}:{_b64(server)}"
    if not VERIFIER.fullmatch(verifier):  # pragma: no cover - built above
        raise ValueError("not a SCRAM-SHA-256 verifier")
    return verifier


def verifier_literal(verifier: str) -> str:
    """``verifier`` as a PostgreSQL escape-string literal, each ``:`` written ``\\x3a``, so
    the statement's text holds no ``:name`` the Data API could read as a parameter mark.
    PostgreSQL reads the literal back as the verifier itself."""
    if not VERIFIER.fullmatch(verifier):
        raise ValueError("not a SCRAM-SHA-256 verifier")
    return "E'" + verifier.replace(":", "\\x3a") + "'"


def matches(password: str, verifier: str) -> bool:
    """Whether ``verifier`` is ``password``'s, as the server checks a sign-in."""
    if not VERIFIER.fullmatch(verifier):
        return False
    salt = base64.b64decode(verifier.split("$")[1].split(":")[1])
    try:
        expected = scram_verifier(password, salt)
    except ValueError:
        return False
    return secrets.compare_digest(verifier, expected)
