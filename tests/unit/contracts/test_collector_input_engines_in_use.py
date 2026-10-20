"""CollectorInput 3.1 (#963): an optional ``engines_in_use`` declaration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts.collector_input import CollectorInput

BASE = {
    "job_id": "j1",
    "engine": "mysql",
    "cluster_endpoint": "offline",
    "port": 3306,
    "database_name": "d",
    "mode": "offline",
    "offline_config": {"s3_bucket": "b", "s3_key": "k"},
}


def test_absent_means_none_so_an_old_input_loads_unchanged() -> None:
    assert CollectorInput.model_validate(BASE).engines_in_use == []


def test_ids_are_normalised() -> None:
    declared = CollectorInput.model_validate({**BASE, "engines_in_use": ["OpenSearch "]})
    assert declared.engines_in_use == ["opensearch"]


def test_a_malformed_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        CollectorInput.model_validate({**BASE, "engines_in_use": ["open search"]})


def test_the_minor_moved() -> None:
    major, minor = map(int, CollectorInput.model_fields["contract_version"].default.split("."))
    assert (major, minor) >= (3, 1)  # #963 moved it past 3.0
