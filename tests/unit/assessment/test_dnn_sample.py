# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The dnn sample (#1929): a SQL Server collection from a real application, DNN
Platform with Hotcakes Commerce under load, assessed once (its run takes several seconds,
so it stays out of the sample-parametrized tests; see ``REAL_SAMPLES``)."""

from __future__ import annotations

from typing import Any

import pytest

from src.storage.assignment_versioning import resolve_effective_assignment_version
from tests.unit.assessment.conftest import (
    PLACED_JOB,
    database_name,
    placed_run,
    sample_collection,
)

NAME = "dnn"


@pytest.fixture(scope="module")
def placed() -> tuple[Any, Any, Any]:
    return placed_run(NAME)


def test_the_sample_is_the_recorded_collection() -> None:
    """The figures the folder's README states; a re-recorded sample updates both."""
    raw = sample_collection(NAME)
    assert database_name(NAME) == "DNN"
    assert raw["metadata"]["engine"] == "sqlserver"
    assert (len(raw["tables"]), len(raw["procedures"]), len(raw["queries"])) == (234, 861, 234)


def test_it_is_assessed_as_a_sql_server_source(placed: tuple[Any, Any, Any]) -> None:
    result, state, store = placed
    assert result.summary.source_engine == "sqlserver"
    effective = resolve_effective_assignment_version(store, NAME, PLACED_JOB)
    assert result.summary.assignment_version == effective == state.assignment["version"]
    assert result.summary.tables


def test_a_heterogeneous_source_gets_routings_aurora_choice(placed: tuple[Any, Any, Any]) -> None:
    result, state, _ = placed
    choice = state.assignment["aurora_engine_choice"]
    assert choice["engine"] in {"aurora_postgresql", "aurora_mysql"}
    assert result.summary.relational_engine.engine == choice["engine"]
    assert result.summary.relational_engine.reason == choice["reason"]
