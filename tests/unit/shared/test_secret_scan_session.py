# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The credential scan stays cheap per text without finding less (#2098): a session
builds detect-secrets' settings once, a line no plugin pattern matches skips the
plugins' dispatch, and a line without a credential word skips the key-value pairs."""

from __future__ import annotations

import threading

import pytest

from src.shared import secret_scan

# Credential shapes, built here so no scanner reads one in this file, and lines that
# hold none.
_LINES = [
    "  CALL push('" + "AKIA" + "IOSFODNN7EXAMPLE');",
    "token = 'xox" + "b-1234567890-1234567890-" + "a" * 24 + "'",
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    "auth: " + ".".join(["ey" + "JhbGciOiJIUzI1NiJ9", "ey" + "JzdWIiOiIxIn0", "c2lnbmF0dXJl"]),
    "gh = '" + "gh" + "p_" + "Wxyz" * 9 + "'",
    "conn = 'https://admin:" + "Vq8rT2mz@db.internal:5432/app'",
    "  CALL connect('8.8." + "8.8');",
    "db_" + "pass" + "word = 'Vq8rT2mz'",
    '{"api_' + 'key": "' + "Zr4k" * 4 + '"}',
    "| user | pass" + "word |",
    "CREATE FUNCTION public.f1() RETURNS text LANGUAGE sql",
    "    UPDATE accounts SET balance = balance - p_amount WHERE id = p_from;",
    "AS $function$SELECT '" + "x" * 300 + "'$function$",
    "",
]


def test_a_line_no_plugin_pattern_matches_holds_nothing_the_plugins_report():
    with secret_scan.scan_session():
        assert secret_scan._denylist is not None
        found = 0
        for line in _LINES:
            fast = sorted(s.type for s in secret_scan._detect_secrets_line(line))
            full = sorted(s.type for s in secret_scan._scan_line(line))
            assert fast == full, line
            found += bool(full)
    assert found >= 6
    assert secret_scan._denylist is None


def _keyword_and_entropy_plugins():
    from detect_secrets.core.plugins.initialize import from_plugin_classname

    return [
        from_plugin_classname(name)
        for name in ("KeywordDetector", "Base64HighEntropyString", "HexHighEntropyString")
    ]


_LOOK_ALIKE = "look-alike"


def _overriding_plugins():
    from detect_secrets.plugins.aws import AWSKeyDetector
    from detect_secrets.plugins.base import RegexBasedDetector

    class OwnLine(AWSKeyDetector):
        def analyze_line(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return super().analyze_line(*args, **kwargs)

    class OwnString(AWSKeyDetector):
        def analyze_string(self, string):  # type: ignore[no-untyped-def]
            yield from super().analyze_string(string)

    class JwtTokenDetector(RegexBasedDetector):
        """Named like the JWT plugin, but finds more than its patterns."""

        secret_type = _LOOK_ALIKE
        denylist = ()

        def analyze_string(self, string):  # type: ignore[no-untyped-def]
            yield string

    return [OwnLine(), OwnString(), JwtTokenDetector()]


@pytest.mark.parametrize("plugin", [*_keyword_and_entropy_plugins(), *_overriding_plugins()],
                         ids=lambda p: type(p).__name__)  # fmt: skip
def test_a_plugin_that_finds_more_than_its_patterns_turns_the_skip_off(plugin, monkeypatch):
    import detect_secrets.settings as settings

    monkeypatch.setattr(settings, "get_plugins", lambda: [plugin])
    assert secret_scan._plugin_denylist() is None


def test_with_keyword_and_entropy_plugins_the_scan_is_the_full_scan(monkeypatch):
    extra = [{"name": "KeywordDetector"}, {"name": "Base64HighEntropyString", "limit": 4.5},
             {"name": "HexHighEntropyString", "limit": 3.0}]  # fmt: skip
    monkeypatch.setattr(secret_scan, "_PLUGINS", [*secret_scan._PLUGINS, *extra])
    lines = [*_LINES, "secret = '" + bytes(range(1, 19)).hex() + "'"]
    with secret_scan.scan_session():
        assert secret_scan._denylist is None
        kinds = set()
        for line in lines:
            fast = sorted(s.type for s in secret_scan._detect_secrets_line(line))
            assert fast == sorted(s.type for s in secret_scan._scan_line(line)), line
            kinds.update(fast)
    assert {"Secret Keyword", "Hex High Entropy String"} <= kinds


# A GitHub token, Basic Auth credentials and a JWT: kinds only detect-secrets finds.
_PLUGIN_ONLY = {
    "GitHub Token": "gh = '" + "gh" + "p_" + "Wxyz" * 9 + "'",
    "Basic Auth Credentials": "conn = 'https://admin:" + "Vq8rT2mz@db.internal:5432/app'",
    "JSON Web Token": _LINES[3],
}


def test_a_guard_that_fails_never_turns_detect_secrets_off(monkeypatch):
    """The guard raising means the full scan in that session and in every later one;
    the rule set it reports stays what ran (ADR-034 Decision 4)."""
    import types

    import detect_secrets.plugins.base as base
    import detect_secrets.settings as settings

    modules = {"detect_secrets.plugins.base": base, "detect_secrets.settings": settings}

    def import_module(name: str):  # type: ignore[no-untyped-def]
        if name == "detect_secrets.plugins.jwt":
            raise RuntimeError("guard broke")
        return modules[name]

    # The guard is the module's only importlib user after it loads.
    monkeypatch.setattr(
        secret_scan, "importlib", types.SimpleNamespace(import_module=import_module)
    )
    with secret_scan.scan_session():
        assert secret_scan._denylist is None
        for kind, line in _PLUGIN_ONLY.items():
            assert kind in secret_scan.secret_kinds(line), kind
    monkeypatch.undo()
    assert secret_scan._open_sessions == [] and secret_scan._denylist is None
    assert secret_scan.rule_set() == "regex+detect-secrets"
    for kind, line in _PLUGIN_ONLY.items():
        assert kind in secret_scan.secret_kinds(line), kind


def test_a_scan_in_another_thread_waits_for_the_session_to_end():
    """detect-secrets' settings are process-wide: a session that ends while another
    thread scans would leave that scan with no plugins. The other thread waits."""
    inside, other_in, other_may_scan = threading.Event(), threading.Event(), threading.Event()
    found: list[set[str]] = []

    def holder() -> None:
        with secret_scan.scan_session():
            inside.set()
            other_in.wait(timeout=1)  # never set while the lock holds the other out
        other_may_scan.set()

    def scanner() -> None:
        inside.wait(timeout=5)
        with secret_scan.scan_session():
            other_in.set()
            other_may_scan.wait(timeout=5)
            found.append(secret_scan.secret_kinds(_PLUGIN_ONLY["GitHub Token"]))

    threads = [threading.Thread(target=holder), threading.Thread(target=scanner)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert found and "GitHub Token" in found[0]
    assert secret_scan._open_sessions == []


def test_sessions_nest_and_build_the_settings_once(monkeypatch):
    built = []
    transient = secret_scan._transient_settings

    def counting(config):
        built.append(config)
        return transient(config)

    monkeypatch.setattr(secret_scan, "_transient_settings", counting)
    with secret_scan.scan_session(), secret_scan.scan_session():
        for line in _LINES:
            secret_scan.secret_kinds(line)
    assert len(built) == 1
    for line in _LINES[:3]:
        secret_scan.secret_kinds(line)
    assert len(built) == 4  # outside a session, each scan builds its own


@pytest.mark.parametrize(
    "key",
    [
        "pass" + "word", "passwd", "pwd", "pass" + "phrase", "secret", "db-pass",
        "database_pass", "key_pass", "contraseña", "contrasena", "token", "api_key",
        "x-api-key", "auth-key", "service_key", "account_key", "db_key", "database-key",
        "privKey", "private_key", "client_key", "access_key", "secret_key",
        # Unicode case folding: the Kelvin sign is a k, the long s an s.
        "toKen", "ſecret",
    ],
)  # fmt: skip
def test_every_credential_key_holds_a_word_the_pair_skip_looks_for(key):
    line = f"{key} = 'Vq8rT2mz'"
    assert any(rx.search(line) for rx in secret_scan._PAIR_RES), key
    assert secret_scan._KEY_WORD_RE.search(line) is not None, key


def test_the_kinds_are_the_findings_without_their_lines():
    text = "\n".join(_LINES)
    findings = secret_scan.secret_findings(text)
    kinds = secret_scan.secret_kinds(text)
    assert kinds and kinds == {f.rsplit(" at line ", 1)[0] for f in findings}
