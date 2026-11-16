# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The caching_sha2_password authentication string the routine proof's runner user is
created with (#2298), against known vectors. The module under test is imported inside each
test, so on a branch without it a test fails in its body."""

from __future__ import annotations

import importlib
from typing import Any

import pytest


def _ma() -> Any:
    return importlib.import_module("src.tools.routines.mysql_auth")


#: Drepper's SHA-256 crypt with 5,000 rounds (glibc ``$5$``, a 16-character salt at most):
#: the first is the specification's own vector, all three agree with ``openssl passwd -5``.
SHA256_CRYPT = [
    ("Hello world!", "saltstring", "5B8vYYiY.CVt1RlTTf8KbXBH3hsxY/GNooZaBBGWEc5"),  # pragma: allowlist secret
    ("x", "saltstring", "8lNOPUYH/6hNVwI72WGU4WW1KQqblnIzRCd9nWOKs5A"),  # pragma: allowlist secret
    ("a" * 70, "saltstring", "YbOpHS5oOxi.H5n.hqPAp3huEcjb3Z1IDPoKxWGH8F4"),  # pragma: allowlist secret
]  # fmt: skip

#: ``HEX(authentication_string)`` of users a MySQL 8.0.46 server created with
#: ``IDENTIFIED WITH caching_sha2_password BY '<password>'``: a 20-byte salt of any byte but
#: NUL and ``$`` (none of crypt's alphabet here), then the digest.
MYSQL_80 = [
    ("Hello world!", "244124303035245032324B39012A233E160A6D20164E6C3B214173362F3234614633706B6F31687762"  # pragma: allowlist secret
     "6168356C53435A435658585769716E34565A344B7A704442307A715743"),  # pragma: allowlist secret
    ("x", "24412430303524310370060D38730231792C13061B6472582C3530744C4A6435304C575876726347577471"  # pragma: allowlist secret
     "524A6C4D70457A34686741352F502F5A414E4E717A54347A504D41"),  # pragma: allowlist secret
    ("a" * 70, "2441243030352436736C1E7D280939704E06070D7449262D616C1B72473651354D393575456D49745055"  # pragma: allowlist secret
     "6B5675372F53416B3274726C6E536656646D45535661515A6156412E"),  # pragma: allowlist secret
    ("Zk3_-pQ9rT2vW8xY1aB4cD6eF0gH7iJ5", "2441243030352451276E686E122A23675A7E4175640F0C4E0F14273"  # pragma: allowlist secret
     "87A462E55744D643272526E446156724A37615444516858704144765968626B5A6B4867456755646B7234"),  # pragma: allowlist secret
]  # fmt: skip


@pytest.mark.parametrize(("password", "salt", "digest"), SHA256_CRYPT)
def test_the_digest_is_drepper_sha256_crypt(password, salt, digest):
    ma = _ma()
    assert ma.sha256_crypt_digest(password.encode(), salt.encode()) == digest


@pytest.mark.parametrize(("password", "stored"), MYSQL_80)
def test_the_digest_matches_what_mysql_stores_with_its_20_byte_salt(password, stored):
    """MySQL keeps all 20 salt bytes, where glibc cuts a salt at 16: the digest must too."""
    ma = _ma()
    raw = bytes.fromhex(stored)
    assert len(raw) == 70 and raw[:7] == b"$A$005$"
    assert ma.sha256_crypt_digest(password.encode(), raw[7:27]) == raw[27:].decode("ascii")
    assert ma.sha256_crypt_digest(password.encode(), raw[7:23]) != raw[27:].decode("ascii")


def test_the_authentication_string_is_mysqls_exact_form():
    """``$A$005$``, 20 salt characters, 43 digest characters: MySQL refuses any other length
    with 1827 ("The password hash doesn't have the expected format"), and the alphabet keeps
    quotes, backslashes and ``$`` out of the SQL literal."""
    ma = _ma()
    salt = "a5XZDNjiFGBi5YNZXLqC"
    auth = ma.caching_sha2_auth_string("S3cret-pass_word", salt)
    assert len(auth) == 70 and auth.startswith("$A$005$" + salt)
    assert auth[27:] == ma.sha256_crypt_digest(b"S3cret-pass_word", salt.encode())
    assert ma.AUTH_STRING.fullmatch(auth)
    fresh = ma.caching_sha2_auth_string("S3cret-pass_word")
    assert ma.AUTH_STRING.fullmatch(fresh) and fresh != ma.caching_sha2_auth_string(
        "S3cret-pass_word"
    )


@pytest.mark.parametrize(
    "salt", ["short", "a" * 21, "a5XZDNjiFGBi5YNZXLq'", "a5XZDNjiFGBi5YNZXLq$"]
)
def test_a_salt_outside_the_form_is_refused(salt):
    ma = _ma()
    with pytest.raises(ValueError):
        ma.caching_sha2_auth_string("pw", salt)


def test_matches_reads_the_salt_back_and_checks_the_password():
    ma = _ma()
    auth = ma.caching_sha2_auth_string("right")
    assert ma.matches("right", auth) and not ma.matches("wrong", auth)
    assert not ma.matches("right", auth[:-1]) and not ma.matches("right", "*" + "0" * 40)
