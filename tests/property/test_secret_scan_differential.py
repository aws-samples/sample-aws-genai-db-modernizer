# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The credential scan never masks or finds less than main did (#2595).

``src/shared/secret_scan_baseline.py`` holds main's key-value rules; the public scan
runs them as a first pass and its newer rules (#2247, #2260, #2262, #2263) as a second,
and returns the union. This test checks the union differentially: on generated text in
the shapes the scan reads (SQL in each dialect and none, dynamic SQL, comments, JSON at
two escape levels, YAML, Markdown and prose; string prefixes, doubled quotes, brackets,
backslashes and quotes inside values), every character the baseline masks is masked by
the public spans, every baseline finding is still a finding, and a secret part the
baseline masks never survives comment masking or a complete CI redaction. The values
are synthetic: random letters and digits.
"""

from __future__ import annotations

import json
import string

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from ci.llm import private_scan
from src.shared import secret_scan_baseline as baseline
from src.shared.secret_scan import credential_spans, secret_findings, secret_kinds
from src.tools.sql.redact import scrub_comment
from tests.hypothesis_examples import scaled

_ENGINES = (None, "mysql", "postgresql", "sqlserver", "oracle")
# A secret part: letters and digits, long enough never to occur in the text around it.
_PART = st.text(alphabet=string.ascii_letters + string.digits, min_size=8, max_size=12)
_KEYS = st.sampled_from(
    ["password", "api_key", "token", "secret", "DB_PASSWORD", "contrasena", "user_password",
     "pwd", "secret_key", "x-api-key", "PASSWORD"]
)  # fmt: skip
_SEPARATORS = st.sampled_from(["=", " = ", ": ", "=", ":\t", " ="])
_MIDDLES = st.sampled_from(
    ["", "'", '"', "\\", "''", '""', "'.", "'+", "'|", "'||", "'=", '".', "\\'", '\\"', "]",
     "[]", "{}", "}", ")", "'}", "',", '",', "\\n", " ", "' ", '" ', "_", "''.", "`", "@", ";",
     "'::", "N'", "q'[", "]'"]
)  # fmt: skip
_QUOTINGS = st.sampled_from(
    ["{v}", "'{v}'", '"{v}"', "N'{v}'", "E'{v}'", "B'{v}'", "X'{v}'", "q'[{v}]'", "U&'{v}'",
     "_utf8mb4'{v}'", "''{v}''", "N''{v}''", "`{v}`", "'{v}", '"{v}', "[{v}]", "{{{v}}}",
     "_{v}", "N'{v}'.", "q''[{v}]''", "'{v}' AND x = 'y'"]
)  # fmt: skip
_CONTEXTS = st.sampled_from(
    ["{p}", "SET @c = '{p}';", "UPDATE cfg SET v = '{p}' WHERE id = 1;",
     "EXEC('ALTER LOGIN sa WITH {p}')", "EXEC(N'ALTER LOGIN sa WITH {p}')",
     "EXEC sp_executesql N'{p}'", "SET @sql = N'{p}';", "EXECUTE IMMEDIATE '{p}';",
     "SELECT format('{p}');", "-- {p}", "/* {p} */", "# {p}", "x = '{p}' + y",
     "conn(\"{p}\")", "SELECT '{p}'::json;", "- {p}", "  {p}", "prose before {p} after",
     "-- the admins' {p}", "Plan B's {p}", "'x' , {p}", "SELECT 'C:\\' , 'x' , {p}",
     "echo '{p}' $X", "| {p} |", "`{p}`", "**{p}**"]
)  # fmt: skip
_WRAPS = st.sampled_from(["raw", "json", "json-in-json", "json-key"])


@st.composite
def _texts(draw: st.DrawFn) -> tuple[str, tuple[str, ...]]:
    head, tail = draw(_PART), draw(_PART)
    value = draw(_QUOTINGS).replace("{v}", head + draw(_MIDDLES) + tail)
    pair = draw(_KEYS) + draw(_SEPARATORS) + value
    line = draw(_CONTEXTS).replace("{p}", pair)
    wrap = draw(_WRAPS)
    if wrap == "json":
        line = json.dumps({"text": line})
    elif wrap == "json-in-json":
        line = json.dumps({"input": json.dumps({"text": line})})
    elif wrap == "json-key":
        line = json.dumps({draw(_KEYS): head + draw(_MIDDLES) + tail})
    return line, (head, tail)


def _positions(spans: list[tuple[int, int]]) -> set[int]:
    return {i for a, b in spans for i in range(a, b)}


def _inside(text: str, part: str, spans: list[tuple[int, int]]) -> bool:
    """Whether every occurrence of ``part`` lies inside ``spans``."""
    covered = _positions(spans)
    at = text.find(part)
    if at == -1:
        return False
    while at != -1:
        if not all(i in covered for i in range(at, at + len(part))):
            return False
        at = text.find(part, at + 1)
    return True


def _baseline_pair_spans(text: str, narrow: bool) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = 0
    lines = text.splitlines(keepends=True)
    tables = baseline._table_hits([line.rstrip("\r\n") for line in lines], narrow)
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        found = [span for _kind, span in baseline._pair_hits(body, narrow)]
        found += [span for _kind, span, _key in tables.get(index, [])]
        spans += [(offset + a, offset + b) for a, b in found]
        offset += len(line)
    return spans


def _check(text: str, parts: tuple[str, ...]) -> None:
    base_spans = baseline.credential_spans(text)
    base_positions = _positions(base_spans)
    for engine in _ENGINES:
        assert base_positions <= _positions(credential_spans(text, engine)), (engine, text)
        masked = scrub_comment(text, source_engine=engine)
        for part in parts:
            if _inside(text, part, base_spans):
                assert part not in masked, (engine, text, masked)
    for narrow in (False, True):
        assert set(baseline.secret_findings(text, narrow_key_names=narrow)) <= set(
            secret_findings(text, narrow_key_names=narrow)
        ), (narrow, text)
        out = private_scan.redact(text, narrow_key_names=narrow)
        if out.complete:
            for part in parts:
                if _inside(text, part, _baseline_pair_spans(text, narrow)):
                    assert part not in out.text, (narrow, text, out.text)
    assert baseline.secret_kinds(text) <= secret_kinds(text), text
    for engine in _ENGINES:
        assert baseline.secret_kinds(text) <= secret_kinds(text, source_engine=engine), text


_A, _B = "Ab1Cd2" + "x9", "Ef3Gh4" + "y8"  # pragma: allowlist secret


@settings(max_examples=scaled(1000), deadline=None, suppress_health_check=list(HealthCheck))
@given(_texts())
# The six shapes the round-3 review of #2595 found the branch masking less than main.
@example(("SET @c = 'password=_" + _A + _B + "';", (_A, _B)))
@example(("password=N'" + _A + "'." + _B, (_A, _B)))
@example(("SET @c = 'password=" + _A + "'." + _B, (_A, _B)))
@example(("SET @sql = N'ALTER LOGIN sa WITH PASSWORD=N''" + _A + "''." + _B + "''';", (_A, _B)))
@example(("SET @c = 'password=q''[" + _A + _B + "]''';", (_A, _B)))
@example((json.dumps({"cmd": "echo 'password=' " + _A + _B}), (_A, _B)))
def test_the_scan_never_masks_or_finds_less_than_main(case: tuple[str, tuple[str, ...]]) -> None:
    text, parts = case
    _check(text, parts)
