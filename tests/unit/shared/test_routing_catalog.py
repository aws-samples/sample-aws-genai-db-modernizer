"""The rule data #921 moved out of code into the catalog keeps its values (no routing change),
except where a later issue changed one on purpose (#984: full text and upserts get the
same Aurora bonus on both engines).

Each constant below was a literal in code before #921 and is now built from
``src/catalog/routing.toml``, ``gates.toml``, ``triage_signals.toml``, the engine files'
new fields or the pattern files' table bonuses. The pins are the pre-move literals, so a
catalog edit that changes routing fails here and has to say why; the loader tests check
that a broken file fails at load time instead of reaching a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.referee import (
    aurora_choice,
    cache_overlay,
    consolidation_validator,
    reality_check_floors,
    reality_check_rules,
    tie_break,
    triage,
)
from src.agents.referee.assignment_resolver import (
    JUSTIFICATION_GATED_OVERRIDE_ENGINES,
    SIGNAL_ENGINE_OVERRIDES,
    SIGNAL_OVERRIDE_REASON_CODES,
)
from src.agents.referee.capability_registry import (
    CAPABILITY_FACTS,
    ENGINE_CAPABILITIES,
    SIGNAL_TO_CAPABILITY,
)
from src.agents.referee.engine_exclusions import EXCLUSION_RULES, check_all_exclusions
from src.agents.referee.feasibility_review import _engine_role, _replication_recommendation
from src.contracts.move_ledger import ReasonCode
from src.shared import catalog
from src.shared.cache_engines import HOT_READ_MIN_CALLS_PER_SECOND
from src.shared.catalog import CatalogError
from src.shared.catalog.engines import load_engine_catalog
from src.shared.catalog.patterns import (
    feature_complexity_bonus,
    load_pattern_catalog,
    patterns,
    table_bonuses,
)
from src.shared.catalog.routing import load_gates_file, load_routing_file, load_triage_signals_file
from src.shared.engine_capabilities import PATTERN_ONLY_ENGINES

CATALOG = Path(__file__).resolve().parents[3] / "src" / "catalog"
AURORA = ("aurora_postgresql", "aurora_mysql")


class TestThresholdsKeepTheirValues:
    def test_the_shared_thresholds(self) -> None:
        got = {
            "TARGET_ENGINE_COUNT": reality_check_rules.TARGET_ENGINE_COUNT,
            "UNIQUE_DELTA_THRESHOLD": reality_check_rules.UNIQUE_DELTA_THRESHOLD,
            "MANDATORY_ENGINE_COST_FLOOR": reality_check_floors.MANDATORY_ENGINE_COST_FLOOR,
            "MANDATORY_ENGINE_MAX_QUERY_SHARE": (
                reality_check_floors.MANDATORY_ENGINE_MAX_QUERY_SHARE
            ),
            "CHEAPER_ENGINE_MIN_SAVING_SHARE": tie_break.CHEAPER_ENGINE_MIN_SAVING_SHARE,
            "CHEAPER_ENGINE_MIN_SAVING_MONTHLY": tie_break.CHEAPER_ENGINE_MIN_SAVING_MONTHLY,
            "ORPHAN_ENGINE_THRESHOLD": consolidation_validator.ORPHAN_ENGINE_THRESHOLD,
            "MARGIN_RATIO": aurora_choice.MARGIN_RATIO,
        }
        assert got == {
            "TARGET_ENGINE_COUNT": 2,
            "UNIQUE_DELTA_THRESHOLD": 15,
            "MANDATORY_ENGINE_COST_FLOOR": 100,
            "MANDATORY_ENGINE_MAX_QUERY_SHARE": 0.05,
            "CHEAPER_ENGINE_MIN_SAVING_SHARE": 0.40,
            "CHEAPER_ENGINE_MIN_SAVING_MONTHLY": 100.0,
            "ORPHAN_ENGINE_THRESHOLD": 5,
            "MARGIN_RATIO": 0.05,
        }
        # A count stays an int (it is compared with len() and printed with {}).
        assert isinstance(reality_check_rules.TARGET_ENGINE_COUNT, int)
        assert isinstance(consolidation_validator.ORPHAN_ENGINE_THRESHOLD, int)

    def test_the_engine_thresholds(self) -> None:
        rule = reality_check_floors.FLOOR_RULES["opensearch"]
        assert (
            rule.min_calls_per_second,
            rule.native_override_min_calls_per_second,
            rule.min_corpus_rows_avg,
        ) == (1.0, 10.0, 1000.0)
        assert (
            HOT_READ_MIN_CALLS_PER_SECOND,
            cache_overlay.CACHE_MAX_ROWS_AVG,
            cache_overlay.WRITE_HEAVY_TABLE_MIN_WRITE_SHARE,
        ) == (1.0, 100.0, 0.5)

    def test_an_unknown_threshold_is_a_key_error(self) -> None:
        with pytest.raises(KeyError):
            catalog.threshold("no_such_threshold")
        with pytest.raises(KeyError):
            catalog.engine_threshold("dynamodb", "min_calls_per_second")

    def test_a_threshold_needs_a_number_and_a_reason(self, tmp_path: Path) -> None:
        bad = tmp_path / "routing.toml"
        bad.write_text('[thresholds.x]\nvalue = true\nwhy = "y"\n', encoding="utf-8")
        with pytest.raises(CatalogError, match="number"):
            load_routing_file(bad)
        bad.write_text("[thresholds.x]\nvalue = 1\n", encoding="utf-8")
        with pytest.raises(CatalogError):
            load_routing_file(bad)


class TestTheEngineFieldsKeepTheirValues:
    def test_engine_sets(self) -> None:
        assert PATTERN_ONLY_ENGINES == {"opensearch"}
        assert aurora_choice.AURORA_ENGINES == set(AURORA)
        assert reality_check_rules.AURORA_ENGINES == set(AURORA)
        assert reality_check_rules.SPECIALIST_ABSORB_SIGNALS == {
            "opensearch": {"text_search", "aggregations"}
        }
        assert reality_check_rules.SPECIALIST_ABSORB_CAPABILITIES == {
            "opensearch": {"text_pattern_match"}
        }

    def test_triage_engines(self) -> None:
        assert triage.ANALYSIS_AGENTS == {
            "dynamodb",
            "documentdb",
            "opensearch",
            "elasticache",
            "aurora_postgresql",
            "aurora_mysql",
        }
        assert triage.BASELINE == "aurora"
        assert triage.DEFERRED == {"keyspaces", "neptune"}
        assert triage.SOURCE_ENGINE_TO_AURORA == {
            "mysql": "aurora_mysql",
            "mariadb": "aurora_mysql",
            "postgresql": "aurora_postgresql",
            "postgres": "aurora_postgresql",
        }

    def test_a_source_engine_names_one_target(self, tmp_path: Path) -> None:
        for path in sorted((CATALOG / "engines").glob("*.toml")):
            text = path.read_text(encoding="utf-8")
            if path.stem == "aurora_mysql":
                text = text.replace('["mysql", "mariadb"]', '["mysql", "postgres"]')
            (tmp_path / path.name).write_text(text, encoding="utf-8")
        with pytest.raises(CatalogError, match="'postgres' is named by both"):
            load_engine_catalog(tmp_path)


class TestTheGatesKeepTheirValues:
    def test_the_capability_matrix(self) -> None:
        relational = {
            "multi_doc_acid",
            "strong_consistency",
            "scan_engine",
            "ranking_window",
            "offset_window",
            "text_pattern_match",
            "aggregation",
            "complex_joins",
            "sql_admin",
            "computed_join",
        }
        assert ENGINE_CAPABILITIES == {
            "dynamodb": {"multi_doc_acid", "strong_consistency"},
            "documentdb": {
                "multi_doc_acid",
                "strong_consistency",
                "aggregation",
                "text_pattern_match",
                # #968: $setWindowFields ranks on 8.0.2+; LAG, LEAD and NTILE run nowhere.
                "ranking_window",
            },
            "opensearch": {
                "text_pattern_match",
                "scan_engine",
                "ranking_window",
                "offset_window",
                "aggregation",
            },
            "aurora_postgresql": relational,
            "aurora_mysql": relational,
        }

    def test_what_requires_a_capability(self) -> None:
        assert {cap: facts for cap, facts in CAPABILITY_FACTS.items() if facts} == {
            "text_pattern_match": ("text_search",),
            "scan_engine": ("recursive_cte",),
            "ranking_window": ("ranking_window_function",),
            "offset_window": ("offset_window_function",),
            "aggregation": ("needs_aggregation",),
            "computed_join": ("computed_join_key",),
        }
        assert SIGNAL_TO_CAPABILITY == {
            "text_search": "text_pattern_match",
            "aggregations": "aggregation",
            "complex_joins": "complex_joins",
        }

    def test_the_exclusions_in_order(self) -> None:
        assert [(r.engine, r.rule_id) for r in EXCLUSION_RULES] == [
            ("dynamodb", "ddb-no-leading-wildcard"),
            ("opensearch", "os-no-write-subquery"),
            ("elasticache", "ec-no-complex-join"),
        ]

    @pytest.mark.parametrize(
        ("sql", "excluded"),
        [
            ("SELECT * FROM a WHERE x LIKE '%a'", {"dynamodb"}),
            ("UPDATE t SET a = 1 WHERE id IN (SELECT id FROM u)", {"opensearch"}),
            ("SELECT * FROM a JOIN b ON a.id = b.a_id JOIN c ON c.id = b.c_id", {"elasticache"}),
            # One join is below the ElastiCache rule's min of 2.
            ("SELECT * FROM a JOIN b ON a.id = b.a_id", set()),
            ("SELECT * FROM a WHERE id = 1", set()),
        ],
    )
    def test_each_exclusion_reads_its_fact(self, sql: str, excluded: set[str]) -> None:
        assert {r.excluded_engine for r in check_all_exclusions("q1", sql, "mysql")} == excluded

    def _gates_with(self, tmp_path: Path, old: str, new: str) -> Path:
        path = tmp_path / "gates.toml"
        text = (CATALOG / "gates.toml").read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return path

    def test_a_gate_that_disagrees_with_its_pattern_files_fails(self, tmp_path: Path) -> None:
        """The gate and the pattern files' supported flags are one fact (#921)."""
        path = self._gates_with(
            tmp_path,
            'engines = ["documentdb", "opensearch", "aurora_postgresql", "aurora_mysql"]\n'
            'facts = ["text_search"]',
            'engines = ["opensearch", "aurora_postgresql", "aurora_mysql"]\n'
            'facts = ["text_search"]',
        )
        with pytest.raises(CatalogError, match="documentdb lacks it"):
            load_gates_file(path)

    def test_a_gate_naming_a_non_target_fails(self, tmp_path: Path) -> None:
        path = self._gates_with(
            tmp_path,
            'engines = ["aurora_postgresql", "aurora_mysql"]\nfacts = ["computed_join_key"]',
            'engines = ["neptune", "aurora_postgresql", "aurora_mysql"]\n'
            'facts = ["computed_join_key"]',
        )
        with pytest.raises(CatalogError, match="not routing targets"):
            load_gates_file(path)

    def test_a_signal_requires_one_capability(self, tmp_path: Path) -> None:
        path = self._gates_with(
            tmp_path,
            'triage_signals = ["complex_joins"]',
            'triage_signals = ["complex_joins", "aggregations"]',
        )
        with pytest.raises(CatalogError, match="'aggregations' requires both"):
            load_gates_file(path)


class TestTheTriageSignalsKeepTheirValues:
    def test_the_overrides(self) -> None:
        assert SIGNAL_ENGINE_OVERRIDES == {
            "text_search": "opensearch",
            "graph_traversal": "neptune",
        }
        assert JUSTIFICATION_GATED_OVERRIDE_ENGINES == {"opensearch"}
        assert SIGNAL_OVERRIDE_REASON_CODES == {"text_search": ReasonCode.OVERRIDE_TEXT_SEARCH}

    def test_every_signal_triage_builds_has_its_targets(self) -> None:
        collector = {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {"tables": []},
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM posts WHERE title LIKE '%a%'",
                        "query_type": "SELECT",
                        "calls_per_second": 20,
                        "rows_returned_avg": 1,
                        "has_text_search": True,
                    }
                ]
            },
        }
        result = triage.triage(collector)
        assert result.signals
        for sig in result.signals:
            assert sig.targets == catalog.signal_targets(sig.signal), sig.signal
        by_name = {s.signal: s.targets for s in result.signals}
        assert by_name["text_search"] == ["opensearch"]
        assert by_name["key_value_lookups"] == ["dynamodb", "elasticache", "documentdb"]

    def test_a_reason_code_needs_an_override_engine(self, tmp_path: Path) -> None:
        path = tmp_path / "triage_signals.toml"
        path.write_text(
            '[signals.x]\ntargets = []\noverride_reason_code = "override.text_search"\n',
            encoding="utf-8",
        )
        with pytest.raises(CatalogError, match="needs an override_engine"):
            load_triage_signals_file(path)

    def test_an_unknown_target_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "triage_signals.toml"
        path.write_text('[signals.x]\ntargets = ["newdb"]\n', encoding="utf-8")
        with pytest.raises(CatalogError, match="newdb"):
            load_triage_signals_file(path)


# The pre-#921 in-code bonuses, per engine: pattern type -> (pattern points, complexity points)
# for a table showing that pattern alone.
PRE_921_BONUSES = {
    "dynamodb": {
        "key-value-lookup": (15, 0),
        "range-query": (10, 0),
        "metadata-config-store": (10, 0),
        "session-store": (12, 0),
        "time-series-event-log": (10, 0),
        "write-heavy-ingestion": (8, 0),
        "bounded-parent-child": (5, -10),
        "many-to-many-junction": (5, -10),
        "co-accessed-tables": (5, -10),
        "adjacency-list": (5, -10),
    },
    "documentdb": {
        # #967: content-management (12) and product-catalog (10) are variant-attributes
        # (10); flexible-schema is json-document-query; write-time-aggregation (15) is no
        # longer a fit.
        "variant-attributes": (10, 0),
        "nested-document": (12, 0),
        "extended-reference": (10, 0),
        "json-document-query": (8, 0),
        "aggregation-pipeline": (8, 0),
    },
    "opensearch": {
        "full-text-search": (15, 0),
        "wildcard-search": (10, 0),
        "fuzzy-search": (10, 0),
        "regex-search": (5, 0),
        "time-aggregation": (15, 0),
        "time-range-query": (10, 0),
        "high-ingest": (15, 0),
    },
    "aurora_mysql": {
        "cte-recursive": (15, 0),
        "window-function": (10, 0),
        "lateral-join": (10, 0),
        "multi-table-update": (10, 0),
        "group-concat-pivot": (5, 0),
        "json-document-query": (10, 0),
        "fulltext-search": (10, 0),
        "upsert-duplicate-key": (5, 0),
    },
    "aurora_postgresql": {
        "cte-recursive": (15, 0),
        "window-function": (10, 0),
        "lateral-join": (10, 0),
        "multi-table-update": (10, 0),
        "group-concat-pivot": (5, 0),
        "json-document-query": (10, 0),  # #967: was jsonb-operations
        "upsert-conflict": (5, 0),
        # #984: the same bonus as Aurora MySQL's fulltext-search.
        "full-text-native": (10, 0),
    },
}
PRE_921_FEATURES = {
    "aurora_mysql": {
        "cte-recursive",
        "window-function",
        "lateral-join",
        "multi-table-update",
        "group-concat-pivot",
        "stored-routine",
        # #984: full text and the upsert are per-query features now, not table-level ones.
        # #1102: so is a JSON read (json-document-query).
    },
    "aurora_postgresql": {
        "cte-recursive",
        "window-function",
        "lateral-join",
        "multi-table-update",
        "group-concat-pivot",
        "stored-routine",
        # #1102: json-document-query (was jsonb-operations, #967) is a per-query feature now.
        "array-operations",
    },
    # #967: polymorphic-data's credit is not carried into variant-attributes, which would
    # widen it to typed-entity reads; #1102: json-document-query (was flexible-schema) is a
    # per-query feature now. DocumentDB has no table-level complexity credit (Decision 14).
}


class TestTheTableBonusesKeepTheirValues:
    def test_every_bonus_is_the_pre_move_value(self) -> None:
        got: dict[str, dict[str, tuple[int, int]]] = {}
        for entry in patterns():
            for engine, support in entry.engines.items():
                if support.table_bonus or support.complexity_bonus:
                    got.setdefault(engine, {})[entry.id] = (
                        support.table_bonus,
                        support.complexity_bonus,
                    )
        assert got == PRE_921_BONUSES

    def test_every_complexity_feature_is_the_pre_move_set(self) -> None:
        got: dict[str, set[str]] = {}
        for entry in patterns():
            for engine, support in entry.engines.items():
                if support.complexity_feature:
                    got.setdefault(engine, set()).add(entry.id)
        assert got == PRE_921_FEATURES

    def test_a_group_counts_once(self) -> None:
        denorm = ["bounded-parent-child", "many-to-many-junction", "adjacency-list"]
        assert table_bonuses("dynamodb", denorm) == (5, -10)
        assert table_bonuses("dynamodb", [*denorm, "key-value-lookup"]) == (20, -10)
        assert table_bonuses("opensearch", ["wildcard-search", "fuzzy-search"], "search") == (
            10,
            0,
        )

    def test_an_opensearch_profile_reads_only_its_own_fits(self) -> None:
        both = ["full-text-search", "time-aggregation"]
        assert table_bonuses("opensearch", both, "search") == (15, 0)
        assert table_bonuses("opensearch", both, "timeseries") == (15, 0)
        assert table_bonuses("opensearch", ["time-range-query"], "search") == (0, 0)

    def test_a_pattern_another_engine_owns_adds_nothing(self) -> None:
        assert table_bonuses("dynamodb", ["full-text-search", "cte-recursive"]) == (0, 0)
        assert table_bonuses("dynamodb", []) == (0, 0)

    @pytest.mark.parametrize(
        ("engine", "shown", "points"),
        [
            ("aurora_mysql", [], 0),
            ("aurora_mysql", ["cte-recursive"], 10),
            # #984: full text no longer counts toward a table's complexity.
            ("aurora_mysql", ["cte-recursive", "fulltext-search"], 10),
            ("aurora_mysql", ["cte-recursive", "fulltext-search", "lateral-join"], 15),
            ("aurora_postgresql", ["array-operations"], 10),
            # #984: no upsert counts toward a table's complexity on either engine.
            ("aurora_postgresql", ["upsert-conflict"], 0),
            ("aurora_mysql", ["upsert-duplicate-key"], 0),
            ("aurora_postgresql", ["upsert-conflict", "full-text-native"], 0),
            # #1102: a JSON read no longer counts toward a table's complexity anywhere.
            ("documentdb", ["variant-attributes", "json-document-query"], 0),
            ("aurora_postgresql", ["json-document-query"], 0),
            ("aurora_mysql", ["json-document-query"], 0),
            ("aurora_mysql", ["json-document-query", "cte-recursive"], 10),
            ("dynamodb", ["key-value-lookup"], 0),
        ],
    )
    def test_the_feature_count_tiers(self, engine: str, shown: list[str], points: int) -> None:
        assert feature_complexity_bonus(engine, shown) == points

    def test_a_feature_both_aurora_engines_report_scores_the_same_on_both(self) -> None:
        """#920: a shared SQL feature never favours one Aurora engine (Decision 6)."""
        for entry in patterns():
            pg, my = (entry.engines.get(e) for e in AURORA)
            if pg is None or my is None or pg.kind != "fit" or my.kind != "fit":
                continue
            assert (pg.table_bonus, pg.complexity_feature) == (
                my.table_bonus,
                my.complexity_feature,
            ), entry.id

    # A feature both Aurora engines run natively, written as a different pattern file per
    # dialect (#984): PostgreSQL's file, then MySQL's.
    DIALECT_PAIRS = (
        ("full-text-native", "fulltext-search"),
        ("upsert-conflict", "upsert-duplicate-key"),
    )

    @pytest.mark.parametrize(("pg_pattern", "mysql_pattern"), DIALECT_PAIRS)
    def test_a_dialect_pair_gets_the_same_bonus_on_both_engines(
        self, pg_pattern: str, mysql_pattern: str
    ) -> None:
        """#984: ADR-031 Decision 6 compares the two engines' totals, so a feature both
        run natively never favours one on a missing bonus; each engine's documented
        strength stays in its fit score."""
        from src.shared.catalog.access_classes import access_classes, feature_patterns
        from src.shared.catalog.patterns import pattern

        pg = pattern(pg_pattern).engines["aurora_postgresql"]
        my = pattern(mysql_pattern).engines["aurora_mysql"]
        assert (pg.kind, my.kind) == ("fit", "fit")
        # The same table-summary bonus, and it is a real one.
        assert pg.table_bonus == my.table_bonus > 0
        # Neither is a table-level routing credit (Decision 14)...
        assert not pg.complexity_feature and not my.complexity_feature
        assert pg.complexity_bonus == my.complexity_bonus == 0
        # ...each rates its own statement per query, on the same fact, in every class.
        facts = {f.pattern: f.fact for f in feature_patterns()}
        assert facts.get(pg_pattern) is not None
        assert facts[pg_pattern] == facts[mysql_pattern]
        assert not {pg_pattern, mysql_pattern} & {p for c in access_classes() for p in c.patterns}

    def test_a_dialect_pair_scores_the_same_table_adjustment(self) -> None:
        for pg_pattern, mysql_pattern in self.DIALECT_PAIRS:
            assert table_bonuses("aurora_postgresql", [pg_pattern]) == table_bonuses(
                "aurora_mysql", [mysql_pattern]
            )
            assert feature_complexity_bonus(
                "aurora_postgresql", [pg_pattern]
            ) == feature_complexity_bonus("aurora_mysql", [mysql_pattern])

    def test_an_upsert_credits_the_upsert_statement_not_its_table(self) -> None:
        """#984, ADR-031 Decision 14: a query's pattern dimension is its own class. The
        upsert patterns rate a statement showing the upsert fact, on each engine at its
        documented fit (80); a query without the fact earns nothing from them."""
        from src.shared.catalog.access_classes import access_classes, class_score

        # An upsert by a non-key filter: its class rates Aurora 75 without the feature.
        ad_hoc = next(c for c in access_classes() if c.id == "ad_hoc_filter")
        for engine in AURORA:
            assert class_score(ad_hoc, engine) == 75
            assert class_score(ad_hoc, engine, frozenset({"write", "upsert"})) == 80
            assert class_score(ad_hoc, engine, frozenset({"write"})) == 75
        # Never on an engine whose upsert it is not documented for, nor on a cache.
        assert class_score(ad_hoc, "elasticache", frozenset({"write", "upsert"})) == 0

    def test_the_classifier_sets_the_upsert_fact(self) -> None:
        """The upsert class fact comes from the classifier on a real statement (review of
        #1090), and a plain write of the same table does not get it."""
        from src.agents.referee.access_pattern import SchemaKeys, classify_query

        collector = {
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.t",
                        "table_name": "t",
                        "columns": [{"column_name": "a", "data_type": "int"}],
                        "primary_key": ["a"],
                    }
                ]
            }
        }
        keys = SchemaKeys.from_collector(collector)

        def facts(text: str, query_type: str, source: str) -> frozenset[str]:
            query = {"query_text": text, "query_type": query_type, "tables_accessed": ["app.t"]}
            return classify_query(query, keys, source).facts

        assert "upsert" in facts(
            "INSERT INTO t (a) VALUES (1) ON CONFLICT (a) DO NOTHING", "INSERT", "postgresql"
        )
        assert "upsert" in facts(
            "INSERT INTO t (a) VALUES (1) ON DUPLICATE KEY UPDATE a = 2", "INSERT", "mysql"
        )
        assert "upsert" not in facts("INSERT INTO t (a) VALUES (1)", "INSERT", "postgresql")
        assert "upsert" not in facts("UPDATE t SET a = 2 WHERE a = 1", "UPDATE", "mysql")

    def test_full_text_credits_the_full_text_query_in_any_class(self) -> None:
        """#984: a key-anchored full-text query gets each Aurora engine's full-text fit as a
        feature; a query without the fact gets nothing from it."""
        from src.shared.catalog.access_classes import access_classes, class_score

        key_scoped = next(c for c in access_classes() if c.id == "key_scoped")
        assert class_score(key_scoped, "aurora_postgresql", frozenset({"full_text"})) == 75
        assert class_score(key_scoped, "aurora_mysql", frozenset({"full_text"})) == 80
        assert class_score(key_scoped, "aurora_postgresql") == class_score(
            key_scoped, "aurora_mysql"
        )

    JSON_ENGINES = {"aurora_postgresql": 85, "aurora_mysql": 85, "documentdb": 80}

    def test_no_table_level_json_credit_remains(self) -> None:
        """#1102, ADR-031 Decision 14: a query's pattern dimension is its own class, so a
        JSON read is never a table-level complexity credit on any engine; it rates the
        JSON query itself, as a [[features]] entry on the json_access fact."""
        from src.shared.catalog.access_classes import access_classes, feature_patterns
        from src.shared.catalog.engines import engine_entry
        from src.shared.catalog.patterns import pattern

        entry = pattern("json-document-query")
        for engine, support in entry.engines.items():
            assert not support.complexity_feature, engine
            assert support.complexity_bonus == 0, engine
            assert feature_complexity_bonus(engine, ["json-document-query"]) == 0, engine
        assert {e for e, s in entry.engines.items() if s.kind == "fit"} == set(self.JSON_ENGINES)
        # DocumentDB has no table-level complexity credit left, so none can come back
        # without a catalog error (a complexity_feature needs the engine's tiers).
        documentdb = engine_entry("documentdb")
        assert documentdb is not None and documentdb.complexity_by_feature_count == ()
        facts = {f.pattern: f.fact for f in feature_patterns()}
        assert facts.get("json-document-query") == "json_access"
        assert "json-document-query" not in {p for c in access_classes() for p in c.patterns}

    def test_a_json_read_credits_the_json_query_not_its_table(self) -> None:
        """#1102: the JSON query gets each engine's documented JSON fit, in any class
        that scores the engine above 0; a class that scores it 0 keeps it at 0 (#1123: a
        JSON read does not make DocumentDB serve an unanchored join). A query without the
        fact gets nothing from it, and no engine without a JSON fit gains anything."""
        from src.shared.catalog.access_classes import access_classes, class_score

        json_fact = frozenset({"json_access"})
        for access_class in access_classes():
            for engine, fit in self.JSON_ENGINES.items():
                base = class_score(access_class, engine)
                shown = class_score(access_class, engine, json_fact)
                assert shown == (max(base, fit) if base else 0), (access_class.id, engine)
            for engine in ("dynamodb", "opensearch", "elasticache"):
                assert class_score(access_class, engine, json_fact) == class_score(
                    access_class, engine
                ), (access_class.id, engine)
        # The zero cells #1118's review named stay at 0 (#1123).
        unanchored_join = next(c for c in access_classes() if c.id == "unanchored_join")
        assert class_score(unanchored_join, "documentdb") == 0
        assert class_score(unanchored_join, "documentdb", json_fact) == 0
        text_search = next(c for c in access_classes() if c.id == "text_search")
        for engine in self.JSON_ENGINES:
            assert class_score(text_search, engine, json_fact) == 0, engine
        # A key read of a JSON table: the class alone, then the JSON read itself.
        key_lookup = next(c for c in access_classes() if c.id == "key_lookup")
        for engine in AURORA:
            assert class_score(key_lookup, engine) == 80
            assert class_score(key_lookup, engine, json_fact) == 85
        # Both Aurora engines get the same JSON credit (Decision 6).
        assert self.JSON_ENGINES["aurora_postgresql"] == self.JSON_ENGINES["aurora_mysql"]

    @pytest.mark.parametrize(
        ("text", "source", "shown"),
        [
            ("SELECT doc->>'a' FROM t WHERE a = 1", "postgresql", True),
            ("SELECT JSON_EXTRACT(doc, '$.a') FROM t WHERE a = 1", "mysql", True),
            ("SELECT * FROM t WHERE doc @> '{\"a\": 1}'", "postgresql", True),
            ("SELECT doc FROM t WHERE a = 1", "postgresql", False),
            ("SELECT doc FROM t WHERE a = 1", "mysql", False),
            ("SELECT * FROM t WHERE note = 'a->b'", "postgresql", False),
            ("SELECT * FROM t -- JSON_EXTRACT(doc, '$.a')", "mysql", False),
        ],
    )
    def test_the_classifier_sets_the_json_fact(self, text: str, source: str, shown: bool) -> None:
        """The json_access class fact comes from the classifier on the query's own text,
        read in the source's dialect; a plain read of the same table does not get it."""
        from src.agents.referee.access_pattern import SchemaKeys, classify_query

        collector = {
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.t",
                        "table_name": "t",
                        "columns": [
                            {"column_name": "a", "data_type": "int"},
                            {"column_name": "doc", "data_type": "json"},
                        ],
                        "primary_key": ["a"],
                    }
                ]
            }
        }
        keys = SchemaKeys.from_collector(collector)
        query = {"query_text": text, "query_type": "SELECT", "tables_accessed": ["app.t"]}
        assert ("json_access" in classify_query(query, keys, source).facts) is shown

    def test_the_upsert_fact(self) -> None:
        from src.tools.sql.query_facts import query_facts

        assert query_facts(
            "INSERT INTO t (a) VALUES (1) ON CONFLICT (a) DO NOTHING", "postgresql"
        ).upsert
        assert query_facts("INSERT INTO t VALUES (1) ON DUPLICATE KEY UPDATE a = 1", "mysql").upsert
        assert not query_facts("SELECT 'on conflict' FROM t", "postgresql").upsert
        assert not query_facts("UPDATE t SET a = 1 RETURNING id", "postgresql").upsert
        assert query_facts("REPLACE INTO t VALUES (1)", "mysql").upsert
        # Review of #1090: never inside a literal, a comment or a quoted identifier.
        for text in (
            "SELECT * FROM t WHERE note = 'replace into x'",
            "SELECT * FROM t -- on duplicate key update",
            "SELECT * FROM t /* on duplicate key update */",
            "SELECT `on duplicate key update` FROM t",
            "SELECT `replace into` FROM t",
        ):
            assert not query_facts(text, "mysql").upsert, text
        assert not query_facts('SELECT "on conflict" FROM t', "postgresql").upsert
        assert not query_facts("SELECT [replace into] FROM t", "sqlserver").upsert

    def _catalog_with(self, tmp_path: Path, name: str, old: str, new: str) -> Path:
        for path in sorted((CATALOG / "patterns").glob("*.toml")):
            text = path.read_text(encoding="utf-8")
            if path.stem == name:
                assert old in text
                text = text.replace(old, new, 1)
            (tmp_path / path.name).write_text(text, encoding="utf-8")
        return tmp_path

    def test_a_group_whose_members_disagree_fails(self, tmp_path: Path) -> None:
        directory = self._catalog_with(
            tmp_path,
            "adjacency-list",
            'bonus_group = "denormalizable-join"\ntable_bonus = 5',
            'bonus_group = "denormalizable-join"\ntable_bonus = 6',
        )
        with pytest.raises(CatalogError, match="bonus_group 'denormalizable-join'"):
            load_pattern_catalog(directory)

    def test_a_bonus_is_only_for_a_fit(self, tmp_path: Path) -> None:
        directory = self._catalog_with(
            tmp_path,
            "adjacency-list",
            '[engines.documentdb]\nkind = "unscored"\nsupported = true\n',
            '[engines.documentdb]\nkind = "unscored"\nsupported = true\ntable_bonus = 5\n',
        )
        with pytest.raises(CatalogError, match="cannot carry"):
            load_pattern_catalog(directory)

    def test_a_feature_needs_the_engine_tiers(self, tmp_path: Path) -> None:
        directory = self._catalog_with(
            tmp_path,
            "key-value-lookup",
            "table_bonus = 15",
            "table_bonus = 15\ncomplexity_feature = true",
        )
        with pytest.raises(CatalogError, match="complexity_by_feature_count"):
            load_pattern_catalog(directory)


class TestTheEngineRoleIsTheCatalogs:
    def test_every_target_engine_reads_its_file(self) -> None:
        assert {e: _engine_role(e) for e in sorted(triage.ANALYSIS_AGENTS)} == {
            "aurora_mysql": "relational",
            "aurora_postgresql": "relational",
            "documentdb": "document",
            "dynamodb": "key_value",
            "elasticache": "cache",
            "opensearch": "search",
        }

    def test_an_id_routing_cannot_target_is_other(self) -> None:
        assert _engine_role("neptune") == "other"
        assert _engine_role("aurora") == "other"
        assert _engine_role("not_an_engine") == "other"

    @pytest.mark.parametrize(
        ("reads", "expected"),
        [
            ({"elasticache"}, "cache-aside or write-through caching"),
            ({"opensearch"}, "CDC / zero-ETL replication into the search index"),
            ({"documentdb"}, "a CQRS read model fed by a change stream"),
            ({"dynamodb"}, "a CQRS read model fed by a change stream"),
            ({"neptune"}, "a CDC or CQRS replication pattern from the write store"),
        ],
    )
    def test_the_replication_pattern_follows_the_role(self, reads: set[str], expected: str):
        assert expected in _replication_recommendation({"aurora_mysql"}, reads)

    def test_a_cache_read_alone_is_not_a_second_owner(self) -> None:
        text = _replication_recommendation({"aurora_mysql"}, {"elasticache", "opensearch"})
        assert "CQRS read model" not in text
