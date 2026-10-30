# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every analysis writer takes ``contract_version`` from the model (#974, ADR-008 Decision 2).

Each analysis agent used to pass ``contract_version="2.1"`` itself, so a
MINOR bump of ``AnalysisOutputContract``'s default would have left them
writing ``2.1``. This test moves the default to a version no code spells out
and checks that every engine's ``analysis.json``, and the placeholder for an
engine with no agent, says that version.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.agents.analysis.handler import run_analysis
from src.contracts.analysis_output import AnalysisOutputContract
from tests.unit.agents.test_analysis_handler import _mock_store

BUMPED = "2.99"
ENGINES = [
    "dynamodb",
    "documentdb",
    "opensearch",
    "elasticache",
    "aurora_mysql",
    "aurora_postgresql",
    "neptune",  # no agent: the handler writes a placeholder
]


@pytest.fixture
def bumped_default() -> Iterator[None]:
    field = AnalysisOutputContract.model_fields["contract_version"]
    original = field.default
    field.default = BUMPED
    AnalysisOutputContract.model_rebuild(force=True)
    try:
        yield
    finally:
        field.default = original
        AnalysisOutputContract.model_rebuild(force=True)


@pytest.mark.parametrize("engine", ENGINES)
def test_the_written_analysis_says_the_model_version(bumped_default: None, engine: str) -> None:
    store = _mock_store()
    run_analysis("job-001", "mydb", engine, store)
    body = store._written[f"mydb/job-001/analysis-{engine}/analysis.json"]
    assert body["contract_version"] == BUMPED


def test_the_fixture_restores_the_default(bumped_default: None) -> None:
    assert AnalysisOutputContract.model_fields["contract_version"].default == BUMPED
