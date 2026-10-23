"""ADR-017 Decision 5: a workload segment is all queries matching one catalog pattern (#703).

One detected pattern is one segment. A segment is a catalog pattern (its id and type come from
the catalog) holding every query that matched it, each once, and a pattern nothing matched
yields no segment. Aurora emits ``aurora-common-10`` (standard CRUD) as one segment holding
every matching query, whatever table each touches (#720).
"""

from __future__ import annotations

from collections import Counter

import pytest

from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql
from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg
from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import Confidence
from src.shared.catalog.patterns import patterns as catalog_patterns
from src.tools.analysis import (
    aurora_common_analysis_tools,
    documentdb_analysis_tools,
    dynamodb_analysis_tools,
)
from src.tools.analysis.aurora_common_pattern_catalog import PATTERN_BY_ID as AURORA_COMMON_PATTERNS
from src.tools.analysis.aurora_mysql_pattern_catalog import MYSQL_PATTERN_BY_ID
from src.tools.analysis.aurora_pg_pattern_catalog import PG_PATTERN_BY_ID
from src.tools.analysis.documentdb_pattern_catalog import PATTERN_BY_ID as DOCUMENTDB_PATTERNS
from src.tools.analysis.dynamodb_pattern_catalog import PATTERN_BY_ID as DYNAMODB_PATTERNS
from src.tools.analysis.opensearch_pattern_catalog import PATTERN_BY_ID as OPENSEARCH_PATTERNS
from src.tools.analysis.pattern_catalog_base import CatalogPattern
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.fixtures.opensearch_pattern_fixtures import get_fulltext_search_fixture

TARGETS = {
    "aurora-postgresql": (
        analyze_for_aurora_pg,
        TargetDatabase.AURORA_POSTGRESQL,
        get_ecommerce_collector_output,
        {**AURORA_COMMON_PATTERNS, **PG_PATTERN_BY_ID},
    ),
    "aurora-mysql": (
        analyze_for_aurora_mysql,
        TargetDatabase.AURORA_MYSQL,
        get_ecommerce_collector_output,
        {**AURORA_COMMON_PATTERNS, **MYSQL_PATTERN_BY_ID},
    ),
    "dynamodb": (
        analyze_for_dynamodb,
        TargetDatabase.DYNAMODB,
        get_ecommerce_collector_output,
        DYNAMODB_PATTERNS,
    ),
    "documentdb": (
        analyze_for_documentdb,
        TargetDatabase.DOCUMENTDB,
        get_ecommerce_collector_output,
        DOCUMENTDB_PATTERNS,
    ),
    "opensearch": (
        analyze_for_opensearch,
        TargetDatabase.OPENSEARCH,
        get_fulltext_search_fixture,
        OPENSEARCH_PATTERNS,
    ),
}


@pytest.fixture(params=sorted(TARGETS))
def analysis(request):
    analyze, target, collector, catalog = TARGETS[request.param]
    collector_output = collector()
    kwargs = {} if request.param == "opensearch" else {"llm_mode": "none"}
    contract, _trace, _diagram = analyze(
        AnalysisInput(job_id="t", collector_output=collector_output, target_database=target),
        **kwargs,
    )
    query_ids = {q["query_id"] for q in collector_output["queries"]["query_patterns"]}
    return request.param, contract.workload_analysis.patterns_detected, catalog, query_ids


def test_the_fixture_detects_patterns(analysis) -> None:
    engine, patterns, _catalog, _query_ids = analysis
    assert patterns, f"{engine}: nothing detected, so the segment checks below prove nothing"


def test_one_pattern_is_one_segment(analysis) -> None:
    engine, patterns, _catalog, _query_ids = analysis
    repeated = [pid for pid, n in Counter(p.pattern_id for p in patterns).items() if n > 1]
    assert not repeated, f"{engine}: pattern emitted as several segments: {repeated}"


def test_a_segment_is_a_catalog_pattern_holding_real_queries_once_each(analysis) -> None:
    engine, patterns, catalog, query_ids = analysis
    for p in patterns:
        assert p.pattern_id in catalog, (engine, p.pattern_id)
        assert p.pattern_type == catalog[p.pattern_id].pattern_type, (engine, p.pattern_id)
        held = p.query_ids or []
        assert len(held) == len(set(held)), (engine, p.pattern_id, "a query is listed twice")
        assert set(held) <= query_ids, (engine, p.pattern_id, "a query not in the collector")


@pytest.mark.parametrize(
    ("module", "catalog"),
    [
        (dynamodb_analysis_tools, DYNAMODB_PATTERNS),
        (documentdb_analysis_tools, DOCUMENTDB_PATTERNS),
    ],
    ids=["dynamodb", "documentdb"],
)
def test_the_segment_holds_every_query_it_was_given_and_none_other(module, catalog) -> None:
    pattern = next(iter(catalog.values()))
    other = list(catalog.values())[1]
    queries = [
        {"query_id": "q1", "tables_accessed": ["t.a"]},
        {"query_id": "q2", "tables_accessed": ["t.b"]},
        {"query_id": "q3", "tables_accessed": ["t.a"]},
    ]

    segments = module._build_patterns_from_matches(
        [(pattern, queries[:2]), (other, []), (catalog[list(catalog)[2]], queries[2:])]
    )

    by_id = {s.pattern_id: s for s in segments}
    assert list(by_id) == [
        pattern.pattern_id,
        list(catalog)[2],
    ]  # no segment for a pattern nothing matched
    assert by_id[pattern.pattern_id].query_ids == ["q1", "q2"]
    assert by_id[pattern.pattern_id].table_ids == ["t.a", "t.b"]
    assert by_id[list(catalog)[2]].query_ids == ["q3"]


def test_aurora_standard_crud_is_one_segment_across_tables() -> None:
    """The ecommerce fixture has CRUD on several tables; all of it is one aurora-common-10."""
    collector_output = get_ecommerce_collector_output()
    contract, trace, _ = analyze_for_aurora_pg(
        AnalysisInput(
            job_id="t",
            collector_output=collector_output,
            target_database=TargetDatabase.AURORA_POSTGRESQL,
        ),
        llm_mode="none",
    )
    patterns = contract.workload_analysis.patterns_detected
    crud = [p for p in patterns if p.pattern_id == "aurora-common-10"]
    assert len(crud) == 1
    assert len(crud[0].table_ids) > 1, "the fixture must exercise more than one table"
    assert len(set(crud[0].query_ids)) == len(crud[0].query_ids)
    summaries = [s for s in trace["pattern_summaries"] if s["pattern_id"] == "aurora-common-10"]
    assert len(summaries) == 1


def _segment_fixture():
    collector_output = {
        "queries": {
            "query_patterns": [
                {"query_id": "a", "tables_accessed": ["t.a"], "calls_per_second": 1.0},
                {"query_id": "b", "tables_accessed": ["t.b"], "calls_per_second": 99.0},
            ]
        }
    }
    segment = aurora_common_analysis_tools.Pattern(
        pattern_id="p-seg",
        pattern_type="seg",
        confidence=Confidence.MEDIUM,
        description="d",
        query_ids=["a", "b"],
        table_ids=["t.a", "t.b"],
    )
    small = aurora_common_analysis_tools.Pattern(
        pattern_id="p-small",
        pattern_type="small",
        confidence=Confidence.MEDIUM,
        description="d",
        query_ids=["a"],
        table_ids=["t.a"],
    )
    return collector_output, segment, small


@pytest.mark.parametrize(
    ("weighting", "expected"),
    [
        # per_table: both patterns weigh 1.0 on t.a (its own calls), so the mean of 100 and 0.
        ("per_table", 50),
        # segment (the default): the segment weighs 100 (every query), the small one 1.0.
        ("segment", int((100 * 100 + 0 * 1) / 101)),
    ],
)
def test_the_declared_segment_weighting_decides_a_tables_weight(weighting, expected) -> None:
    collector_output, segment, small = _segment_fixture()
    catalog = {
        "p-seg": CatalogPattern("p-seg", "seg", "d", 100, segment_weighting=weighting),
        "p-small": CatalogPattern("p-small", "small", "d", 0),
    }
    score = aurora_common_analysis_tools.compute_catalog_pattern_score(
        [segment, small], collector_output, catalog, table_id="t.a"
    )
    assert score == expected


def test_without_a_table_the_whole_segment_always_weighs() -> None:
    collector_output, segment, small = _segment_fixture()
    catalog = {
        "p-seg": CatalogPattern("p-seg", "seg", "d", 100, segment_weighting="per_table"),
        "p-small": CatalogPattern("p-small", "small", "d", 0),
    }
    assert aurora_common_analysis_tools.compute_catalog_pattern_score(
        [segment, small], collector_output, catalog
    ) == int((100 * 100) / 101)


def test_only_standard_crud_declares_per_table_weighting() -> None:
    declared = {p.id for p in catalog_patterns() if p.segment_weighting == "per_table"}
    assert declared == {"standard-crud"}
    assert AURORA_COMMON_PATTERNS["aurora-common-10"].segment_weighting == "per_table"
    assert all(
        p.segment_weighting == "segment"
        for pid, p in AURORA_COMMON_PATTERNS.items()
        if pid != "aurora-common-10"
    )


def test_the_catalog_loader_rejects_an_unknown_segment_weighting() -> None:
    from pydantic import ValidationError

    from src.shared.catalog.patterns import PatternEntry

    base = {"id": "x", "name": "X", "detectors": [], "penalty": 0, "engines": {}}
    assert PatternEntry.model_validate(base).segment_weighting == "segment"
    with pytest.raises(ValidationError):
        PatternEntry.model_validate({**base, "segment_weighting": "per_database"})
