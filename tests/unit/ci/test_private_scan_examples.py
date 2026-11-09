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

import pytest

from ci.llm import private_scan

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
        lambda body, emails: [("token value", None)] if _LOOKS_REAL in body else [],
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
