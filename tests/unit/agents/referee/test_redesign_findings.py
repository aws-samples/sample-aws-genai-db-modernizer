"""Each query is scored as written and after redesign (#1231, maintainer decision 2026-10-20).

ADR-031 Decision 2 Amendment 13: routing scores a query's pattern dimension on the class
a redesigned target gives it (its own equality filters, ``IS NOT NULL`` tests a partial
index backs and ``OR`` of
lookups keyed) when that redesign anchors it, and only where a key works (no enum,
category, flag, date or low-cardinality column; never a keyset batch walk). The same
redesign labels each anti-pattern finding: ``fixable`` only where it removes the read the
penalty is about (the pattern file's ``removed_by_redesign``), else ``intrinsic``, which
keeps its penalty. One module (``redesign_findings``) decides both. No test calls a model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.referee.access_pattern import KEY_SCOPED, is_redesign_key_eligible
from src.agents.referee.query_pattern import (
    QueryPatternModel,
    access_patterns_record,
    redesign_requirements,
)
from src.agents.referee.query_scoring import (
    ScoringContext,
    anti_pattern_findings,
    build_anti_pattern_map,
    score_query_for_engine,
)
from src.agents.referee.redesign_findings import (
    FIXABLE,
    INTRINSIC,
    VIA_EQUALITY,
    VIA_NOT_NULL_TEST,
    VIA_OR_BRANCH,
    RedesignModel,
)
from src.agents.referee.substring_match import scan_adjustments
from src.shared.catalog.patterns import PatternEntry, pattern, removed_by_redesign

SCAN = "frequent-full-scan"
ORDERED_SCAN = "ordered-scan"
AGGREGATE = "complex-aggregation"
WIDE_WRITE = "wide-transaction"
CORRELATED = "correlated-subquery-joins"
CANNOT_RUN = "wildcard-search"
WORKLOAD_FIT = "no-relational-need"
KV = "dynamodb"  # the key-value engine whose class scores the redesign changes


def _column(name: str, data_type: str, **extra) -> dict:
    return {"column_name": name, "data_type": data_type, **extra}


def _collector(queries: list[dict], *, rows: int = 100_000, **cardinality: int) -> dict:
    columns = [
        _column("id", "integer"),
        _column("customer_id", "integer"),
        _column("email", "varchar(20)"),
        _column("region", "varchar(20)"),
        _column("trigger", "varchar(20)"),
        _column("line_no", "integer"),
        _column("shipped_at", "integer"),
        _column("status", "varchar(20)"),
        _column("order_type", "integer"),
        _column("kind", "enum('a','b')"),
        _column("claimed_by", "integer"),
        _column("token", "varchar(64)"),
        _column("prev_token", "varchar(64)"),
        _column("created_at", "timestamp"),
        _column("is_paid", "boolean"),
        _column("note", "text"),
    ]
    for column in columns:
        if column["column_name"] in cardinality:
            column["cardinality"] = cardinality[column["column_name"]]
    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "primary_key": ["id"],
                    "row_count": rows,
                    "columns": columns,
                    "indexes": [
                        {"index_name": "o_cust", "columns": ["customer_id"]},
                        {
                            "index_name": "o_line",
                            "columns": ["customer_id", "line_no"],
                            "is_unique": True,
                        },
                        {
                            "index_name": "o_open",
                            "columns": ["id"],
                            "predicate": "claimed_by IS NOT NULL",
                        },
                    ],
                    "foreign_keys": [{"columns": ["customer_id"], "referenced_table": "customers"}],
                },
                {
                    "table_id": "shop.customers",
                    "table_name": "customers",
                    "primary_key": ["id"],
                    "row_count": 20_000,
                    "columns": [_column("id", "integer"), _column("country", "varchar(2)")],
                    "indexes": [],
                    "foreign_keys": [],
                },
            ]
        },
        "queries": {"query_patterns": queries},
    }


def _q(qid: str, text: str, qtype: str = "SELECT", tables=("shop.orders",)) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "calls_per_second": 5.0,
        "tables_accessed": list(tables),
    }


def _model(text: str, qtype: str = "SELECT", **kw) -> tuple[QueryPatternModel, dict]:
    query = _q("q", text, qtype)
    return QueryPatternModel.from_pipeline(_collector([query], **kw), {}), query


def _label(text: str, pattern_type: str, qtype: str = "SELECT", **kw):
    query = _q("q", text, qtype)
    return RedesignModel.from_collector(_collector([query], **kw)).classify(query, pattern_type)


# ---------------------------------------------------------------------------
# The double score
# ---------------------------------------------------------------------------


class TestTheRedesignApplies:
    def test_an_equality_on_a_column_with_no_index_is_scored_as_a_keyed_read(self):
        model, query = _model("SELECT * FROM orders WHERE email = $1")
        found = model.redesign(query)
        assert found is not None
        assert found.written.name == "ad_hoc_filter" and found.redesigned.name == KEY_SCOPED
        assert [(k.label, k.via, k.cardinality) for k in found.keys] == [
            ("orders.email", VIA_EQUALITY, "unknown")
        ]
        assert model.written_pattern_score(query, KV) == 0
        assert model.pattern_score(query, KV) > 0

    def test_an_is_not_null_queue_a_partial_index_backs_is_a_sparse_index(self):
        model, query = _model(
            "SELECT * FROM orders WHERE claimed_by IS NOT NULL ORDER BY id LIMIT 10"
        )
        found = model.redesign(query)
        assert found is not None and [(k.label, k.via) for k in found.keys] == [
            ("orders.claimed_by", VIA_NOT_NULL_TEST)
        ]

    def test_an_or_of_two_token_lookups_is_two_indexes(self):
        model, query = _model("SELECT * FROM orders WHERE token = $1 OR prev_token = $2")
        found = model.redesign(query)
        assert found is not None and found.redesigned.name == KEY_SCOPED
        assert sorted((k.label, k.via) for k in found.keys) == [
            ("orders.prev_token", VIA_OR_BRANCH),
            ("orders.token", VIA_OR_BRANCH),
        ]

    def test_statistics_that_show_a_category_name_high_cardinality_overrule_the_name(self):
        model, query = _model("SELECT * FROM orders WHERE status = $1", status=60_000)
        assert model.redesign(query) is not None


class TestNoRedesign:
    @pytest.mark.parametrize(
        "text",
        [
            "SELECT * FROM orders WHERE status = $1",  # a category name, no statistics
            "SELECT * FROM orders WHERE order_type = $1",  # a type
            "SELECT * FROM orders WHERE kind = $1",  # an enum type
            "SELECT * FROM orders WHERE is_paid = $1",  # true/false
            "SELECT * FROM orders WHERE created_at = $1",  # a point in time
        ],
    )
    def test_a_column_no_key_works_on(self, text):
        model, query = _model(text)
        assert model.redesign(query) is None
        assert model.pattern_score(query, KV) == model.written_pattern_score(query, KV)

    def test_a_low_cardinality_column_by_the_statistics(self):
        model, query = _model("SELECT * FROM orders WHERE email = $1", email=5)
        assert model.redesign(query) is None

    def test_a_keyset_batch_walk_reads_everything_whatever_is_indexed(self):
        model, query = _model(
            "SELECT * FROM orders WHERE id > $1 AND claimed_by IS NOT NULL ORDER BY id LIMIT 10"
        )
        assert model.redesign(query) is None

    def test_is_null_is_never_a_sparse_index_key(self):
        """A sparse index leaves out exactly the rows an IS NULL reads, an anti-join's
        included."""
        for text in (
            "SELECT * FROM orders WHERE claimed_by IS NULL ORDER BY id LIMIT 10",
            "SELECT o.id FROM orders o LEFT JOIN customers c ON c.id = o.customer_id "
            "WHERE c.id IS NULL",
        ):
            model, query = _model(text)
            assert model.redesign(query) is None, text

    def test_is_not_null_with_no_selectivity_signal_is_no_key(self):
        model, query = _model("SELECT * FROM orders WHERE shipped_at IS NOT NULL")
        assert model.redesign(query) is None
        assert (
            "no selectivity signal"
            in _label("SELECT * FROM orders WHERE shipped_at IS NOT NULL", SCAN).evidence
        )

    @pytest.mark.parametrize("column", ["region", "trigger"])
    def test_a_region_or_trigger_name_with_no_statistics_is_a_category(self, column):
        model, query = _model(f"SELECT * FROM orders WHERE {column} = $1")
        assert model.redesign(query) is None

    @pytest.mark.parametrize(
        "column",
        [
            _column("flag", "tinyint(1)"),  # MySQL's boolean
            _column("media", "hotlinked_media_status"),  # a category-named enum type
            _column("is_ready", "integer"),  # a flag prefix on a non-boolean type
            _column("has_items", "integer"),
            _column("flair_icon", "varchar(40)"),
            _column("theme_color", "varchar(8)"),
            _column("region_code", "varchar(8)"),  # a region-like word mid-name
            _column("body", "text"),  # unbounded text
            _column("payload", "jsonb"),
        ],
    )
    def test_the_type_and_name_rules(self, column):
        assert not is_redesign_key_eligible(column, 100_000)

    def test_statistics_overrule_a_name_only_above_an_absolute_floor(self):
        status = _column("status", "varchar(20)")
        # A large table with many distinct values: the statistics decide.
        assert is_redesign_key_eligible({**status, "cardinality": 60_000}, 100_000)
        # Too few distinct values, whatever their share of the rows.
        assert not is_redesign_key_eligible({**status, "cardinality": 3}, 100)
        assert not is_redesign_key_eligible({**_column("code", "integer"), "cardinality": 50}, 60)
        # A small table: the count says nothing, so the name still decides.
        assert not is_redesign_key_eligible({**status, "cardinality": 500}, 1_000)
        assert is_redesign_key_eligible({**_column("code", "integer"), "cardinality": 500}, 1_000)

    def test_a_partial_index_counts_only_when_it_selects_the_non_null_rows(self):
        query = _q("q", "SELECT * FROM orders WHERE claimed_by IS NOT NULL ORDER BY id LIMIT 5")
        collector = _collector([query])
        orders, _ = collector["database_schema"]["tables"]
        orders["indexes"][-1]["predicate"] = "claimed_by IS NULL"
        assert QueryPatternModel.from_pipeline(collector, {}).redesign(query) is None

    def test_a_later_column_of_a_composite_unique_key_is_no_key_alone(self):
        model, query = _model("SELECT * FROM orders WHERE line_no = $1")
        assert model.redesign(query) is None
        assert "composite key" in _label("SELECT * FROM orders WHERE line_no = $1", SCAN).evidence

    def test_a_correlated_pin_is_a_join_edge_not_a_value(self):
        correlated = (
            "SELECT c.id FROM customers c WHERE c.id = $1 AND EXISTS "
            "(SELECT 1 FROM orders o WHERE o.email = c.country)"
        )
        model, query = _model(correlated)
        assert model.redesign(query) is None
        # The same subquery pinned to a value is a lookup the redesign keys.
        model, query = _model(correlated.replace("c.country", "$2"))
        assert model.redesign(query) is not None
        # An OR of correlated pins is two join edges, not two token lookups.
        either = (
            "SELECT c.id FROM customers c WHERE c.id = $1 AND EXISTS "
            "(SELECT 1 FROM orders o WHERE o.token = c.country OR o.prev_token = c.country)"
        )
        model, query = _model(either)
        assert model.redesign(query) is None
        model, query = _model(either.replace("c.country", "$2", 1).replace("c.country", "$3"))
        assert model.redesign(query) is not None

    def test_a_write_never_keys_the_column_it_rewrites(self):
        model, query = _model("UPDATE orders SET email = $1 WHERE email = $2", "UPDATE")
        assert model.redesign(query) is None
        label = _label("UPDATE orders SET email = $1 WHERE email = $2", WIDE_WRITE, "UPDATE")
        assert label.kind == INTRINSIC and "rewrites" in label.evidence

    def test_an_or_with_a_branch_no_key_works_on(self):
        model, query = _model("SELECT * FROM orders WHERE token = $1 OR status = $2")
        assert model.redesign(query) is None

    def test_rows_a_key_already_anchors(self):
        model, query = _model("SELECT * FROM orders WHERE customer_id = $1 AND email = $2")
        assert model.redesign(query) is None

    def test_a_grouped_aggregate_stays_an_aggregate(self):
        model, query = _model("SELECT email, COUNT(*) FROM orders WHERE note = $1 GROUP BY email")
        assert model.redesign(query) is None

    def test_whole_reads_ranges_and_unreadable_statements(self):
        for text in (
            "SELECT * FROM orders",
            "SELECT * FROM orders WHERE created_at > $1",
            "SELEKT what FROM where",
        ):
            model, query = _model(text)
            assert model.redesign(query) is None, text

    def test_no_schema_no_redesign(self):
        query = _q("q", "SELECT * FROM orders WHERE email = $1")
        assert QueryPatternModel().redesign(query) is None


# ---------------------------------------------------------------------------
# Anti-pattern labels: only where the redesign removes the read the penalty is about
# ---------------------------------------------------------------------------


class TestFixable:
    def test_a_scan_the_redesign_anchors(self):
        label = _label(
            "SELECT * FROM orders WHERE claimed_by IS NOT NULL ORDER BY id LIMIT 5", SCAN
        )
        assert label.kind == FIXABLE and label.new_columns == ("orders.claimed_by",)

    def test_a_whole_read_of_a_small_reference_table(self):
        label = _label("SELECT * FROM orders ORDER BY email", SCAN, rows=50)
        assert label.kind == FIXABLE and "small reference table" in label.evidence

    def test_an_ungrouped_aggregate_the_redesign_anchors(self):
        label = _label("SELECT COUNT(*) FROM orders WHERE email = $1", AGGREGATE)
        assert label.kind == FIXABLE

    def test_a_write_by_a_plain_filter_the_redesign_keys(self):
        label = _label("UPDATE orders SET note = $1 WHERE email = $2", WIDE_WRITE, "UPDATE")
        assert label.kind == FIXABLE and "key scoped" in label.evidence


class TestIntrinsic:
    @pytest.mark.parametrize(
        "text",
        [
            "SELECT * FROM orders",  # a whole read of a large table
            "SELECT * FROM orders ORDER BY id LIMIT 20",  # a keyset batch walk
            "SELECT * FROM orders WHERE id > $1 AND claimed_by IS NOT NULL ORDER BY id LIMIT 5",
            "SELECT * FROM orders WHERE claimed_by IS NULL ORDER BY id LIMIT 5",
            "SELECT * FROM orders WHERE created_at > $1",
            "SELECT * FROM orders WHERE status = $1",
        ],
    )
    def test_a_scan_no_redesign_anchors(self, text):
        assert _label(text, SCAN).kind == INTRINSIC

    def test_a_grouped_aggregate_on_keyed_rows(self):
        label = _label(
            "SELECT email, COUNT(*) FROM orders WHERE customer_id = $1 GROUP BY email", AGGREGATE
        )
        assert label.kind == INTRINSIC

    def test_a_write_a_range_or_a_subquery_picks_keeps_decision_3(self):
        for text in (
            "DELETE FROM orders WHERE created_at < $1",
            "DELETE FROM orders WHERE id IN (SELECT id FROM orders WHERE email = $1)",
        ):
            assert _label(text, WIDE_WRITE, "DELETE").kind == INTRINSIC, text

    @pytest.mark.parametrize("pattern_type", [CORRELATED, CANNOT_RUN, WORKLOAD_FIT, "unknown-id"])
    def test_a_pattern_no_redesign_removes_even_on_redesigned_rows(self, pattern_type):
        label = _label("SELECT * FROM orders WHERE email = $1", pattern_type)
        assert label.kind == INTRINSIC and "no redesign removes" in label.evidence

    def test_the_evidence_tells_an_ineligible_column_from_an_uncollected_one(self):
        refused = _label("SELECT * FROM orders WHERE is_paid = $1", SCAN)
        assert "orders.is_paid: a date, time, true/false, enum or category" in refused.evidence
        unknown = _label("SELECT * FROM orders WHERE colour = $1", SCAN)
        assert "orders.colour: not in the collected schema" in unknown.evidence


def test_the_catalog_says_which_reads_a_redesign_removes():
    assert removed_by_redesign()[SCAN] == "rows"
    assert removed_by_redesign()[WIDE_WRITE] == "write_shape"
    for absent in (CORRELATED, CANNOT_RUN, WORKLOAD_FIT, "window-functions"):
        assert absent not in removed_by_redesign()
    entry = pattern(CANNOT_RUN).model_dump(exclude_defaults=True)
    with pytest.raises(ValueError, match="removed_by_redesign"):
        PatternEntry.model_validate({**entry, "removed_by_redesign": "rows"})


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------

REDESIGNED = _q("redesigned", "SELECT * FROM orders WHERE email = $1")
WHOLE = _q("whole", "SELECT * FROM orders")
KEYED = _q("keyed", "SELECT * FROM orders WHERE customer_id = $1")


def _analysis(anti: dict[str, list[str]], patterns: dict[str, list[str]] | None = None) -> dict:
    return {
        "table_recommendations": [
            {
                "table_id": "shop.orders",
                "score_breakdown": {
                    "pattern_match_score": 80,
                    "complexity_score": 80,
                    "performance_score": 80,
                    "cost_score": 80,
                },
            }
        ],
        "workload_analysis": {
            "anti_patterns_detected": [
                {"anti_pattern_type": t, "query_ids": q} for t, q in anti.items()
            ],
            "patterns_detected": [
                {"pattern_type": t, "query_ids": q} for t, q in (patterns or {}).items()
            ],
        },
    }


def _outputs() -> dict:
    return {
        KV: _analysis({SCAN: ["redesigned", "whole"]}),
        # A wildcard-search fit on another engine penalises KV (cannot_run).
        "opensearch": _analysis({}, {CANNOT_RUN: ["redesigned"]}),
    }


def _ctx(queries=(REDESIGNED, WHOLE, KEYED)) -> ScoringContext:
    return ScoringContext.from_pipeline(
        _outputs(), {"query_capabilities": {}}, _collector(list(queries))
    )


def test_the_scorer_scores_the_redesigned_class_and_keeps_only_intrinsic_penalties():
    ctx = _ctx()
    kinds = {(f.query_id, f.pattern): f.kind for f in ctx.anti_pattern_findings}
    assert kinds == {
        ("redesigned", SCAN): FIXABLE,
        ("redesigned", CANNOT_RUN): INTRINSIC,
        ("whole", SCAN): INTRINSIC,
    }
    assert ctx.anti_pattern_map[("redesigned", KV)] == 40  # the cannot_run one
    assert ctx.anti_pattern_map[("whole", KV)] == 20
    bare = ScoringContext.from_pipeline(
        {KV: _analysis({})}, {"query_capabilities": {}}, _collector([REDESIGNED])
    )
    # The pattern dimension reads the redesigned class: KV scores above its written 0.
    assert ctx.pattern_model.written_pattern_score(REDESIGNED, KV) == 0
    assert (score_query_for_engine(REDESIGNED, KV, bare).score or 0) > 0


def test_the_decision_trace_names_the_assumed_key_where_the_engine_relies_on_it():
    ctx = _ctx()
    assert {qid for qid, _ in ctx.redesign_notes} == {"redesigned"}
    note = ctx.redesign_notes[("redesigned", KV)]
    assert "orders.email" in note and "ad hoc filter as written" in note
    # An engine whose written score is at least as good relies on nothing.
    for _qid, engine in ctx.redesign_notes:
        assert ctx.pattern_model.relies_on_redesign(REDESIGNED, engine), engine


def test_a_redesign_never_lowers_an_engines_pattern_score():
    """Each engine's pattern score is the better of as written and after redesign: an
    index never makes an engine worse at a query (#1231 re-review)."""
    queries = [
        _q("a", "SELECT * FROM orders WHERE email = $1"),
        _q("b", "SELECT * FROM orders WHERE token = $1 OR prev_token = $2"),
        _q("c", "SELECT * FROM orders WHERE claimed_by IS NOT NULL ORDER BY id LIMIT 5"),
        _q(
            "d",
            "SELECT c.country FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.email = $1",
            tables=("shop.orders", "shop.customers"),
        ),
    ]
    model = QueryPatternModel.from_pipeline(_collector(queries), {})
    lowered_somewhere = False
    for query in queries:
        assert model.redesign(query) is not None, query["query_id"]
        for engine in ("aurora_postgresql", "aurora_mysql", "documentdb", KV, "opensearch"):
            written = model.written_pattern_score(query, engine)
            redesigned = model.redesigned_pattern_score(query, engine)
            assert model.pattern_score(query, engine) == max(written, redesigned or 0)
            assert model.pattern_score(query, engine) >= written
            lowered_somewhere |= (redesigned or 0) < written
            assert model.relies_on_redesign(query, engine) == ((redesigned or 0) > written)
    assert lowered_somewhere, "the fixture must have an engine the redesign alone would lower"


def test_without_a_collector_nothing_is_redesigned_and_every_finding_keeps_its_penalty():
    ctx = ScoringContext.from_pipeline(_outputs(), {"query_capabilities": {}}, None)
    assert dict(ctx.anti_pattern_map) == build_anti_pattern_map(_outputs())[0]
    assert ctx.redesign_notes == {}


def test_build_anti_pattern_map_skips_exactly_the_fixable_findings():
    full, _ = build_anti_pattern_map(_outputs())
    skipped, _ = build_anti_pattern_map(_outputs(), {("whole", KV, SCAN)})
    assert {k: v for k, v in full.items() if k != ("whole", KV)} == skipped


def test_a_fixable_substring_scan_costs_nothing_and_no_note_says_it_scores_lower():
    collector = _collector([])
    collector["queries"]["query_patterns"] = [
        {
            **_q("s", "SELECT * FROM orders WHERE email = $1 AND note LIKE '%x'"),
            "calls_per_second": 50.0,
        }
    ]
    for table in collector["database_schema"]["tables"]:
        table["row_count"] = 10_000_000
    engines = ["aurora_mysql", "documentdb", "opensearch"]
    penalties, _notes = scan_adjustments(collector, engines)
    assert penalties, "the fixture must cross the scan threshold"
    lowered, notes = scan_adjustments(collector, engines, exempt=set(penalties))
    assert lowered == {}
    assert not any("lower here" in n for n in notes.values())


# ---------------------------------------------------------------------------
# The record and the designer requirement
# ---------------------------------------------------------------------------


def test_the_record_carries_both_scores_the_assumed_key_and_each_finding():
    collector = _collector([REDESIGNED, WHOLE, KEYED])
    record = access_patterns_record(collector, _outputs())
    assert record["schema_version"] == 2 and record["redesigned_queries"] == 1
    rows = {r["query_id"]: r for r in record["queries"]}
    row = rows["redesigned"]
    assert row["class"] == "ad_hoc_filter" and row["class_scores"][KV] == 0
    redesign = row["redesign"]
    assert redesign["class"] == KEY_SCOPED and redesign["class_scores"][KV] > 0
    assert redesign["assumed_keys"] == [
        {
            "column": "orders.email",
            "table_id": "shop.orders",
            "via": VIA_EQUALITY,
            "cardinality": "unknown",
        }
    ]
    assert rows["whole"]["redesign"] is None and rows["keyed"]["redesign"] is None
    ctx = _ctx()
    assert [f.as_record() for f in anti_pattern_findings(_outputs(), collector)] == [
        f.as_record() for f in ctx.anti_pattern_findings
    ]
    (requirement,) = redesign_requirements(record, KV)
    assert requirement == {
        "query_id": "redesigned",
        "routing_depends": True,
        "assumed_keys": [{"column": "orders.email", "via": VIA_EQUALITY, "cardinality": "unknown"}],
        "class_as_written": "ad_hoc_filter",
        "class_after_redesign": KEY_SCOPED,
        "lifted_anti_patterns": [SCAN],
        "evidence": [requirement["evidence"][0]],
    }
    # Another engine's designer gets the redesign but not KV's lifted finding, and an
    # engine whose written score was at least as good does not depend on it.
    (other,) = redesign_requirements(record, "documentdb")
    assert other["lifted_anti_patterns"] == [] and other["assumed_keys"]
    (aurora,) = redesign_requirements(record, "aurora_postgresql")
    assert aurora["routing_depends"] is False


def test_a_version_1_record_holds_no_requirement():
    """A record written before #1231 has no ``redesign`` and no findings: it still loads
    and gives no designer a requirement."""
    old = {
        "schema_version": 1,
        "queries": [{"query_id": "q", "class": "key_lookup", "reason": "", "facts": []}],
    }
    assert redesign_requirements(old, KV) == []
    assert redesign_requirements(None, KV) == []


def test_only_the_redesign_module_decides_a_redesign():
    """One definition: only the pattern model builds the redesign it scores, the scorer
    reuses that model to label findings, and only ``redesign_findings`` reads which
    reads a redesign removes."""
    src = Path(__file__).resolve().parents[4] / "src"
    builders, readers = set(), set()
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(src).as_posix()
        if "RedesignModel.from_collector(" in text or "RedesignModel(" in text:
            builders.add(rel)
        if "removed_by_redesign()" in text:
            readers.add(rel)
    assert builders == {"agents/referee/query_pattern.py", "agents/referee/query_scoring.py"}
    assert readers - {"shared/catalog/patterns.py"} == {"agents/referee/redesign_findings.py"}
