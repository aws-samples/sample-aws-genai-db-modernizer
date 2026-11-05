# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Both SQL Server collectors record the routine each statement ran in (#1891).

``sys.dm_exec_sql_text(sql_handle).objectid`` names the procedure, function or trigger a
cached statement belongs to. The offline script writes the links as their own section,
``query_routines`` (one row per digest and routine: SQL Server 2016 has no
``STRING_AGG``), and the live collector reads the same links in its own query; both end
up on ``QueryPattern.parent_routine_ids`` as ``<schema>.<name>``. A collection written
before #1891 has no such section, and its statements record none (None), so nothing
downstream changes for it. The collections here are synthetic (the trailgear sample,
``docs/examples/trailgear``, with links added).
"""

from __future__ import annotations

import copy
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract, QueryPattern
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_collection_check import (
    _KNOWN_KEYS,
    OfflineCollectionError,
    validate_offline_collection,
)
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.routine_links import routine_id
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts" / "collect-sqlserver.sql"
SAMPLE = REPO / "docs" / "examples" / "trailgear" / "trailgear.zip"  # synthetic SQL Server


def _sample() -> dict[str, Any]:
    with zipfile.ZipFile(SAMPLE) as z:
        data: dict[str, Any] = json.loads(
            extract_json_object(z.read("trailgear-collection.json").decode("utf-8"))
        )
    return data


def _with_links(links: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """The sample with a ``query_routines`` section, and its first three digests."""
    raw = _sample()
    digests = [str(q["digest"]) for q in raw["queries"][:3]]
    raw["query_routines"] = [
        {**link, "digest": digests[link.pop("i")]} for link in copy.deepcopy(links)
    ]
    return raw, digests


def _parsed(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        q["query_id"]: q for q in parse_offline_collection(json.loads(json.dumps(raw)))["queries"]
    }


# -- offline ------------------------------------------------------------------------


def test_the_offline_parser_records_each_statements_routines() -> None:
    raw, (d0, d1, d2) = _with_links(
        [
            {"i": 0, "routine_schema": "dbo", "routine_name": "usp_PlaceOrder"},
            {"i": 0, "routine_schema": "sales", "routine_name": "usp_Reorder"},
            {"i": 1, "routine_schema": None, "routine_name": "trg_Audit"},
        ]
    )
    parsed = _parsed(raw)
    assert parsed[d0]["parent_routine_ids"] == ["dbo.usp_PlaceOrder", "sales.usp_Reorder"]
    # A routine with no schema reads as dbo's, the collector's form for its procedures.
    assert parsed[d1]["parent_routine_ids"] == ["dbo.trg_Audit"]
    # The section says the statement ran in none: an ad hoc or prepared statement.
    assert parsed[d2]["parent_routine_ids"] == []


@pytest.mark.passes_on_main  # main never reads a routine, so it records none either
def test_a_collection_without_the_section_records_no_routine() -> None:
    raw = _sample()
    raw.pop("query_routines", None)  # a collection written before #1891
    parsed = _parsed(raw)
    assert {q.get("parent_routine_ids") for q in parsed.values()} == {None}


def test_an_empty_section_says_every_statement_ran_in_none() -> None:
    # The sample's workload runs ad hoc: the script still writes the section, empty.
    raw = _sample()
    assert raw["query_routines"] == []
    assert {tuple(q["parent_routine_ids"]) for q in _parsed(raw).values()} == {()}


def test_literal_variants_of_one_shape_keep_every_routine() -> None:
    raw, (d0, _, _) = _with_links([{"i": 0, "routine_schema": "dbo", "routine_name": "usp_A"}])
    variant = {**raw["queries"][0], "total_time_ms": 1.0}
    raw["queries"].append(variant)
    other = {**variant, "digest": "0xVARIANT"}
    raw["queries"].append(other)
    raw["query_routines"].append({"digest": d0, "routine_schema": "dbo", "routine_name": "usp_B"})
    parsed = _parsed(raw)
    assert parsed[d0]["parent_routine_ids"] == ["dbo.usp_A", "dbo.usp_B"]
    assert parsed["0xVARIANT"]["parent_routine_ids"] == []


def test_the_routines_reach_the_collector_output() -> None:
    raw, (d0, d1, _) = _with_links([{"i": 0, "routine_schema": "dbo", "routine_name": "usp_A"}])
    out = build_offline_collector_output(
        parse_offline_collection(json.loads(json.dumps(raw))),
        job_id="job-1891",
        database_name=str(raw["metadata"].get("database_name") or "TrailGear"),
        s3_key="trailgear-collection.json",
    )
    contract = CollectorOutputContract.model_validate(out)
    by_id = {p.query_id: p for p in contract.queries.query_patterns}
    assert by_id[d0].parent_routine_ids == ["dbo.usp_A"]
    assert by_id[d1].parent_routine_ids == []


def test_the_section_is_a_known_list_of_objects() -> None:
    raw, _ = _with_links([{"i": 0, "routine_schema": "dbo", "routine_name": "usp_A"}])
    assert "query_routines" in _KNOWN_KEYS
    assert "query_routines" not in validate_offline_collection(raw, source="test")
    raw["query_routines"] = "dbo.usp_A"
    with pytest.raises(OfflineCollectionError, match="'query_routines' is"):
        validate_offline_collection(raw, source="test")


@pytest.mark.passes_on_main  # the field is new; an old output never had it
def test_an_older_output_loads_with_no_routines() -> None:
    pattern = QueryPattern.model_validate(
        {
            "query_id": "q1",
            "query_text": "SELECT 1",
            "frequency_per_hour": 1,
            "tables_accessed": ["t"],
        }
    )
    assert pattern.parent_routine_ids is None


@pytest.mark.parametrize(
    ("schema", "name", "expected"),
    [("dbo", "p", "dbo.p"), ("", "p", "dbo.p"), ("sales", " p ", "sales.p"), ("dbo", "", None)],
)
def test_one_id_form(schema: str, name: str, expected: str | None) -> None:
    assert routine_id(schema, name) == expected


# -- the offline script ----------------------------------------------------------------


def _section(text: str) -> str:
    match = re.search(r"SET @query_routines = \((.*?)\n\);", text, re.DOTALL)
    assert match, "the script has no query_routines section"
    return match.group(1)


def test_the_script_reads_each_statements_module() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    section = _section(text)
    # Keyed by the digest section 1 gives each shape.
    assert "CONVERT(VARCHAR(40), qs.query_hash, 1) AS digest" in section
    assert "CONVERT(VARCHAR(40), g.query_hash, 1) AS digest" in text
    assert "CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st" in section
    assert "OBJECT_SCHEMA_NAME(st.objectid, st.dbid) AS routine_schema" in section
    assert "OBJECT_NAME(st.objectid, st.dbid) AS routine_name" in section
    # Only this database's modules, and only statements that ran in one.
    assert "st.objectid IS NOT NULL" in section
    assert "st.dbid = DB_ID()" in section
    assert "SELECT DISTINCT" in section and "STRING_AGG" not in section


def test_the_script_reads_the_links_before_the_schema_and_writes_them_out() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.index("SET @queries = (") < text.index("SET @query_routines = (")
    assert text.index("SET @query_routines = (") < text.index("SET @metadata = (")
    assert "'\"query_routines\":' + ISNULL(@query_routines, '[]')" in text
    assert "DECLARE @query_routines NVARCHAR(MAX);" in text


# -- live -------------------------------------------------------------------------------


def _live(
    routine_rows: list[dict[str, Any]] | Exception, shapes: list[dict[str, Any]]
) -> list[dict]:
    ssm = MagicMock()
    ssm.run_sql_json.side_effect = [routine_rows, shapes]
    collector = SQLServerRemoteCollector(ssm, host="h", port=1433, database="d", secret_arn="s")
    patterns = collector.collect_query_patterns()
    routines_sql = ssm.run_sql_json.call_args_list[0].kwargs["sql"]
    assert "st.objectid IS NOT NULL" in routines_sql and "st.dbid = DB_ID()" in routines_sql
    return patterns


_SHAPES = [
    {"query_id": "0xAA", "query_text": "SELECT COUNT(*) FROM #t", "execution_count": 50},
    {"query_id": "0xBB", "query_text": "SELECT 1 FROM dbo.Users", "execution_count": 50},
]


def test_the_live_collector_records_each_statements_routines() -> None:
    rows = [
        {"query_id": "0xAA", "routine_schema": "dbo", "routine_name": "usp_Search"},
        {"query_id": "0xAA", "routine_schema": "dbo", "routine_name": "usp_Count"},
    ]
    by_id = {p["query_id"]: p for p in _live(rows, _SHAPES)}
    assert by_id["0xAA"]["parent_routine_ids"] == ["dbo.usp_Count", "dbo.usp_Search"]
    assert by_id["0xBB"]["parent_routine_ids"] == []


def test_a_failed_link_read_records_none_and_keeps_the_statements() -> None:
    by_id = {p["query_id"]: p for p in _live(RuntimeError("denied"), _SHAPES)}
    assert set(by_id) == {"0xAA", "0xBB"}
    assert {p["parent_routine_ids"] for p in by_id.values()} == {None}


def test_the_live_and_offline_reads_are_one_read() -> None:
    """The live query selects what the script's section does, keyed by query_hash."""
    ssm = MagicMock()
    ssm.run_sql_json.return_value = []
    SQLServerRemoteCollector(
        ssm, host="h", port=1433, database="d", secret_arn="s"
    ).collect_query_routines()
    live = " ".join(ssm.run_sql_json.call_args.kwargs["sql"].split())
    offline = " ".join(_section(SCRIPT.read_text(encoding="utf-8")).split())
    for clause in (
        "OBJECT_SCHEMA_NAME(st.objectid, st.dbid) AS routine_schema",
        "OBJECT_NAME(st.objectid, st.dbid) AS routine_name",
        "FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st",
        "WHERE st.objectid IS NOT NULL AND st.dbid = DB_ID() AND OBJECT_NAME(st.objectid, st.dbid) IS NOT NULL",
    ):
        assert clause in live and clause in offline, clause


def test_the_live_routines_reach_the_collector_output() -> None:
    from src.agents.collector.sqlserver_collector import _build_queries

    rows = [{"query_id": "0xAA", "routine_schema": "dbo", "routine_name": "usp_Search"}]
    queries = _build_queries(_live(rows, _SHAPES))
    by_id = {p.query_id: p for p in queries.query_patterns}
    assert by_id["0xAA"].parent_routine_ids == ["dbo.usp_Search"]
    assert by_id["0xBB"].parent_routine_ids == []
