# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SCRAM-SHA-256 verifier of the routine proof's PostgreSQL runner role (#2352).

The vectors are RFC 7677's exchange and two verifiers a local PostgreSQL 17 stored for
``CREATE ROLE`` given a plain password, with SCRAM-SHA-256 as its password encryption.
The module is imported inside each test, so on a branch without it a test fails in its body."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
from typing import Any

import pytest


def _auth() -> Any:
    return importlib.import_module("src.tools.routines.postgresql_auth")


RFC_PROOF = b"dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="  # pragma: allowlist secret
RFC_SERVER_SIGNATURE = b"6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="  # pragma: allowlist secret


def test_the_keys_match_the_rfc_7677_exchange():
    """RFC 7677 section 3: user ``user``, password ``pencil``, salt ``W22ZaJ0SNY7soEsUEjb6gQ==``,
    4096 iterations. The client proof and the server signature built from the keys here are
    the RFC's, so StoredKey and ServerKey are SCRAM-SHA-256's own."""
    auth = _auth()
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    client_key, stored_key, server_key = auth.scram_keys("pencil", salt, 4096)
    message = (
        b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
        b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
        b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    )
    signature = hmac.new(stored_key, message, hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, signature, strict=True))
    assert base64.b64encode(proof) == RFC_PROOF
    server = hmac.new(server_key, message, hashlib.sha256).digest()
    assert base64.b64encode(server) == RFC_SERVER_SIGNATURE
    assert hashlib.sha256(client_key).digest() == stored_key


#: What a local PostgreSQL 17 stored for each password (its salt reused here).
POSTGRESQL_17 = {
    "pencil": "SCRAM-SHA-256$4096:oVOetINChzTFF3ypbWtgjA==$e28C/6S2BKj8VA7B6BqwacoJqWke1TyNQ0vjn0A8ljQ="
    ":49yvqcy8ElAeGzQ5TrS57JwiY5HTjl2bUdRqAY1C9x8=",
    "Tr0ub4dor-x_yZ": "SCRAM-SHA-256$4096:FBZAnUFvMAXRSb9Sup6l1w==$7Ncz4ZZVnXeHVJJoiTUe30iJQU9lat8Ok017"
    "mufLCJM=:XUoMB8IVLes6UoUzPeFvHBtcIWXkssH0/piVuXSrxSA=",
}  # pragma: allowlist secret


@pytest.mark.parametrize("password", sorted(POSTGRESQL_17))
def test_the_verifier_is_the_one_postgresql_stores(password):
    auth = _auth()
    stored = POSTGRESQL_17[password]
    salt = base64.b64decode(stored.split("$")[1].split(":")[1])
    assert auth.scram_verifier(password, salt) == stored
    assert auth.VERIFIER.fullmatch(stored)


def test_a_new_verifier_has_postgresqls_form_and_a_fresh_salt():
    auth = _auth()
    one, two = auth.scram_verifier("same-password"), auth.scram_verifier("same-password")
    assert auth.VERIFIER.fullmatch(one) and auth.VERIFIER.fullmatch(two) and one != two
    assert one.startswith("SCRAM-SHA-256$4096:") and "'" not in one and "\\" not in one
    with pytest.raises(ValueError, match="16 bytes"):
        auth.scram_verifier("x", b"short")


def test_only_the_right_password_matches():
    auth = _auth()
    stored = POSTGRESQL_17["pencil"]
    assert auth.matches("pencil", stored)
    assert not auth.matches("pencil!", stored) and not auth.matches("Pencil", stored)
    assert not auth.matches("pencil", "md5" + "0" * 32) and not auth.matches("pencil", "pencil")


def test_a_password_saslprep_would_change_is_refused():
    """PostgreSQL runs the password through SASLprep first, which leaves printable ASCII as
    it is; the module takes no other password rather than prepare one itself."""
    auth = _auth()
    for password in ("café", "tab\t", "nbsp\u00a0", ""):
        with pytest.raises(ValueError, match="printable ASCII"):
            auth.scram_verifier(password)
    assert not auth.matches("café", POSTGRESQL_17["pencil"])


def test_the_literal_holds_no_colon_and_reads_back_as_the_verifier():
    """The colons are written ``\\x3a`` in an escape string, so the statement's text holds no
    ``:name`` a Data API parameter mark could be read from; PostgreSQL reads it back as the
    verifier (checked on a local PostgreSQL 17)."""
    auth = _auth()
    stored = POSTGRESQL_17["pencil"]
    literal = auth.verifier_literal(stored)
    assert literal.startswith("E'") and literal.endswith("'") and ":" not in literal
    assert literal[2:-1].replace("\\x3a", ":") == stored
    with pytest.raises(ValueError, match="not a SCRAM-SHA-256 verifier"):
        auth.verifier_literal("pencil' OR '1")
