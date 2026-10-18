"""The pattern catalog (ADR-031 Decision 8, #492): strict loading, generated constants.

``src/catalog/patterns/<pattern_type>.toml`` is the one place a pattern's per-engine
treatment (fit score, anti-pattern severity, penalty, capability exemption, why) is
written down. This file guards that:

- the loader rejects a malformed pattern file (unknown, missing or mistyped field; a
  field the entry's kind does not allow; an inconsistent penalty rule; an id that is not
  the file name; one catalog id defined twice; a capability that names different engines
  in two files);
- the constants that used to be hand-kept (the per-engine ``*_PATTERNS`` /
  ``*_ANTI_PATTERNS`` lists, the Aurora hint and guidance sentences, OpenSearch's profile
  type sets, ``query_scoring``'s penalty and capability tables and
  ``engine_capabilities``'s engine sets) are now exactly the catalog's view, pinned to
  their pre-catalog values;
- every field is read by a consumer, every detector resolves, every triage signal is one
  triage builds, and ElastiCache's inline patterns match their catalog entries;
- the generated Markdown matrix is fresh (``scripts/render_catalog.py --check``).
"""

from __future__ import annotations

import ast
import importlib.util
import textwrap
from pathlib import Path

import pytest

from src.agents.referee import query_scoring, substring_match, triage
from src.shared import engine_capabilities
from src.shared.catalog import CatalogError
from src.shared.catalog.patterns import (
    EngineSupport,
    PatternEntry,
    load_pattern_catalog,
    patterns,
)
from src.tools.analysis import (
    aurora_common_analysis_tools,
    aurora_common_pattern_catalog,
    aurora_mysql_analysis_tools,
    aurora_mysql_pattern_catalog,
    aurora_pg_analysis_tools,
    aurora_pg_pattern_catalog,
    documentdb_analysis_tools,
    documentdb_pattern_catalog,
    dynamodb_analysis_tools,
    dynamodb_pattern_catalog,
    opensearch_analysis_tools,
    opensearch_pattern_catalog,
    redis_analysis_tools,
)

ROOT = Path(__file__).resolve().parents[3]
RENDER_SCRIPT = ROOT / "scripts" / "render_catalog.py"

ENGINES = (
    "dynamodb",
    "documentdb",
    "opensearch",
    "elasticache",
    "aurora_postgresql",
    "aurora_mysql",
)


def _render_module():
    spec = importlib.util.spec_from_file_location("render_catalog", RENDER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unscored(engines=ENGINES, skip=()) -> str:
    return "".join(
        f'\n[engines.{e}]\nkind = "unscored"\nsupported = true\nwhy = "No effect."\n'
        for e in engines
        if e not in skip
    )


FIT = textwrap.dedent("""\
    id = "example"
    detectors = ["src.tools.analysis.dynamodb_analysis_tools:analyze_dynamodb_use_cases"]
    penalty = 0

    [engines.dynamodb]
    kind = "fit"
    supported = true
    catalog = "dynamodb"
    catalog_id = "dynamodb-99"
    score = 80
    why = "Single-item lookups."
    """) + _unscored(skip=("dynamodb",))


def _write(directory: Path, name: str, text: str) -> None:
    (directory / f"{name}.toml").write_text(text, encoding="utf-8")


def _penalised(kind_for_documentdb: str, extra: str = "", top: str = "") -> str:
    """A search-shaped pattern: OpenSearch reports it, the rest take a 40-point penalty."""
    return (
        textwrap.dedent(f"""\
            id = "example"
            detectors = []
            penalty = 40
            penalty_basis = "cannot_run"
            {top}
            [engines.opensearch]
            kind = "fit"
            supported = true
            catalog = "opensearch"
            catalog_id = "os-99"
            score = 80
            profile = "search"
            why = "Search."

            [engines.documentdb]
            {kind_for_documentdb}
            why = "Documentdb."
            {extra}
            """)
        + "".join(
            f'\n[engines.{e}]\nkind = "penalized"\nsupported = false\nwhy = "Cannot."\n'
            for e in ENGINES
            if e not in ("opensearch", "documentdb")
        )
    )


class TestLoaderIsStrict:
    def test_a_valid_file_loads(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", FIT)
        (entry,) = load_pattern_catalog(tmp_path)
        assert entry.engines["dynamodb"].score == 80
        assert entry.engines["documentdb"].kind == "unscored"

    @pytest.mark.parametrize(
        ("new", "error"),
        [
            pytest.param("score = 0", "cannot carry", id="score-0-on-native"),
            pytest.param("penalty = 0", "cannot carry", id="penalty-0-on-unscored"),
            pytest.param("guidance_names_engine = false", "cannot carry", id="false-flag"),
            pytest.param("concerns = []", "cannot carry", id="empty-list"),
        ],
    )
    def test_a_zero_or_empty_value_is_still_present(self, tmp_path: Path, new, error) -> None:
        """Presence means "written in the file": ``0``, ``0.0`` and ``false`` are values."""
        kind = "native" if new.startswith("score") else "unscored"
        text = FIT.replace(
            '[engines.documentdb]\nkind = "unscored"\nsupported = true\n',
            f'[engines.documentdb]\nkind = "{kind}"\nsupported = true\n{new}\n',
        )
        if kind == "native":
            text = text.replace("penalty = 0\n", 'capability = "text_search"\npenalty = 0\n')
        _write(tmp_path, "example", text)
        with pytest.raises(CatalogError, match=error):
            load_pattern_catalog(tmp_path)

    def test_a_zero_score_and_a_zero_severity_load(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", FIT.replace("score = 80", "score = 0"))
        assert load_pattern_catalog(tmp_path)[0].engines["dynamodb"].score == 0
        anti = (
            FIT.replace('kind = "fit"', 'kind = "anti_pattern"')
            .replace("score = 80", "severity = 0.0")
            .replace("penalty = 0", 'penalty = 0\npenalty_basis = "guidance_only"')
        )
        _write(tmp_path, "example", anti)
        assert load_pattern_catalog(tmp_path)[0].engines["dynamodb"].severity == 0.0

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(
                lambda t: t.replace("penalty = 0", "penalty = 0\nextra = 1"), id="unknown"
            ),
            pytest.param(lambda t: t.replace("score = 80", "score = 80\nweight = 1"), id="entry"),
            pytest.param(lambda t: t.replace("penalty = 0\n", ""), id="missing-penalty"),
            pytest.param(lambda t: t.replace('kind = "fit"', 'kind = "great"'), id="bad-kind"),
            pytest.param(lambda t: t.replace("score = 80", "score = 180"), id="score-range"),
            pytest.param(lambda t: t.replace("score = 80\n", ""), id="fit-without-score"),
            pytest.param(
                lambda t: t.replace("score = 80", 'score = 80\nconfidence = "high"'),
                id="score-and-confidence",
            ),
            pytest.param(
                lambda t: t.replace('catalog_id = "dynamodb-99"\n', ""), id="fit-without-id"
            ),
            pytest.param(lambda t: t.replace("score = 80", "score = 80\nseverity = 0.5"), id="sev"),
            pytest.param(
                lambda t: t.replace(
                    'kind = "fit"\nsupported = true', 'kind = "fit"\nsupported = false'
                ),
                id="supported-mismatch",
            ),
            pytest.param(
                lambda t: t.replace("score = 80", 'score = 80\nprofile = "search"'),
                id="profile-off-opensearch",
            ),
            pytest.param(
                lambda t: t.replace('kind = "unscored"', 'kind = "native"', 1), id="native-no-cap"
            ),
            pytest.param(
                lambda t: t.replace(
                    'kind = "unscored"\nsupported = true',
                    'kind = "penalized"\nsupported = false',
                    1,
                ),
                id="penalized-without-penalty",
            ),
            pytest.param(lambda t: t.replace('"src.tools', '"src tools'), id="bad-detector"),
            pytest.param(lambda t: t.replace('why = "Single-item lookups."', 'why = ""'), id="why"),
            pytest.param(
                lambda t: t.replace("penalty = 0", 'penalty = 0\npenalty_aliases = ["x"]'),
                id="alias-without-penalty",
            ),
            pytest.param(lambda t: t + "[engines.mongodb]\nkind = 'unscored'\n", id="engine"),
        ],
    )
    def test_a_malformed_file_fails_at_load(self, tmp_path: Path, edit) -> None:
        _write(tmp_path, "example", edit(FIT))
        with pytest.raises(CatalogError, match="example.toml"):
            load_pattern_catalog(tmp_path)

    def test_an_unknown_engine_id_fails(self, tmp_path: Path) -> None:
        text = FIT + '\n[engines.mongodb]\nkind = "unscored"\nsupported = true\nwhy = "x"\n'
        _write(tmp_path, "example", text)
        with pytest.raises(CatalogError, match="not a catalog engine, or a legacy id"):
            load_pattern_catalog(tmp_path)

    def test_a_future_engine_takes_only_future_entries(self, tmp_path: Path) -> None:
        future = '\n[engines.neptune]\nkind = "future"\nsupported = false\nwhy = "Not yet."\n'
        _write(tmp_path, "example", FIT + future)
        (entry,) = load_pattern_catalog(tmp_path)
        assert entry.engines["neptune"].kind == "future"
        _write(
            tmp_path,
            "example",
            FIT + future.replace('"future"', '"unscored"').replace("false", "true"),
        )
        with pytest.raises(CatalogError, match="future engine only"):
            load_pattern_catalog(tmp_path)
        _write(tmp_path, "example", FIT + future.replace("false", "true"))
        with pytest.raises(CatalogError, match="a future entry is never supported"):
            load_pattern_catalog(tmp_path)
        active = FIT.replace(
            '[engines.documentdb]\nkind = "unscored"\nsupported = true',
            '[engines.documentdb]\nkind = "future"\nsupported = false',
        )
        _write(tmp_path, "example", active)
        with pytest.raises(CatalogError, match="future engine only"):
            load_pattern_catalog(tmp_path)

    def test_a_penalty_reaches_every_engine_that_cannot_run_a_fit(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", _penalised('kind = "penalized"\nsupported = false'))
        (entry,) = load_pattern_catalog(tmp_path)
        assert entry.engines["dynamodb"].kind == "penalized"
        _write(tmp_path, "example", _penalised('kind = "unscored"\nsupported = true'))
        with pytest.raises(CatalogError, match="should be penalized"):
            load_pattern_catalog(tmp_path)

    def test_a_redesign_needs_a_capability_and_a_smaller_penalty(self, tmp_path: Path) -> None:
        native = 'kind = "native"\nsupported = true'
        top = 'capability = "text_search"\n'
        _write(tmp_path, "example", _penalised(native, top=top))
        assert load_pattern_catalog(tmp_path)[0].capability == "text_search"
        redesign = 'kind = "redesign"\nsupported = true\npenalty = 50'
        _write(tmp_path, "example", _penalised(redesign, top=top))
        with pytest.raises(CatalogError, match="redesign penalty"):
            load_pattern_catalog(tmp_path)
        _write(tmp_path, "example", _penalised(native))
        with pytest.raises(CatalogError, match="needs the pattern's capability"):
            load_pattern_catalog(tmp_path)

    def test_the_id_must_be_the_file_name(self, tmp_path: Path) -> None:
        _write(tmp_path, "other", FIT)
        with pytest.raises(CatalogError, match="does not match the file name"):
            load_pattern_catalog(tmp_path)

    def test_a_catalog_id_is_defined_once(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", FIT)
        _write(tmp_path, "second", FIT.replace('id = "example"', 'id = "second"'))
        with pytest.raises(CatalogError, match="dynamodb-99 is defined differently"):
            load_pattern_catalog(tmp_path)

    def test_an_alias_cannot_shadow_a_pattern(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", _penalised('kind = "penalized"\nsupported = false'))
        other = (
            _penalised(
                'kind = "penalized"\nsupported = false', top='penalty_aliases = ["example"]\n'
            )
            .replace('id = "example"', 'id = "second"')
            .replace("os-99", "os-98")
        )
        _write(tmp_path, "second", other)
        with pytest.raises(CatalogError, match="already used"):
            load_pattern_catalog(tmp_path)

    def test_a_capability_names_the_same_engines_everywhere(self, tmp_path: Path) -> None:
        top = 'capability = "text_search"\n'
        _write(tmp_path, "example", _penalised('kind = "native"\nsupported = true', top=top))
        other = (
            _penalised('kind = "native"\nsupported = true', top=top)
            .replace('id = "example"', 'id = "second"')
            .replace("os-99", "os-98")
            .replace(
                '[engines.dynamodb]\nkind = "penalized"\nsupported = false',
                '[engines.dynamodb]\nkind = "native"\nsupported = true',
            )
        )
        _write(tmp_path, "second", other)
        with pytest.raises(CatalogError, match="capability 'text_search' names"):
            load_pattern_catalog(tmp_path)

    def test_a_toml_syntax_error_names_the_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "broken", 'id = "broken\n')
        with pytest.raises(CatalogError, match="broken.toml"):
            load_pattern_catalog(tmp_path)

    def test_an_empty_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(CatalogError, match="no pattern files"):
            load_pattern_catalog(tmp_path)

    def test_the_shipped_catalog_loads(self) -> None:
        assert load_pattern_catalog() == patterns()


def _ids(entries, field: str) -> list[tuple[str, str, object]]:
    return [(e.pattern_id, e.pattern_type, getattr(e, field)) for e in entries]


class TestConstantsAreTheCatalogsView:
    """Each formerly hand-kept table, pinned to its pre-catalog routing values.

    Order matters: the analysis tools index into these lists (``DYNAMODB_PATTERNS[0]``).
    A deliberate change here changes analysis scores, so it moves queries: report it.
    """

    def test_dynamodb(self) -> None:
        assert _ids(dynamodb_pattern_catalog.DYNAMODB_PATTERNS, "base_score") == [
            ("dynamodb-01", "key-value-lookup", 100),
            ("dynamodb-02", "range-query", 90),
            ("dynamodb-03", "write-heavy-ingestion", 85),
            ("dynamodb-04", "time-series-event-log", 85),
            ("dynamodb-05", "metadata-config-store", 90),
            ("dynamodb-06", "session-store", 90),
            ("dynamodb-07a", "bounded-parent-child", 75),
            ("dynamodb-07b", "many-to-many-junction", 65),
            ("dynamodb-07c", "co-accessed-tables", 70),
            ("dynamodb-07d", "adjacency-list", 60),
        ]
        assert _ids(dynamodb_pattern_catalog.DYNAMODB_ANTI_PATTERNS, "severity_weight") == [
            ("dynamodb-ap-01", "frequent-full-scan", 0.5),
            ("dynamodb-ap-02", "complex-aggregation", 0.7),
            ("dynamodb-ap-03", "unbounded-result-set", 0.2),
            ("dynamodb-ap-04", "wide-transaction", 0.3),
            ("dynamodb-ap-05", "ordered-scan", 0.5),
        ]

    def test_documentdb(self) -> None:
        assert _ids(documentdb_pattern_catalog.DOCUMENTDB_PATTERNS, "base_score") == [
            # #967: content-management, product-catalog and polymorphic-data are one
            # variant-attributes pattern (80), flexible-schema is the JSON pattern, and
            # write-time-aggregation is a cross-engine design note, not a fit.
            ("documentdb-01", "variant-attributes", 80),
            ("documentdb-04", "nested-document", 85),
            ("documentdb-05", "aggregation-pipeline", 75),
            ("documentdb-06", "json-document-query", 80),
            ("documentdb-07", "extended-reference", 85),
        ]
        assert _ids(documentdb_pattern_catalog.DOCUMENTDB_ANTI_PATTERNS, "severity_weight") == [
            ("documentdb-anti-01", "heavy-cross-collection-joins", 0.6),
            ("documentdb-anti-02", "unbounded-array-growth", 0.7),
            ("documentdb-anti-03", "high-frequency-small-updates", 0.4),
            ("documentdb-anti-04", "graph-traversal-hierarchy", 0.8),
            ("documentdb-anti-05", "negation-heavy-queries", 0.4),
            ("documentdb-anti-06", "faceted-analytics", 0.5),
            ("documentdb-anti-07", "window-functions", 0.5),
            ("documentdb-anti-08", "correlated-subquery-joins", 0.7),
        ]

    def test_opensearch(self) -> None:
        assert _ids(opensearch_pattern_catalog.OPENSEARCH_PATTERNS, "base_score") == [
            ("os-01", "full-text-search", 95),
            ("os-02", "wildcard-search", 80),
            ("os-03", "regex-search", 75),
            ("os-04", "fuzzy-search", 85),
            ("os-05", "time-range-query", 85),
            ("os-06", "time-aggregation", 90),
            ("os-07", "high-ingest", 90),
        ]
        assert _ids(opensearch_pattern_catalog.OPENSEARCH_ANTI_PATTERNS, "severity_weight") == [
            ("os-ap-01", "multi-index-joins", 0.8),
            ("os-ap-02", "acid-transactions", 0.6),
            ("os-ap-03", "audit-columns-only", 0.1),
        ]
        assert opensearch_pattern_catalog.SEARCH_PATTERN_TYPES == {
            "full-text-search",
            "wildcard-search",
            "regex-search",
            "fuzzy-search",
        }
        assert opensearch_pattern_catalog.TIMESERIES_PATTERN_TYPES == {
            "time-range-query",
            "time-aggregation",
            "high-ingest",
        }

    def test_aurora(self) -> None:
        common = aurora_common_pattern_catalog
        assert _ids(common.AURORA_COMMON_PATTERNS, "base_score") == [
            ("aurora-common-01", "complex-join", 90),
            ("aurora-common-02", "aggregation-analytics", 85),
            ("aurora-common-03", "transactional-write", 90),
            ("aurora-common-04", "referential-integrity", 85),
            ("aurora-common-05", "reporting-query", 80),
            ("aurora-common-06", "correlated-subquery", 80),
            ("aurora-common-07", "multi-row-mutation", 75),
            ("aurora-common-08", "pagination", 85),
            ("aurora-common-09", "simple-join", 70),
            ("aurora-common-10", "standard-crud", 60),
            ("aurora-common-11", "batch-in-list", 70),
            ("aurora-common-12", "key-value-lookup", 80),
        ]
        assert _ids(common.AURORA_COMMON_ANTI_PATTERNS, "severity_weight") == [
            ("aurora-anti-01", "high-frequency-pk-lookup", 0.8),
            ("aurora-anti-02", "simple-cache-read", 0.7),
            ("aurora-anti-03", "full-text-search-candidate", 0.6),
            ("aurora-anti-04", "unbounded-time-series-ingest", 0.4),
            ("aurora-anti-05", "no-relational-need", 0.6),
            ("aurora-anti-06", "single-access-pattern-table", 0.5),
            ("aurora-anti-07", "high-volume-text-search", 0.7),
        ]
        assert _ids(aurora_pg_pattern_catalog.AURORA_PG_PATTERNS, "base_score") == [
            ("aurora-pg-01", "cte-recursive", 95),
            ("aurora-pg-02", "window-function", 90),
            ("aurora-pg-03", "json-document-query", 85),  # #967: the merged JSON pattern
            ("aurora-pg-04", "array-operations", 80),
            ("aurora-pg-05", "full-text-native", 75),
            ("aurora-pg-06", "lateral-join", 85),
            ("aurora-pg-07", "upsert-conflict", 80),
            # #920: the SQL features Aurora MySQL has too, each with its own fit.
            ("aurora-pg-08", "stored-routine", 70),
            ("aurora-pg-09", "multi-table-update", 75),
            ("aurora-pg-10", "group-concat-pivot", 75),
        ]
        assert _ids(aurora_mysql_pattern_catalog.AURORA_MYSQL_PATTERNS, "base_score") == [
            ("aurora-mysql-01", "json-document-query", 85),
            ("aurora-mysql-02", "fulltext-search", 80),
            ("aurora-mysql-03", "upsert-duplicate-key", 80),
            ("aurora-mysql-04", "group-concat-pivot", 75),
            ("aurora-mysql-05", "stored-routine", 70),
            ("aurora-mysql-06", "multi-table-update", 80),
            # #920: MySQL 8.0's window functions, recursive CTEs and LATERAL.
            ("aurora-mysql-07", "window-function", 85),
            ("aurora-mysql-08", "cte-recursive", 90),
            ("aurora-mysql-09", "lateral-join", 80),
        ]

    def test_aurora_hints_compose_the_anti_pattern_descriptions(self) -> None:
        common = aurora_common_pattern_catalog
        assert len(common.CATALOG_ENGINE_HINTS) == 7
        assert len(common.CATALOG_ENGINE_GUIDANCE) == 7
        for ap, hint in zip(
            common.AURORA_COMMON_ANTI_PATTERNS, common.CATALOG_ENGINE_HINTS, strict=True
        ):
            assert ap.description.endswith(" " + hint)
            assert ap.guidance in common.CATALOG_ENGINE_GUIDANCE
        assert common.NO_RELATIONAL_NEED_ENGINE_HINT.startswith("This workload has no structural")
        assert common.TIME_SERIES_INGEST_GUIDANCE.startswith("Set a retention rule")

    def test_query_scoring_tables(self) -> None:
        search = {"aurora_postgresql", "aurora_mysql", "documentdb"}
        assert query_scoring.ANTI_PATTERN_PENALTIES == {
            "text_search": 40,
            "wildcard-search": 40,
            "full-text-search": 40,
            "regex-search": 40,
            "fuzzy-search": 40,
            "complex-aggregation": 30,
            "complex_aggregation": 30,
            "multi-index-joins": 30,
            "acid-transactions": 40,
            "high-frequency-pk-lookup": 20,
            "simple-cache-read": 20,
            "no-relational-need": 20,
            "single-access-pattern-table": 20,
            "high-volume-text-search": 20,
            "wide-transaction": 10,
            # #920: the anti-patterns that cost 0 before the one penalty rule.
            "frequent-full-scan": 20,
            "ordered-scan": 20,
            "unbounded-result-set": 10,
            "heavy-cross-collection-joins": 20,
            "correlated-subquery-joins": 20,
            "graph-traversal-hierarchy": 10,
            "window-functions": 10,
            "unbounded-array-growth": 10,
            "large-result-sets": 10,
        }
        assert query_scoring.CAPABILITY_SIGNAL_ENGINES == {
            "acid-transactions": search | {"dynamodb"},
            "wildcard-search": search,
            "full-text-search": search,
            "regex-search": search,
            "fuzzy-search": {"aurora_postgresql"},
        }
        # ADR-031 Decision 3 (#493): DynamoDB's transactions are native; a write too
        # wide for one is the wide-transaction anti-pattern above, not a redesign.
        assert query_scoring.CAPABILITY_REDESIGN_PENALTIES == {}
        assert query_scoring.CAPABILITY_REDESIGN_NOTES == {}
        assert set(query_scoring.CAPABILITY_EXEMPT_NOTES) == {
            "wildcard-search",
            "full-text-search",
            "regex-search",
            "fuzzy-search",
        }
        assert len(set(query_scoring.CAPABILITY_EXEMPT_NOTES.values())) == 1

    def test_engine_capability_sets(self) -> None:
        search = {"aurora_postgresql", "aurora_mysql", "documentdb"}
        # ADR-031 Decision 3 (#493): DynamoDB's transactions are native.
        assert engine_capabilities.ACID_TRANSACTION_ENGINES == search | {"dynamodb"}
        assert engine_capabilities.TEXT_SEARCH_ENGINES == search
        assert engine_capabilities.FUZZY_SEARCH_ENGINES == {"aurora_postgresql"}


def _attribute_names(paths: list[Path]) -> set[str]:
    """Attribute names read in ``paths``, outside any class body (the models themselves)."""
    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        in_classes = {
            id(n) for c in ast.walk(tree) if isinstance(c, ast.ClassDef) for n in ast.walk(c)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and id(node) not in in_classes:
                names.add(node.attr)
    return names


class TestEveryFieldIsUsed:
    def test_every_field_is_read_outside_the_model_definitions(self) -> None:
        # The loader's own derived views count (they build query_scoring's tables); the
        # model classes, whose validators read every field, do not.
        consumers = [*(ROOT / "src").rglob("*.py"), RENDER_SCRIPT]
        read = _attribute_names(consumers)
        fields = (set(PatternEntry.model_fields) - {"engines"}) | set(EngineSupport.model_fields)
        unused = sorted(fields - read)
        assert not unused, f"pattern fields nothing reads (remove them or use them): {unused}"


# The analysis modules a detector may live in (imported here, not by name at run time).
DETECTOR_MODULES = {
    m.__name__: m
    for m in (
        aurora_common_analysis_tools,
        aurora_mysql_analysis_tools,
        aurora_pg_analysis_tools,
        documentdb_analysis_tools,
        dynamodb_analysis_tools,
        opensearch_analysis_tools,
        redis_analysis_tools,
        # The scorer's own detector for a pattern no analysis reports (#493): it reads
        # the facts layer's leading-wildcard fact, never a score.
        substring_match,
    )
}

# Catalog entries no analysis emits today (kept so the catalog says so out loud):
# two catalog-only anti-patterns, a penalty key no analysis reports, and the two call-rate
# entries that are not fits (#920: call rate is cost evidence, #494).
UNDETECTED = {
    "unbounded-array-growth",
    "high-frequency-small-updates",
    "text_search",
    "low-frequency-read",
    "low-frequency-write",
    # A cross-engine design note since #967: no analysis reports it as a fit.
    "write-time-aggregation",
    # Catalog-only: the access-pattern class "ad hoc filter" reads it (#728); the
    # classifier detects the class from the SQL facade's facts.
    "secondary-attribute-filter",
}


# The hard capability gate's name for each pattern ``capability`` it backs. The gate has
# no fuzzy-match capability, so ``fuzzy_search`` is not gate-backed.
GATE_CAPABILITY = {"acid_transactions": "multi_doc_acid", "text_search": "text_pattern_match"}
# (pattern, engine) pairs where the catalog and the gate disagree on purpose. None since
# #493 gave DynamoDB multi_doc_acid (ADR-031 Decision 3: TransactWriteItems is ACID).
GATE_DISAGREEMENTS: set[tuple[str, str]] = set()


class TestSupportedMatchesTheCapabilityGate:
    def test_every_gate_backed_pattern_agrees_with_capability_registry(self) -> None:
        from src.agents.referee.capability_registry import can_engine_serve_capability

        checked: set[str] = set()
        wrong: list[str] = []
        for entry in patterns():
            gate_cap = GATE_CAPABILITY.get(entry.capability or "")
            if gate_cap is None:
                continue
            checked.add(entry.id)
            for engine, support in entry.engines.items():
                if support.kind == "future" or (entry.id, engine) in GATE_DISAGREEMENTS:
                    continue
                gate = can_engine_serve_capability(engine, [gate_cap])
                if support.supported is not gate:
                    wrong.append(f"{entry.id}.toml [engines.{engine}]: gate says {gate}")
        assert checked == {
            "acid-transactions",
            "wildcard-search",
            "full-text-search",
            "regex-search",
        }
        assert not wrong, wrong

    def test_the_listed_disagreements_are_real(self) -> None:
        from src.agents.referee.capability_registry import can_engine_serve_capability

        for pattern_id, engine in GATE_DISAGREEMENTS:
            support = next(p for p in patterns() if p.id == pattern_id).engines[engine]
            gate_cap = GATE_CAPABILITY[next(p for p in patterns() if p.id == pattern_id).capability]
            assert support.supported is not can_engine_serve_capability(engine, [gate_cap])


class TestCatalogMatchesTheCode:
    def test_every_detector_resolves_to_a_function(self) -> None:
        for entry in patterns():
            for ref in entry.detectors:
                module, _, symbol = ref.partition(":")
                assert module in DETECTOR_MODULES, ref
                assert callable(getattr(DETECTOR_MODULES[module], symbol, None)), ref

    def test_the_undetected_patterns_are_the_known_ones(self) -> None:
        assert {e.id for e in patterns() if not e.detectors} == UNDETECTED

    def test_every_triage_signal_is_one_triage_builds(self) -> None:
        built = _triage_signal_names()
        for entry in patterns():
            if entry.triage_signal:
                assert entry.triage_signal in built, (entry.id, entry.triage_signal)

    def test_elasticache_inline_patterns_match_their_catalog_entries(self) -> None:
        """The redis analysis builds its patterns inline (no catalog constant yet)."""
        found = _inline_redis_patterns()
        by_id = {
            s.catalog_id: (e.id, s)
            for e in patterns()
            for s in e.engines.values()
            if s.catalog == "elasticache"
        }
        assert set(found) == set(by_id)
        for catalog_id, (pattern_type, extra) in found.items():
            entry_type, support = by_id[catalog_id]
            assert entry_type == pattern_type, catalog_id
            if "severity_weight" in extra:
                assert support.severity == extra["severity_weight"]
            if "confidence" in extra:
                assert support.confidence == extra["confidence"]


def _triage_signal_names() -> set[str]:
    tree = ast.parse(Path(triage.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and len(node.args) > 1:
            names.add(str(node.args[0].value))
        for kw in node.keywords:
            if kw.arg == "signal" and isinstance(kw.value, ast.Constant):
                names.add(str(kw.value.value))
    assert names, "parsed no triage signals"
    return names


def _inline_redis_patterns() -> dict[str, tuple[str, dict]]:
    tree = ast.parse(Path(redis_analysis_tools.__file__).read_text(encoding="utf-8"))
    found: dict[str, tuple[str, dict]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in ("Pattern", "AntiPattern"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        prefix = "anti_" if node.func.id == "AntiPattern" else ""
        pid = ast.literal_eval(kw[f"{prefix}pattern_id"])
        ptype = ast.literal_eval(kw[f"{prefix}pattern_type"])
        extra: dict = {}
        if "severity_weight" in kw:
            extra["severity_weight"] = ast.literal_eval(kw["severity_weight"])
        conf = kw.get("confidence")
        if isinstance(conf, ast.Attribute):
            extra["confidence"] = conf.attr.lower()
        found[pid] = (ptype, extra)
    return found


class TestGeneratedMarkdown:
    def test_the_committed_matrix_is_fresh(self) -> None:
        module = _render_module()
        from src.shared.catalog import engines

        expected = module.render_md(engines(), patterns())
        assert module.MD_OUT.read_text(encoding="utf-8") == expected, (
            "docs/architecture/rule-catalog.md is stale. "
            "Run: uv run python scripts/render_catalog.py"
        )

    def test_every_pattern_has_a_row(self) -> None:
        text = (ROOT / "docs" / "architecture" / "rule-catalog.md").read_text(encoding="utf-8")
        for entry in patterns():
            assert f"| [`{entry.id}`](#{entry.id}) |" in text
            assert f"\n### {entry.id}\n" in text
