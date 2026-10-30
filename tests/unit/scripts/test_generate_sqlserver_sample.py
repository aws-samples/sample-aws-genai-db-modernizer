# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/generate_sqlserver_sample.py (#618): the synthetic SQL Server sample.

The committed ``docs/examples/trailgear/trailgear.zip`` must be exactly what
the generator writes (never hand-edited), and what it writes must be the raw
format ``scripts/collect-sqlserver.sql`` produces, so the sample goes through
the same offline parsing path a real SQL Server collection does.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from scripts import generate_sqlserver_sample as gen
from src.tools.database.offline_parser import detect_source_engine, parse_offline_collection

REPO = Path(__file__).resolve().parents[3]
COLLECT_SQL = REPO / "scripts" / "collect-sqlserver.sql"


def _collection() -> dict:
    text = gen.render()
    header, body = text.split("\n", 1)
    assert header == "collection_output"
    data: dict = json.loads(body)
    return data


def test_committed_zip_matches_the_generator() -> None:
    # The member text, not the zip bytes: a different deflate build gives
    # different bytes for the same content.
    assert gen.committed_text() == gen.render(), (
        "docs/examples/trailgear/trailgear.zip is stale or hand-edited; regenerate it with "
        "uv run python scripts/generate_sqlserver_sample.py, then regenerate the routing "
        'snapshot: uv run python -m tests.e2e.routing_snapshot --write --reason "..."'
    )


def test_check_mode_reports_the_committed_zip_is_current(capsys: pytest.CaptureFixture) -> None:
    assert gen.main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cache_candidates_stay_clear_of_the_hot_read_floor() -> None:
    """Seed jitter is +-6%, so a read within about 10% of the 1 call/s floor could
    move in or out of the cache overlay on a seed change."""
    from src.agents.referee.cache_overlay import HOT_READ_MIN_CALLS_PER_SECOND as floor

    near = [
        t[:60]
        for cps, _rows, _ms, t in gen.WORKLOAD
        if t.lstrip().upper().startswith("SELECT") and 0.9 * floor <= cps <= 1.12 * floor
    ]
    assert near == []


def test_output_is_deterministic() -> None:
    assert gen.render() == gen.render()
    assert gen.render_zip() == gen.render_zip()


def test_the_seed_only_moves_the_numbers() -> None:
    a, b = gen.build_collection(gen.SEED), gen.build_collection(gen.SEED + 1)
    assert a != b
    assert [q["query_text"] for q in a["queries"]] != []
    assert sorted(q["query_text"] for q in a["queries"]) == sorted(
        q["query_text"] for q in b["queries"]
    )
    assert a["tables"][0]["table_id"] == b["tables"][0]["table_id"]


def test_zip_holds_one_member_named_like_the_other_samples() -> None:
    with zipfile.ZipFile(io.BytesIO(gen.render_zip())) as zf:
        assert zf.namelist() == ["trailgear-collection.json"]


def _section(sql: str, var: str) -> str:
    start = sql.index(f"SET @{var} = (")
    # The clause itself starts a line; a comment can mention FOR JSON mid-line.
    end = re.compile(r"\n\s*FOR JSON").search(sql, start)
    assert end is not None, var
    return sql[start : end.start()]


def _aliases(text: str) -> set[str]:
    """Every identifier the section's SELECT can emit: an ``AS`` alias, or a
    bare/qualified column selected without one (``ic.key_ordinal``)."""
    return set(re.findall(r"\bAS\s+(\w+)", text)) | set(re.findall(r"(?:\.|\s)(\w+)\s*,?\n", text))


def test_top_level_keys_match_the_collection_script() -> None:
    sql = COLLECT_SQL.read_text()
    assembly = sql[sql.index("Assemble Final JSON") :]
    script_keys = set(re.findall(r'"(\w+)":', assembly))
    assert set(_collection()) == script_keys


@pytest.mark.parametrize(
    ("section", "var"),
    [
        ("queries", "queries"),
        ("tables", "tables"),
        ("columns", "columns"),
        ("indexes", "indexes"),
        ("foreign_keys", "foreign_keys"),
        ("primary_keys", "primary_keys"),
        ("views", "views"),
        ("procedures", "procedures"),
        ("triggers", "triggers"),
        ("wait_events", "wait_events"),
    ],
)
def test_row_fields_are_the_collection_scripts_own(section: str, var: str) -> None:
    rows = _collection()[section]
    assert rows, f"{section} is empty"
    allowed = _aliases(_section(COLLECT_SQL.read_text(), var))
    for row in rows:
        assert set(row) <= allowed, (section, set(row) - allowed)


@pytest.mark.parametrize("section", ["metadata", "global_stats", "io_stats", "os_stats"])
def test_object_fields_are_the_collection_scripts_own(section: str) -> None:
    allowed = _aliases(_section(COLLECT_SQL.read_text(), section))
    assert set(_collection()[section]) <= allowed


def test_the_offline_parser_reads_it_as_sql_server() -> None:
    data = _collection()
    parsed = parse_offline_collection(data)
    assert detect_source_engine(parsed["metadata"]) == ("sqlserver", 1433)
    assert 25 <= len(parsed["tables"]) <= 40
    assert 80 <= len(parsed["queries"]) <= 120
    assert len({q["query_id"] for q in parsed["queries"]}) == len(data["queries"])


def test_the_workload_uses_t_sql_shapes() -> None:
    texts = [q["query_text"] for q in _collection()["queries"]]
    for shape in (
        "SELECT TOP (",
        "WITH (NOLOCK)",
        "EXEC [dbo].[usp_",
        "OFFSET @Skip ROWS FETCH NEXT",
        "MERGE [",
        "NEXT VALUE FOR",
        "JSON_VALUE(",
        "OPENJSON(",
        "LIKE '%' + @Term",
        "[Audit].[EventLog]",
    ):
        assert any(shape in t for t in texts), shape


def test_every_query_names_only_collected_tables() -> None:
    """Every table a query reads or writes is in the collected schema (a
    synthetic sample has no excuse for the #382 kind of mismatch)."""
    data = _collection()
    known = {t["table_name"] for t in data["tables"]}
    ignore = {"Thread", "OrderNumberSeq"}  # the recursive CTE's name; a sequence
    for q in data["queries"]:
        for name in re.findall(
            r"(?:FROM|JOIN|INTO|UPDATE|MERGE)\s+\[(?:\w+\]\.\[)?(\w+)\]", q["query_text"]
        ):
            assert name in known | ignore, (name, q["query_text"][:80])
