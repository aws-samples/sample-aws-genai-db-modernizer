# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``caching_sha2_password`` authentication string of a MySQL account, computed here
(#2298), so the routine proof's runner user is created with
``IDENTIFIED WITH caching_sha2_password AS '<string>'`` and its password never appears in SQL
text: the RDS Data API takes no bound parameter in ``CREATE USER ... IDENTIFIED BY ?``.

MySQL 8.0 stores that string as ``$A$005$`` followed by a 20-character salt and a 43-character
digest (70 characters in all), and an ``AS`` clause must give exactly that form: ``A`` names
SHA-256, ``005`` is the round count in thousands, and the digest is Ulrich Drepper's SHA-256
crypt (the algorithm of glibc's ``$5$``) of the password with that salt and 5,000 rounds, in
crypt's own base-64 alphabet. The one difference from glibc's ``$5$`` is the salt's length:
glibc cuts a salt at 16 characters, MySQL keeps 20. :func:`sha256_crypt_digest` therefore takes
the salt as given and never cuts it. Pure Python, on :mod:`hashlib` only.
"""

from __future__ import annotations

import hashlib
import re
import secrets

#: crypt's base-64 alphabet, as MySQL's ``crypt_genhash_impl`` writes the digest.
_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
#: MySQL's salt length for caching_sha2_password (glibc's ``$5$`` keeps 16).
SALT_LENGTH = 20
#: The round count MySQL writes as ``005``: 5 x 1,000.
ROUNDS = 5000
#: An authentication string this module writes: no quote, backslash or ``$`` in the salt, so
#: it can stand inside a single-quoted SQL literal unchanged.
AUTH_STRING = re.compile(r"^\$A\$005\$[./0-9A-Za-z]{20}[./0-9A-Za-z]{43}$")

#: The 32 digest bytes, three at a time, in the order Drepper's SHA-256 crypt encodes them.
_ORDER = ((0, 10, 20), (21, 1, 11), (12, 22, 2), (3, 13, 23), (24, 4, 14), (15, 25, 5),
          (6, 16, 26), (27, 7, 17), (18, 28, 8), (9, 19, 29))  # fmt: skip


def _b64(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_ITOA64[w & 0x3F])
        w >>= 6
    return "".join(out)


def _repeat(digest: bytes, length: int) -> bytes:
    return (digest * (length // len(digest) + 1))[:length]


def sha256_crypt_digest(password: bytes, salt: bytes, rounds: int = ROUNDS) -> str:
    """Drepper's SHA-256 crypt digest (43 characters) of ``password`` with ``salt``, taken as
    given (never cut at 16 characters, as MySQL keeps 20)."""
    sha = hashlib.sha256
    alt = sha(password + salt + password).digest()
    a = sha(password + salt)
    n = len(password)
    while n > 32:
        a.update(alt)
        n -= 32
    a.update(alt[:n])
    n = len(password)
    while n > 0:
        a.update(alt if n & 1 else password)
        n >>= 1
    digest = a.digest()
    p = _repeat(sha(password * len(password)).digest(), len(password))
    s = _repeat(sha(salt * (16 + digest[0])).digest(), len(salt))
    for r in range(rounds):
        c = sha(p if r & 1 else digest)
        if r % 3:
            c.update(s)
        if r % 7:
            c.update(p)
        c.update(digest if r & 1 else p)
        digest = c.digest()
    return "".join(_b64(digest[i], digest[j], digest[k], 4) for i, j, k in _ORDER) + _b64(
        0, digest[31], digest[30], 3
    )


def new_salt() -> str:
    """A random 20-character salt from crypt's alphabet (no quote, backslash or ``$``)."""
    return "".join(secrets.choice(_ITOA64) for _ in range(SALT_LENGTH))


def caching_sha2_auth_string(password: str, salt: str | None = None) -> str:
    """The ``caching_sha2_password`` authentication string of ``password``, the form
    ``CREATE USER ... IDENTIFIED WITH caching_sha2_password AS '<string>'`` takes."""
    salt = new_salt() if salt is None else salt
    if len(salt) != SALT_LENGTH or any(ch not in _ITOA64 for ch in salt):
        raise ValueError("a caching_sha2_password salt is 20 characters of crypt's alphabet")
    auth = f"$A$005${salt}" + sha256_crypt_digest(password.encode("utf-8"), salt.encode("ascii"))
    if not AUTH_STRING.fullmatch(auth):  # pragma: no cover - built above
        raise ValueError("not a caching_sha2_password authentication string")
    return auth


def matches(password: str, auth: str) -> bool:
    """Whether ``auth`` (a ``$A$005$`` string) is ``password``'s, as the server checks it."""
    if not AUTH_STRING.fullmatch(auth):
        return False
    salt = auth[7 : 7 + SALT_LENGTH]
    return secrets.compare_digest(auth, caching_sha2_auth_string(password, salt))
