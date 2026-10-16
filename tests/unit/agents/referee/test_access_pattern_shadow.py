"""The per-query access-pattern score, in shadow (ADR-031 Decision 14, Amendment 5, #728).

The shadow model scores a query's pattern dimension from its own access-pattern
class (``src/catalog/access_classes.toml``), never from its tables' patterns, and
keeps the other three dimensions, the gates and the penalty exactly as they are.
Routing does not read it: these tests pin the class scores, the per-query rule,
the #784 and #885 acceptance criteria, and that computing and writing the shadow
never changes an assignment.
"""

from __future__ import annotations

import copy
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.agents.referee.access_pattern import (
    AD_HOC_FILTER,
    AGGREGATION,
    KEY_LOOKUP,
    KEY_RANGE,
    KEY_SCOPED,
    TEXT_SEARCH,
    UNANCHORED_JOIN,
)
from src.agents.referee.access_pattern_shadow import (
    SHADOW_FILE,
    build_pattern_model,
    build_shadow,
    weights_from_analysis,
)
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.query_scoring import (
    QueryPatternModel,
    ScoringContext,
    compute_query_confidence,
    score_query_for_engine,
)
from src.shared.catalog.access_classes import access_classes, class_engine_scores
from src.tools.analysis.scoring import DEFAULT_WEIGHTS

DDB_WEIGHTS = {"pattern_match": 0.6, "complexity": 0.2, "performance": 0.05, "cost": 0.15}
AURORA_WEIGHTS = {"pattern_match": 0.4, "complexity": 0.3, "performance": 0.2, "cost": 0.1}


class TestClassScoresComeFromThePatternCatalog:
    def test_the_class_by_engine_matrix(self):
        """Each class's score on an engine is its patterns' strongest catalog fit, else 0.

        Pinned so a catalog change that moves a class score shows up here.
        """
        assert class_engine_scores() == {
            KEY_LOOKUP: {
                "aurora_mysql": 60,
                "aurora_postgresql": 60,
                "documentdb": 60,
                "dynamodb": 100,
                "elasticache": 0,
                "opensearch": 0,
            },
            KEY_SCOPED: {
                "aurora_mysql": 70,
                "aurora_postgresql": 70,
                "documentdb": 85,
                "dynamodb": 75,
                "elasticache": 0,
                "opensearch": 0,
            },
            KEY_RANGE: {
                "aurora_mysql": 85,
                "aurora_postgresql": 85,
                "documentdb": 85,
                "dynamodb": 0,
                "elasticache": 0,
                "opensearch": 0,
            },
            AD_HOC_FILTER: {
                "aurora_mysql": 75,
                "aurora_postgresql": 75,
                "documentdb": 75,
                "dynamodb": 0,
                "elasticache": 0,
                "opensearch": 75,
            },
            UNANCHORED_JOIN: {
                "aurora_mysql": 90,
                "aurora_postgresql": 90,
                "documentdb": 0,
                "dynamodb": 0,
                "elasticache": 0,
                "opensearch": 0,
            },
            AGGREGATION: {
                "aurora_mysql": 85,
                "aurora_postgresql": 90,
                "documentdb": 75,
                "dynamodb": 0,
                "elasticache": 0,
                "opensearch": 0,
            },
            TEXT_SEARCH: {
                "aurora_mysql": 80,
                "aurora_postgresql": 75,
                "documentdb": 75,
                "dynamodb": 0,
                "elasticache": 0,
                "opensearch": 95,
            },
        }

    def test_time_aggregation_counts_only_for_a_time_bucket(self):
        """#920: ``time-aggregation`` (90) rates date-bucket aggregations, not every one."""
        with_bucket = class_engine_scores(frozenset({"time_bucket"}))
        assert with_bucket[AGGREGATION] == {
            **class_engine_scores()[AGGREGATION],
            "opensearch": 90,
        }
        assert with_bucket[KEY_RANGE] == class_engine_scores()[KEY_RANGE]

    def test_a_timestamp_range_pattern_is_not_in_the_range_class(self):
        """#920: a timestamp column is never a key, so the range class never holds one."""
        classes = {c.id: c for c in access_classes()}
        assert "time-range-query" not in classes[KEY_RANGE].patterns

    def test_a_conditional_pattern_names_a_listed_pattern_and_a_known_fact(self):
        from src.shared.catalog.access_classes import QUERY_FACTS

        for access_class in access_classes():
            for pattern_id, fact in access_class.only_when.items():
                assert pattern_id in access_class.patterns
                assert fact in QUERY_FACTS

    def test_a_native_entry_counts_and_every_listed_one_is_rated(self):
        """DocumentDB's ``$text`` is ``native`` in full-text-search: it counts at its
        ``class_score``, and no listed pattern leaves a native entry unrated."""
        from src.shared.catalog.access_classes import unrated_native_entries
        from src.shared.catalog.patterns import pattern

        support = pattern("full-text-search").engines["documentdb"]
        assert support.kind == "native" and support.class_score == 75
        assert class_engine_scores()[TEXT_SEARCH]["documentdb"] == 75
        assert unrated_native_entries() == []

    def test_an_unanchored_range_is_a_scan_on_dynamodb_not_a_range_query(self):
        """``range-query`` needs a partition value; with none the read is the
        frequent-full-scan anti-pattern, which counts nothing."""
        classes = {c.id: c for c in access_classes()}
        assert "range-query" not in classes[KEY_RANGE].patterns
        assert "frequent-full-scan" in classes[KEY_RANGE].patterns
        assert class_engine_scores()[KEY_RANGE]["dynamodb"] == 0

    def test_every_class_score_comes_from_a_pattern_file_entry_with_its_source(self):
        """No number lives in the class file or the scorer: every non-zero cell is a
        fit ``score`` or a ``class_score`` whose ``why`` names where the number comes from."""
        from src.shared.catalog.patterns import pattern

        for access_class in access_classes():
            for engine, score in class_engine_scores()[access_class.id].items():
                if not score:
                    continue
                sources = [
                    pattern(p).engines[engine]
                    for p in access_class.patterns
                    if engine in pattern(p).engines
                ]
                match = [
                    e
                    for e in sources
                    if (e.kind == "fit" and e.score == score) or e.class_score == score
                ]
                assert match, (access_class.id, engine)
                for entry in match:
                    if entry.kind != "fit":
                        assert "Class score (#728)" in entry.why, (access_class.id, engine)
                        _assert_the_cited_source_exists(entry, access_class.id, engine)

    def test_the_class_scorer_names_no_engine(self):
        import ast
        from pathlib import Path

        import src.shared.catalog.access_classes as module
        from src.shared.catalog.engines import engine_ids

        tree = ast.parse(Path(module.__file__).read_text())
        literals = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert not literals & set(engine_ids())

    def test_the_catalog_lists_the_seven_classes_in_severity_order(self):
        from src.agents.referee.access_pattern import SCORED_CLASSES

        assert tuple(c.id for c in access_classes()) == SCORED_CLASSES


def _assert_the_cited_source_exists(entry, class_id, engine):
    """A ``class_score`` names its source as ``(<pattern>, <score>)``: that pattern file
    must exist and hold a ``fit`` entry with exactly that score, equal to the class score."""
    import re

    from src.shared.catalog.patterns import pattern

    cited = re.findall(r"\(([a-z0-9-]+), (\d+)\)", entry.why)
    assert cited, (class_id, engine, "no (pattern, score) source in why")
    for pattern_id, score in cited:
        source = pattern(pattern_id)  # KeyError names a pattern with no file
        fits = {s.score for s in source.engines.values() if s.kind == "fit"}
        assert int(score) in fits, (class_id, engine, pattern_id, score, fits)
        assert int(score) == entry.class_score, (class_id, engine, pattern_id, score)


def test_the_source_check_rejects_a_score_the_catalog_does_not_have():
    from src.shared.catalog.patterns import EngineSupport

    made_up = EngineSupport(
        kind="unscored",
        supported=True,
        why="Class score (#728): 70, as Aurora's pagination (pagination, 70).",
        class_score=70,
    )
    with pytest.raises(AssertionError):
        _assert_the_cited_source_exists(made_up, "key_range", "documentdb")


def _rec(table_id, pattern, complexity=80, performance=60, cost=70, weights=AURORA_WEIGHTS):
    from src.contracts.analysis_output import ScoreBreakdown
    from src.tools.analysis.scoring import compute_confidence

    breakdown = {
        "pattern_match_score": pattern,
        "complexity_score": complexity,
        "performance_score": performance,
        "cost_score": cost,
    }
    return {
        "table_id": table_id,
        "confidence_score": compute_confidence(ScoreBreakdown(**breakdown), weights),
        "score_breakdown": breakdown,
    }


def _analysis(*recs, patterns=()):
    return {
        "table_recommendations": list(recs),
        "workload_analysis": {"patterns_detected": list(patterns), "anti_patterns_detected": []},
    }


JOIN = {
    "query_id": "join",
    "query_text": "SELECT t.*, c.name FROM topics t JOIN categories c ON c.id = t.category_id "
    "WHERE t.id = $1",
    "query_type": "SELECT",
    "tables_accessed": ["d.topics", "d.categories"],
}


def _model(engine, cls, weights):
    return QueryPatternModel(
        pattern_scores={"join": {engine: class_engine_scores()[cls][engine]}},
        weights={(engine, t): weights for t in ("d.topics", "d.categories", "d.users")},
    )


class TestTheQueryScoresItsOwnPattern:
    def test_without_a_model_the_score_is_todays(self):
        analysis = _analysis(_rec("d.topics", 70), _rec("d.categories", 100))
        today = compute_query_confidence("join", JOIN, "dynamodb", analysis)
        assert today == compute_query_confidence(
            "join", JOIN, "dynamodb", analysis, pattern_model=None
        )

    def test_the_pattern_dimension_is_the_querys_class_score(self):
        analysis = _analysis(
            _rec("d.topics", 70, weights=DDB_WEIGHTS),
            _rec("d.categories", 100, weights=DDB_WEIGHTS),
        )
        model = _model("dynamodb", KEY_SCOPED, DDB_WEIGHTS)
        # pattern 75 (key-scoped on DynamoDB), complexity 80, performance 60, cost 70.
        expected = int(75 * 0.6 + 80 * 0.2 + 60 * 0.05 + 70 * 0.15)
        assert (
            compute_query_confidence("join", JOIN, "dynamodb", analysis, pattern_model=model)
            == expected
        )

    def test_885_a_join_does_not_inherit_its_tables_strongest_pattern(self):
        """``topics`` + ``categories`` never takes ``categories``' key-lookup 100 (#885)."""
        model = _model("dynamodb", KEY_SCOPED, DDB_WEIGHTS)
        lookup_on_categories = _analysis(
            _rec("d.topics", 70, weights=DDB_WEIGHTS),
            _rec("d.categories", 100, weights=DDB_WEIGHTS),
        )
        no_lookup = _analysis(
            _rec("d.topics", 70, weights=DDB_WEIGHTS), _rec("d.categories", 60, weights=DDB_WEIGHTS)
        )
        assert compute_query_confidence(
            "join", JOIN, "dynamodb", lookup_on_categories, pattern_model=model
        ) == compute_query_confidence("join", JOIN, "dynamodb", no_lookup, pattern_model=model)

    def test_784_more_relational_evidence_on_a_table_never_lowers_the_score(self):
        """The ``web_crawler_requests`` repro (#784): a weaker extra pattern on the table
        pulled Aurora's table score 79 -> 78. The query's own class decides now."""
        query = {
            "query_id": "join",
            "query_text": "SELECT * FROM topics WHERE id = $1",
            "query_type": "SELECT",
            "tables_accessed": ["d.topics"],
        }
        model = _model("aurora_postgresql", KEY_LOOKUP, AURORA_WEIGHTS)
        before = _analysis(_rec("d.topics", 79))
        after = _analysis(
            _rec("d.topics", 78),
            patterns=[
                {"pattern_type": "standard-crud", "query_ids": ["other"], "table_ids": ["d.topics"]}
            ],
        )
        assert compute_query_confidence(
            "join", query, "aurora_postgresql", after, pattern_model=model
        ) >= compute_query_confidence(
            "join", query, "aurora_postgresql", before, pattern_model=model
        )

    @given(
        st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=6),
        st.integers(min_value=0, max_value=100),
    )
    def test_885_no_pattern_found_on_a_table_moves_the_querys_score(self, others, extra):
        """Whatever patterns the table's other queries match, the score stays the same."""
        model = _model("dynamodb", KEY_SCOPED, DDB_WEIGHTS)
        reference = compute_query_confidence(
            "join",
            JOIN,
            "dynamodb",
            _analysis(
                _rec("d.topics", 50, weights=DDB_WEIGHTS),
                _rec("d.categories", 50, weights=DDB_WEIGHTS),
            ),
            pattern_model=model,
        )
        for score in [*others, extra]:
            analysis = _analysis(
                _rec("d.topics", score, weights=DDB_WEIGHTS),
                _rec("d.categories", 100 - score, weights=DDB_WEIGHTS),
            )
            assert (
                compute_query_confidence("join", JOIN, "dynamodb", analysis, pattern_model=model)
                == reference
            )

    def test_885_another_tables_traffic_does_not_move_the_score(self):
        model = _model("dynamodb", KEY_SCOPED, DDB_WEIGHTS)
        quiet = _analysis(
            _rec("d.topics", 70, weights=DDB_WEIGHTS),
            _rec("d.categories", 90, weights=DDB_WEIGHTS),
            _rec("d.users", 40, performance=10, weights=DDB_WEIGHTS),
        )
        busy = _analysis(
            _rec("d.topics", 70, weights=DDB_WEIGHTS),
            _rec("d.categories", 90, weights=DDB_WEIGHTS),
            _rec("d.users", 95, performance=100, cost=10, weights=DDB_WEIGHTS),
        )
        assert compute_query_confidence(
            "join", JOIN, "dynamodb", quiet, pattern_model=model
        ) == compute_query_confidence("join", JOIN, "dynamodb", busy, pattern_model=model)

    def test_no_all_tables_fallback(self):
        """A query with no rated table of its own scores 0, not the schema's average."""
        model = _model("dynamodb", KEY_LOOKUP, DDB_WEIGHTS)
        query = {**JOIN, "tables_accessed": ["d.unrated"]}
        analysis = _analysis(_rec("d.topics", 90, weights=DDB_WEIGHTS))
        assert (
            compute_query_confidence("join", query, "dynamodb", analysis, pattern_model=model) == 0
        )

    def test_a_pattern_only_engine_with_no_class_score_scores_zero(self):
        model = _model("opensearch", KEY_SCOPED, DEFAULT_WEIGHTS)
        analysis = _analysis(_rec("d.topics", 90), _rec("d.categories", 90))
        assert (
            compute_query_confidence("join", JOIN, "opensearch", analysis, pattern_model=model) == 0
        )

    def test_a_query_outside_the_model_keeps_todays_rule(self):
        model = QueryPatternModel(pattern_scores={}, weights={})
        analysis = _analysis(_rec("d.topics", 70), _rec("d.categories", 100))
        assert compute_query_confidence(
            "join", JOIN, "dynamodb", analysis, pattern_model=model
        ) == compute_query_confidence("join", JOIN, "dynamodb", analysis)

    def test_the_control_keeps_each_tables_own_pattern_score(self):
        """The attribution control changes only which tables a query is scored on."""
        analysis = _analysis(
            _rec("d.topics", 70, weights=DDB_WEIGHTS),
            _rec("d.categories", 100, weights=DDB_WEIGHTS),
        )
        control = QueryPatternModel(
            pattern_scores={"join": {}},
            weights={("dynamodb", t): DDB_WEIGHTS for t in ("d.topics", "d.categories")},
            identity=True,
        )
        assert compute_query_confidence(
            "join", JOIN, "dynamodb", analysis, pattern_model=control
        ) == compute_query_confidence("join", JOIN, "dynamodb", analysis)

    def test_the_penalty_and_the_gates_still_apply(self):
        analysis = _analysis(_rec("d.topics", 70), _rec("d.categories", 100))
        model = _model("aurora_postgresql", KEY_SCOPED, AURORA_WEIGHTS)
        ctx = ScoringContext(
            analysis_outputs={"aurora_postgresql": analysis},
            anti_pattern_map={("join", "aurora_postgresql"): 15},
            pattern_model=model,
        )
        base = compute_query_confidence(
            "join", JOIN, "aurora_postgresql", analysis, pattern_model=model
        )
        assert score_query_for_engine(JOIN, "aurora_postgresql", ctx).score == base - 15
        gated = ScoringContext(
            analysis_outputs={"aurora_postgresql": analysis},
            query_capabilities={"join": ["full_text_search"]},
            pattern_model=model,
        )
        result = score_query_for_engine(JOIN, "dynamodb", gated)
        assert result.score is None


def _collection():
    def table(name, pk, columns, indexes=()):
        return {
            "table_id": f"shop.{name}",
            "table_name": name,
            "schema_name": "shop",
            "row_count": 1000,
            "size_mb": 1.0,
            "primary_key": pk,
            "columns": [{"column_name": c, "data_type": "int"} for c in columns],
            "indexes": [
                {"index_name": f"ix_{c}", "columns": [c], "is_unique": False} for c in indexes
            ],
            "foreign_keys": None,
        }

    def query(qid, text, tables, qtype="SELECT"):
        return {
            "query_id": qid,
            "query_text": text,
            "query_type": qtype,
            "tables_accessed": [f"shop.{t}" for t in tables],
            "calls_per_second": 5.0,
            "has_joins": len(tables) > 1,
        }

    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {
            "tables": [
                table("orders", ["id"], ["id", "customer_id", "status"], indexes=["customer_id"]),
                table("items", ["id"], ["id", "order_id", "qty"], indexes=["order_id"]),
            ]
        },
        "queries": {
            "query_patterns": [
                query("q1", "SELECT * FROM orders WHERE id = $1", ["orders"]),
                query(
                    "q2",
                    "SELECT * FROM orders o JOIN items i ON i.order_id = o.id WHERE o.id = $1",
                    ["orders", "items"],
                ),
                query("q3", "SELECT status, count(*) FROM orders GROUP BY status", ["orders"]),
                query("q4", "UPDATE orders SET status = $1 WHERE id = $2", ["orders"], "UPDATE"),
            ]
        },
    }


def _engine_analysis(pattern, weights):
    return _analysis(
        _rec("shop.orders", pattern, weights=weights), _rec("shop.items", pattern, weights=weights)
    )


@pytest.fixture
def pipeline():
    collector = _collection()
    triage = {
        "selected_agents": [{"agent_type": "aurora_postgresql"}, {"agent_type": "dynamodb"}],
        "query_capabilities": {},
        "signals": [],
    }
    analysis = {
        "aurora_postgresql": _engine_analysis(80, AURORA_WEIGHTS),
        "dynamodb": _engine_analysis(80, DDB_WEIGHTS),
    }
    return triage, analysis, collector


class TestTheShadowNeverChangesRouting:
    def test_resolve_without_a_model_is_unchanged(self, pipeline):
        triage, analysis, collector = pipeline
        default = AssignmentResolver().resolve(triage, analysis, collector)
        explicit = AssignmentResolver().resolve(triage, analysis, collector, pattern_model=None)
        assert default.model_dump(exclude={"timestamp"}) == explicit.model_dump(
            exclude={"timestamp"}
        )

    def test_building_the_shadow_does_not_touch_its_inputs(self, pipeline):
        triage, analysis, collector = pipeline
        before = copy.deepcopy((triage, analysis, collector))
        build_shadow(triage, analysis, collector, weights_from_analysis(analysis))
        assert (triage, analysis, collector) == before

    def test_the_handler_writes_the_shadow_next_to_an_identical_assignment(
        self, pipeline, tmp_path, monkeypatch
    ):
        from src.agents.referee import assignment_handler
        from src.storage.local_store import LocalArtifactStore

        triage, analysis, collector = pipeline

        def seed(root):
            store = LocalArtifactStore(str(root))
            store.write_json("shop/j/referee-triage/triage.json", triage)
            store.write_json("shop/j/collector/output.json", collector)
            for engine in analysis:
                store.write_json(f"shop/j/analysis-{engine}/analysis.json", analysis[engine])
            return store

        with_shadow = seed(tmp_path / "a")
        assignment_handler.run_assignment_resolver("j", "shop", with_shadow)
        monkeypatch.setattr(assignment_handler, "write_access_pattern_shadow", lambda *a, **k: None)
        without = seed(tmp_path / "b")
        assignment_handler.run_assignment_resolver("j", "shop", without)

        def assignment(store):
            data = store.read_json("shop/j/assignment/v1/assignment.json")
            data.pop("timestamp", None)
            return data

        assert assignment(with_shadow) == assignment(without)
        assert with_shadow.exists(f"shop/j/assignment/v1/{SHADOW_FILE}")
        assert not without.exists(f"shop/j/assignment/v1/{SHADOW_FILE}")


class TestTheShadowArtifact:
    def test_every_query_has_its_class_and_both_results(self, pipeline):
        triage, analysis, collector = pipeline
        shadow = build_shadow(triage, analysis, collector, weights_from_analysis(analysis))
        rows = {r["query_id"]: r for r in shadow["queries"]}
        assert {q: r["class"] for q, r in rows.items()} == {
            "q1": KEY_LOOKUP,
            "q2": KEY_SCOPED,
            "q3": AGGREGATION,
            "q4": KEY_LOOKUP,
        }
        for row in rows.values():
            assert set(row["current"]) == {"engine", "scores"}
            assert set(row["shadow"]) >= {"engine", "scores", "rule"}
            assert row["reason"]
        json.dumps(shadow)  # serialisable as written

    def test_a_key_lookup_scores_its_class_on_each_engine(self, pipeline):
        triage, analysis, collector = pipeline
        shadow = build_shadow(triage, analysis, collector, weights_from_analysis(analysis))
        q1 = next(r for r in shadow["queries"] if r["query_id"] == "q1")
        # complexity 80, performance 60, cost 70 on both tables.
        assert q1["shadow"]["scores"]["dynamodb"] == int(
            100 * 0.6 + 80 * 0.2 + 60 * 0.05 + 70 * 0.15
        )
        assert q1["shadow"]["scores"]["aurora_postgresql"] == int(
            60 * 0.4 + 80 * 0.3 + 60 * 0.2 + 70 * 0.1
        )
        assert q1["current"]["scores"]["dynamodb"] == int(
            80 * 0.6 + 80 * 0.2 + 60 * 0.05 + 70 * 0.15
        )

    def test_the_summary_counts_classes_and_engine_changes(self, pipeline):
        triage, analysis, collector = pipeline
        shadow = build_shadow(triage, analysis, collector, weights_from_analysis(analysis))
        summary = shadow["summary"]
        assert summary["queries"] == 4
        assert summary["by_class"] == {KEY_LOOKUP: 2, KEY_SCOPED: 1, AGGREGATION: 1}
        changed = [r for r in shadow["queries"] if r["current"]["engine"] != r["shadow"]["engine"]]
        assert summary["engine_changes"] == len(changed)
        assert sum(summary["moves"].values()) == len(changed)
        by_class = sum(n for pairs in summary["moves_by_class"].values() for n in pairs.values())
        assert by_class + sum(summary["rule_moves"].values()) == len(changed)
        for row in shadow["queries"]:
            assert row["shadow"]["control_engine"]
        assert shadow["class_scores"] == class_engine_scores()

    def test_a_time_bucket_aggregation_gets_its_conditional_score(self, pipeline):
        _, analysis, collector = pipeline
        collector["queries"]["query_patterns"].append(
            {
                "query_id": "tb",
                "query_text": "SELECT date_trunc('day', status), count(*) FROM orders GROUP BY 1",
                "query_type": "SELECT",
                "tables_accessed": ["shop.orders"],
            }
        )
        model, classes = build_pattern_model(collector, weights_from_analysis(analysis))
        assert classes["tb"].name == AGGREGATION and "time_bucket" in classes["tb"].facts
        assert model.pattern_scores["tb"]["opensearch"] == 90
        assert model.pattern_scores["q3"]["opensearch"] == 0

    def test_the_model_covers_only_scored_classes(self, pipeline):
        triage, analysis, collector = pipeline
        collector["queries"]["query_patterns"].append(
            {"query_id": "u", "query_text": "SET search_path TO public", "query_type": "OTHER"}
        )
        model, classes = build_pattern_model(collector, weights_from_analysis(analysis))
        assert classes["u"].name == "utility"
        assert "u" not in model.pattern_scores
        assert model.pattern_scores["q1"]["dynamodb"] == 100

    def test_weights_are_the_ones_each_engines_analysis_used(self, pipeline):
        """Read from ``analysis.json`` alone: the referee never reads the trace (ADR-017)."""
        _, analysis, _ = pipeline
        weights = weights_from_analysis(analysis)
        assert weights[("dynamodb", "shop.orders")] == DDB_WEIGHTS
        assert weights[("aurora_postgresql", "shop.items")] == AURORA_WEIGHTS

    def test_an_opensearch_table_gets_the_profile_that_gave_its_confidence(self):
        from src.tools.analysis.opensearch_pattern_catalog import (
            OPENSEARCH_SEARCH_WEIGHTS,
            OPENSEARCH_TIMESERIES_WEIGHTS,
        )

        search = _rec("d.a", 90, 70, 20, 80, weights=OPENSEARCH_SEARCH_WEIGHTS)
        timeseries = _rec("d.b", 90, 70, 20, 80, weights=OPENSEARCH_TIMESERIES_WEIGHTS)
        weights = weights_from_analysis({"opensearch": _analysis(search, timeseries)})
        assert weights[("opensearch", "d.a")] == OPENSEARCH_SEARCH_WEIGHTS
        assert weights[("opensearch", "d.b")] == OPENSEARCH_TIMESERIES_WEIGHTS


# Positional arguments each model-taking callable takes before ``pattern_model``
# (keyword-only), so a positional model is caught even if the signature changes back.
_MODEL_TAKERS = {"compute_query_confidence": 4, "resolve": 3, "_query_pattern_confidence": 5}


def _model_passes(tree):
    """``(lineno, why)`` for every call that hands a model on outside a pass-through.

    A pass-through is ``pattern_model=pattern_model`` inside a function that itself takes
    ``pattern_model``, or ``pattern_model=<ctx>.pattern_model`` (reading a scoring context).
    """
    import ast

    found = []

    def visit(node, params):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "QueryPatternModel":
                found.append((node.lineno, "builds a QueryPatternModel"))
            if name in _MODEL_TAKERS and len(node.args) > _MODEL_TAKERS[name]:
                found.append((node.lineno, f"passes a positional model to {name}"))
            for k in node.keywords:
                if k.arg != "pattern_model":
                    continue
                through = (
                    isinstance(k.value, ast.Name)
                    and k.value.id == "pattern_model"
                    and "pattern_model" in params
                ) or (isinstance(k.value, ast.Attribute) and k.value.attr == "pattern_model")
                if not through:
                    found.append((node.lineno, f"passes pattern_model= to {name}"))
        for child in ast.iter_child_nodes(node):
            visit(child, params)

    visit(tree, set())
    return found


def test_only_the_shadow_builds_or_passes_a_pattern_model():
    """ADR-031 Decision 14 Amendment 5: no production caller passes a model to routing,
    by keyword or by position, in ``src/`` or ``scripts/``."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[4]
    allowed = {"src/agents/referee/access_pattern_shadow.py"}
    found = {
        f"{path.relative_to(repo).as_posix()}:{line} {why}"
        for root in ("src", "scripts")
        for path in (repo / root).rglob("*.py")
        if path.relative_to(repo).as_posix() not in allowed
        for line, why in _model_passes(ast.parse(path.read_text()))
    }
    assert not found, sorted(found)


def test_the_model_guard_catches_every_way_of_passing_one():
    import ast

    snippet = """
def route(collector):
    resolver.resolve(t, a, collector, pattern_model=build())
    compute_query_confidence(q, query, e, analysis, model)
    QueryPatternModel(pattern_scores={})
    pattern_model = make()
    resolver.resolve(t, a, collector, pattern_model=pattern_model)

def through(ctx, pattern_model=None):
    compute_query_confidence(q, query, e, a, pattern_model=pattern_model)
    compute_query_confidence(q, query, e, a, pattern_model=ctx.pattern_model)
"""
    assert [line for line, _ in _model_passes(ast.parse(snippet))] == [3, 4, 5, 7]

    import src.agents.referee.query_scoring as scoring

    assert "pattern_model" in scoring.compute_query_confidence.__kwdefaults__
