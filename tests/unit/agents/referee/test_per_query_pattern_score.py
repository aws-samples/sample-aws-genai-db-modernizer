"""The per-query access-pattern score is the routing path (ADR-031 Decision 14, Amendment 5, #728).

A query's pattern dimension on an engine is its own access-pattern class's score there
(``src/catalog/access_classes.toml``, read through
:class:`src.agents.referee.query_pattern.QueryPatternModel`), never its tables'
pattern scores; the other three dimensions, the gates and the penalty are unchanged.
These tests pin the class scores, the scorer's rule, that the routing context builds
the model from the collection, and the guard that keeps every production score on it.
The #784, #885 and criterion 4b acceptance tests are in
``test_per_query_pattern_acceptance.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.agents.referee.access_pattern import (
    AD_HOC_FILTER,
    AGGREGATION,
    KEY_LOOKUP,
    KEY_RANGE,
    KEY_SCOPED,
    REFERENCE_READ,
    SCORED_CLASSES,
    TEXT_SEARCH,
    UNANCHORED_JOIN,
    UNPARSED,
    UTILITY,
)
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.query_pattern import QueryPatternModel, weights_from_analysis
from src.agents.referee.query_scoring import (
    ScoringContext,
    compute_query_confidence,
    score_query_for_engine,
)
from src.agents.referee.routing_trace import RoutingTrace
from src.shared.catalog.access_classes import (
    access_classes,
    class_engine_scores,
    class_score,
    feature_patterns,
)
from src.tools.analysis.scoring import DEFAULT_WEIGHTS

REPO = Path(__file__).resolve().parents[4]
# DynamoDB's weights since #1002 (cost at the common 10%; ADR-031 Decision 5 Amendment 8).
DDB_WEIGHTS = {"pattern_match": 0.64, "complexity": 0.21, "performance": 0.05, "cost": 0.10}
AURORA_WEIGHTS = {"pattern_match": 0.4, "complexity": 0.3, "performance": 0.2, "cost": 0.1}


class TestClassScoresComeFromThePatternCatalog:
    def test_the_class_by_engine_matrix(self):
        """Each class's score on an engine is its patterns' strongest catalog rating, else 0.

        Pinned so a catalog change that moves a class score shows up here.
        """
        assert class_engine_scores() == {
            # #920: Aurora's own key-value-lookup fit (80, a B-tree point read); #983
            # review: DocumentDB's parity anchor follows Aurora's own 80 (was 60).
            KEY_LOOKUP: {
                "aurora_mysql": 80,
                "aurora_postgresql": 80,
                "documentdb": 80,
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
            # #920 proposed default: DynamoDB's metadata-config-store (90) rates
            # reference data; the others keep the range class's pagination (85).
            REFERENCE_READ: {
                "aurora_mysql": 85,
                "aurora_postgresql": 85,
                "documentdb": 85,
                "dynamodb": 90,
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
            # #920 (catalog PR C): window-function counts only for a query with a
            # window, so with none both Aurora engines are rated by their aggregation fits.
            AGGREGATION: {
                "aurora_mysql": 85,
                "aurora_postgresql": 85,
                "documentdb": 75,
                "dynamodb": 0,
                "elasticache": 0,
                "opensearch": 0,
            },
            # #920 (catalog PR C): each text pattern counts only for its own kind of
            # text search, and a text-search query always shows one; with none, nothing
            # rates it. The per-kind scores: tests/unit/agents/referee/test_text_search_class.py.
            TEXT_SEARCH: {
                "aurora_mysql": 0,
                "aurora_postgresql": 0,
                "documentdb": 0,
                "dynamodb": 0,
                "elasticache": 0,
                "opensearch": 0,
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

    def test_range_query_counts_for_a_key_scoped_read_only_with_a_partition_range(self):
        """#933's shape: DynamoDB's Query (range-query, 90) rates a key-scoped read that
        pins a partition key and ranges another column; any other key-scoped read keeps
        its item-collection rating (bounded-parent-child, 75)."""
        plain = class_engine_scores()[KEY_SCOPED]
        ranged = class_engine_scores(frozenset({"partition_range"}))[KEY_SCOPED]
        assert plain["dynamodb"] == 75
        assert ranged == {**plain, "dynamodb": 90}
        for access_class in SCORED_CLASSES:
            if access_class != KEY_SCOPED:
                assert (
                    class_engine_scores(frozenset({"partition_range"}))[access_class]
                    == class_engine_scores()[access_class]
                )

    def test_batch_in_list_rates_the_key_lookup_class_and_raises_no_score(self):
        """#920: ``batch-in-list`` (Aurora 70) describes the ``WHERE id IN (...)`` lookups
        the class includes; every engine it rates rates key-value-lookup at least as high,
        so no class score changes, and its score cites its sources."""
        from src.shared.catalog.patterns import pattern

        key_lookup = next(c for c in access_classes() if c.id == KEY_LOOKUP)
        assert "batch-in-list" in key_lookup.patterns
        without = key_lookup.model_copy(
            update={"patterns": tuple(p for p in key_lookup.patterns if p != "batch-in-list")}
        )
        for engine, score in class_engine_scores()[KEY_LOOKUP].items():
            assert class_score(without, engine) == score, engine
        for engine in ("aurora_postgresql", "aurora_mysql"):
            entry = pattern("batch-in-list").engines[engine]
            assert entry.kind == "fit" and entry.score == 70
            assert "PostgreSQL documentation" in entry.why and "MySQL Reference Manual" in entry.why

    @pytest.mark.parametrize(
        "fact",
        ["recursive_cte", "lateral_join", "stored_routine", "upsert", "full_text", "json_access"],
    )
    def test_a_sql_feature_carries_its_documented_credit_in_every_class(self, fact):
        """#985, #1123: a query showing a SQL feature is rated, in whatever class it falls
        in, at least at each engine's own catalog rating for that feature (recursive CTEs:
        Aurora PostgreSQL 95, Aurora MySQL 90) wherever its class gives the engine a score
        above 0, and at 0 wherever the class gives it 0: a feature raises a class score,
        never lifts a 0. No engine without a rating for the feature gains anything."""
        from src.shared.catalog.patterns import pattern

        ratings: dict[str, int] = {}
        for feature in feature_patterns():
            if feature.fact != fact:
                continue
            for engine, entry in pattern(feature.pattern).engines.items():
                if entry.kind == "fit" and entry.score is not None:
                    ratings[engine] = max(ratings.get(engine, 0), entry.score)
        assert ratings, fact
        facts = frozenset({fact})
        shown = class_engine_scores(facts)
        for access_class in access_classes():
            for engine, score in shown[access_class.id].items():
                own = class_score(access_class, engine, facts, with_features=False)
                expected = max(own, ratings.get(engine, 0)) if own > 0 else 0
                assert score == expected, (access_class.id, engine, own)

    def test_a_feature_never_lifts_a_class_score_of_0(self):
        """#1123: in every class, for every feature and every other fact a query can show
        with it, an engine its class scores 0 stays at 0 (a JSON read does not make
        DocumentDB serve an unanchored join, an upsert does not give Aurora MySQL a
        similarity search), and an engine its class scores above 0 never goes down."""
        from src.shared.catalog.access_classes import QUERY_FACTS

        engines = class_engine_scores()[KEY_LOOKUP]
        lifted_somewhere = False
        for feature in feature_patterns():
            for other in sorted(QUERY_FACTS | {""}):
                facts = frozenset({feature.fact, other} - {""})
                for access_class in access_classes():
                    for engine in engines:
                        own = class_score(access_class, engine, facts, with_features=False)
                        score = class_score(access_class, engine, facts)
                        if own == 0:
                            assert score == 0, (feature.pattern, other, access_class.id, engine)
                        else:
                            assert score >= own, (feature.pattern, other, access_class.id, engine)
                            lifted_somewhere |= score > own
        # The rule bites where a feature fit is above the class's own non-zero score.
        assert lifted_somewhere

    @pytest.mark.parametrize(
        ("class_id", "facts", "engine", "own"),
        [
            # #1118 review: a JSON read on an unanchored join DocumentDB cannot serve.
            (UNANCHORED_JOIN, {"json_access"}, "documentdb", 0),
            # A similarity search Aurora MySQL has no index or function for.
            (TEXT_SEARCH, {"similarity_match", "upsert"}, "aurora_mysql", 0),
            (TEXT_SEARCH, {"similarity_match", "json_access"}, "documentdb", 0),
            # A prefix regex only DocumentDB rates in the class.
            (TEXT_SEARCH, {"prefix_regex", "recursive_cte"}, "aurora_postgresql", 0),
            (TEXT_SEARCH, {"prefix_regex", "lateral_join"}, "aurora_mysql", 0),
            (TEXT_SEARCH, {"prefix_regex", "stored_routine"}, "aurora_postgresql", 0),
        ],
    )
    def test_a_feature_leaves_these_zero_class_scores_at_0(self, class_id, facts, engine, own):
        """#1123: named cases that a feature fit lifted before the cap (DocumentDB's
        unanchored join with a JSON read went from 0 to 80)."""
        access_class = next(c for c in access_classes() if c.id == class_id)
        assert class_score(access_class, engine, frozenset(facts), with_features=False) == own
        assert class_score(access_class, engine, frozenset(facts)) == 0
        assert class_engine_scores(frozenset(facts))[class_id][engine] == 0

    def test_a_scan_level_class_score_is_not_capped_further(self):
        """#1123 caps a feature only at a class score of 0: where the class rates the
        engine as a scan (45, a leading-wildcard LIKE on Aurora MySQL and DocumentDB), a
        JSON read still raises it to the engine's JSON fit."""
        text_search = next(c for c in access_classes() if c.id == TEXT_SEARCH)
        facts = frozenset({"leading_wildcard", "json_access"})
        for engine, fit in (("aurora_mysql", 85), ("documentdb", 80)):
            assert class_score(text_search, engine, facts, with_features=False) == 45
            assert class_score(text_search, engine, facts) == fit

    def test_parity_anchors_read_the_same_capped_ratings(self):
        """#1123: the parity-anchor notes come from the same capped ratings as the score.
        A feature on a class that scores the engine 0 adds no note (DocumentDB's
        unanchored join with a JSON read); where a feature fit with its own evidence
        rates the engine at least as high as the borrowed class score, the note goes
        (DocumentDB's key lookup, 80 against JSON 80; its ad hoc filter, 75 against 80),
        and an engine with no rating for the feature keeps its note (OpenSearch)."""
        from src.shared.catalog.access_classes import class_parity_anchors

        json_read = frozenset({"json_access"})
        assert "documentdb" not in class_parity_anchors(UNANCHORED_JOIN, json_read)
        assert class_engine_scores(json_read)[UNANCHORED_JOIN]["documentdb"] == 0
        assert "documentdb" in class_parity_anchors(KEY_LOOKUP)
        assert "documentdb" not in class_parity_anchors(KEY_LOOKUP, json_read)
        plain, shown = class_parity_anchors(AD_HOC_FILTER), class_parity_anchors(
            AD_HOC_FILTER, json_read
        )
        assert {"documentdb", "opensearch"} <= set(plain)
        assert "documentdb" not in shown
        assert shown["opensearch"] == plain["opensearch"]

    def test_the_recursive_cte_edge_is_the_catalogs(self):
        """Aurora PostgreSQL's documented edge (SEARCH/CYCLE, no default recursion cap)
        shows in the class score wherever the class itself rates both engines lower."""
        shown = class_engine_scores(frozenset({"recursive_cte"}))[UNANCHORED_JOIN]
        assert (shown["aurora_postgresql"], shown["aurora_mysql"]) == (95, 90)

    def test_a_small_reference_read_is_reference_data_not_a_scan(self):
        classes = {c.id: c for c in access_classes()}
        assert "metadata-config-store" in classes[REFERENCE_READ].patterns
        assert "frequent-full-scan" not in classes[REFERENCE_READ].patterns
        assert class_engine_scores()[REFERENCE_READ]["dynamodb"] == 90

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
        # #920: 60, $text's own documented limits (was 75, borrowed).
        assert support.kind == "native" and support.class_score == 60
        assert class_engine_scores(frozenset({"full_text"}))[TEXT_SEARCH]["documentdb"] == 60
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
        fit ``score`` or a ``class_score`` whose ``why`` names where the number comes from:
        the engine's public documentation, or, for a parity anchor (#936), the borrowed
        ``(<pattern>, <score>)``."""
        from src.shared.catalog.access_classes import QUERY_FACTS
        from src.shared.catalog.patterns import pattern

        for access_class, engine, score in (
            (c, e, n)
            for facts in (frozenset(), QUERY_FACTS - {"write"})
            for c in access_classes()
            for e, n in class_engine_scores(facts)[c.id].items()
        ):
            if not score:
                continue
            listed = [*access_class.patterns, *(f.pattern for f in feature_patterns())]
            sources = [pattern(p).engines[engine] for p in listed if engine in pattern(p).engines]
            match = [
                e
                for e in sources
                if (e.kind == "fit" and e.score == score) or e.class_score == score
            ]
            assert match, (access_class.id, engine)
            for entry in match:
                if entry.kind != "fit":
                    assert "Class score (#728)" in entry.why, (access_class.id, engine)
                    if entry.parity_anchor:
                        _assert_the_cited_source_exists(entry, access_class.id, engine)
                    else:
                        assert re.search(
                            r"documentation|Reference Manual|Developer Guide", entry.why
                        ), (access_class.id, engine, entry.why)

    def test_the_class_scorer_names_no_engine(self):
        import src.agents.referee.query_pattern as model_module
        import src.shared.catalog.access_classes as module
        from src.shared.catalog.engines import engine_ids

        for source in (module, model_module):
            tree = ast.parse(Path(source.__file__).read_text())
            literals = {
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            assert not literals & set(engine_ids()), source.__name__

    def test_the_catalog_lists_the_eight_classes_in_severity_order(self):
        assert tuple(c.id for c in access_classes()) == SCORED_CLASSES
        assert len(SCORED_CLASSES) == 8


def _assert_the_cited_source_exists(entry, class_id, engine):
    """A parity anchor's ``class_score`` names its source as ``(<pattern>, <score>)``: that
    pattern file must exist and rate some engine at exactly that score from its own
    evidence (a ``fit``, or a ``class_score`` that is not itself an anchor), equal to the
    class score."""
    from src.shared.catalog.patterns import pattern

    cited = re.findall(r"\(([a-z0-9-]+), (\d+)\)", entry.why)
    assert cited, (class_id, engine, "no (pattern, score) source in why")
    for pattern_id, score in cited:
        source = pattern(pattern_id)  # KeyError names a pattern with no file
        rated = {
            s.score if s.kind == "fit" else s.class_score
            for s in source.engines.values()
            if not s.parity_anchor
        }
        assert int(score) in rated, (class_id, engine, pattern_id, score, rated)
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


# ---------------------------------------------------------------------------
# The scorer's rule, through the routing context
# ---------------------------------------------------------------------------


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


def _table(name, pk, columns, indexes=(), row_count=100_000):
    return {
        "table_id": f"d.{name}",
        "table_name": name,
        "schema_name": "d",
        "row_count": row_count,
        "size_mb": 10.0,
        "primary_key": pk,
        "columns": [{"column_name": c, "data_type": "int"} for c in columns],
        "indexes": [{"index_name": f"ix_{c}", "columns": [c], "is_unique": False} for c in indexes],
        "foreign_keys": None,
    }


def _query(qid, text, tables, qtype="SELECT"):
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "tables_accessed": [f"d.{t}" for t in tables],
        "calls_per_second": 1.0,
        "has_joins": len(tables) > 1,
    }


def _collector(*queries):
    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {
            "tables": [
                _table("topics", ["id"], ["id", "category_id", "title"], indexes=["category_id"]),
                _table("categories", ["id"], ["id", "name"]),
                _table("users", ["id"], ["id", "name"]),
                _table("flags", ["id"], ["id", "name"], row_count=40),
            ]
        },
        "queries": {"query_patterns": list(queries)},
    }


JOIN = _query(
    "join",
    "SELECT t.*, c.name FROM topics t JOIN categories c ON c.id = t.category_id WHERE t.id = $1",
    ["topics", "categories"],
)
LOOKUP = _query("lookup", "SELECT * FROM topics WHERE id = $1", ["topics"])


def _score(query, engine, analysis_outputs, collector=None):
    ctx = ScoringContext.from_pipeline(analysis_outputs, None, collector or _collector(query))
    return score_query_for_engine(query, engine, ctx).score


class TestTheQueryScoresItsOwnPattern:
    def test_the_pattern_dimension_is_the_querys_class_score(self):
        analysis = _analysis(
            _rec("d.topics", 70, weights=DDB_WEIGHTS),
            _rec("d.categories", 100, weights=DDB_WEIGHTS),
        )
        # pattern 75 (key-scoped on DynamoDB), complexity 80, performance 60. Cost is the
        # query's own (#494), not the tables' 70: DynamoDB has no standing cost and one
        # read a second costs it $0.32 a month, a cost score of 100.
        expected = int(75 * 0.64 + 80 * 0.21 + 60 * 0.05 + 100 * 0.10)
        assert _score(JOIN, "dynamodb", {"dynamodb": analysis}) == expected

    def test_a_partition_range_read_takes_dynamodbs_query_rating(self):
        ranged = _query(
            "ranged",
            "SELECT * FROM topics WHERE category_id = $1 AND id > $2",
            ["topics"],
        )
        model = QueryPatternModel.from_pipeline(_collector(ranged), {})
        assert model.classification(ranged).name == KEY_SCOPED
        assert "partition_range" in model.classification(ranged).facts
        assert model.pattern_score(ranged, "dynamodb") == 90
        plain = _query("plain", "SELECT * FROM topics WHERE category_id = $1", ["topics"])
        assert model.pattern_score(plain, "dynamodb") == 75

    def test_a_joins_range_is_not_a_partition_range(self):
        """DynamoDB's Query reads one table: a key-anchored join keeps the join rating."""
        joined = _query(
            "joined",
            "SELECT t.id FROM topics t JOIN categories c ON c.id = t.category_id "
            "WHERE t.category_id = $1 AND t.id > $2",
            ["topics", "categories"],
        )
        model = QueryPatternModel.from_pipeline(_collector(joined), {})
        assert model.classification(joined).name == KEY_SCOPED
        assert "partition_range" not in model.classification(joined).facts
        assert model.pattern_score(joined, "dynamodb") == 75

    def test_a_small_table_read_whole_is_a_reference_read(self):
        everything = _query("all", "SELECT id, name FROM flags", ["flags"])
        big = _query("big", "SELECT id, name FROM users", ["users"])
        model = QueryPatternModel.from_pipeline(_collector(everything, big), {})
        assert model.classification(everything).name == REFERENCE_READ
        assert model.classification(big).name == KEY_RANGE
        limited = _query("lim", "SELECT id FROM flags ORDER BY id LIMIT $1", ["flags"])
        assert model.classification(limited).name == KEY_RANGE

    def test_no_all_tables_fallback(self):
        """A classified query with no rated table of its own scores 0, not the average."""
        unrated = _query("unrated", "SELECT * FROM users WHERE id = $1", ["users"])
        analysis = _analysis(_rec("d.topics", 90, weights=DDB_WEIGHTS))
        assert _score(unrated, "dynamodb", {"dynamodb": analysis}) == 0

    def test_a_pattern_only_engine_with_no_class_score_scores_zero(self):
        analysis = _analysis(_rec("d.topics", 90), _rec("d.categories", 90))
        assert _score(JOIN, "opensearch", {"opensearch": analysis}) == 0

    @pytest.mark.parametrize(
        ("query", "label"),
        [
            (_query("set", "SET search_path TO public", [], "OTHER"), UTILITY),
            (_query("proc", "EXEC dbo.usp_PlaceOrder @Id = @Id", [], "OTHER"), UTILITY),
        ],
    )
    def test_a_statement_with_no_class_keeps_the_pre_switch_rule(self, query, label):
        """No class, no class score: the pre-switch rule (here the all-tables mean)."""
        analysis = _analysis(_rec("d.topics", 90), _rec("d.categories", 60))
        model = QueryPatternModel.from_pipeline(_collector(query), {})
        assert model.classification(query).name == label
        assert not model.has_class_score(query)
        mean = int(
            (
                analysis["table_recommendations"][0]["confidence_score"]
                + analysis["table_recommendations"][1]["confidence_score"]
            )
            / 2
        )
        assert _score(query, "aurora_postgresql", {"aurora_postgresql": analysis}) == mean

    def test_an_unparsed_statement_keeps_its_tables_score(self):
        """A data statement the SQL facade cannot read is ``unparsed``."""
        replace_into = _query("rep", "UPDATE topics SET title = (((", ["topics"], "UPDATE")
        collector = {
            **_collector(replace_into),
            "metadata": {"source_database": {"engine": "mysql"}},
        }
        analysis = _analysis(_rec("d.topics", 90))
        model = QueryPatternModel.from_pipeline(collector, {})
        assert model.classification(replace_into).name == UNPARSED
        assert (
            _score(replace_into, "aurora_mysql", {"aurora_mysql": analysis}, collector)
            == analysis["table_recommendations"][0]["confidence_score"]
        )

    def test_the_penalty_and_the_gates_still_apply(self):
        analysis = _analysis(_rec("d.topics", 70), _rec("d.categories", 100))
        base = ScoringContext.from_pipeline({"aurora_postgresql": analysis}, None, _collector(JOIN))
        unpenalised = score_query_for_engine(JOIN, "aurora_postgresql", base).score
        penalised = ScoringContext(
            analysis_outputs={"aurora_postgresql": analysis},
            anti_pattern_map={("join", "aurora_postgresql"): 15},
            pattern_model=base.pattern_model,
        )
        assert score_query_for_engine(JOIN, "aurora_postgresql", penalised).score == (
            unpenalised - 15
        )
        gated = ScoringContext(
            analysis_outputs={"aurora_postgresql": analysis},
            query_capabilities={"join": ["full_text_search"]},
            pattern_model=base.pattern_model,
        )
        assert score_query_for_engine(JOIN, "dynamodb", gated).score is None


class TestASqlFeatureKeepsItsCreditPerQuery:
    """#985: recursive CTEs, LATERAL and stored routines are in no class of their own;
    a feature pattern rates a query that shows the feature in whatever class it is."""

    ORG = {
        "table_id": "dbo.Org",
        "table_name": "Org",
        "schema_name": "dbo",
        "row_count": 50_000,
        "size_mb": 10.0,
        "primary_key": ["Id"],
        "columns": [{"column_name": c, "data_type": "int"} for c in ("Id", "ParentId")],
        "indexes": [{"index_name": "ix_parent", "columns": ["ParentId"], "is_unique": False}],
        "foreign_keys": None,
    }
    TREE = (
        "WITH [Tree] AS (SELECT [Id], [ParentId] FROM [Org] WHERE [Id] = @Id "
        "UNION ALL SELECT o.[Id], o.[ParentId] FROM [Org] o JOIN [Tree] t "
        "ON o.[ParentId] = t.[Id]) SELECT [Id] FROM [Tree]"
    )

    def _run(self, text):
        query = {
            "query_id": "tree",
            "query_text": text,
            "query_type": "SELECT",
            "tables_accessed": ["dbo.Org"],
            "calls_per_second": 1.0,
        }
        collector = {
            "metadata": {"source_database": {"engine": "sqlserver"}},
            "database_schema": {"tables": [self.ORG]},
            "queries": {"query_patterns": [query]},
        }
        analysis = {
            engine: _analysis(_rec("dbo.Org", 80))
            for engine in ("aurora_mysql", "aurora_postgresql")
        }
        triage = {
            "selected_agents": [{"agent_type": e} for e in analysis],
            "query_capabilities": {},
            "signals": [],
        }
        ctx = ScoringContext.from_pipeline(analysis, triage, collector)
        scores = {e: score_query_for_engine(query, e, ctx).score for e in analysis}
        choice = AssignmentResolver().resolve(triage, analysis, collector).aurora_engine_choice
        return ctx.pattern_model.classification(query), scores, choice

    def test_a_recursive_cte_on_a_sql_server_source_prefers_aurora_postgresql_by_its_margin(
        self,
    ):
        from src.shared.catalog.patterns import pattern

        fits = pattern("cte-recursive").engines
        margin = int(
            AURORA_WEIGHTS["pattern_match"]
            * (fits["aurora_postgresql"].score - fits["aurora_mysql"].score)
        )
        classification, scores, choice = self._run(self.TREE)
        assert "recursive_cte" in classification.facts
        assert scores["aurora_postgresql"] - scores["aurora_mysql"] == margin == 2
        assert choice is not None and choice.engine == "aurora_postgresql"
        assert choice.totals["aurora_postgresql"] - choice.totals["aurora_mysql"] == margin

    def test_without_the_recursion_the_two_aurora_engines_score_the_same(self):
        flat = (
            "WITH [Tree] AS (SELECT [Id], [ParentId] FROM [Org] WHERE [Id] = @Id) "
            "SELECT t.[Id] FROM [Tree] t JOIN [Org] o ON o.[ParentId] = t.[Id]"
        )
        classification, scores, choice = self._run(flat)
        assert "recursive_cte" not in classification.facts
        assert scores["aurora_postgresql"] == scores["aurora_mysql"]


class TestTheSwitchIsTheRoutingPath:
    def test_the_routing_context_builds_the_model_from_the_collection(self):
        analysis = {"dynamodb": _analysis(_rec("d.topics", 70, weights=DDB_WEIGHTS))}
        ctx = ScoringContext.from_pipeline(analysis, None, _collector(LOOKUP))
        assert set(ctx.pattern_model.keys.tables) == {
            "d.topics",
            "d.categories",
            "d.users",
            "d.flags",
        }
        assert ctx.pattern_model.source_engine == "postgresql"
        assert ctx.pattern_model.weights == weights_from_analysis(analysis)
        assert ctx.pattern_model.pattern_score(LOOKUP, "dynamodb") == 100

    def test_the_resolver_scores_every_query_by_its_class(self):
        collector = _collector(LOOKUP, JOIN)
        triage = {
            "selected_agents": [{"agent_type": "aurora_postgresql"}, {"agent_type": "dynamodb"}],
            "query_capabilities": {},
            "signals": [],
        }
        analysis = {
            "aurora_postgresql": _analysis(_rec("d.topics", 80), _rec("d.categories", 80)),
            "dynamodb": _analysis(
                _rec("d.topics", 80, weights=DDB_WEIGHTS),
                _rec("d.categories", 80, weights=DDB_WEIGHTS),
            ),
        }
        trace = RoutingTrace()
        AssignmentResolver().resolve(triage, analysis, collector, routing_trace=trace)
        assert trace.by_query["lookup"]["access_class"] == KEY_LOOKUP
        assert trace.by_query["join"]["access_class"] == KEY_SCOPED
        ladder = trace.by_query["lookup"]["engines"]
        # The cost dimension is the query's own (#494): DynamoDB has no standing cost
        # (100); Aurora PostgreSQL's $250 is shared by the workload's two queries, $125
        # each, 100 * 100 / (100 + 125) = 44.
        assert ladder["dynamodb"] == int(100 * 0.64 + 80 * 0.21 + 60 * 0.05 + 100 * 0.10)
        assert ladder["aurora_postgresql"] == int(80 * 0.4 + 80 * 0.3 + 60 * 0.2 + 44 * 0.1)

    def test_the_assignment_handler_writes_no_shadow(self, tmp_path):
        from src.agents.referee import assignment_handler
        from src.storage.local_store import LocalArtifactStore

        store = LocalArtifactStore(str(tmp_path))
        store.write_json(
            "d/j/referee-triage/triage.json",
            {"selected_agents": [{"agent_type": "dynamodb"}], "query_capabilities": {}},
        )
        store.write_json("d/j/collector/output.json", _collector(LOOKUP))
        store.write_json(
            "d/j/analysis-dynamodb/analysis.json",
            _analysis(_rec("d.topics", 80, weights=DDB_WEIGHTS)),
        )
        assignment_handler.run_assignment_resolver("j", "d", store)
        assert store.exists("d/j/assignment/v1/assignment.json")
        assert not list((tmp_path / "d" / "j" / "assignment" / "v1").glob("*shadow*"))

    def test_the_handler_writes_the_access_patterns_record(self, tmp_path):
        """#936: per query, the class routing used and, per engine whose class score is a
        parity anchor, the anchor note (the shadow's ``parity_anchors`` until the switch)."""
        from src.agents.referee import assignment_handler
        from src.agents.referee.query_pattern import ACCESS_PATTERNS_FILE
        from src.shared.catalog.access_classes import PARITY_ANCHOR_NOTE
        from src.storage.local_store import LocalArtifactStore

        adhoc = _query("adhoc", "SELECT * FROM topics WHERE title = $1", ["topics"])
        setting = _query("set", "SET search_path TO public", [], "OTHER")
        store = LocalArtifactStore(str(tmp_path))
        store.write_json(
            "d/j/referee-triage/triage.json",
            {"selected_agents": [{"agent_type": "dynamodb"}], "query_capabilities": {}},
        )
        store.write_json("d/j/collector/output.json", _collector(LOOKUP, adhoc, setting))
        store.write_json(
            "d/j/analysis-dynamodb/analysis.json",
            _analysis(_rec("d.topics", 80, weights=DDB_WEIGHTS)),
        )
        assignment_handler.run_assignment_resolver("j", "d", store)
        trace = store.read_json(f"d/j/assignment/v1/{ACCESS_PATTERNS_FILE}")
        rows = {r["query_id"]: r for r in trace["queries"]}
        assert rows["adhoc"]["class"] == AD_HOC_FILTER
        assert sorted(rows["adhoc"]["parity_anchors"]) == ["documentdb", "opensearch"]
        assert rows["adhoc"]["parity_anchors"]["documentdb"] == (
            f"{PARITY_ANCHOR_NOTE} (secondary-attribute-filter, 75)"
        )
        assert rows["lookup"]["class_scores"]["dynamodb"] == 100
        assert sorted(rows["lookup"]["parity_anchors"]) == ["documentdb"]
        assert rows["set"]["class"] == UTILITY
        assert rows["set"]["class_scores"] == rows["set"]["parity_anchors"] == {}
        assert trace["by_class"] == {AD_HOC_FILTER: 1, KEY_LOOKUP: 1, UTILITY: 1}

    def test_a_time_bucket_aggregation_gets_its_conditional_score(self):
        bucket = _query(
            "tb",
            "SELECT date_trunc('day', title), count(*) FROM topics GROUP BY 1",
            ["topics"],
        )
        plain = _query("pl", "SELECT title, count(*) FROM topics GROUP BY title", ["topics"])
        model = QueryPatternModel.from_pipeline(_collector(bucket, plain), {})
        assert model.classification(bucket).name == AGGREGATION
        assert "time_bucket" in model.classification(bucket).facts
        assert model.pattern_score(bucket, "opensearch") == 90
        assert model.pattern_score(plain, "opensearch") == 0

    def test_each_query_is_classified_once_per_model(self, monkeypatch):
        import src.agents.referee.query_pattern as module

        calls = []
        real = module.classify_query

        def counting(query, keys, source):
            calls.append(query["query_id"])
            return real(query, keys, source)

        monkeypatch.setattr(module, "classify_query", counting)
        model = QueryPatternModel.from_pipeline(_collector(LOOKUP), {})
        for engine in ("dynamodb", "aurora_postgresql", "documentdb"):
            model.pattern_score(LOOKUP, engine)
        assert calls == ["lookup"]

    def test_weights_are_the_ones_each_engines_analysis_used(self):
        """Read from ``analysis.json`` alone: the referee never reads the trace (ADR-017)."""
        analysis = {
            "dynamodb": _analysis(_rec("d.topics", 80, weights=DDB_WEIGHTS)),
            "aurora_postgresql": _analysis(_rec("d.users", 80)),
        }
        weights = weights_from_analysis(analysis)
        assert weights[("dynamodb", "d.topics")] == DDB_WEIGHTS
        assert weights[("aurora_postgresql", "d.users")] == AURORA_WEIGHTS

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

    def test_a_bare_context_still_scores_by_class_with_no_schema_keys(self):
        """The bare dataclass (tests only) has a model too: no keys, default weights."""
        analysis = _analysis(_rec("d.topics", 70, weights=DEFAULT_WEIGHTS))
        ctx = ScoringContext(analysis_outputs={"dynamodb": analysis})
        assert ctx.pattern_model.keys.tables == {}
        # With no keys a pinned column addresses nothing: an ad hoc filter (DynamoDB 0).
        assert ctx.pattern_model.classification(LOOKUP).name == AD_HOC_FILTER
        # Cost is the query's own (#494): DynamoDB has no standing cost, one read a
        # second costs $0.32 a month, a cost score of 100.
        assert score_query_for_engine(LOOKUP, "dynamodb", ctx).score == int(
            0 * 0.4 + 80 * 0.3 + 60 * 0.2 + 100 * 0.1
        )


# ---------------------------------------------------------------------------
# The guard: every production score reads the query's own class
# ---------------------------------------------------------------------------

# Where production code may build these (and nowhere else in src/ or scripts/).
_BUILDERS = {
    "QueryPatternModel": "src/agents/referee/query_pattern.py",
    "ScoringContext": "src/agents/referee/query_scoring.py",
}


def _bypasses(tree, path):
    """``(lineno, why)`` for every way a module could score without the routing model.

    - building a :class:`QueryPatternModel` or a :class:`ScoringContext` outside the
      module that owns its ``from_pipeline``;
    - passing ``pattern_model=`` anything but a scoring context's own model
      (``<ctx>.pattern_model``) or ``QueryPatternModel.from_pipeline(...)``;
    - ``dataclasses.replace`` of a context's ``pattern_model``.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in _BUILDERS and _BUILDERS[name] != path:
            found.append((node.lineno, f"builds a {name}"))
        for k in node.keywords:
            if k.arg != "pattern_model":
                continue
            value = k.value
            from_context = isinstance(value, ast.Attribute) and value.attr == "pattern_model"
            from_pipeline = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "from_pipeline"
                and getattr(value.func.value, "id", None) == "QueryPatternModel"
            )
            if not (from_context or from_pipeline):
                found.append((node.lineno, f"passes its own pattern_model= to {name}"))
    return found


def test_every_production_score_reads_the_querys_own_class():
    """ADR-031 Decision 14 Amendment 5 (#728): the per-query class model is the one
    routing path. Only ``ScoringContext.from_pipeline`` builds a context, only
    ``QueryPatternModel.from_pipeline`` builds its model, and every score reads the
    context's model; the scorer has no default that skips it."""
    found = {
        f"{rel}:{line} {why}"
        for root in ("src", "scripts")
        for path in (REPO / root).rglob("*.py")
        if (rel := path.relative_to(REPO).as_posix())
        for line, why in _bypasses(ast.parse(path.read_text()), rel)
    }
    assert not found, sorted(found)
    assert "pattern_model" not in (compute_query_confidence.__kwdefaults__ or {})


def test_the_guard_catches_every_way_of_bypassing_the_model():
    snippet = """
def route(collector, ctx):
    ScoringContext(analysis_outputs=a)
    QueryPatternModel(keys=k)
    compute_query_confidence(q, query, e, a, pattern_model=make())
    replace(ctx, pattern_model=other)
    compute_query_confidence(q, query, e, a, pattern_model=ctx.pattern_model)
    ScoringContext.from_pipeline(a, t, collector)
    x = dict(pattern_model=QueryPatternModel.from_pipeline(collector, a))
"""
    assert [line for line, _ in _bypasses(ast.parse(snippet), "src/x.py")] == [3, 4, 5, 6]
