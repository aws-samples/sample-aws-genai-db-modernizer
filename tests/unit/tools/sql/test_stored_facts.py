# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The parsed query facts stored on each collected query pattern (#934).

The collector parses every query once and stores the facade's two readings
(``SqlFacts`` and ``AccessFacts``) on its pattern (``parsed_facts``); every later
step reads them back instead of parsing the query again. These tests pin that a
stored reading is exactly what a fresh parse gives (so it is a shortcut, never a
second definition), that a reading this build can't vouch for is parsed instead,
that an older collection with no stored facts still works, and that the
deterministic pipeline parses each collected query once.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import zipfile
from functools import cached_property
from pathlib import Path

import pytest

from src.contracts.collector_output import QueryPattern
from src.shared.sql_spans import DEFAULT_SPAN_SETTINGS
from src.tools.database.offline_parser import detect_source_engine, parse_offline_collection
from src.tools.sql import facade
from src.tools.sql import query_facts as query_facts_module
from src.tools.sql.facade import query_readings
from src.tools.sql.query_facts import (
    ColumnTypes,
    QueryFacts,
    query_facts,
    remember_collected_facts,
    remember_stored_facts,
)
from src.tools.sql.stored_facts import (
    _READER_SOURCES,
    FACTS_VERSION,
    readings_from_stored,
    stored_readings,
    text_digest,
)
from tests.unit.tools.sql.table_refs_snapshot import SAMPLES, load_sample
from tests.unit.tools.sql.test_one_definition_per_fact import PROBES

_REPO = Path(__file__).resolve().parents[4]


def _as_dicts(readings) -> tuple[dict, dict]:
    # ``asdict`` keeps the fields equality skips (``TableRef.quoted``).
    return dataclasses.asdict(readings[0]), dataclasses.asdict(readings[1])


@pytest.fixture(autouse=True)
def _fresh_caches():
    query_facts_module._query_facts.cache_clear()
    query_facts_module._STORED_READINGS.clear()
    yield
    query_facts_module._query_facts.cache_clear()
    query_facts_module._STORED_READINGS.clear()


@pytest.fixture(scope="module")
def collected() -> list[tuple[dict, str]]:
    """Every sample query pattern as the offline collector writes it, with its engine."""
    found = []
    for name in SAMPLES:
        data = load_sample(name)
        engine, _port = detect_source_engine(data.get("metadata"))
        found += [(q, engine) for q in parse_offline_collection(data)["queries"]]
    return found


# ---------------------------------------------------------------------------
# A stored reading is exactly a fresh parse
# ---------------------------------------------------------------------------


def test_every_collected_pattern_stores_its_readings(collected):
    assert len(collected) > 1_500
    assert all(isinstance(q["parsed_facts"], dict) for q, _engine in collected)
    # The samples hold both kinds of reading: parsed, and the regex fallback.
    kinds = {q["parsed_facts"]["sql"]["parsed_by"] for q, _engine in collected}
    assert kinds == {"sqlglot", "regex"}


def test_stored_facts_equal_freshly_parsed_facts(collected):
    """On every sample query (MySQL, PostgreSQL and SQL Server collections)."""
    for pattern, engine in collected:
        text = pattern["query_text"]
        stored = readings_from_stored(pattern["parsed_facts"], text, engine)
        assert stored is not None, text[:80]
        assert _as_dicts(stored) == _as_dicts(query_readings(text, engine)), text[:80]


@pytest.mark.parametrize("text,engine", PROBES, ids=[p[0][:40] for p in PROBES])
def test_a_stored_probe_reads_back_as_parsed(text, engine):
    """A probe statement for each fact (#893), through the stored form and back."""
    fresh = query_readings(text, engine)
    stored = stored_readings(text, engine, *fresh)
    assert json.loads(json.dumps(stored)) == stored  # plain JSON
    assert _as_dicts(readings_from_stored(stored, text, engine)) == _as_dicts(fresh)


@pytest.fixture(scope="module")
def sample_types() -> dict[str, ColumnTypes | None]:
    """Each sample's column types, by source engine (one sample per engine)."""
    types = {}
    for name in SAMPLES:
        data = load_sample(name)
        engine, _port = detect_source_engine(data.get("metadata"))
        tables = parse_offline_collection(data)["tables"]
        types[engine] = ColumnTypes.from_collector(
            {
                "metadata": {"source_database": {"engine": engine}},
                "database_schema": {"tables": tables},
            }
        )
    return types


def test_every_query_fact_reads_the_same_from_stored_facts(collected, sample_types, monkeypatch):
    """Every public fact of every sample query, read with the stored facts remembered and
    no parse allowed, equals the fact read from a parse; the timestamp column (#935) with
    the sample's column types and with none."""
    names = [
        n
        for n, v in vars(QueryFacts).items()
        if isinstance(v, cached_property | property) and not n.startswith("_")
    ]

    def read(facts: QueryFacts, engine: str) -> dict:
        values = {n: getattr(facts, n) for n in names}
        values["access_facts"] = dataclasses.asdict(facts.access_facts())
        values["text_search_type"] = facts.text_search_type()
        values["timestamp_column"] = facts.timestamp_column(sample_types[engine])
        values["timestamp_column_by_name"] = facts.timestamp_column(None)
        return values

    assert all(sample_types.values())
    expected = {(q["query_text"], e): read(QueryFacts(q["query_text"], e), e) for q, e in collected}
    for q, engine in collected:
        remember_stored_facts(q["query_text"], engine, q["parsed_facts"])

    def no_parse(text, engine):
        raise AssertionError(f"parsed a query with stored facts: {text[:80]}")

    monkeypatch.setattr(query_facts_module, "query_readings", no_parse)
    for (text, engine), values in expected.items():
        assert read(query_facts(text, engine), engine) == values, text[:80]


def test_the_live_collectors_store_the_same_readings():
    from tests.unit.tools.database.test_live_sql_parity import LIVE

    sql = "SELECT * FROM a JOIN b ON a.id = b.id WHERE a.x > ? ORDER BY a.y LIMIT 5"
    for engine, collectors in LIVE.items():
        for live in collectors:
            row = live(sql)
            stored = readings_from_stored(row["parsed_facts"], sql, engine)
            assert stored is not None, (engine, live.__name__)
            assert _as_dicts(stored) == _as_dicts(query_readings(sql, engine))


def test_a_live_collector_parses_each_query_once(monkeypatch):
    """Its text-search fields (#929) read the same parse as its tables and facts."""
    from tests.unit.tools.database.test_live_sql_parity import _live_postgres

    calls = []
    real = facade._parse
    monkeypatch.setattr(
        facade, "_parse", lambda sql, engine: calls.append(sql) or real(sql, engine)
    )
    row = _live_postgres("SELECT * FROM topics WHERE title ILIKE $1 -- once")
    assert row["has_text_search"] is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# What a stored reading can't vouch for is parsed instead
# ---------------------------------------------------------------------------

_TEXT = "SELECT * FROM t WHERE a LIKE '%x' AND b > 1"


def _stored(text: str = _TEXT, engine: str = "postgresql") -> dict:
    return stored_readings(text, engine, *query_readings(text, engine))


@pytest.mark.parametrize(
    "change",
    [
        lambda s: {**s, "version": "another build"},
        lambda s: {**s, "source_engine": "mysql"},
        lambda s: {**s, "text_sha256": text_digest("SELECT 2")},
        lambda s: {k: v for k, v in s.items() if k != "sql"},
        lambda s: {**s, "sql": {**s["sql"], "join_count": "1"}},
        lambda s: {**s, "sql": {**s["sql"], "parsed_by": "a guess"}},
        lambda s: {**s, "sql": {**s["sql"], "not_a_fact": True}},
        lambda s: {**s, "access": {**s["access"], "scopes": [{"sources": "t"}]}},
        lambda s: {**s, "access": []},
        lambda s: "not an object",
        lambda s: None,
    ],
    ids=[
        "version",
        "engine",
        "text",
        "no sql",
        "wrong type",
        "bad literal",
        "unknown field",
        "bad scope",
        "access not an object",
        "a string",
        "none",
    ],
)
def test_a_reading_it_cannot_vouch_for_is_parsed_instead(change):
    stored = change(_stored())
    assert readings_from_stored(stored, _TEXT, "postgresql") is None
    remember_stored_facts(_TEXT, "postgresql", stored)
    assert _as_dicts(query_facts(_TEXT, "postgresql")._readings) == _as_dicts(
        query_readings(_TEXT, "postgresql")
    )
    assert query_facts(_TEXT, "postgresql").leading_wildcard_like


def test_stored_facts_are_read_for_their_own_engine_only():
    remember_stored_facts(_TEXT, "postgresql", _stored())
    assert query_facts_module._STORED_READINGS.get((_TEXT, "mysql", DEFAULT_SPAN_SETTINGS)) is None
    assert readings_from_stored(_stored(), _TEXT, "PostgreSQL ") is not None  # as normalized


def test_the_version_follows_the_code_that_reads_the_facts():
    assert FACTS_VERSION is not None
    for name in _READER_SOURCES:
        assert (_REPO / "src" / name).is_file(), name
    # The modules the facade reads its facts with are all in it.
    tree = ast.parse((_REPO / "src" / "tools" / "sql" / "facade.py").read_text(encoding="utf-8"))
    imported = {
        node.module.removeprefix("src.").replace(".", "/") + ".py"
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src.")
    }
    # regex_tables reads the table references, which are not stored.
    assert imported - {"tools/sql/regex_tables.py"} <= set(_READER_SOURCES)


def test_a_facade_error_reading_is_not_stored(monkeypatch):
    from src.tools.database import live_sql

    def boom(sql, engine):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(facade, "read_sql", boom)
    assert live_sql.LiveQueryReader("mysql", "test").fields("q", "SELECT 1")["parsed_facts"] is None


# ---------------------------------------------------------------------------
# An older collection, with no stored facts
# ---------------------------------------------------------------------------


def test_an_old_collection_without_stored_facts_still_loads_and_is_parsed(monkeypatch):
    with zipfile.ZipFile(_REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        raw = z.read("wordpress-collection.json").decode("utf-8")
    assert "parsed_facts" not in raw  # a collection written before #934
    pattern = QueryPattern.model_validate(
        {"query_id": "q", "query_text": _TEXT, "frequency_per_hour": 1.0, "tables_accessed": ["t"]}
    )
    assert pattern.parsed_facts is None
    old = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "queries": {"query_patterns": [pattern.model_dump(exclude={"parsed_facts"})]},
    }
    remember_collected_facts(old)
    assert query_facts_module._STORED_READINGS == {}
    calls = []
    real = query_facts_module.query_readings
    monkeypatch.setattr(
        query_facts_module, "query_readings", lambda t, e: calls.append(t) or real(t, e)
    )
    assert query_facts(_TEXT, "postgresql").leading_wildcard_like
    assert calls == [_TEXT]


def test_remember_collected_facts_reads_the_contract_shape():
    pattern = {"query_text": _TEXT, "parsed_facts": _stored()}
    remember_collected_facts(
        {
            "metadata": {"source_database": {"engine": "PostgreSQL"}},
            "queries": {"query_patterns": [pattern, {"query_text": "SELECT 1"}, "junk"]},
        }
    )
    assert set(query_facts_module._STORED_READINGS) == {
        (_TEXT, "postgresql", DEFAULT_SPAN_SETTINGS)
    }
    remember_collected_facts(None)
    remember_collected_facts({"queries": None})


# ---------------------------------------------------------------------------
# The deterministic pipeline parses each collected query once
# ---------------------------------------------------------------------------


def test_the_pipeline_parses_each_collected_query_once(tmp_path, monkeypatch):
    """Collect wordpress, then run triage, the analyses, assignment and the reality check
    in this process: after the collector's own parse, no step parses a collected query
    again (before #934 each was parsed about eleven times on discourse)."""
    import scripts.run_assessment as run_assessment
    from src.storage.local_store import LocalArtifactStore

    with zipfile.ZipFile(_REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    parsed: list[str] = []
    real = facade._parse
    monkeypatch.setattr(
        facade, "_parse", lambda sql, engine: parsed.append(sql) or real(sql, engine)
    )

    job_id, db = run_assessment.phase_collect(
        str(tmp_path / "wordpress-collection.json"), "wordpress", store
    )
    collector = store.read_json(f"{db}/{job_id}/collector/output.json")
    texts = {q["query_text"] for q in collector["queries"]["query_patterns"]}
    assert set(parsed) >= texts
    query_facts_module._query_facts.cache_clear()
    query_facts_module._STORED_READINGS.clear()
    parsed.clear()

    selected = run_assessment.phase_triage(store, job_id, db)
    run_assessment.phase_analysis(store, job_id, db, selected)
    run_assessment.phase_assignment(store, job_id, db)
    run_assessment.phase_reality_check(store, job_id, db, "none")

    assert sorted(t[:60] for t in set(parsed) & texts) == []


def test_stored_facts_are_kept_per_source_settings():
    """Two collections of one process with the same text and other string settings
    (#1482) keep their stored readings apart."""
    from src.shared.sql_spans import SpanSettings, with_span_settings

    strict = SpanSettings(no_backslash_escapes=True)
    query_facts_module.remember_stored_facts(_TEXT, "mysql", {"a": 1}, strict)
    assert (_TEXT, "mysql", strict) in query_facts_module._STORED_READINGS
    assert (_TEXT, "mysql", DEFAULT_SPAN_SETTINGS) not in query_facts_module._STORED_READINGS
    with with_span_settings(strict):
        query_facts_module.remember_stored_facts(_TEXT, "postgresql", {"a": 1})
    assert (_TEXT, "postgresql", strict) in query_facts_module._STORED_READINGS
