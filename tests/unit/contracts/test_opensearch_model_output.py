"""The OpenSearch ``AccessPattern.opensearch_dsl`` must never carry a
top-level ``inner_hits`` key.

OpenSearch rejects a top-level ``inner_hits`` with an HTTP 400
``parsing_exception`` -- ``inner_hits`` is only meaningful nested inside a
``nested``/``has_child``/``has_parent`` query clause, or inside ``collapse``
(issue #95).

``opensearch_dsl`` is not always a single JSON object, though: the designer
skill's translation table uses ``GET /<index>/_doc/<id>`` for ``get_by_id``
and NDJSON (or ``POST /<index>/_bulk``) for ``bulk_index``, and the load-test
generator expects those forms. So this check only looks at DSL that parses as
a single JSON object -- anything else (including genuinely malformed JSON)
is passed through untouched; there is nothing structural to check in those
shapes, and rejecting them would contradict the skill and break the load
test.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.contracts.opensearch_model_output import (
    AccessPattern,
    OpenSearchDSLError,
    validate_opensearch_dsl,
)


def _pattern(dsl: dict | str) -> AccessPattern:
    dsl_str = dsl if isinstance(dsl, str) else json.dumps(dsl)
    return AccessPattern(
        pattern_id="OS-AP-1",
        name="Get documents with nested meta by id",
        description="Fetch documents and their nested meta array by id",
        query_ids=["q-1"],
        source_tables=["public.items"],
        source_query="SELECT * FROM items WHERE id IN (?)",
        opensearch_dsl=dsl_str,
        index_or_stream="items",
        operation="search",
        design_rps=1.0,
    )


# ---------------------------------------------------------------------------
# validate_opensearch_dsl — unit tests on the plain function
# ---------------------------------------------------------------------------


def test_top_level_inner_hits_is_rejected():
    """The exact shape from issue #95: inner_hits at the top of the search body."""
    dsl = json.dumps(
        {
            "query": {"terms": {"id": ["1", "2"]}},
            "_source": ["id", "meta"],
            "inner_hits": {"path": {"meta": {"sort": [{"meta.umeta_id": "asc"}], "size": 100}}},
        }
    )
    with pytest.raises(OpenSearchDSLError, match="top-level 'inner_hits'"):
        validate_opensearch_dsl(dsl)


def test_inner_hits_nested_inside_nested_query_is_valid():
    dsl = json.dumps(
        {
            "query": {
                "nested": {
                    "path": "meta",
                    "query": {"match": {"meta.key": "color"}},
                    "inner_hits": {"size": 10},
                }
            }
        }
    )
    validate_opensearch_dsl(dsl)  # does not raise


def test_inner_hits_inside_has_child_query_is_valid():
    dsl = json.dumps(
        {
            "query": {
                "has_child": {
                    "type": "comment",
                    "query": {"match_all": {}},
                    "inner_hits": {},
                }
            }
        }
    )
    validate_opensearch_dsl(dsl)  # does not raise


def test_inner_hits_inside_collapse_is_valid():
    dsl = json.dumps(
        {
            "query": {"match_all": {}},
            "collapse": {
                "field": "user_id",
                "inner_hits": {"name": "most_recent", "size": 5, "sort": [{"@timestamp": "desc"}]},
            },
        }
    )
    validate_opensearch_dsl(dsl)  # does not raise


def test_plain_query_without_inner_hits_is_valid():
    dsl = json.dumps({"query": {"match": {"title": "widget"}}})
    validate_opensearch_dsl(dsl)  # does not raise


def test_empty_object_is_valid():
    validate_opensearch_dsl("{}")  # does not raise


def test_get_by_id_request_line_is_valid():
    """get_by_id's DSL is the literal request line, not JSON (per the skill)."""
    validate_opensearch_dsl("GET /items/_doc/42")  # does not raise


def test_bulk_index_request_line_is_valid():
    """bulk_index's DSL may be the literal request line, not JSON."""
    validate_opensearch_dsl("POST /items/_bulk")  # does not raise


def test_bulk_index_ndjson_is_valid():
    """bulk_index's DSL may be NDJSON: one JSON document per line, no wrapper."""
    ndjson = '{"index": {"_index": "items", "_id": "1"}}\n' '{"id": 1, "name": "widget"}\n'
    validate_opensearch_dsl(ndjson)  # does not raise


def test_genuinely_malformed_json_is_not_rejected():
    """Only dict-shaped DSL is checked; anything else (including malformed
    JSON) is passed through -- rejecting it would contradict get_by_id/
    bulk_index's non-JSON forms, which also fail json.loads."""
    validate_opensearch_dsl("{not json")  # does not raise


# ---------------------------------------------------------------------------
# AccessPattern — the validator wired into the Pydantic contract
# ---------------------------------------------------------------------------


def test_access_pattern_rejects_top_level_inner_hits():
    with pytest.raises(ValidationError, match="top-level 'inner_hits'"):
        _pattern(
            {
                "query": {"terms": {"id": ["1"]}},
                "_source": ["id", "meta"],
                "inner_hits": {"path": {"meta": {}}},
            }
        )


def test_access_pattern_accepts_nested_inner_hits():
    pattern = _pattern(
        {
            "query": {
                "nested": {
                    "path": "meta",
                    "query": {"match": {"meta.key": "color"}},
                    "inner_hits": {"size": 10},
                }
            }
        }
    )
    assert "inner_hits" in pattern.opensearch_dsl


def test_access_pattern_accepts_collapse_inner_hits():
    pattern = _pattern(
        {
            "query": {"match_all": {}},
            "collapse": {"field": "user_id", "inner_hits": {"name": "most_recent"}},
        }
    )
    assert "collapse" in pattern.opensearch_dsl


def test_access_pattern_accepts_get_by_id_request_line():
    pattern = _pattern("GET /items/_doc/42")
    assert pattern.opensearch_dsl == "GET /items/_doc/42"


def test_access_pattern_accepts_bulk_index_ndjson():
    ndjson = '{"index": {"_index": "items", "_id": "1"}}\n{"id": 1, "name": "widget"}\n'
    pattern = _pattern(ndjson)
    assert pattern.opensearch_dsl == ndjson
