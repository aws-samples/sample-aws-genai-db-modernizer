# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SQL Server statements keep their line breaks, so a ``--`` comment ends (#1874).

Both collectors replaced CR, LF and tab with spaces. A statement with a ``--`` line
comment then became one line whose comment ran to its end, and the parser fell back
to the regex. The offline script now keeps the text as written (FOR JSON escapes
the control characters); the live query, whose sqlcmd output is one row per line,
writes them as escapes the collector decodes.

There is no SQL Server here: each script's ``query_text`` expression is parsed as
T-SQL with sqlglot and run on SQLite, with the statement's ``SUBSTRING`` standing
for the whole statement text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlglot
from sqlglot import exp

from src.tools.aws.ssm_executor import _parse_tabular_output
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector
from src.tools.sql.facade import table_references
from src.tools.sql.redact import redact_literals
from src.tools.sql.shadow import TableShadow

_REPO = Path(__file__).resolve().parents[3]
SCRIPT = _REPO / "scripts" / "collect-sqlserver.sql"
SAMPLE = _REPO / "docs" / "examples" / "trailgear" / "trailgear.zip"  # synthetic SQL Server

# The statement in #1874 (DNN Platform's vw_Users read), with its line break.
ISSUE_EXAMPLE = (
    "SELECT * FROM dbo.vw_Users u WHERE (IsDeleted <= CASE @IncludeDeleted WHEN 0 THEN 0 "
    "ELSE 1 END   --less than equal ...\r\n"
    "  ) AND u.PortalId = @PortalId ORDER BY u.UserName"
)
TEXTS = [
    ISSUE_EXAMPLE,
    "SELECT [Id]\n\tFROM [dbo].[Orders] -- open ones\nWHERE [Status] = 1",
    # Backslashes, including ones before n, r and t, are text: never a line break.
    "SELECT * FROM [dbo].[Files] WHERE [Path] = N'C:\\new\\temp\\raw\\' -- a \\n\r\nAND 1 = 1",
    "SELECT 1",
]


def _code(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _query_text_item(sql: str) -> str:
    """The per-statement ``... AS query_text`` projection of ``sql`` (comments dropped)."""
    code = _code(sql)
    match = re.search(
        r"last_execution_time,\s*\n(\s*(?:REPLACE|SUBSTRING)\(.*?AS query_text),", code, re.S
    )
    assert match is not None
    return match.group(1)


def _whole_text(node: exp.Expression) -> exp.Expression:
    """The statement's ``SUBSTRING(st.text, ...)`` as the column holding the statement."""
    if isinstance(node, exp.Substring):
        return exp.column("t")
    return node


def _run(item: str, raw: str) -> str:
    tree = sqlglot.parse_one(f"SELECT {item} FROM x", read="tsql").transform(_whole_text)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE x (t)")
    db.execute("INSERT INTO x VALUES (?)", (raw,))
    (value,) = db.execute(tree.sql("sqlite")).fetchone()
    db.close()
    return str(value)


def _offline_text(raw: str) -> str:
    """What the offline script writes as ``query_text`` for the statement ``raw``."""
    return _run(_query_text_item(SCRIPT.read_text()), raw)


def _live_sql() -> str:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = []
    collector = SQLServerRemoteCollector(ssm, host="h", port=1433, database="d", secret_arn="s")
    collector.collect_query_patterns()
    return str(ssm.run_sql_json.call_args.kwargs["sql"])


def _live_patterns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = rows
    collector = SQLServerRemoteCollector(ssm, host="h", port=1433, database="d", secret_arn="s")
    return collector.collect_query_patterns()


def _live_row(raw: str, query_id: str | None = "0x9CDCF2B119774A97") -> dict[str, Any]:
    """The row the live query returns for ``raw``, through sqlcmd's tab-separated output."""
    text = _run(_query_text_item(_live_sql()), raw)
    out = "query_id\tquery_text\texecution_count\n--------\t----------\t---------------\n"
    out += f"{query_id or ''}\t{text}\t50\n\n(1 rows affected)\n"
    (row,) = _parse_tabular_output(out)
    return row


def _flat(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


# --- offline script ------------------------------------------------------------------


@pytest.mark.parametrize("raw", TEXTS)
def test_offline_script_keeps_the_statement_as_written(raw: str) -> None:
    assert _offline_text(raw) == raw


def test_the_issue_example_parses_as_collected() -> None:
    refs = table_references(_offline_text(ISSUE_EXAMPLE), "sqlserver")
    assert (refs.parsed_by, refs.fallback_reason, refs.truncated) == ("sqlglot", None, False)
    assert [(t.schema, t.name) for t in refs.tables] == [("dbo", "vw_Users")]


def _sample() -> dict[str, Any]:
    with zipfile.ZipFile(SAMPLE) as z:
        data: dict[str, Any] = json.loads(
            extract_json_object(z.read("trailgear-collection.json").decode("utf-8"))
        )
    return data


def _parsed(raw: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], TableShadow]:
    shadow = TableShadow(source_engine="sqlserver")
    # A FOR JSON document: the line breaks in a text are JSON escapes.
    parsed = parse_offline_collection(json.loads(json.dumps(raw)), shadow)
    return {q["query_id"]: q for q in parsed["queries"]}, shadow


def test_a_commented_statement_reads_its_tables_from_the_parser() -> None:
    raw = _sample()
    statement = (
        "SELECT TOP (10) a.[ArticleID], a.[Title] -- the card's fields\r\n"
        "FROM [KnowledgeArticle] a JOIN [Product] p ON p.[ProductID] = a.[ProductID]\r\n"
        "WHERE a.[Title] LIKE @Term ORDER BY a.[PublishedDate] DESC"
    )
    raw["queries"][0] = {**raw["queries"][0], "query_text": _offline_text(statement)}
    digest = raw["queries"][0]["digest"]
    queries, shadow = _parsed(raw)
    assert digest not in {f["query_id"] for f in shadow.fallbacks}
    assert queries[digest]["tables_accessed"] == [
        "trailgear.KnowledgeArticle",
        "trailgear.Product",
    ]
    assert queries[digest]["has_joins"] is True


def _multi_line(text: str) -> str:
    """``text`` with line breaks and tabs where its spaces were, and no comment."""
    return re.sub(r" (FROM|WHERE|JOIN|ORDER BY|GROUP BY) ", "\r\n\t\\1 ", text)


@pytest.mark.passes_on_main
def test_statements_without_comments_keep_their_ids_and_facts() -> None:
    """The same statements collected with line breaks read as their flattened form
    did: the same query ids (the script's query_hash), facts, tables and redaction."""
    flat = _sample()
    kept = copy.deepcopy(flat)
    for q in kept["queries"]:
        q["query_text"] = _multi_line(q["query_text"])
    assert sum("\n" in q["query_text"] for q in kept["queries"]) >= 60
    before, _ = _parsed(flat)
    after, _ = _parsed(kept)
    assert list(after) == list(before)
    for qid, q in after.items():
        old = before[qid]
        for field in (
            "query_type",
            "tables_accessed",
            "has_joins",
            "join_count",
            "has_aggregations",
        ):
            assert q[field] == old[field], (qid, field)
        assert _flat(q["query_text"]).split() == old["query_text"].split(), qid


@pytest.mark.passes_on_main
@pytest.mark.parametrize("raw", TEXTS)
def test_redaction_masks_the_same_literals_with_line_breaks(raw: str) -> None:
    """Redacting the kept text masks what redacting its flattened form masks after
    the comment ends: the literals of the statement's last line."""
    kept = redact_literals(_offline_text(raw), "sqlserver")
    last_line = re.split(r"\r?\n", kept)[-1]
    flat = redact_literals(_flat(raw), "sqlserver")
    assert flat.endswith(_flat(last_line)), (kept, flat)


# --- live collector ------------------------------------------------------------------


@pytest.mark.parametrize("raw", TEXTS)
@pytest.mark.passes_on_main
def test_live_collector_decodes_the_encoded_statement(raw: str) -> None:
    row = _live_row(raw)
    assert not re.search(r"[\r\n\t]", str(row["query_text"]))  # one sqlcmd line
    (pattern,) = _live_patterns([row])
    assert pattern["query_text"] == raw.strip()


def test_live_collector_reads_past_the_comment() -> None:
    (pattern,) = _live_patterns([_live_row(ISSUE_EXAMPLE)])
    assert pattern["tables_accessed"] == ["dbo.vw_Users"]
    assert pattern["filter_columns"] is not None and "PortalId" in pattern["filter_columns"]
    assert pattern["sort_columns"] == ["u.UserName"]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("raw", TEXTS)
def test_live_text_hash_query_id_is_unchanged(raw: str) -> None:
    """A row with no query_hash gets the id it had when the text was flattened (these
    statements are under 256 characters; longer ones were cut before, #1884)."""
    (pattern,) = _live_patterns([_live_row(raw, query_id=None)])
    assert pattern["query_id"] == hashlib.sha256(_flat(raw).strip().encode()).hexdigest()[:16]


@pytest.mark.passes_on_main
def test_live_backslash_cut_off_at_the_end_is_kept() -> None:
    row = {"query_id": "0x01", "query_text": "SELECT 'C:\\", "execution_count": 50}
    (pattern,) = _live_patterns([row])
    assert pattern["query_text"] == "SELECT 'C:\\"


@pytest.mark.passes_on_main
def test_a_leading_line_comment_tag_ends_at_its_line() -> None:
    """docs/contracts/assess.md: a SQL Server collection made with the current script or
    live collector keeps a leading ``--`` tag, and the statement after it (#1874); in
    an older one the tag ran into the statement and is not read."""
    tagged = "-- controller='cart',action='show'\r\nSELECT [ProductID] FROM [Product] WHERE [ProductID] = @Id"
    raw = _sample()
    raw["queries"][0] = {**raw["queries"][0], "query_text": _offline_text(tagged)}
    digest = raw["queries"][0]["digest"]
    queries, _ = _parsed(raw)
    (live,) = _live_patterns([_live_row(tagged)])
    for q in (queries[digest], live):
        assert q["query_tags"] == ["controller='cart',action='show'"]
    assert queries[digest]["tables_accessed"] == ["trailgear.Product"]
    assert live["tables_accessed"] == ["Product"]
    # An older, flattened collection: the tag runs into the statement and is not read.
    raw["queries"][0]["query_text"] = _flat(tagged)
    old, _ = _parsed(raw)
    assert old[digest]["query_tags"] == []
