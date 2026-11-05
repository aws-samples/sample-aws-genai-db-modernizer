# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Oracle statements keep their line breaks, so a ``--`` comment ends (#1880).

Both Oracle collectors replaced line breaks with spaces. A statement with a ``--``
line comment then became one line whose comment ran to its end, and sqlglot read the
part before the comment as the whole statement: its tables were lost, with no parse
error. The offline script now keeps the text as written (``JSON_OBJECT`` escapes the
control characters); the live query, whose sqlplus output is one row per line, writes
backslashes, CR, LF and tab as escapes the collector decodes.

There is no Oracle database here: each collector's ``query_text`` expression is parsed
as Oracle SQL with sqlglot and run on SQLite, with ``g.query_text`` standing for the
statement's text, and the live rows go through sqlplus-shaped fixed-width output.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess  # nosec B404 — runs the generated printf with bash, no shell input
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlglot
from sqlglot import exp

from src.tools.aws.ssm_executor import SSMExecutor, _build_sql_command
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.sql.facade import table_references
from src.tools.sql.redact import redact_literals
from src.tools.sql.shadow import TableShadow

_REPO = Path(__file__).resolve().parents[3]
SCRIPT = _REPO / "scripts" / "collect-oracle.sql"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA"  # pragma: allowlist secret  # nosec B105 — test fixture ARN


def _client_error(match: str | None = None) -> Any:
    """``pytest.raises`` for the parser's ``SqlClientError`` (a ``RuntimeError``),
    named by its class so this file imports on main's code."""
    return pytest.raises(RuntimeError, match=match)


LIVE_CAP = 4000  # the live query's DBMS_LOB.SUBSTR amount, sqlplus's column width

# The statement in #1880, with its line break.
ISSUE_EXAMPLE = (
    "SELECT o.order_id -- the key\n"
    "FROM app.orders o JOIN app.customers c ON c.id = o.customer_id WHERE o.status = :1"
)
TEXTS = [
    ISSUE_EXAMPLE,
    "SELECT c.name\r\n\tFROM app.customers c -- active only\r\nWHERE c.active = 'Y'",
    # Backslashes, including ones before n, r and t, are text: never a line break.
    "SELECT * FROM app.files f WHERE f.path = 'C:\\new\\temp\\raw\\' -- a \\n\nAND f.id = :1",
    "SELECT p.name -- café ☕\nFROM app.products p WHERE p.label = 'naïve'",
    "SELECT 1 FROM DUAL",
]


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _whole_text(node: exp.Expression) -> exp.Expression:
    """Oracle's functions as SQLite runs them, ``g.query_text`` as the statement."""
    if isinstance(node, exp.Column) and node.name == "query_text":
        return exp.column("t")
    if isinstance(node, exp.Anonymous) and node.name.upper() == "TO_CLOB":
        inner: exp.Expression = node.expressions[0]
        return inner.transform(_whole_text)
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Anonymous):
        if f"{node.this.name}.{node.expression.name}".upper() == "DBMS_LOB.SUBSTR":
            text, amount, offset = node.expression.expressions
            text, offset, amount = (e.transform(_whole_text) for e in (text, offset, amount))
            return exp.Substring(this=text, start=offset, length=amount)
    return node


def _oracle_regexp_replace(text: str, pattern: str, replacement: str) -> str:
    # Oracle's POSIX class [[:cntrl:]] is the ASCII control characters.
    return re.sub(pattern.replace("[:cntrl:]", "\\x00-\\x1f\\x7f"), replacement, text)


def _run(item: str, raw: str) -> str:
    tree = sqlglot.parse_one(f"SELECT {item} FROM x", read="oracle").transform(_whole_text)
    db = sqlite3.connect(":memory:")
    db.create_function("REGEXP_REPLACE", 3, _oracle_regexp_replace)
    # LENGTHB counts bytes (AL32UTF8 is UTF-8); SQLite's LENGTH counts characters, as
    # Oracle's does.
    db.create_function("LENGTHB", 1, lambda v: None if v is None else len(v.encode()))
    db.execute("CREATE TABLE x (t)")
    db.execute("INSERT INTO x VALUES (?)", (raw,))
    (value,) = db.execute(tree.sql("sqlite")).fetchone()
    db.close()
    return str(value)


def _offline_text(raw: str) -> str:
    """What the offline script's JSON_OBJECT gets as ``query_text`` for ``raw``."""
    code = _code(SCRIPT.read_text())
    match = re.search(r"'query_text' VALUE (.*?),\n", code)
    assert match is not None
    return _run(match.group(1), raw)


def _live_sql() -> str:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = []
    _collector(ssm).collect_query_patterns(owner="APP")
    return str(ssm.run_sql_json.call_args.kwargs["sql"])


def _live_text(raw: str) -> str:
    """The ``query_text`` value the live query returns for ``raw``."""
    match = re.search(r"g\.query_key AS query_id,\s*\n\s*(.*?\s+AS query_text),", _live_sql(), re.S)
    assert match is not None
    return _run(re.sub(r"\s+AS query_text$", "", match.group(1)), raw)


def _collector(ssm: Any) -> OracleRemoteCollector:
    return OracleRemoteCollector(
        ssm, host="db.example", port=1521, database="APP", secret_arn=SECRET_ARN
    )


# The live query's columns: (name, sqlplus display width, numeric). A string column is
# as wide as its type (VARCHAR2(4000) for the text) and cuts its heading; a NUMBER column
# is NUMWIDTH (10) or its heading wide, whichever is wider.
_NUMBERS = ["EXECUTION_COUNT", "TOTAL_ELAPSED_US", "TOTAL_CPU_US", "TOTAL_LOGICAL_READS",
            "TOTAL_PHYSICAL_READS", "TOTAL_ROWS"]  # fmt: skip
_COLUMNS = [
    ("QUERY_ID", 44, False),
    ("QUERY_TEXT", LIVE_CAP, False),
    *[(name, max(10, len(name)), True) for name in _NUMBERS],
    ("FIRST_SEEN", 19, False),
    ("LAST_SEEN", 19, False),
]


def _sqlplus(rows: list[list[str]]) -> str:
    """sqlplus output with HEADING ON and TRIMOUT ON: a header cut to each column's
    width, a dashes line with a space between columns, strings left-aligned and
    numbers right-aligned in their columns, trailing spaces trimmed."""

    def line(cells: list[str], numeric: list[bool]) -> str:
        out = []
        for cell, (_, width, _), num in zip(cells, _COLUMNS, numeric, strict=True):
            out.append(cell.rjust(width) if num else cell[:width].ljust(width))
        return " ".join(out).rstrip()

    plain = [False] * len(_COLUMNS)
    header = line([name[:width] for name, width, _ in _COLUMNS], plain)
    dashes = " ".join("-" * width for _, width, _ in _COLUMNS)
    data = [line(r, [num for _, _, num in _COLUMNS]) for r in rows]
    return "\n".join(["", header, dashes, *data, ""])


def _sqlplus_row(text: str, query_id: str = "fms_1234567890") -> list[str]:
    return [query_id, text, "50", "900000", "500000", "100", "3", "40",
            "2026-01-01/00:00:00", "2026-01-02 00:00:00"]  # fmt: skip


def _live_patterns(output: str) -> list[dict[str, Any]]:
    cred = MagicMock()
    cred.region = "us-east-1"
    ssm = SSMExecutor(cred, "i-0123456789abcdef0")
    ssm.run_command = MagicMock(return_value=output)  # type: ignore[method-assign]
    return _collector(ssm).collect_query_patterns(owner="APP")


def _live_pattern(raw: str, query_id: str = "fms_1234567890") -> dict[str, Any]:
    output = _sqlplus([_sqlplus_row(_live_text(raw), query_id)])
    (pattern,) = _live_patterns(output)
    return pattern


def _flat(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


# --- offline script ------------------------------------------------------------------


@pytest.mark.parametrize("raw", TEXTS)
def test_offline_script_keeps_the_statement_as_written(raw: str) -> None:
    assert _offline_text(raw) == raw


def test_the_issue_example_keeps_its_tables_as_collected() -> None:
    refs = table_references(_offline_text(ISSUE_EXAMPLE), "oracle")
    assert (refs.parsed_by, refs.fallback_reason, refs.truncated) == ("sqlglot", None, False)
    assert [(t.schema, t.name) for t in refs.tables] == [("app", "orders"), ("app", "customers")]


def _row(digest: str, text: str, total_time_ms: float = 10.0) -> dict[str, Any]:
    return {
        "digest": digest,
        "query_text": text,
        "execution_count": 20,
        "total_time_ms": total_time_ms,
        "avg_time_ms": total_time_ms / 20,
        "min_time_ms": 0,
        "max_time_ms": 0,
        "total_rows_sent": 20,
        "total_rows_examined": 40,
        "total_rows_affected": 0,
        "lock_time_ms": 0,
        "first_seen": "2026-01-01 00:00:00",
        "last_seen": "2026-01-02 00:00:00",
    }


_METADATA = {
    "version": "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0 - Production",
    "database_name": "APP",
    "current_schema": "APP",
}


def _collection(rows: list[dict[str, Any]]) -> str:
    """The script's DBMS_OUTPUT: one JSON_OBJECT per line (JSON escapes the control
    characters, so a statement never spans lines), sentinels closing each list."""
    lines = ["{", '"queries": [']
    lines += [json.dumps(r, ensure_ascii=False) + "," for r in rows]
    lines += ['{"_sentinel": true}', "],", '"metadata": ', json.dumps(_METADATA) + ","]
    lines += ['"tables": [', '{"_sentinel": true}', "]", "}"]
    return "\n".join(lines)


def _parsed(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], TableShadow]:
    output = _collection(rows)
    assert output.count("\n") == len(rows) + 9  # one line per statement
    shadow = TableShadow(source_engine="oracle")
    parsed = parse_offline_collection(json.loads(extract_json_object(output)), shadow)
    return {q["query_id"]: q for q in parsed["queries"]}, shadow


def test_a_commented_statement_reads_its_tables_from_the_parser() -> None:
    queries, shadow = _parsed([_row("fms_1", _offline_text(ISSUE_EXAMPLE))])
    assert not shadow.fallbacks
    assert queries["fms_1"]["tables_accessed"] == ["app.orders", "app.customers"]
    assert queries["fms_1"]["has_joins"] is True


def test_statements_that_differ_after_a_comment_stay_apart() -> None:
    """Flattened, the comment swallowed what made two statements different, so the
    parser's literal-shape merge (``_merge_oracle_shapes``; ``literal_shape`` drops
    comments) merged them into one, whatever their digests (``fms_`` ones too)."""
    a = "SELECT o.id -- a note\nFROM app.orders o WHERE o.status = :1"
    b = "SELECT o.id -- a note\nFROM app.order_lines o WHERE o.qty > :1"
    queries, _ = _parsed([_row("sql_a", _offline_text(a), 20.0), _row("sql_b", _offline_text(b))])
    assert list(queries) == ["sql_a", "sql_b"]
    assert queries["sql_b"]["tables_accessed"] == ["app.order_lines"]


# Synthetic statements with no comment: two literal variants with their own SQL_IDs
# (a collection made before the FORCE_MATCHING_SIGNATURE grouping), and a join.
_PLAIN = [
    ("fms_11", "SELECT o.id, o.total FROM app.orders o WHERE o.customer_id = :1 ORDER BY o.id"),
    ("sql_v1", "SELECT * FROM app.customers c WHERE c.id = 1"),
    ("sql_v2", "SELECT * FROM app.customers c WHERE c.id = 2"),
    (
        "fms_12",
        "SELECT c.region, COUNT(*) FROM app.orders o JOIN app.customers c "
        "ON c.id = o.customer_id WHERE o.created_at > SYSDATE - 7 GROUP BY c.region",
    ),
    ("fms_13", "UPDATE app.orders SET status = 'SHIPPED' WHERE id = :1"),
    ("fms_14", "INSERT INTO app.order_lines (order_id, sku, qty) VALUES (:1, :2, :3)"),
]


def _multi_line(text: str) -> str:
    """``text`` with line breaks and tabs where its spaces were, and no comment."""
    return re.sub(r" (FROM|WHERE|JOIN|ON|ORDER BY|GROUP BY|SET|VALUES) ", "\r\n\t\\1 ", text)


@pytest.mark.passes_on_main
def test_statements_without_comments_keep_their_ids_and_facts() -> None:
    """The same statements collected with line breaks read as their flattened form
    did: the same query ids (the script's digest, and the merged literal variants),
    facts and tables."""
    flat = [_row(d, t, 10.0 + i) for i, (d, t) in enumerate(_PLAIN)]
    kept = copy.deepcopy(flat)
    for r in kept:
        r["query_text"] = _multi_line(r["query_text"])
    assert sum("\n" in r["query_text"] for r in kept) == len(_PLAIN)
    before, _ = _parsed(flat)
    after, _ = _parsed(kept)
    assert list(after) == list(before) == ["fms_11", "sql_v2", "fms_12", "fms_13", "fms_14"]
    for qid, q in after.items():
        old = before[qid]
        for field in ("query_type", "tables_accessed", "has_joins", "join_count"):
            assert q[field] == old[field], (qid, field)
        assert q["has_aggregations"] == old["has_aggregations"], qid
        assert q["execution_count"] == old["execution_count"], qid
        assert _flat(q["query_text"]).split() == old["query_text"].split(), qid


@pytest.mark.passes_on_main
@pytest.mark.parametrize("raw", TEXTS)
def test_redaction_masks_the_same_literals_with_line_breaks(raw: str) -> None:
    """Redacting the kept text masks what redacting its flattened form masks after the
    comment ends (the literals of the statement's last line), and redacting it again
    changes nothing."""
    kept = redact_literals(raw, "oracle")
    assert redact_literals(kept, "oracle") == kept
    last_line = re.split(r"\r?\n", kept)[-1]
    flat = redact_literals(_flat(raw), "oracle")
    assert flat.endswith(_flat(last_line)), (kept, flat)


@pytest.mark.passes_on_main
def test_offline_rows_fit_one_dbms_output_line() -> None:
    """DBMS_OUTPUT wraps a line longer than LINESIZE. The text is cut at 2000
    characters; even if every one is a control character JSON writes as six (\\u0001)
    and the row's other fields are at their widest, the row stays under LINESIZE."""
    assert "DBMS_LOB.SUBSTR(SQL_FULLTEXT, 2000, 1) AS query_text" in SCRIPT.read_text()
    linesize = int(re.search(r"^SET LINESIZE (\d+)$", SCRIPT.read_text(), re.M).group(1))  # type: ignore[union-attr]
    widest = _row("fms_" + "9" * 40, "\x01" * 2000, 1e30)
    widest.update(
        {k: 10**38 for k in ("execution_count", "total_rows_sent", "total_rows_examined")}
    )
    line = json.dumps(widest, ensure_ascii=True) + ","
    assert len(line.encode()) < linesize == 32767


# --- live collector ------------------------------------------------------------------


@pytest.mark.parametrize("raw", TEXTS)
def test_live_collector_decodes_the_encoded_statement(raw: str) -> None:
    sent = _live_text(raw)
    assert not re.search(r"[\r\n\t]", sent)  # one sqlplus line, no tab to expand
    assert _live_pattern(raw)["query_text"] == raw.strip()


def test_live_collector_reads_past_the_comment() -> None:
    pattern = _live_pattern(ISSUE_EXAMPLE)
    assert pattern["tables_accessed"] == ["app.orders", "app.customers"]
    assert pattern["has_joins"] is True
    assert pattern["filter_columns"] is not None and "status" in pattern["filter_columns"]


def test_a_tab_in_the_statement_keeps_sqlplus_columns() -> None:
    """A raw tab in sqlplus's fixed-width output moved every later column when the
    parser expanded it; the encoded text has none."""
    pattern = _live_pattern("SELECT a,\tb FROM app.orders WHERE id = :1")
    assert pattern["query_text"] == "SELECT a,\tb FROM app.orders WHERE id = :1"
    assert (pattern["execution_count"], pattern["last_seen"]) == (50, "2026-01-02 00:00:00")


@pytest.mark.parametrize("raw", TEXTS)
def test_live_text_hash_query_id_is_unchanged(raw: str) -> None:
    """A row with no query key hashes its text with CR, LF and tab as spaces: for CR and
    LF the id main gave. A tab used to shift sqlplus's columns, so main's id for the tab
    case hashed a misread text (that case fails on main). A real row never has no key:
    query_key falls back to the SQL_ID."""
    pattern = _live_pattern(raw, query_id="")
    assert pattern["query_id"] == hashlib.sha256(_flat(raw).strip().encode()).hexdigest()[:16]


def test_a_statement_past_the_cap_is_cut_on_its_encoded_form() -> None:
    """The live query cuts the encoded text at sqlplus's column width (4000), so a long
    statement with line breaks arrives as a prefix of itself, decoded; a cut between
    a backslash and its letter leaves the backslash."""
    raw = "SELECT a -- note\r\n\tFROM app.t\r\n" * 120 + "WHERE id = :1"
    assert len(raw) < LIVE_CAP == len(_live_text(raw))
    text = _live_pattern(raw)["query_text"]
    assert raw.startswith(text) and len(text) > LIVE_CAP * 0.8
    cut = _live_text("x" * (LIVE_CAP - 1) + "\n")
    assert len(cut) == LIVE_CAP and cut.endswith("\\")
    assert _live_pattern("x" * (LIVE_CAP - 1) + "\n")["query_text"] == "x" * (LIVE_CAP - 1) + "\\"


def _reviewer_statement() -> str:
    """81 lines, 3,927 characters, one ``é`` on its first line: 3,928 bytes."""
    lines = ["SELECT 'é' AS accent, o.id"] + [f"  , o.col_{i:02d}" for i in range(79)]
    head = "\n".join(lines) + "\n"
    return head + "FROM app.orders o WHERE o.id = :1".ljust(3927 - len(head), " ")


def test_the_live_cut_stays_within_4000_bytes() -> None:
    """SQL caps DBMS_LOB.SUBSTR's VARCHAR2 at 4000 bytes (ORA-06502 past it), while its
    amount counts characters. The escapes add bytes, so a 3,928-byte statement with 80
    line breaks and one two-byte character would come back at 4,001 bytes from a cut at
    4000 characters. The cut is 4000 less the statement's extra bytes. There is no
    Oracle database here: this checks the generated SQL and its arithmetic, run on SQLite
    with LENGTHB counting UTF-8 bytes."""
    raw = _reviewer_statement()
    assert (len(raw), raw.count("\n") + 1, len(raw.encode())) == (3927, 81, 3928)
    encoded = raw.replace("\\", "\\\\").replace("\n", "\\n")
    assert len(encoded[:LIVE_CAP].encode()) == 4001  # the character cut alone
    sent = _live_text(raw)
    assert len(sent.encode()) == 4000 and len(sent) == LIVE_CAP - 1
    assert raw.startswith(_live_pattern(raw)["query_text"])
    # ASCII keeps 4000 characters; a statement of four-byte characters stays within bytes.
    assert len(_live_text("SELECT 1 -- x\n" * 400)) == LIVE_CAP
    for text in ("☕" * 1000, "é" * 1999 + "\n" * 3, "😀" * 1000):
        assert len(_live_text(text).encode()) <= LIVE_CAP, text[:3]
    sql = _live_sql()
    assert "4000 - (LENGTHB(g.query_text) - LENGTH(g.query_text)), 1)" in sql


@pytest.mark.passes_on_main
def test_the_live_text_is_a_varchar2_that_fits_the_line() -> None:
    """sqlplus shows a CLOB at SET LONG (80 by default) and the live preamble sets no
    LONG, so the text must reach sqlplus as a VARCHAR2 (DBMS_LOB.SUBSTR) of at most
    4000 bytes; LINESIZE 32767 holds that and every other column on one line."""
    sql = _live_sql()
    item = re.search(r"g\.query_key AS query_id,\s*\n\s*(.*?)\s+AS query_text,", sql, re.S)
    assert item is not None
    # A CLOB (SQL_FULLTEXT, TO_CLOB) only ever inside the outermost DBMS_LOB.SUBSTR.
    projection = item.group(1)
    assert "SQL_FULLTEXT" not in projection
    assert "TO_CLOB" not in projection or projection.startswith("DBMS_LOB.SUBSTR(")
    assert "DBMS_LOB.SUBSTR(SQL_FULLTEXT, 4000, 1) AS query_text" in sql
    command = _build_sql_command("oracle", "h", 1521, "APP", SECRET_ARN, "SELECT 1;", "us-east-1")
    assert "SET LINESIZE 32767" in command and "SET LONG" not in command
    assert sum(width + 1 for _, width, _ in _COLUMNS) < 32767


@pytest.mark.passes_on_main
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_sqlplus_receives_the_query_as_written() -> None:
    """The live command pipes the SQL through printf, which reads backslash escapes and
    % directives in its format: the query carries neither, so sqlplus reads it as the
    collector wrote it."""
    sql = _live_sql()
    command = _build_sql_command("oracle", "h", 1521, "APP", SECRET_ARN, sql, "us-east-1")
    printf = command.split(" && ")[-1].split(" | sqlplus ", 1)[0]
    assert printf.startswith("printf '")
    out = subprocess.run(  # nosec B603 B607 — fixed argv, the command built above
        ["bash", "-c", printf], capture_output=True, text=True, check=True
    ).stdout
    assert out.split("SET TRIMOUT ON\n", 1)[1] == sql + "\n"


@pytest.mark.passes_on_main
def test_live_backslash_with_nothing_after_it_is_kept() -> None:
    (pattern,) = _live_patterns(_sqlplus([_sqlplus_row("SELECT 'C:\\")]))
    assert pattern["query_text"] == "SELECT 'C:\\"


TAGGED = "-- controller='cart',action='show'\nSELECT o.id FROM app.orders o WHERE o.id = :1"


def test_a_leading_line_comment_tag_ends_at_its_line() -> None:
    """docs/contracts/assess.md: an Oracle collection made with the current script or
    live collector keeps a leading ``--`` tag, and the statement after it; in an older
    one the tag ran into the statement and is not read."""
    queries, _ = _parsed([_row("fms_1", _offline_text(TAGGED))])
    live = _live_pattern(TAGGED)
    for q in (queries["fms_1"], live):
        assert q["query_tags"] == ["controller='cart',action='show'"]
        assert q["tables_accessed"] == ["app.orders"]
    # An older, flattened collection: the tag runs into the statement and is not read.
    old, _ = _parsed([_row("fms_2", _flat(TAGGED))])
    assert old["fms_2"]["query_tags"] == []


ORA_TRAILER = (
    "ERROR:\n"
    "ORA-06502: PL/SQL: numeric or value error: character string buffer too small\n"
    "ORA-06512: at line 1\n"
)


def test_an_ora_error_after_a_row_fails_the_capture() -> None:
    """The review's probe: one good row and an ORA-06502 trailer used to give 4
    patterns (three named after the error lines); the collector now raises, and the
    Oracle collector records the query stage as failed rather than as these rows."""
    output = _sqlplus([_sqlplus_row(_live_text(ISSUE_EXAMPLE))]) + ORA_TRAILER
    with _client_error("ORA-06502"):
        _live_patterns(output)


def test_an_sqlplus_error_with_no_rows_fails_the_capture() -> None:
    output = 'SP2-0734: unknown command beginning "SELEC 1..." - rest of line ignored.\n'
    with _client_error("SP2-0734"):
        _live_patterns(output)
    with _client_error("ORA-00942"):
        _live_patterns(
            "  FROM V$SQL\n       *\nERROR at line 12:\nORA-00942: table or view does not exist\n"
        )


def test_a_statement_mentioning_an_error_is_a_row() -> None:
    """Only a line that starts with an error is one: a row's first column is a value, or
    padding when it is empty, so a statement that names ORA-00001 reads as a row."""
    raw = (
        "SELECT 'ORA-00001: unique constraint' AS msg FROM app.errors -- ERROR: noted\nWHERE 1 = 1"
    )
    assert _live_pattern(raw)["query_text"] == raw
    (pattern,) = _live_patterns(_sqlplus([_sqlplus_row("ORA-00001 in text", query_id="")]))
    assert pattern["query_text"] == "ORA-00001 in text"


def test_the_live_command_stops_sqlplus_on_an_error() -> None:
    command = _build_sql_command("oracle", "h", 1521, "APP", SECRET_ARN, "SELECT 1;", "us-east-1")
    preamble = command.split("printf '", 1)[1].split("SELECT 1;", 1)[0].split("\\n")
    assert preamble[:2] == ["WHENEVER SQLERROR EXIT SQL.SQLCODE", "WHENEVER OSERROR EXIT FAILURE"]


def test_a_failed_capture_is_recorded_as_failed() -> None:
    """The probe's output through the Oracle collector's query stage: recorded as a
    failed statement query (#384), never as rows."""
    from src.agents.collector.oracle_collector import _collect_queries_raw

    cred = MagicMock()
    cred.region = "us-east-1"
    ssm = SSMExecutor(cred, "i-0123456789abcdef0")
    output = _sqlplus([_sqlplus_row(_live_text(ISSUE_EXAMPLE))]) + ORA_TRAILER
    ssm.run_command = MagicMock(return_value=output)  # type: ignore[method-assign]
    db = _collector(ssm)
    opts = MagicMock(collect_query_patterns=True, min_executions=10, query_limit=None)
    with patch.object(db, "get_current_schema", return_value="APP"):
        rows = _collect_queries_raw(db, opts)
    assert rows == {"error": "the statement query failed (SqlClientError)"}
