# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The credential scan reads SQL quotes and backslashes in the source dialect (#2262).

In T-SQL and Oracle strings a backslash is an ordinary character, so a value ending in
one ends at the next quote. In MySQL a backslash escapes a quote unless the session sets
``NO_BACKSLASH_ESCAPES``, and in PostgreSQL it does in an ``E'...'`` string and, with
``standard_conforming_strings`` off, in any string. The scan reads those both ways and
masks what either reading puts in the value, the rule the literal redaction follows
(#1482), and so does a scan given no dialect. SQL's doubled quote nests a string in
dynamic SQL (``EXEC('... PASSWORD=''...''')``) in every dialect. Every value here is
synthetic and built at run time.
"""

from __future__ import annotations

import json

import pytest

from src.agents.collector.routine_sources import write_routine_sources
from src.contracts.routine_sources import RoutineSource
from src.shared import secret_scan
from src.shared.secret_scan import credential_spans, secret_findings, secret_kinds
from src.storage.local_store import LocalArtifactStore
from src.tools.sql.redact import redact_definition, scrub_comment

_PATH = "C:\\" + "temp" + "\\"
_VALUE = "Zq9" + "Xv7" + "Wm3Kt5"  # pragma: allowlist secret


def _masked(text: str, engine: str | None) -> str:
    return scrub_comment(text, source_engine=engine)


@pytest.mark.parametrize("engine", ["sqlserver", "oracle", "mssql"])
def test_a_literal_backslash_dialect_masks_the_value_and_never_less_than_main(engine: str) -> None:
    """In SQL Server and Oracle the value ends at the quote after its backslash; the
    union with main's rules (#2595) masks at least what they mask, so the SQL after it
    is masked as on main."""
    from src.shared import secret_scan_baseline as baseline

    for text in (
        "password = '" + _PATH + "' , user = 'bob'",
        "password = N'" + _PATH + "' , user = 'bob'",
    ):
        spans = credential_spans(text, source_engine=engine)
        start = text.index(_PATH)
        assert any(a <= start and start + len(_PATH) <= b for a, b in spans)
        assert set(baseline.credential_spans(text)) <= set(spans) or all(
            any(a <= x and y <= b for a, b in spans) for x, y in baseline.credential_spans(text)
        )
        assert "temp" not in _masked(text, engine)


@pytest.mark.parametrize("engine", ["mysql", "mariadb", "postgresql", "db2", None])
def test_a_backslash_read_both_ways_masks_what_either_reading_holds(engine: str | None) -> None:
    """A setting can move where the string ends, so the longer reading is masked."""
    text = "password = '" + _PATH + "' , user = 'bob'"
    masked = _masked(text, engine)
    assert "temp" not in masked and "bob" not in masked
    assert masked.startswith("password = '?")


def test_a_postgresql_escape_string_reads_its_backslashes() -> None:
    value = _VALUE[:4] + "\\'" + _VALUE[4:]
    text = "password = E'" + value + "' , user = 'bob'"
    for engine in ("postgresql", "sqlserver", None):
        masked = _masked(text, engine)
        assert _VALUE[4:] not in masked and _VALUE[:4] not in masked, engine


@pytest.mark.parametrize("engine", ["sqlserver", "oracle", "mysql", "postgresql", None])
def test_a_doubled_quote_nests_a_value_in_dynamic_sql(engine: str | None) -> None:
    text = "EXEC('ALTER LOGIN sa WITH PASSWORD=''" + _VALUE + "''')"
    assert secret_findings(text, source_engine=engine) == ["password value at line 1"]
    assert _masked(text, engine) == "EXEC('ALTER LOGIN sa WITH PASSWORD=''?''')"
    # A doubled quote that opens no nested string is an empty value, as before.
    assert secret_findings("password = '' AND x = 1", source_engine=engine) == []


def test_every_entry_point_takes_the_source_engine() -> None:
    body = "CREATE PROCEDURE p AS -- password = '" + _PATH + "' , user = 'bob'\nSELECT 1"
    assert secret_kinds(body, source_engine="sqlserver") == {"password value"}
    assert secret_findings(body, source_engine="sqlserver") == ["password value at line 1"]
    out = redact_definition(body, "sqlserver")
    assert "temp" not in out and "SELECT 1" in out


def test_the_collector_scans_each_body_in_its_source_engine(tmp_path, monkeypatch) -> None:
    seen: list[str | None] = []
    real = secret_scan.secret_kinds

    def spy(text: str, key_names: bool = True, source_engine: str | None = None) -> set[str]:
        seen.append(source_engine)
        return real(text, key_names, source_engine=source_engine)

    monkeypatch.setattr(secret_scan, "secret_kinds", spy)
    output = {"metadata": {"source_database": {"engine": "postgresql", "version": "16"}}}
    source = RoutineSource(
        kind="procedure", routine_id="p", schema_name="public", specific_name="p",
        create_statement="CREATE PROCEDURE p() LANGUAGE sql AS $$ SELECT 1 $$",
        origin="pg_get_functiondef",
    )  # fmt: skip
    write_routine_sources(LocalArtifactStore(base_dir=str(tmp_path)), "shop", "j", output, [source])
    assert seen == ["postgresql"]


# -- #2595 review: prefixed strings, both readings, other prefixes -----------------------

_A, _B = "Ab1" + "Cd2", "Ef3" + "Gh4"  # pragma: allowlist secret
_ENGINES = ["sqlserver", "oracle", "mysql", "postgresql", None]


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "text",
    [
        "EXEC(N'ALTER LOGIN sa WITH PASSWORD=N''{v}''')",
        "EXEC sp_executesql N'ALTER LOGIN sa WITH PASSWORD=''{v}'''",
        "SET @sql = N'CREATE LOGIN app WITH PASSWORD=''{v}'', CHECK_POLICY=OFF'",
        "PERFORM E'ALTER ROLE app PASSWORD=''{v}'''",
    ],
    ids=["exec-n", "sp-executesql", "set-sql", "e-string"],
)
def test_dynamic_sql_in_a_prefixed_string_is_a_finding(engine: str | None, text: str) -> None:
    text = text.format(v=_A + _B)
    assert secret_findings(text, source_engine=engine) == ["password value at line 1"]
    masked = _masked(text, engine)
    assert _A not in masked and _B not in masked


@pytest.mark.parametrize("engine", ["mysql", "postgresql", None])
def test_the_strings_open_around_a_key_are_read_both_ways(engine: str | None) -> None:
    """``'C:\\'`` closes in one reading and not in the other, so no string is taken
    as open around the key and the quote in the value is part of it."""
    text = "SELECT 'C:\\' , 'x' , password=" + _A + "'." + _B
    assert _B not in _masked(text, engine)
    if engine is None:
        from ci.llm import private_scan

        assert _B not in private_scan.redact(text, narrow_key_names=True).text


@pytest.mark.parametrize(
    "text",
    ["password = _utf8mb4'{a} {b}'", "password = q'[{a} {b}]'", "password = U&'{a} {b}'",
     "password = X'{a} {b}'"],
    ids=["charset", "q-quote", "unicode", "hex"],
)  # fmt: skip
def test_other_sql_string_prefixes_are_read_as_quoted_values(text: str) -> None:
    text = text.format(a=_A, b=_B)
    for engine in _ENGINES:
        masked = _masked(text, engine)
        assert _A not in masked and _B not in masked, (engine, masked)


def test_comment_masking_with_a_literal_dialect_masks_no_less_than_the_longer_reading() -> None:
    """Comment text is not read by the engine, so masking takes the longer reading too."""
    text = "password = '" + _A + "\\' " + _B + "'"
    assert _B not in _masked(text, "sqlserver") and _B not in _masked(text, "oracle")


_OPENERS_SHAPES = {
    "unclosed-single-quote": lambda n: "password='a " * (n // 12),
    "unclosed-double-quote": lambda n: 'password="a ' * (n // 12),
}


@pytest.mark.passes_on_main  # main never had this regression of this PR's first head
@pytest.mark.parametrize("make", list(_OPENERS_SHAPES.values()), ids=list(_OPENERS_SHAPES))
def test_the_scan_is_linear_in_repeated_unclosed_openers(make) -> None:  # type: ignore[no-untyped-def]
    from ci.llm import private_scan
    from tests.unit.tools.sql._timing import assert_linear

    def scan(text: str) -> None:
        secret_findings(text)
        private_scan.redact(text)

    assert_linear(make, scan, sizes=(2_000, 16_000))


# -- #2595 review round 2: a one-letter word before a quote, two strings closing -------


@pytest.mark.parametrize(
    "text",
    [
        "-- Plan B's password={a}'.{b}",
        "-- plan N's password={a}'.{b}",
        "-- vitamin E's token={a}'+{b}",
        "-- form X's password={a}'={b}",
        "-- the q's secret={a}'|{b}",
        'see Plan B" ; password={a}".{b}',
    ],
    ids=["b", "n", "e", "x", "q", "double-quote"],
)
@pytest.mark.parametrize("engine", _ENGINES)
def test_a_one_letter_word_before_a_quote_never_ends_a_value_early(
    engine: str | None, text: str
) -> None:
    """``B's`` may be SQL's ``B'...'`` or prose: the scan takes the reading that masks
    more, so the quote in the value is part of it."""
    from ci.llm import private_scan

    text = text.format(a=_A, b=_B)
    for line in (text, json.dumps({"t": text})):
        assert _B not in _masked(line, engine), line
        assert _B not in private_scan.redact(line, narrow_key_names=True).text, line


@pytest.mark.parametrize("engine", ["mysql", "postgresql", None])
def test_an_escaped_quote_never_closes_a_string_around_the_key(engine: str | None) -> None:
    """``\\'`` closes a different string in each backslash reading, so it never ends a
    value where a setting moves that reading."""
    from ci.llm import private_scan

    text = "x \\' y password=" + _A + "\\'." + _B
    assert _B not in _masked(text, engine)
    assert _B not in private_scan.redact(text, narrow_key_names=True).text
