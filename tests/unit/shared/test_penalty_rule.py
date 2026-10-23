"""One rule sizes every penalty in the pattern catalog (ADR-031 Decision 8, #920).

Before #920 every DocumentDB anti-pattern, and DynamoDB's full-scan anti-patterns, cost
0 points, while Aurora's cost 15 to 35: a DocumentDB anti-pattern only added a sentence
to the report, an Aurora one moved queries. That is an engine bias living in data.

Now each pattern file that carries a penalty or reports an anti-pattern names its
``penalty_basis``: the consequence the engine's public documentation states for a query
showing the pattern. The basis fixes the points, the same for every engine:

- ``cannot_run`` 40: the engine cannot run the pattern at all;
- ``needs_redesign`` 30: no server-side operation serves the access path; it needs
  precomputation, a data-model redesign or another engine;
- ``reads_more_than_returns`` 20: the engine runs it, but reads more than it returns (a
  scan or a per-collection join stage), so the work grows with the data;
- ``workload_fit`` 20: the engine runs it natively, but the query uses none of what
  distinguishes the engine, and another candidate's documentation names this access
  path as its native one;
- ``bounded_limit`` 10: the engine runs it within a documented bound the design must
  respect (a page size, an item count, a document size, a version or cluster type);
- ``guidance_only`` 0: the documentation sets no limit the query hits, or another pattern
  already prices the same fact; the entry is guidance.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from src.agents.referee.query_scoring import build_anti_pattern_map
from src.shared.catalog.patterns import PENALTY_POINTS, PatternEntry, patterns
from src.tools.analysis.aurora_pg_analysis_tools import analyze_aurora_pg_use_cases
from src.tools.analysis.documentdb_analysis_tools import analyze_documentdb_use_cases
from src.tools.analysis.dynamodb_analysis_tools import analyze_dynamodb_use_cases
from src.tools.analysis.opensearch_analysis_tools import analyze_opensearch_use_cases
from src.tools.analysis.redis_analysis_tools import analyze_redis_use_cases

# Every pattern's basis, pinned so a change is a deliberate, reviewed edit.
EXPECTED_BASIS = {
    "acid-transactions": "cannot_run",
    "full-text-search": "cannot_run",
    "fuzzy-search": "cannot_run",
    "regex-search": "cannot_run",
    "text_search": "cannot_run",
    "wildcard-search": "cannot_run",
    "complex-aggregation": "needs_redesign",
    "multi-index-joins": "needs_redesign",
    "correlated-subquery-joins": "reads_more_than_returns",
    "frequent-full-scan": "reads_more_than_returns",
    "ordered-scan": "reads_more_than_returns",
    "heavy-cross-collection-joins": "reads_more_than_returns",
    "high-volume-text-search": "reads_more_than_returns",
    "substring-match": "reads_more_than_returns",
    "high-frequency-pk-lookup": "workload_fit",
    "no-relational-need": "workload_fit",
    "simple-cache-read": "workload_fit",
    "single-access-pattern-table": "workload_fit",
    "graph-traversal-hierarchy": "bounded_limit",
    "large-result-sets": "bounded_limit",
    "unbounded-array-growth": "bounded_limit",
    "unbounded-result-set": "bounded_limit",
    "wide-transaction": "bounded_limit",
    "window-functions": "bounded_limit",
    "audit-columns-only": "guidance_only",
    "faceted-analytics": "bounded_limit",
    "full-text-search-candidate": "guidance_only",
    "high-frequency-small-updates": "guidance_only",
    "negation-heavy-queries": "guidance_only",
    "unbounded-time-series-ingest": "guidance_only",
}

# A public documentation source named in the text.
_SOURCE_RE = re.compile(
    r"Developer Guide|API Reference|Reference Manual|User Guide|documentation|"
    r"Documentation|docs\.opensearch\.org"
)


def _carries_a_penalty(entry: PatternEntry) -> bool:
    kinds = {s.kind for s in entry.engines.values()}
    return bool(entry.penalty) or bool(kinds & {"anti_pattern", "penalized", "scan", "redesign"})


class TestTheRule:
    def test_the_points_per_basis(self) -> None:
        assert PENALTY_POINTS == {
            "cannot_run": 40,
            "needs_redesign": 30,
            "reads_more_than_returns": 20,
            "workload_fit": 20,
            "bounded_limit": 10,
            "guidance_only": 0,
        }

    def test_every_penalty_is_its_basis_points(self) -> None:
        wrong = [
            f"{e.id}: basis {e.penalty_basis}, penalty {e.penalty}"
            for e in patterns()
            if _carries_a_penalty(e)
            and (e.penalty_basis is None or e.penalty != PENALTY_POINTS[e.penalty_basis])
        ]
        assert not wrong, wrong

    def test_every_pattern_with_a_penalty_names_the_expected_basis(self) -> None:
        found = {e.id: e.penalty_basis for e in patterns() if _carries_a_penalty(e)}
        assert found == EXPECTED_BASIS

    def test_every_anti_pattern_cites_public_documentation(self) -> None:
        missing = [
            f"{e.id}.toml [engines.{engine}]"
            for e in patterns()
            for engine, s in e.engines.items()
            if s.kind == "anti_pattern" and not _SOURCE_RE.search(s.why)
        ]
        assert not missing, missing

    def test_the_loader_rejects_a_penalty_that_is_not_its_basis_points(self) -> None:
        good = next(e for e in patterns() if e.id == "wide-transaction")
        data = good.model_dump(exclude_defaults=True)
        data["penalty"] = 15
        with pytest.raises(ValueError, match="penalty_basis"):
            PatternEntry.model_validate(data)

    def test_the_loader_rejects_an_anti_pattern_with_no_basis(self) -> None:
        good = next(e for e in patterns() if e.id == "wide-transaction")
        data = good.model_dump(exclude_defaults=True)
        data.pop("penalty_basis")
        with pytest.raises(ValueError, match="penalty_basis"):
            PatternEntry.model_validate(data)


# ---------------------------------------------------------------------------
# Each changed penalty, end to end: a positive query loses the basis points on the
# engine whose analysis reports it, a negative query loses nothing.
# ---------------------------------------------------------------------------


def _col(name: str, data_type: str) -> dict[str, Any]:
    return {"column_name": name, "data_type": data_type, "nullable": False}


def _table(name: str, columns: list[dict], fks: list[dict] | None = None) -> dict[str, Any]:
    return {
        "table_id": f"public.{name}",
        "table_name": name,
        "schema_name": "public",
        "row_count": 2_000_000,
        "size_mb": 1500.0,
        "columns": columns,
        "primary_key": ["id"],
        "foreign_keys": fks or [],
        "indexes": [
            {"index_name": f"{name}_pkey", "columns": ["id"], "is_unique": True, "is_primary": True}
        ],
    }


TABLES = [
    _table("orders", [_col("id", "bigint"), _col("status", "varchar"), _col("created_at", "ts")]),
    _table(
        "order_items",
        [_col("id", "bigint"), _col("order_id", "bigint"), _col("sku", "varchar")],
        fks=[{"columns": ["order_id"], "referenced_table": "orders", "referenced_columns": ["id"]}],
    ),
    _table("skus", [_col("id", "bigint"), _col("sku", "varchar"), _col("vendor_id", "bigint")]),
    _table("vendors", [_col("id", "bigint"), _col("name", "varchar")]),
    _table(
        "categories",
        [_col("id", "bigint"), _col("parent_id", "bigint")],
        fks=[
            {
                "columns": ["parent_id"],
                "referenced_table": "categories",
                "referenced_columns": ["id"],
            }
        ],
    ),
]


def _q(
    qid: str,
    sql: str,
    tables: list[str],
    *,
    cps: float = 1.0,
    rows: float = 5.0,
    joins: int | None = None,
) -> dict[str, Any]:
    joins = len(tables) - 1 if joins is None else joins
    return {
        "query_id": qid,
        "query_text": sql,
        "query_type": sql.split()[0].upper(),
        "calls_per_second": cps,
        "frequency_per_hour": cps * 3600,
        "tables_accessed": [f"public.{t}" for t in tables],
        "rows_returned_avg": rows,
        "execution_time_ms_avg": 4.0,
        "has_joins": joins > 0,
        "join_count": joins,
    }


KEY_READ = "SELECT * FROM orders WHERE id = $1"
FOUR_TABLE_JOIN = (
    "SELECT o.id FROM orders o JOIN order_items i ON i.order_id = o.id "
    "JOIN skus s ON s.sku = i.sku JOIN vendors v ON v.id = s.vendor_id WHERE o.id = $1"
)
FOUR_TABLES = ["orders", "order_items", "skus", "vendors"]

ANALYSES = {
    "dynamodb": analyze_dynamodb_use_cases,
    "documentdb": analyze_documentdb_use_cases,
    "opensearch": analyze_opensearch_use_cases,
    "aurora_postgresql": analyze_aurora_pg_use_cases,
    "elasticache": analyze_redis_use_cases,
}

# (pattern, engine, positive query, negative query)
CASES = [
    (
        "frequent-full-scan",
        "dynamodb",
        _q("pos", "SELECT * FROM orders ORDER BY created_at DESC", ["orders"], rows=500),
        _q("neg", KEY_READ, ["orders"], rows=1),
    ),
    (
        # A top-N over a whole table at any call rate: a Scan returns no order, so the
        # first rows in key order need every item read (#920, PR B).
        "ordered-scan",
        "dynamodb",
        _q("pos", "SELECT * FROM orders ORDER BY id ASC LIMIT $1", ["orders"], cps=0.001),
        _q(
            "neg",
            "SELECT * FROM order_items WHERE order_id = $1 ORDER BY id LIMIT $2",
            ["order_items"],
        ),
    ),
    (
        "complex-aggregation",
        "dynamodb",
        _q("pos", "SELECT status, COUNT(*) FROM orders WHERE id > $1 GROUP BY status", ["orders"]),
        _q("neg", KEY_READ, ["orders"], rows=1),
    ),
    (
        "unbounded-result-set",
        "dynamodb",
        _q("pos", "SELECT * FROM order_items WHERE order_id = $1", ["order_items"], rows=50_000),
        _q(
            "neg",
            "SELECT * FROM order_items WHERE order_id = $1 LIMIT 100",
            ["order_items"],
            rows=100,
        ),
    ),
    (
        "heavy-cross-collection-joins",
        "documentdb",
        _q("pos", FOUR_TABLE_JOIN, FOUR_TABLES),
        _q(
            "neg",
            "SELECT o.id FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.id = $1",
            ["orders", "order_items"],
        ),
    ),
    (
        "correlated-subquery-joins",
        "documentdb",
        _q(
            "pos",
            "SELECT o.id FROM orders o WHERE EXISTS ( SELECT 1 FROM order_items i "
            "WHERE i.order_id = o.id AND i.sku = $1 )",
            ["orders", "order_items"],
        ),
        _q("neg", KEY_READ, ["orders"]),
    ),
    (
        "graph-traversal-hierarchy",
        "documentdb",
        _q(
            "pos",
            "SELECT c.id, p.id FROM categories c JOIN categories p ON p.id = c.parent_id "
            "WHERE c.id = $1",
            ["categories"],
            joins=1,
        ),
        _q("neg", "SELECT * FROM categories WHERE id = $1", ["categories"]),
    ),
    (
        # #968: several groupings of one row set ($facet, 8.0.2+); a GROUP BY over two
        # columns is one $group with a compound _id and is not the pattern.
        "faceted-analytics",
        "documentdb",
        _q(
            "pos",
            "SELECT status, created_at, COUNT(*) FROM orders GROUP BY ROLLUP (status, created_at)",
            ["orders"],
        ),
        _q(
            "neg",
            "SELECT status, created_at, COUNT(*) FROM orders GROUP BY status, created_at",
            ["orders"],
        ),
    ),
    (
        # #968: one key's COUNT is a Query with Select = COUNT, not a redesign.
        "complex-aggregation",
        "dynamodb",
        _q("pos", "SELECT COUNT(*) FROM order_items WHERE price > $1", ["order_items"]),
        _q("neg", "SELECT COUNT(*) FROM order_items WHERE order_id = $1", ["order_items"]),
    ),
    (
        # LEAD alone and NTILE alone are the offset_window gate too.
        "window-functions",
        "documentdb",
        _q(
            "pos",
            "SELECT id, RANK() OVER (ORDER BY created_at) FROM orders WHERE status = $1",
            ["orders"],
        ),
        _q(
            "neg",
            "SELECT id, LEAD(total) OVER (ORDER BY created_at) FROM orders WHERE status = $1",
            ["orders"],
        ),
    ),
    (
        "window-functions",
        "documentdb",
        _q(
            "pos",
            "SELECT id, AVG(total) OVER (PARTITION BY status) FROM orders WHERE status = $1",
            ["orders"],
        ),
        _q(
            "neg",
            "SELECT id, NTILE(4) OVER (ORDER BY total) FROM orders WHERE status = $1",
            ["orders"],
        ),
    ),
    (
        # #968: LAG alone is the offset_window gate, not this version-bound entry.
        "window-functions",
        "documentdb",
        _q(
            "pos",
            "SELECT id, SUM(total) OVER (ORDER BY created_at) FROM orders WHERE status = $1",
            ["orders"],
        ),
        _q(
            "neg",
            "SELECT id, LAG(total) OVER (ORDER BY created_at) FROM orders WHERE status = $1",
            ["orders"],
        ),
    ),
    (
        "window-functions",
        "documentdb",
        _q(
            "pos",
            "SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) FROM orders WHERE status = $1",
            ["orders"],
        ),
        _q("neg", "SELECT id FROM orders WHERE status = $1 ORDER BY created_at", ["orders"]),
    ),
    (
        "multi-index-joins",
        "opensearch",
        _q("pos", FOUR_TABLE_JOIN, FOUR_TABLES),
        _q("neg", KEY_READ, ["orders"]),
    ),
    (
        "high-frequency-pk-lookup",
        "aurora_postgresql",
        _q("pos", KEY_READ, ["orders"], cps=500, rows=1),
        _q("neg", KEY_READ, ["orders"], cps=5, rows=1),
    ),
    (
        "simple-cache-read",
        "aurora_postgresql",
        _q("pos", "SELECT * FROM orders WHERE status = $1 LIMIT 5", ["orders"], cps=500, rows=5),
        _q("neg", "SELECT * FROM orders WHERE status = $1 LIMIT 5", ["orders"], cps=1, rows=5),
    ),
    (
        "large-result-sets",
        "elasticache",
        _q("pos", "SELECT * FROM order_items WHERE order_id = $1", ["order_items"], rows=50_000),
        _q("neg", "SELECT * FROM order_items WHERE order_id = $1", ["order_items"], rows=50),
    ),
]

# Detected, and priced at 0: the documentation sets no limit the detected query hits.
GUIDANCE_ONLY_CASES = [
    (
        "negation-heavy-queries",
        "documentdb",
        _q("pos", "SELECT * FROM orders WHERE id = $1 AND status NOT IN ('a', 'b')", ["orders"]),
    ),
]


def _penalties(
    engine: str, query: dict, only: str | None = None
) -> tuple[dict[str, int], set[str]]:
    """The penalty per query id on ``engine``, from ``only`` that anti-pattern when given
    (a query may show several, and the biggest penalty wins), and the anti-patterns
    ``engine``'s analysis reports for ``query``."""
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": TABLES},
        "queries": {"query_patterns": [query]},
    }
    analysis = ANALYSES[engine](collector)
    reported = {
        ap.anti_pattern_type
        for ap in analysis.anti_patterns_detected or []
        if query["query_id"] in (ap.query_ids or [])
    }
    dumped = analysis.model_dump()
    if only is not None:
        dumped["anti_patterns_detected"] = [
            ap
            for ap in dumped.get("anti_patterns_detected") or []
            if ap["anti_pattern_type"] == only
        ]
    penalties, _notes = build_anti_pattern_map({engine: {"workload_analysis": dumped}})
    return {qid: p for (qid, e), p in penalties.items() if e == engine}, reported


@pytest.mark.parametrize(
    ("pattern_id", "engine", "positive", "negative"), CASES, ids=[c[0] for c in CASES]
)
def test_a_query_showing_the_anti_pattern_loses_its_basis_points(
    pattern_id, engine, positive, negative
):
    entry = next(e for e in patterns() if e.id == pattern_id)
    points = PENALTY_POINTS[entry.penalty_basis]
    assert points > 0
    penalties, reported = _penalties(engine, positive, only=pattern_id)
    assert pattern_id in reported
    assert penalties.get("pos") == points
    negative_penalties, negative_reported = _penalties(engine, negative, only=pattern_id)
    assert pattern_id not in negative_reported
    assert "neg" not in negative_penalties


@pytest.mark.parametrize(
    ("pattern_id", "engine", "query"), GUIDANCE_ONLY_CASES, ids=[c[0] for c in GUIDANCE_ONLY_CASES]
)
def test_a_guidance_only_anti_pattern_is_reported_but_costs_nothing(pattern_id, engine, query):
    penalties, reported = _penalties(engine, query)
    assert pattern_id in reported
    assert "pos" not in penalties


class TestTheWideTransactionPenaltyIsCalibrated:
    """#916: the wide-transaction penalty is its basis's points, and the catalog says why."""

    def test_it_is_the_bounded_limit_basis(self) -> None:
        entry = next(p for p in patterns() if p.id == "wide-transaction")
        assert (entry.penalty, entry.penalty_basis) == (
            PENALTY_POINTS["bounded_limit"],
            "bounded_limit",
        )

    def test_the_catalog_states_why_with_its_source(self) -> None:
        entry = next(p for p in patterns() if p.id == "wide-transaction")
        assert entry.penalty_why
        assert "100 items" in entry.penalty_why
        assert "Amazon DynamoDB Developer Guide" in entry.penalty_why

    def test_the_rule_catalog_renders_the_reason(self) -> None:
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[3] / "docs/architecture/rule-catalog.md"
        ).read_text()
        section = text.split("### wide-transaction", 1)[1].split("\n### ", 1)[0]
        assert "Penalty: 10 (`bounded_limit`)." in section

    def test_a_reason_needs_a_basis(self) -> None:
        with pytest.raises(ValueError, match="penalty_why explains a penalty_basis"):
            PatternEntry.model_validate(
                {
                    "id": "x",
                    "name": "X",
                    "detectors": [],
                    "penalty": 0,
                    "penalty_why": "y",
                    "engines": {},
                }
            )
