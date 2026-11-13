# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/llm/private_scan.py: a designer's example values pass, nothing else does (#2053).

The scan allows exactly what ``src/shared/example_values.py`` defines (documentation
address ranges for IP findings, angle-bracket placeholders for key-name findings, RFC
2606 domains for emails), and ``redact`` replaces each flagged value with a marker that
names its kind, never a whole line, and a private-key block whole.
"""

from __future__ import annotations

import json
import random
import string

import pytest

from ci.llm import private_scan, replay, run

# Built at run time so no scanner reads a credential-shaped literal in this file.
_LOOKS_REAL = "q9Zk3vLmP0aB7cXy" + "W2rT8nE5"  # pragma: allowlist secret
_PASSWORD = "hunter2" + "xyz9"  # pragma: allowlist secret
_KEY_HEAD = "-----BEGIN RSA PRIVATE" + " KEY-----"  # pragma: allowlist secret
_KEY_TAIL = "-----END RSA PRIVATE" + " KEY-----"  # pragma: allowlist secret
_KEY_BODY = ["MIIEow" + "IBAAKCAQEA" + "Zk3vLmP0aB7c" * 4, "W2rT8nE5q9Zk" * 5]


def _ip(value: str) -> list[str]:
    return private_scan.secret_findings(f'  "ip_address": "{value}",')


@pytest.mark.parametrize(
    "address",
    [
        "192.0.2.1",
        "198.51.100.200",
        "203.0.113.9:8080",
        "192.0.2.0/24",
        "203.0.113.128/25",
        "2001:db8::1",
        "2001:db8:ffff::9",
        "::ffff:192.0.2.1",
    ],
)
def test_a_documentation_address_passes(address: str) -> None:
    assert _ip(address) == []


@pytest.mark.parametrize(
    "address",
    ["192.0.2.0/16", "2606:4700::1111", "2001:db9::1", "::ffff:8.8.8.8"],
    ids=["wider-than-its-range", "public-ipv6", "ipv6-near-miss", "ipv4-mapped-public"],
)
def test_a_public_address_fails(address: str) -> None:
    assert _ip(address) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "address", ["203.0.114.1", "203.0.112.255", "198.51.101.3", "192.0.3.1", "8.8.4.4:53"]
)
def test_a_real_address_near_a_documentation_range_still_fails(address: str) -> None:
    assert _ip(address) == ["Public IP (ipv4) at line 1"]


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "value",
    ["::1", "fe80::1", "5.7.44", "1.3.6.1.4.1.311.21.8", "12:30:45", "Ab::Cd"],
    ids=["loopback", "link-local", "version", "oid", "time", "hex-word"],
)
def test_what_is_not_a_public_address_is_no_finding(value: str) -> None:
    assert _ip(value) == []


@pytest.mark.parametrize(
    "line",
    [
        "password=<password>",
        "DB_PASSWORD=<db-password>",
        '"user_password": "<db password>"',
    ],
    ids=["password-assignment", "env-password", "json-password"],
)
def test_a_placeholder_passes_a_key_name_finding(line: str) -> None:
    assert private_scan.secret_findings(line) == []


@pytest.mark.parametrize(
    "line",
    [
        "db_password=192.0.2.5",
        '"password": "<token>' + _LOOKS_REAL + '"',
        '"user_password": "<' + _PASSWORD + '>"',
    ],
    ids=["documentation-ip-as-a-password", "placeholder-prefix", "secret-in-brackets"],
)
def test_an_allowance_holds_only_for_its_own_kind_and_shape(line: str) -> None:
    assert private_scan.secret_findings(line) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "line",
    ['"access_token": "' + _LOOKS_REAL + '"', "password=" + _PASSWORD],
    ids=["access-token", "password-assignment"],
)
def test_a_real_looking_secret_still_fails(line: str) -> None:
    assert private_scan.secret_findings(line) != []


@pytest.mark.parametrize(
    "email",
    ["user@example.com", "a@example.org", "b@example.net", "c@mail.example.com", "d@shop.example"],
)
def test_an_example_email_passes(email: str) -> None:
    assert private_scan.identity_findings(f'"email": "{email}"') == []


@pytest.mark.parametrize("email", ["a@example.com.evil.org", "b@examples.com", "c@example.co"])
def test_a_look_alike_email_fails(email: str) -> None:
    assert private_scan.identity_findings(f'"email": "{email}"') == ["email at line 1"]


def _design() -> dict:
    return {
        "collections": [
            {
                "collection_name": "user_auth_tokens",
                "document_examples": [
                    {
                        "client_ip": "8.8.4.4",
                        "doc_ip": "203.0.113.9",
                        "password": _PASSWORD,
                        "token": "<token>",
                        "access_token": _LOOKS_REAL,
                        "contact": "jane@gmail.com",
                        # A password assignment whose value holds a public IP: two
                        # findings, one inside the other.
                        "dsn_options": "db_password=8.8.8.8" + _PASSWORD,
                        "n": 1,
                    }
                ],
            }
        ]
    }


def test_redact_replaces_only_the_values_and_a_pretty_printed_design_stays_valid_json() -> None:
    text = json.dumps(_design(), indent=2) + "\n"
    result = private_scan.redact(text, emails=True)
    assert result.complete and result.redactable
    assert {kind for kind, _ in result.findings} == {
        "Public IP (ipv4)",
        "password value",
        "token value",
        "email",
    }
    example = json.loads(result.text)["collections"][0]["document_examples"][0]
    assert example == {
        "client_ip": "[REDACTED:public-ip-ipv4]",
        "doc_ip": "203.0.113.9",
        "password": "[REDACTED:password-value]",
        "token": "<token>",
        "access_token": "[REDACTED:token-value]",
        "contact": "[REDACTED:email]",
        "dsn_options": "db_password=[REDACTED:password-value]",
        "n": 1,
    }
    for value in (_PASSWORD, _LOOKS_REAL, "8.8.4.4", "8.8.8.8", "gmail"):
        assert value not in result.text
    assert result.text.count("\n") == text.count("\n")


def test_redact_leaves_a_clean_text_alone() -> None:
    text = '{\n  "ip": "198.51.100.4",\n  "token": "<token>"\n}\n'
    assert private_scan.redact(text, emails=True) == private_scan.Redaction(text)


def test_a_private_key_on_one_json_line_goes_whole_and_is_not_redactable() -> None:
    key = "\n".join([_KEY_HEAD, *_KEY_BODY, _KEY_TAIL]) + "\n"
    text = json.dumps({"tls": {"key": key, "n": 1}}, indent=2) + "\n"
    result = private_scan.redact(text)
    assert json.loads(result.text)["tls"] == {"key": "[REDACTED:private-key]\n", "n": 1}
    assert all(part not in result.text for part in _KEY_BODY) and "PRIVATE" not in result.text
    assert ("Private Key", 3) in result.findings
    assert not result.redactable


def test_a_raw_private_key_block_goes_whole_and_later_lines_keep_their_numbers() -> None:
    text = "\n".join(["a", _KEY_HEAD, *_KEY_BODY, _KEY_TAIL, "b 8.8.8.8", ""])
    result = private_scan.redact(text)
    assert all(part not in result.text for part in _KEY_BODY) and "PRIVATE" not in result.text
    assert result.findings == [
        ("Private Key", 2),
        ("key material", 3),
        ("key material", 4),
        ("Private Key", 5),
        ("Public IP (ipv4)", 6),
    ]
    assert result.text.splitlines()[5] == "b [REDACTED:public-ip-ipv4]"
    assert not result.redactable


def test_a_value_that_cannot_be_placed_makes_the_redaction_incomplete(monkeypatch) -> None:
    from src.shared import secret_scan

    # The scan's lines are read by the shared definition (#1933).
    monkeypatch.setattr(
        secret_scan,
        "_line_hits",
        lambda body, emails, *_: [("token value", None)] if _LOOKS_REAL in body else [],
    )
    result = private_scan.redact(f"a\nkey: {_LOOKS_REAL}\nb\n")
    assert result.findings == [("token value", 2)]
    assert not result.complete and not result.redactable


def test_a_redaction_marker_is_not_itself_a_finding() -> None:
    assert private_scan.secret_findings("password=[REDACTED:password-value]") == []
    assert private_scan.redaction_marker("Public IP (ipv4)") == "[REDACTED:public-ip-ipv4]"


# ---------------------------------------------------------------------------
# Fix round 2: escapes, key material, more key-value forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ['"note": "Seen from:\\n8.8.4.4 today"', '"note": "ip\\t2606:4700::1111"'],
    ids=["ipv4-after-newline-escape", "ipv6-after-tab-escape"],
)
def test_an_ip_right_after_a_json_escape_is_found(text: str) -> None:
    assert private_scan.secret_findings(text) != []


def test_a_redaction_after_a_json_escape_keeps_the_json_valid() -> None:
    doc = {"note": "Contact:\njane@gmail.com\tfrom 8.8.4.4\nok", "n": 1}
    result = private_scan.redact(json.dumps(doc, indent=2), emails=True)
    assert json.loads(result.text) == {
        "note": "Contact:\n[REDACTED:email]\tfrom [REDACTED:public-ip-ipv4]\nok",
        "n": 1,
    }


def test_an_ipv6_network_wider_than_its_range_fails() -> None:
    assert _ip("2001:db8::/16") != [] and _ip("2001:db8:1::/48") == []


@pytest.mark.parametrize(
    "line",
    [
        '{\\"user_password\\": \\"' + _PASSWORD + '\\"}',
        "| password | " + _PASSWORD + " |",
        "**password**: " + _PASSWORD,
        '\\"access_token\\": \\"' + _LOOKS_REAL + '\\"',
    ],
    ids=["double-escaped-json", "markdown-table", "markdown-bold", "double-escaped-token"],
)
def test_more_key_value_forms_are_found(line: str) -> None:
    assert private_scan.secret_findings(line) != []


@pytest.mark.parametrize(
    "line",
    [
        '"apiKeySource": "none"',  # pragma: allowlist secret
        '"wp_woocommerce_api_keys": "dynamodb"',  # pragma: allowlist secret
        '"password": null',
    ],
    ids=["empty-literal", "engine-id", "json-null"],
)
def test_a_non_secret_literal_under_a_key_name_is_no_finding(line: str) -> None:
    assert private_scan.secret_findings(line) == []


def test_key_material_without_its_header_is_found_raw_and_json_escaped() -> None:
    raw = "\n".join(["x", *_KEY_BODY, _KEY_BODY[0], "y"])
    assert {kind for kind, _ in private_scan.key_material(raw)} == {"key material"}
    escaped = json.dumps({"content": "\n".join([*_KEY_BODY, _KEY_BODY[1]]) + "\n"})
    assert private_scan.key_material(escaped) == [("key material", 1)]
    assert private_scan.key_material(json.dumps({"content": _KEY_TAIL})) == [("Private Key", 1)]
    assert not private_scan.redact(raw).redactable


def test_hex_digests_and_a_single_body_line_are_not_key_material() -> None:
    digests = "\n".join(["0123456789abcdef" * 3] * 5)
    assert private_scan.key_material(digests) == []
    assert private_scan.key_material("a\n" + _KEY_BODY[0] + "\nb") == []


# ---------------------------------------------------------------------------
# Fix round 3: every pair on a line, key material inside a line, escapes
# ---------------------------------------------------------------------------

_OTHER = "Qm8xT2" + "pLw9Zr4"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "line",
    [
        '{"x_api_keys": "dynamodb", "api_key": "' + _LOOKS_REAL + '"}',  # pragma: allowlist secret
        '{"apiKeySource": "none", "priv_key": "' + _LOOKS_REAL + '"}',  # pragma: allowlist secret
        '{"a_api_key": "<api-key>", "b_api_key": "' + _LOOKS_REAL + '"}',
    ],
    ids=["engine-map-first", "api-key-source-first", "placeholder-first"],
)
def test_an_allowed_first_pair_never_hides_a_later_one(line: str) -> None:
    result = private_scan.redact(line)
    assert _LOOKS_REAL not in result.text and result.findings


def test_two_real_values_on_one_line_are_both_redacted() -> None:
    line = json.dumps({"a_api_key": _LOOKS_REAL, "b_api_key": _OTHER})
    result = private_scan.redact(line)
    assert json.loads(result.text) == {
        "a_api_key": "[REDACTED:api-key-value]",
        "b_api_key": "[REDACTED:api-key-value]",
    }
    assert result.redactable


@pytest.mark.parametrize(
    "line",
    [
        '"password": "dynamodb"',  # pragma: allowlist secret
        '"db_password": "aurora_postgresql"',  # pragma: allowlist secret
        '"secret": "none"',  # pragma: allowlist secret
        "auth_token=aurora_postgresql:" + _OTHER,
        "  password: " + _OTHER,
        "The password: " + _OTHER,
        '"token": "' + _OTHER + '"',
        '"x-api-key": "' + _OTHER + '"',
        '{"note": "ok", "client_secret": "' + _OTHER + '"}',
    ],
    ids=[
        "engine-id-as-password",
        "engine-id-as-db-password",
        "none-as-secret",
        "token-past-a-colon",
        "yaml",
        "prose",
        "bare-token",
        "x-api-key",
        "second-key",
    ],
)
def test_a_credential_key_value_is_found(line: str) -> None:
    assert private_scan.secret_findings(line) != []


@pytest.mark.parametrize(
    "line",
    [
        '"apiKeySource": "none"',  # pragma: allowlist secret
        '"wp_woocommerce_api_keys": "dynamodb"',  # pragma: allowlist secret
        '"token_expires_at": "2026-01-01"',
        '"auth_token": 9',
        "WHERE post_password = ? AND auth_token = $1)",
        "- `wp_posts.post_password`: varchar(255)",
    ],
    ids=["key-source", "api-keys-table", "expiry", "count", "bind-parameters", "column-note"],
)
def test_a_key_that_is_no_credential_setting_is_no_finding(line: str) -> None:
    assert private_scan.secret_findings(line) == []


def test_a_markdown_table_column_named_password_is_checked() -> None:
    text = "| user | password |\n|---|---|\n| app | " + _OTHER + " |\n| b | <password> |\nend\n"
    assert private_scan.secret_findings(text) == ["password value at line 3"]
    redacted = private_scan.redact(text).text
    assert _OTHER not in redacted and "| app | [REDACTED:password-value] |" in redacted


def test_a_bare_public_ip_alone_on_a_short_line_is_found() -> None:
    assert private_scan.secret_findings("x\n8.8.8.8\n") == ["Public IP (ipv4) at line 2"]


def _body(n: int) -> list[str]:
    return ["".join(f"Ab{i}Zk3vLmP0" for _ in range(6)) + f"x{i}Q" for i in range(n)]


@pytest.mark.parametrize(
    "form",
    ["read-raw", "read-escaped", "grep-n", "double-escaped", "list", "table", "timestamp", "two"],
)
def test_header_less_key_material_is_found_under_every_prefix(form: str) -> None:
    body = _body(4)
    texts = {
        "read-raw": "\n".join(f"    {12 + i}→{b}" for i, b in enumerate(body)),
        "read-escaped": json.dumps(
            {"content": "\n".join(f"    {12 + i}→{b}" for i, b in enumerate(body))}
        ),
        "grep-n": "\n".join(f"certs/app.pem:{12 + i}:{b}" for i, b in enumerate(body)),
        "double-escaped": json.dumps(json.dumps({"content": "\n".join(body)})),
        "list": "\n".join(f"- {b}" for b in body),
        "table": "\n".join(f"| {b} |" for b in body),
        "timestamp": "\n".join(f"2026-11-08T12:00:0{i}Z INFO {b}" for i, b in enumerate(body)),
        "two": json.dumps({"content": "\n".join(body[:2])}),
    }
    assert {kind for kind, _ in private_scan.key_material(texts[form])} == {"key material"}


def test_a_key_joined_on_one_line_is_key_material() -> None:
    assert private_scan.key_material(json.dumps({"blob": "".join(_body(4))})) == [
        ("key material", 1)
    ]


def test_only_a_thinking_signature_in_a_session_transcript_is_left_out() -> None:
    blob = "".join(_body(6))
    thinking = json.dumps(
        {"message": {"content": [{"type": "thinking", "thinking": "", "signature": blob}]}}
    )
    assert private_scan.key_material(thinking, session=True) == []
    # Not a session transcript, or not a thinking item: the field is searched as is.
    assert private_scan.key_material(thinking) == [("key material", 1)]
    other = json.dumps({"message": {"content": [{"type": "text", "signature": blob}]}})
    assert private_scan.key_material(other, session=True) == [("key material", 1)]
    assert private_scan.key_material(json.dumps({"signature": blob})) == [("key material", 1)]


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"n": "Contact\u2014jane@corp-mail.org"}),
        json.dumps({"n": "path\\jane@corp-mail.org"}),
    ],
    ids=["after-unicode-escape", "after-escaped-backslash"],
)
def test_an_email_after_an_escape_is_found_and_redacted(text: str) -> None:
    assert private_scan.identity_findings(text) == ["email at line 1"]
    result = private_scan.redact(text, emails=True)
    assert "corp-mail" not in result.text and json.loads(result.text)


def test_an_ipv4_after_a_unicode_escape_is_found() -> None:
    text = json.dumps({"n": "host\u00a08.8.8.8"})  # the local store writes ASCII escapes
    assert "\\u00a08.8.8.8" in text
    assert private_scan.secret_findings(text) == ["Public IP (ipv4) at line 1"]
    assert json.loads(private_scan.redact(text).text) == {
        "n": "host\u00a0[REDACTED:public-ip-ipv4]"
    }


# ---------------------------------------------------------------------------
# Fix round 4: the exclusions are exactly as narrow as stated
# ---------------------------------------------------------------------------

_AT = "abc" + "123Xy"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "line",
    [
        '"password": "@' + _AT + '"',  # pragma: allowlist secret
        '"password": ":' + _AT + '"',  # pragma: allowlist secret
        'password: "@' + _AT + '"',  # pragma: allowlist secret
        "password = :" + _AT,  # pragma: allowlist secret
        "password = @session_id",  # pragma: allowlist secret
    ],
    ids=["quoted-at", "quoted-colon", "yaml-quoted-at", "named-not-the-key", "named-other-word"],
)
def test_a_value_shaped_like_a_bind_parameter_is_flagged_unless_it_is_one(line: str) -> None:
    assert private_scan.secret_findings(line) != []


@pytest.mark.passes_on_main  # true negatives that main never flagged
@pytest.mark.parametrize(
    "line",
    [
        "WHERE post_password = ?",
        "password = %s",
        "auth_token = $3)",
        "post_password = :post_password",
        "WHERE auth_token = @auth_token",
    ],
    ids=["question-mark", "percent-s", "dollar-n", "named-colon", "named-at"],
)
def test_a_bind_parameter_after_equals_is_no_finding(line: str) -> None:
    assert private_scan.secret_findings(line) == []


@pytest.mark.parametrize(
    "line",
    ['{"password": ' + "4" * 16 + "}", '{"auth_token": 12e5}', '{"pin_secret": 1234.5}'],
    ids=["long-integer", "exponent", "decimal"],
)
def test_only_a_short_integer_under_a_credential_key_is_a_count(line: str) -> None:
    assert private_scan.secret_findings(line) != []
    assert private_scan.secret_findings('{"auth_token": 123456, "password": false}') == []


@pytest.mark.parametrize(
    "line",
    [
        "spring.datasource.password=" + _AT,  # pragma: allowlist secret
        "db.password: " + _AT,  # pragma: allowlist secret
        "app.auth_token=" + _AT,
        "`cfg.api_key`=" + _AT,
        "`wp_posts.post_password`: " + _AT,  # pragma: allowlist secret
    ],
    ids=["java-property", "yaml-dotted", "dotted-token", "backtick-equals", "note-without-type"],
)
def test_a_dotted_key_is_checked_unless_it_is_a_column_note(line: str) -> None:
    assert private_scan.secret_findings(line) != []
    assert private_scan.secret_findings("- `wp_posts.post_password`: varchar(255)") == []
    assert private_scan.secret_findings("- `wp_posts.post_password`: longtext: the hash") == []


@pytest.mark.passes_on_main  # a true negative main never flagged
def test_a_sql_cast_of_a_quoted_name_is_no_pair() -> None:
    line = json.dumps({"column_default": "'header_secret'::character varying"})
    assert private_scan.secret_findings(line) == []


def test_the_pipelines_own_key_notes_on_a_credential_column_are_no_finding() -> None:
    from src.shared.key_refusals import REFUSAL_REASONS, VIA_TEXT, candidate_explanation

    via = next(iter(VIA_TEXT))
    for reasons in ((), (next(iter(REFUSAL_REASONS)),)):
        line = f"- `wp_posts.post_password`: {candidate_explanation(via, reasons)}"
        assert private_scan.secret_findings(line) == [], reasons
    assert private_scan.secret_findings("- `wp_posts.post_password`: approved: x") != []


# ---------------------------------------------------------------------------
# #2053 mirror run: a rate under a *_token key, and unquoted values in JSON
# ---------------------------------------------------------------------------


@pytest.mark.passes_on_main  # guards the 76a4cc79 false positive (#2053 mirror run)
@pytest.mark.parametrize(
    "line",
    ['"credits_per_token": 0.5', '\\"credits_per_token\\": 0.000125', "cost_per_api_key = 1.5"],
    ids=["json", "double-escaped", "assignment"],
)
def test_a_per_unit_rate_is_no_credential_key(line: str) -> None:
    assert private_scan.secret_findings(line) == []


@pytest.mark.parametrize("indent", [None, 2], ids=["one-line", "pretty"])
def test_an_unquoted_secret_number_gets_a_quoted_marker_and_the_json_stays_valid(indent) -> None:
    doc = {"pin_secret": 12345678901234, "auth_token": 12e5, "n": 1}
    result = private_scan.redact(json.dumps(doc, indent=indent))
    assert json.loads(result.text) == {
        "pin_secret": "[REDACTED:password-value]",
        "auth_token": "[REDACTED:token-value]",
        "n": 1,
    }
    assert result.complete and "12345678901234" not in result.text


def test_an_unquoted_secret_number_inside_a_transcript_line_is_fully_redacted() -> None:
    design = json.dumps({"pin_secret": 12345678901234}, indent=2)
    line = json.dumps({"tool_input": {"content": design}})
    result = private_scan.redact(line)
    assert result.complete and result.redactable
    inner = json.loads(json.loads(result.text)["tool_input"]["content"])
    assert inner == {"pin_secret": "[REDACTED:password-value]"}


def test_redact_records_each_finding_s_key_name_never_its_value() -> None:
    line = json.dumps({"reset_token": _LOOKS_REAL, "a_api_key": _OTHER, "b_api_key": _OTHER + "x"})
    result = private_scan.redact(line)
    assert result.keys == {
        ("token value", 1): ["reset_token"],
        ("api key value", 1): ["a_api_key", "b_api_key"],
    }
    names = json.dumps(sorted(result.keys.values()))
    assert _LOOKS_REAL not in names and _OTHER not in names


# ---------------------------------------------------------------------------
# #2179: prose after "token:" or "api_key:" is no credential on the CI scans of
# model-written text, a real value still is, and every other scan stays strict
# ---------------------------------------------------------------------------

# Built at run time so no scanner reads a credential-shaped literal in this file.
_GITHUB = "gh" + "p_" + "Wxyz7" * 8  # pragma: allowlist secret
_AWS_ID = "AKIA" + "QWERTYUIOPASDFGH"  # pragma: allowlist secret
_JWT_VALUE = ".".join(  # pragma: allowlist secret
    ["ey" + "JhbGciOiJIUzI1NiJ9", "ey" + "JzdWIiOiIxIn0", "c2lnbmF0dXJl"]
)
_SYMBOLS = "Abc123" + "!xyz789"  # pragma: allowlist secret
# The flagged discourse design text, rewritten generically.
_TOKEN_PROSE = (
    "Validate a token: GetItem on the tokens table by token_hash, then check the "
    "expiry and the scope in the application."
)


def _upload(text: str) -> tuple[list[str], str | None]:
    """What the e2e-llm upload's scan (``ci/llm/run.py`` ``_scan_file``, the narrowed
    path) finds in ``text`` as a Markdown file: its kinds, and the text it uploads
    (``None``: withheld)."""
    scanned = run._scan_file(text.encode("utf-8"), ".md")
    uploaded = None if scanned.raw is None else scanned.raw.decode("utf-8")
    return sorted({kind for kind, _ in scanned.findings}), uploaded


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"description": _TOKEN_PROSE}),
        json.dumps({"description": _TOKEN_PROSE}, indent=2),
        json.dumps(json.dumps({"description": _TOKEN_PROSE})),
        "- **Validate a token**: GetItem on the tokens table by token_hash",
        "token: GetItem",
        "- api_key: the tenant's key is compared with the stored digest",
        "Token: compared in the application, never stored in plain text.",
        "api_key: BatchGetItem",
        pytest.param("api key: BatchGetItem", marks=pytest.mark.passes_on_main),
        "token: BatchGetItem, then TransactWriteItems on the sessions table",
        "token: GetItem(token_hash) on the email-tokens table",
        # No key name at all ("api key" has a space): main never flagged it either.
        pytest.param(
            "api key: rotated by the application every 90 days",
            marks=pytest.mark.passes_on_main,
        ),
        "api_key: issued per tenant and stored hashed",
        "-- token: looked up by token_hash, then expired_at is checked",
        "/* api_key: compared against the stored hash */",
        "    -- reset_token: written once, read by GetItem",
        "| token | Validate by GetItem on token_hash |",
        "| token_hash | String | partition key |",
        "| attribute | token |\n|---|---|\n| a | GetItem by token_hash |\n",
    ],
    ids=[
        "json-string",
        "pretty-json",
        "double-escaped-json",
        "markdown-bold",
        "token-word",
        "markdown-list-api-key",
        "sentence-token",
        "api-key-camel-case",
        "api-key-words-camel-case",
        "camel-case-operations",
        "identifier-punctuation",
        "api-key-words",
        "api-key-prose",
        "sql-line-comment",
        "sql-block-comment",
        "sql-comment-key",
        "table-row-prose",
        "table-row-type",
        "table-column-prose",
    ],
)
def test_prose_after_a_credential_key_name_is_no_finding(text: str) -> None:
    assert _upload(text) == ([], text)
    assert replay.job_private_data(text) == []


@pytest.mark.passes_on_main  # fail-closed: real values main already found stay found
@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("token: " + _GITHUB, "token value"),
        ("password=" + _SYMBOLS, "password value"),
        ("password: " + _SYMBOLS, "password value"),
        ("api_key: " + _AWS_ID, "api key value"),
        ("token: " + _JWT_VALUE, "token value"),
        (json.dumps({"description": "Validate a token: " + _GITHUB}), "token value"),
        ('password: "correct horse battery staple"', "password value"),  # pragma: allowlist secret
        ("password: 'GetItem'", "password value"),  # pragma: allowlist secret
        ('token: \\"' + "GetItem" + '\\"', "token value"),
        ("password: " + "87654321", "password value"),
        ("password: Abc!defgh", "password value"),
        ("-- password: " + _SYMBOLS, "password value"),
        ("| token | " + _GITHUB + " |", "token value"),
        ("| token | 'GetItem' |", "token value"),
        ("| user | password |\n|---|---|\n| app | " + _SYMBOLS + " |\n", "password value"),
        ('| user | password |\n|---|---|\n| app | "GetItem" |\n', "password value"),
        ("password=hunterxyz", "password value"),
    ],
    ids=[
        "github-token",
        "password-assignment",
        "yaml-password",
        "aws-key-id",
        "jwt",
        "token-inside-prose",
        "quoted-passphrase",
        "quoted-word",
        "escaped-quoted-word",
        "digits",
        "symbol",
        "sql-comment",
        "table-row",
        "quoted-table-row",
        "table-column",
        "quoted-table-column",
        "letters-after-equals",
    ],
)
@pytest.mark.parametrize("plugins", [True, False], ids=["with-plugins", "regex-only"])
def test_a_value_that_looks_like_a_secret_after_a_key_name_is_still_found(
    line: str, kind: str, plugins: bool, monkeypatch
) -> None:
    if not plugins:  # a deployed image, without detect-secrets (#1933)
        from src.shared import secret_scan

        monkeypatch.setattr(secret_scan, "_scan_line", None)
        monkeypatch.setattr(secret_scan, "_transient_settings", None)
    kinds, uploaded = _upload(line)
    assert kind in kinds, kinds
    # Redacted in place (a provider credential's own marker may cover the value), or
    # withheld whole (a cloud credential).
    assert uploaded is None or "[REDACTED:" in uploaded


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("Ab1!xyz", True),
        ("ab12cd", True),
        ("123456", True),
        ("GetItemByTokenHash", False),
        ("GetItem(token_hash)", False),
        ("email-tokens", False),
        ("TTL", False),
        ("HTTPHeader", False),
        ("DynamoDB", False),
        ("token_hash", False),
        ("12345678", True),
        ("192.0.2.5", True),
        (_AWS_ID, True),
        ("sk_live_" + "Abcdefgh" * 3, True),
        ("approved:", True),
        ("two words1", True),
        ("two words", False),
        ("Abcdefgh" * 3 + "Ab", True),
        ("qzxwvkjhgfdsaplmnbvc", True),
        ("QZXWVKJHGFDSAPLMNBVC", True),
    ],
    ids=[
        "short-symbol",
        "short-letters-and-digits",
        "short-digits",
        "camel-case",
        "call",
        "dashed",
        "acronym",
        "leading-acronym",
        "trailing-acronym",
        "identifier",
        "digits",
        "dotted-digits",
        "known-format",
        "stripe-letters-only",
        "colon",
        "spaces-and-a-digit",
        "spaces",
        "over-the-word-length",
        "random-lower-case",
        "random-upper-case",
    ],
)
def test_what_looks_like_a_secret_is_exactly_as_stated(value: str, secret: bool) -> None:
    from src.shared import secret_scan

    assert secret_scan._looks_like_secret(value) is secret


_LETTERS = "hunter" + "xyz"  # pragma: allowlist secret


@pytest.mark.passes_on_main  # fail-closed: the narrowing never reaches a password key
@pytest.mark.parametrize(
    "text",
    [
        "password: " + _LETTERS,  # pragma: allowlist secret
        "  db_password: " + _LETTERS,  # pragma: allowlist secret
        "pwd: " + _LETTERS,
        "passwd: " + _LETTERS,
        "client_secret: " + _LETTERS,  # pragma: allowlist secret
        "password: GetItem",  # pragma: allowlist secret
        "- password: the user's hash is compared with the stored digest",
        "/* password: compared against the stored hash */",
        "| password | " + _LETTERS + " |",
        "| user | password |\n|---|---|\n| app | " + _LETTERS + " |\n",
    ],
    ids=[
        "yaml-letters",
        "yaml-db-password",
        "pwd",
        "passwd",
        "secret",
        "camel-case-word",
        "prose",
        "sql-comment",
        "table-row",
        "table-column",
    ],
)
@pytest.mark.parametrize("plugins", [True, False], ids=["with-plugins", "regex-only"])
def test_a_password_value_after_a_colon_stays_as_strict_as_before(
    text: str, plugins: bool, monkeypatch
) -> None:
    if not plugins:  # a deployed image, without detect-secrets (#1933)
        from src.shared import secret_scan

        monkeypatch.setattr(secret_scan, "_scan_line", None)
        monkeypatch.setattr(secret_scan, "_transient_settings", None)
    kinds, _uploaded = _upload(text)
    assert "password value" in kinds, kinds


def _random_letters(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(string.ascii_letters) for _ in range(n))


# 40 random letters, drawn at run time so no scanner reads a credential-shaped literal.
_RANDOM_40 = _random_letters(random.Random(40), 40)


def _regex_only(monkeypatch) -> None:
    """A deployed image, without detect-secrets (#1933)."""
    from src.shared import secret_scan

    monkeypatch.setattr(secret_scan, "_scan_line", None)
    monkeypatch.setattr(secret_scan, "_transient_settings", None)


@pytest.mark.passes_on_main  # main flagged every one; the PR's first cut narrowed them
@pytest.mark.parametrize("value", [_RANDOM_40, "GetItem"], ids=["random-letters", "word"])
@pytest.mark.parametrize(
    "form",
    [
        "secret_key: {v}",
        "aws_secret_key: {v}",
        "private_key: {v}",
        "priv_key: {v}",
        "access_key: {v}",
        "client_key: {v}",
        "auth_key: {v}",
        "db_key: {v}",
        "database_key: {v}",
        "account_key: {v}",
        "service_key: {v}",
        "-- secret_key: {v}",
        "| secret_key | {v} |",
        "| user | private_key |\n|---|---|\n| app | {v} |\n",
    ],
    ids=[
        "secret-key",
        "aws-secret-key",
        "private-key",
        "priv-key",
        "access-key",
        "client-key",
        "auth-key",
        "db-key",
        "database-key",
        "account-key",
        "service-key",
        "sql-comment",
        "table-row",
        "table-column",
    ],
)
@pytest.mark.parametrize("plugins", [True, False], ids=["with-plugins", "regex-only"])
def test_only_token_and_api_key_names_are_narrowed_every_other_key_stays_strict(
    form: str, value: str, plugins: bool, monkeypatch
) -> None:
    """#2179 review, Important 1: ``secret_key``, ``private_key``, ``access_key`` and the
    rest of the ``api key value`` names are not ``token`` or ``api_key``."""
    if not plugins:
        _regex_only(monkeypatch)
    kinds, _uploaded = _upload(form.format(v=value))
    assert "api key value" in kinds, kinds


# Where a token or API key value is read after a key name, as the narrowing sees it.
_NARROWED_FORMS = (
    "token: {v}",
    "api_key: {v}",
    "x-api-key: {v}",
    "refresh_token: {v}",
    "apiKey: {v}",
    "  auth:\n    token: {v}\n",
    "token: `{v}`",
    "token: {v}.",
    "| token | {v} |",
    "| api_key | {v} |",
    "| user | token |\n|---|---|\n| app | {v} |\n",
    "-- token: {v}",
    "/* api_key: {v} */",
)


@pytest.mark.passes_on_main  # main flagged them; the PR's first cut did not
def test_a_random_run_of_letters_after_a_token_or_api_key_name_is_still_found() -> None:
    """#2179 review, Important 2: a value is prose only when every piece is word-shaped,
    so random letters-only tokens are found again (at least 99% of a seeded sample of
    random 20-letter mixed-case values, spread over every narrowed form), and the PR's
    prose examples (``test_prose_after_a_credential_key_name_is_no_finding``) still
    pass."""
    from src.shared import secret_scan

    rng = random.Random(2179)
    values = [_random_letters(rng, 20) for _ in range(1300)]
    missed = []
    with secret_scan.scan_session():
        for i, value in enumerate(values):
            form = _NARROWED_FORMS[i % len(_NARROWED_FORMS)]
            kinds, _uploaded = _upload(form.format(v=value))
            if not {"token value", "api key value"} & set(kinds):
                missed.append(form)
    assert len(missed) <= len(values) // 100, (len(missed), missed[:5])


@pytest.mark.passes_on_main  # main flagged them; the PR's first cut did not
@pytest.mark.parametrize(
    "form", _NARROWED_FORMS, ids=lambda f: f.split("{")[0].strip(" |-/*\n") or "row"
)
@pytest.mark.parametrize("plugins", [True, False], ids=["with-plugins", "regex-only"])
def test_forty_random_letters_in_every_narrowed_form_are_found(
    form: str, plugins: bool, monkeypatch
) -> None:
    if not plugins:
        _regex_only(monkeypatch)
    kinds, _uploaded = _upload(form.format(v=_RANDOM_40))
    assert {"token value", "api key value"} & set(kinds), kinds


@pytest.mark.passes_on_main  # main flagged them; the PR's first cut did not
@pytest.mark.parametrize(
    "line",
    [
        "token: 123456",
        "api_key: 1234567",
        "token: ab12cd",
        "token: 42",
        "api_key: " + "sk_" + "live_" + "Abcdefgh" * 3,
        "token: " + "rk_" + "test_" + "Abcdefgh" * 3,
    ],
    ids=["otp", "seven-digits", "short-alphanumeric", "short-count", "stripe", "stripe-restricted"],
)
@pytest.mark.parametrize("plugins", [True, False], ids=["with-plugins", "regex-only"])
def test_short_digits_and_a_letters_only_stripe_key_are_found(
    line: str, plugins: bool, monkeypatch
) -> None:
    """#2179 review, Suggestions 1 and 2: a digit is never part of a word, at any length
    (an OTP such as ``token: 123456``), and a Stripe key is a known format even when its
    body is letters only (the regex-only images have no Stripe plugin)."""
    if not plugins:
        _regex_only(monkeypatch)
    kinds, _uploaded = _upload(line)
    assert {"token value", "api key value"} & set(kinds), kinds


@pytest.mark.parametrize(
    "text",
    [
        "-- token: looked up by token_hash, then expired_at is checked",
        "/* api_key: compared against the stored hash */",
        "token: GetItem",
        "| token | Validate by GetItem on token_hash |",
        "-- token: " + _RANDOM_40,
    ],
    ids=["sql-line-comment", "sql-block-comment", "word", "table-row", "random-letters"],
)
def test_the_default_scan_is_strict_and_only_the_ci_scans_narrow(text: str) -> None:
    """#2179 review, Important 3: ``secret_findings`` and ``secret_kinds`` (the
    collector's routine bodies) stay strict by default; only the CI scans of
    model-written text pass ``narrow_key_names=True``."""
    from src.shared import secret_scan

    assert {"token value", "api key value"} & secret_scan.secret_kinds(text)
    assert secret_scan.secret_findings(text)
    assert private_scan.secret_findings(text)
    assert private_scan.redact(text).text != text
    narrowed = private_scan.secret_findings(text, narrow_key_names=True)
    assert (narrowed == []) is (text != "-- token: " + _RANDOM_40)


# 40 base64 characters with upper, lower and digits, not hex: what an AWS secret
# access key looks like, and what the bare-key rule flags (#2209).
_AWS_SECRET_SHAPED = (
    "Zk3vLmP0aB7c" + "W2rT8nE5q9Xy" + "Qm8x/2pLw9Zr" + "4uVb"
)  # pragma: allowlist secret


def _bare(text: str, narrow: bool = True) -> bool:
    return any(
        f.startswith("bare secret key")
        for f in private_scan.secret_findings(text, narrow_key_names=narrow)
    )


@pytest.mark.passes_on_main  # the guard: the bare-key rule is never narrowed (#2209)
@pytest.mark.parametrize(
    "text",
    [
        # Hash-named fields: the rule is fail-closed there too.
        '{"content_hash": "%s"}',
        '{"etag": "%s"}',
        '{"commit_sha": "%s"}',
        '{"file_sha256": "%s"}',
        '{"checksum": "%s"}',
        '{"contentHash": "%s"}',
        "etag: %s",
        '{\\"content_hash\\": \\"%s\\"}',
        # The #2211 review's bypasses of a hash-field skip: abbreviated credential words.
        '{"AWS_SK_SHA256": "%s"}',
        "sk_hash: %s",
        '{"sk_live_sha": "%s"}',
        "pw_hash: %s",
        '{"aws_sak_sha": "%s"}',
        '{"s3cret_hash": "%s"}',
        '{"p4ssword_hash": "%s"}',
        '{"pa_ss_word_hash": "%s"}',
        # ... and credential words in a parent key.
        "secret_key:\n  etag: %s",
        "aws_secret_access_key:\n  checksum: %s",
        '{\n  "credentials": [\n    {"etag": "%s"}\n  ]\n}',
        # Secret-like names, a bare line, unnamed fields.
        '{"aws_secret_access_key": "%s"}',
        '{"secret": "%s"}',
        '{"password_sha1": "%s"}',
        "%s",
        '["%s"]',
        '{"note": "see %s"}',
    ],
    ids=[
        "content-hash",
        "etag",
        "commit-sha",
        "file-sha256",
        "checksum",
        "camel-hash",
        "yaml-etag",
        "json-escaped-hash",
        "aws-sk-sha256",
        "sk-hash",
        "sk-live-sha",
        "pw-hash",
        "aws-sak-sha",
        "s3cret-hash",
        "p4ssword-hash",
        "pa-ss-word-hash",
        "yaml-parent-secret-key",
        "yaml-parent-aws-secret",
        "json-parent-credentials",
        "aws-secret-name",
        "secret",
        "password-sha1",
        "bare-line",
        "unnamed-array-item",
        "prose-value",
    ],
)
def test_a_bare_key_shaped_value_flags_under_every_field_in_every_scan(text: str) -> None:
    """#2209: the bare-key rule stays fail-closed, as on main. A hash-field skip was
    tried and bypassed by abbreviated credential words and by credential words in a
    parent key (#2211 review, round 2), so no field name narrows it, in the CI scans
    or the collector's."""
    from src.shared import secret_scan

    lines = text % _AWS_SECRET_SHAPED
    assert _bare(lines, narrow=True)
    assert _bare(lines, narrow=False)
    assert "bare secret key" in secret_scan.secret_kinds(lines)


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('{"content_hash": "VALUE"}', "content_hash"),
        ("  etag: VALUE", "etag"),
        ("| s3_etag | VALUE |", "s3_etag"),
        ("mysql://svc_rw:VALUE@db.example.com/app", None),  # pragma: allowlist secret
        ('{"\\u005aq9_password": "VALUE"}', None),  # pragma: allowlist secret
        ("the hash is VALUE", None),
    ],
    ids=["json", "yaml", "table-row", "url-user", "escaped-name", "prose"],
)
def test_the_field_before_a_value_is_never_a_url_user_or_a_cut_name(line: str, key) -> None:
    """#2211 review, Important 4: ``preceding_key`` reads no name right after ``/`` (a
    URL's user) or a backslash (a name cut at a JSON escape)."""
    from ci.llm.private_scan import preceding_key

    assert preceding_key(line, line.index("VALUE")) == key
