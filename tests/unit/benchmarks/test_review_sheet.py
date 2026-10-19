"""Blind answer-key review sheets (#1074, step 2).

The sheet must never show the engine the pipeline picked or a score, so a key
written from it is not anchored on today's routing output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from benchmarks.runner.review_sheet import (
    HEADER,
    REVIEW_DIR,
    ROWS_MARKER,
    _short_ids,
    build_rows,
    render,
)
from src.shared.engine_names import ENGINE_DISPLAY_NAMES

REPO = Path(__file__).resolve().parents[3]
SNAPSHOT = REPO / "tests" / "e2e" / "baselines" / "routing_snapshot.json"
SAMPLES = ("wordpress", "discourse", "trailgear")
# Engine ids and display names, plus the source and product family words a
# reason could name (MySQL, PostgreSQL, Redis, ...).
FAMILY_WORDS = (
    "mysql",
    "mariadb",
    "postgres",
    "postgresql",
    "sql server",
    "sqlserver",
    "oracle",
    "redis",
    "valkey",
    "memcached",
    "mongo",
    "mongodb",
    "cassandra",
    "elasticsearch",
    "aurora",
    "dynamo",
    "documentdb",
    "opensearch",
    "elasticache",
    "neptune",
    "keyspaces",
)
ENGINE_WORDS = re.compile(
    "|".join(
        sorted(
            {re.escape(e) for e in ENGINE_DISPLAY_NAMES}
            | {re.escape(n) for n in ENGINE_DISPLAY_NAMES.values()}
            | {re.escape(w) for w in FAMILY_WORDS},
            key=len,
            reverse=True,
        )
    ),
    re.IGNORECASE,
)
TABLES_COLUMN = 4  # zero-based: #, id, SQL, op, tables


def _engine_words(row: list[str]) -> list[str]:
    """Engine words in a row's cells other than SQL, with the row's own table
    names (customer data, e.g. ``mysql_logs``) taken out first."""
    tables = sorted(
        {t.strip() for t in row[TABLES_COLUMN].split(",") if t.strip()}, key=len, reverse=True
    )
    text = " ".join(c for i, c in enumerate(row) if i != SQL_COLUMN)
    for t in tables:
        text = text.replace(t, " ")
    return ENGINE_WORDS.findall(text)


SQL_COLUMN = 2  # zero-based: #, id, SQL


def _q(qid, text, qtype="SELECT", tables=("db.t",), calls=1.0, **kw):
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "tables_accessed": list(tables),
        "calls_per_second": calls,
        **kw,
    }


def _ap(qid, cls="key_lookup", reason="one row of t by its full unique key", facts=()):
    return {
        "query_id": qid,
        "class": cls,
        "reason": reason,
        "facts": list(facts),
        # what the sheet must never show
        "class_scores": {"dynamodb": 100, "aurora_mysql": 80},
        "parity_anchors": {"documentdb": "parity anchor: DocumentDB 80"},
    }


@pytest.fixture
def job():
    collector = {
        "queries": {
            "query_patterns": [
                _q("r1", "SELECT * FROM t WHERE id = ?", calls=90.0),
                _q("r2", "SELECT a FROM t WHERE id = ?", calls=5.0),
                _q("w1", "UPDATE t SET a = ? WHERE id = ?", qtype="UPDATE", calls=1.0),
                _q("g1", "SELECT * FROM u JOIN v ON u.id = v.u_id", tables=("db.u", "db.v")),
                _q("g2", "DELETE FROM v WHERE u_id = ?", qtype="DELETE", tables=("db.v",)),
                _q("x1", "BEGIN", qtype="OTHER", tables=("unknown",), calls=50.0),
                _q("z1", "SELECT 1 FROM t WHERE b = ?", calls=0.0),
            ]
        }
    }
    access = {
        "queries": [
            _ap("r1"),
            _ap("r2"),
            _ap("w1", facts=["write"]),
            _ap("g1", cls="key_scoped", reason="key-anchored join"),
            _ap("g2", facts=["write"]),
            _ap("x1", cls="utility", reason="reads or writes no table data"),
            _ap("z1", cls="ad_hoc_filter", reason="rows of t filtered by b"),
        ]
    }
    assignment = {
        "co_dependency_groups": [["g1", "g2"]],
        "write_groups": {"db.t": ["w1"], "db.v": ["g2"]},
        # engine output the builder must ignore
        "query_assignments": [
            {"query_id": q, "assigned_engine": "dynamodb", "confidence": 95} for q in ("r1", "w1")
        ],
    }
    return collector, access, assignment


def _rows(text: str) -> list[list[str]]:
    body = text.split(ROWS_MARKER, 1)[1]
    out = []
    for line in body.splitlines():
        if re.match(r"^\| \d+ \|", line):
            cells = re.split(r"(?<!\\)\|", line.strip())[1:-1]
            out.append([c.strip() for c in cells])
    return out


def test_rows_never_show_an_engine_or_a_score(job):
    collector, access, assignment = job
    text = render("demo", build_rows(collector, access, assignment), {})
    body = text.split(ROWS_MARKER, 1)[1]
    assert not ENGINE_WORDS.search(body)
    assert "95" not in body and "class_scores" not in body and "parity" not in body
    assert len(_rows(text)) == 7


def test_the_engine_word_check_allows_table_names_but_catches_family_words():
    ok = ["1", "`q`", "`SELECT 1 FROM mysql_logs`", "SELECT", "mysql_logs", "1", "1%", "A"]
    ok += ["", "key_lookup", "one row of mysql_logs by its full unique key", "", "", "", "", ""]
    assert _engine_words(ok) == []
    for word in ("MySQL", "PostgreSQL", "Redis", "Valkey", "Mongo", "Aurora MySQL"):
        leak = list(ok)
        leak[10] = f"one row of mysql_logs, a fit for {word}"
        assert _engine_words(leak), word


def test_same_table_name_in_two_schemas_gets_two_titles():
    collector = {
        "queries": {
            "query_patterns": [
                _q("a", "SELECT * FROM s1.t WHERE id = ?", tables=("s1.t",)),
                _q("b", "SELECT * FROM s2.t WHERE id = ?", tables=("s2.t",)),
                _q("c", "SELECT * FROM s1.u WHERE id = ?", tables=("s1.u",)),
            ]
        }
    }
    access = {"queries": [_ap("a"), _ap("b"), _ap("c")]}
    titles = sorted(s.title for s in build_rows(collector, access, {}))
    assert titles == ["Table s1.t", "Table s2.t", "Table u"]


def test_the_sheet_suggests_no_answer(job):
    text = render("demo", build_rows(*job), {})
    assert "source-compatible" not in text and "usually" not in text


def test_sections_group_co_dependency_members_and_put_writes_first(job):
    sections = build_rows(*job)
    titles = [s.title for s in sections]
    by_title = {s.title: [r.query_id for r in s.rows] for s in sections}
    assert by_title["Co-dependency group CD1"] == ["g2", "g1"]  # the write first
    assert by_title["Table t"] == ["w1", "r1", "r2", "z1"]
    # busiest section first; utility statements last whatever their call rate
    assert titles[0] == "Table t"
    assert titles[-1] == "Utility statements (no table data)"


def test_tiers_and_shape_pointers(job):
    rows = {r.query_id: r for s in build_rows(*job) for r in s.rows}
    assert rows["r1"].tier == "A"
    assert rows["z1"].tier == "C"  # no recorded calls
    assert rows["x1"].tier == "-"  # utility: no tier, not in the shares
    assert rows["r2"].shape_of == rows["r1"].number  # same access path, tables, op
    assert rows["r1"].shape_of is None and rows["w1"].shape_of is None
    assert all(r.shape_of is None or r.shape_of < r.number for r in rows.values())


def test_short_ids_stay_unique():
    long_a = "a" * 12 + "1" * 52
    long_b = "a" * 12 + "2" * 52
    ids = _short_ids([long_a, long_b, "12345"])
    assert ids["12345"] == "12345"
    assert ids[long_a] != ids[long_b]
    assert long_a.startswith(ids[long_a]) and long_b.startswith(ids[long_b])


# --- the committed sheets -----------------------------------------------------


@pytest.mark.parametrize("sample", SAMPLES)
def test_committed_sheet_is_blind_and_covers_every_query(sample):
    text = (REVIEW_DIR / f"{sample}.md").read_text()
    rows = _rows(text)
    width = HEADER.count("|") - 1
    assert all(len(r) == width for r in rows)
    for r in rows:
        # the SQL is the customer's own text; every other cell is routing facts
        assert _engine_words(r) == [], r
        assert r[-3:] == ["", "", ""], r  # acceptable, ideal, rationale left for the reviewer
    assert [int(r[0]) for r in rows] == list(range(1, len(rows) + 1))
    # one row per query of the sample (ids from the routing snapshot's query list);
    # a changed collector regenerates the sheet with `-m benchmarks.runner.review_sheet`
    snapshot = json.loads(SNAPSHOT.read_text())["samples"][sample]["queries"]
    shown = [r[1].strip("`") for r in rows]
    assert len(shown) == len(snapshot)
    assert all(any(q.startswith(s) for q in snapshot) for s in shown)
    assert len(set(shown)) == len(shown)
