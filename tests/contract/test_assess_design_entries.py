# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The design block's published standard covers every designed engine (#1513).

docs/contracts/assess-design-block.md defines an entry for each of the six engines the
pipeline designs. Each one has a consumer fixture (``fixtures/assess_consumer_fixtures.json``)
whose designed result carries it, and that result validates against the published JSON
Schema and reads back through the models unchanged. A designed result with no OpenSearch
design has no OpenSearch entry, and validates. The runs are the consumer contract tests'
own (model phases patched).
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from src.assessment import AssessmentResult
from tests.contract.test_assess_consumer_contract import FIXTURES, _result_schema, _run

#: The engines the published standard defines an entry for.
STANDARD_ENGINES = (
    "dynamodb",
    "aurora_mysql",
    "aurora_postgresql",
    "documentdb",
    "opensearch",
    "elasticache",
)


def _designed(name: str) -> dict:
    return _run(name, "schema_design", False)


def _validates(document: dict) -> None:
    Draft202012Validator(_result_schema()).validate(document)
    text = json.dumps(document)
    assert json.loads(AssessmentResult.model_validate_json(text).model_dump_json()) == document


def test_every_standard_engine_has_a_fixture_whose_design_validates() -> None:
    covered: dict[str, str] = {}
    for name in FIXTURES:
        document = _designed(name)
        for entry in document["designs"] or []:
            covered.setdefault(entry["engine"], name)
    assert sorted(covered) == sorted(STANDARD_ENGINES)
    for name in sorted(set(covered.values())):
        _validates(_designed(name))


def test_a_result_with_the_opensearch_entry_validates() -> None:
    document = _designed("wordpress_opensearch")
    (entry,) = [d for d in document["designs"] if d["engine"] == "opensearch"]
    assert entry["indexes"] and entry["data_streams"]
    _validates(document)


@pytest.mark.passes_on_main
def test_a_consumer_fixture_without_an_opensearch_design_validates() -> None:
    """The fixtures that design no OpenSearch carry no OpenSearch entry; their results
    validate against the schema with the entry in it."""
    for name in ("wordpress", "wordpress_documentdb"):
        document = _designed(name)
        assert document["designs"]
        assert "opensearch" not in {d["engine"] for d in document["designs"]}
        _validates(document)
