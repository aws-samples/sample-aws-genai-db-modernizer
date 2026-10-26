"""The request model refuses what ``assess()`` cannot run (#1182)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.assessment import AssessmentOptions, AssessmentRequest, assess

RAW = {"metadata": {"database_name": "db", "engine": "mysql"}, "queries": [], "tables": []}


def _request(**overrides: Any) -> dict[str, Any]:
    return {"database_name": "shop", "collection": RAW, **overrides}


def test_defaults() -> None:
    req = AssessmentRequest.model_validate(_request())
    assert req.contract_version == "1.1"
    assert req.options == AssessmentOptions(
        target_engines=None,
        run_through="reality_check",
        deterministic=True,
        include_artifacts=False,
    )
    assert req.scope is None


@pytest.mark.parametrize(
    "engines",
    [
        ["dynamodb", "cassandra"],  # not an engine
        ["neptune"],  # a planned engine no analysis covers
        ["aurora"],  # the legacy id
        [],  # a scope names at least one engine
        "dynamodb",  # one string, not a list
    ],
)
def test_refused_engine_scope(engines: Any) -> None:
    with pytest.raises(ValidationError, match="target_engines"):
        AssessmentRequest.model_validate(_request(options={"target_engines": engines}))


def test_engine_scope_is_normalised() -> None:
    req = AssessmentRequest.model_validate(
        _request(options={"target_engines": [" DynamoDB", "aurora_mysql", "dynamodb"]})
    )
    assert req.options.target_engines == ["aurora_mysql", "dynamodb"]


@pytest.mark.parametrize(
    "payload",
    [
        _request(source_engine="sqlserver"),
        _request(options={"stop_after": "analysis"}),
        _request(options={"relational_baseline": "aurora_postgresql"}),
        _request(options={"ruleset_version": "2026.10"}),
        _request(scope={"tables": [], "unknown": 1}),
    ],
)
def test_extra_fields_are_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AssessmentRequest.model_validate(payload)


@pytest.mark.parametrize("name", ["", "   ", "../etc", "a/b", "-x", "db.name"])
def test_bad_database_name(name: str) -> None:
    with pytest.raises(ValidationError, match="database_name"):
        AssessmentRequest.model_validate(_request(database_name=name))


def test_missing_or_empty_collection() -> None:
    with pytest.raises(ValidationError, match="collection"):
        AssessmentRequest.model_validate({"database_name": "shop"})
    with pytest.raises(ValidationError, match="collection is empty"):
        AssessmentRequest.model_validate(_request(collection={}))


def test_other_contract_version_is_refused() -> None:
    with pytest.raises(ValidationError, match="contract_version"):
        AssessmentRequest.model_validate(_request(contract_version="2.0"))


@pytest.mark.parametrize("run_through", ["schema_design", "synthesis"])
def test_later_phases_need_a_model(run_through: str) -> None:
    with pytest.raises(ValidationError, match="needs a model"):
        AssessmentRequest.model_validate(_request(options={"run_through": run_through}))
    request = AssessmentRequest.model_validate(
        _request(options={"run_through": run_through, "deterministic": False})
    )
    assert request.options.run_through == run_through


def test_unknown_run_through() -> None:
    with pytest.raises(ValidationError, match="run_through"):
        AssessmentRequest.model_validate(_request(options={"run_through": "assignment"}))


def test_assess_refuses_before_running(tmp_path: Any) -> None:
    """A refused request writes nothing to the store."""
    from src.storage.local_store import LocalArtifactStore

    with pytest.raises(ValidationError):
        assess(_request(options={"target_engines": ["nope"]}), store=LocalArtifactStore(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_assess_refuses_a_bad_job_id(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="job_id"):
        assess(_request(), job_id="../x")
