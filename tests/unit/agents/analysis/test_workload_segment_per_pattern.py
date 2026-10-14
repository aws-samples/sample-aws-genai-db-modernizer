"""ADR-017 Decision 5: a workload segment is all queries matching one catalog pattern (#703).

One detected pattern is one segment. A segment is a catalog pattern (its id and type come from
the catalog) holding every query that matched it, each once, and a pattern nothing matched
yields no segment. Aurora is not covered here: it emits ``aurora-common-10`` once per table
(#720).
"""

from __future__ import annotations

from collections import Counter

import pytest

from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb
from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb
from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.tools.analysis import documentdb_analysis_tools, dynamodb_analysis_tools
from src.tools.analysis.documentdb_pattern_catalog import PATTERN_BY_ID as DOCUMENTDB_PATTERNS
from src.tools.analysis.dynamodb_pattern_catalog import PATTERN_BY_ID as DYNAMODB_PATTERNS
from src.tools.analysis.opensearch_pattern_catalog import PATTERN_BY_ID as OPENSEARCH_PATTERNS
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.fixtures.opensearch_pattern_fixtures import get_fulltext_search_fixture

TARGETS = {
    "dynamodb": (
        analyze_for_dynamodb,
        TargetDatabase.dynamodb,
        get_ecommerce_collector_output,
        DYNAMODB_PATTERNS,
    ),
    "documentdb": (
        analyze_for_documentdb,
        TargetDatabase.documentdb,
        get_ecommerce_collector_output,
        DOCUMENTDB_PATTERNS,
    ),
    "opensearch": (
        analyze_for_opensearch,
        TargetDatabase.opensearch,
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
