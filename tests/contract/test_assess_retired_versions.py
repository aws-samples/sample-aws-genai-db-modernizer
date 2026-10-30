# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract versions 1.0, 1.1 and 1.2 are retired (#1641): ``assess()`` supports 1.3 only.

Those versions were initial development versions during the beta, and the release that
ships 1.3 (v0.4.0-beta.3) supports 1.3 only. A request naming one is refused, before
anything runs, with ``RetiredContractVersionError`` (a ``ValueError``), whose message
names the supported version and says why; so is a result document naming one when it is
read back. A request with no version gets 1.3, and an unknown version keeps the refusal
any invalid field gets (``pydantic.ValidationError``). The published schemas describe
1.3 only.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError

import src.assessment as library
from scripts import render_contract_schemas as rcs
from src.assessment import (
    CONTRACT_VERSION,
    AssessmentRequest,
    AssessmentResult,
    RetiredContractVersionError,
    assess,
    load_collection,
)
from src.assessment.models import RETIRED_CONTRACT_VERSIONS
from src.storage.local_store import LocalArtifactStore
from tests.assess_models import models_patched

REPO = Path(__file__).resolve().parents[2]
RETIRED = ("1.0", "1.1", "1.2")


@cache
def _collection() -> dict[str, Any]:
    loaded: dict[str, Any] = load_collection(
        REPO / "docs" / "examples" / "wordpress" / "wordpress.zip"
    )
    return loaded


def _request(version: Any = None, run_through: str = "reality_check") -> dict[str, Any]:
    request: dict[str, Any] = {
        "database_name": "wordpress",
        "collection": _collection(),
        "options": {
            "run_through": run_through,
            "deterministic": run_through in ("analysis", "reality_check"),
        },
    }
    if version is not None:
        request["contract_version"] = version
    return request


@cache
def _result_json() -> str:
    return assess(_request(run_through="analysis")).model_dump_json()


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(rcs.schema_path(name).read_text("utf-8"))
    return loaded


def _check_message(exc: RetiredContractVersionError, version: str) -> None:
    text = str(exc)
    assert f"contract_version {version!r} is no longer supported" in text
    assert 'supports contract 1.3 only (pin contract_version "1.3"' in text
    assert "1.0, 1.1 and 1.2 were beta development versions, removed in v0.4.0-beta.3" in text
    assert "docs/contracts/assess.md, 'Removed: 1.0, 1.1 and 1.2'" in text
    assert exc.version == version


# --------------------------------------------------------------------------- requests


def test_the_retired_versions_are_1_0_1_1_and_1_2() -> None:
    assert RETIRED_CONTRACT_VERSIONS == RETIRED
    assert CONTRACT_VERSION == "1.3"
    assert issubclass(RetiredContractVersionError, ValueError)
    assert "RetiredContractVersionError" in library.__all__


@pytest.mark.parametrize("run_through", ["analysis", "reality_check", "schema_design", "synthesis"])
@pytest.mark.parametrize("version", RETIRED)
def test_assess_refuses_a_retired_version_before_anything_runs(
    version: str, run_through: str, tmp_path: Path
) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    with models_patched(), pytest.raises(RetiredContractVersionError) as raised:
        assess(_request(version, run_through), store=store, job_id="retired1")
    _check_message(raised.value, version)
    assert not any(tmp_path.iterdir()), "a refused request wrote artifacts"


@pytest.mark.parametrize("version", RETIRED)
def test_every_way_to_build_a_request_refuses_a_retired_version(version: str) -> None:
    payload: dict[str, Any] = {
        "contract_version": version,
        "database_name": "shop",
        "collection": {"a": 1},
    }
    builders = (
        lambda: AssessmentRequest(**payload),
        lambda: AssessmentRequest.model_validate(payload),
        lambda: AssessmentRequest.model_validate_json(json.dumps(payload)),
    )
    for build in builders:
        with pytest.raises(RetiredContractVersionError) as raised:
            build()
        _check_message(raised.value, version)


@pytest.mark.parametrize("version", RETIRED)
def test_model_validate_strings_refuses_a_retired_version(version: str) -> None:
    with pytest.raises(RetiredContractVersionError) as raised:
        AssessmentRequest.model_validate_strings(
            {"contract_version": version, "database_name": "shop"}
        )
    _check_message(raised.value, version)


@pytest.mark.parametrize("version", RETIRED)
def test_other_pydantic_paths_carry_the_same_refusal(version: str) -> None:
    """A TypeAdapter, or a model that holds a request, raises pydantic's own
    ValidationError; its contract_version error carries the same message, and the
    typed error itself in its ``ctx``."""
    from pydantic import BaseModel, TypeAdapter

    class Batch(BaseModel):
        request: AssessmentRequest

    payload = {"contract_version": version, "database_name": "shop", "collection": {"a": 1}}
    for validate in (
        lambda: TypeAdapter(AssessmentRequest).validate_python(payload),
        lambda: Batch.model_validate({"request": payload}),
    ):
        with pytest.raises(ValidationError) as raised:
            validate()
        [error] = raised.value.errors()
        assert error["loc"][-1] == "contract_version"
        _check_message(error["ctx"]["error"], version)
        assert "beta development versions" in str(raised.value)


def test_model_validate_keeps_its_context() -> None:
    """The refusal wraps the class call and the validate methods, never ``__init__``, so
    pydantic still hands a ``context`` to every validator."""
    from pydantic import ValidationInfo, field_validator

    seen: list[Any] = []

    class Recording(AssessmentRequest):
        @field_validator("database_name")
        @classmethod
        def _record(cls, value: str, info: ValidationInfo) -> str:
            seen.append(info.context)
            return value

    payload = {"database_name": "shop", "collection": {"a": 1}}
    Recording.model_validate(payload, context={"caller": "test"})
    Recording.model_validate_json(json.dumps(payload), context={"caller": "json"})
    assert seen == [{"caller": "test"}, {"caller": "json"}]
    with pytest.raises(RetiredContractVersionError):
        Recording.model_validate({**payload, "contract_version": "1.1"}, context={"c": 1})


def _unvalidated(version: str) -> list[AssessmentRequest]:
    """Requests that skipped validation and name ``version``."""
    valid = AssessmentRequest.model_validate(_request(run_through="analysis"))
    assigned = valid.model_copy()
    assigned.contract_version = version  # type: ignore[assignment]
    return [
        valid.model_copy(update={"contract_version": version}),
        AssessmentRequest.model_construct(**{**valid.__dict__, "contract_version": version}),
        assigned,
    ]


@pytest.mark.parametrize("version", RETIRED)
def test_assess_refuses_an_unvalidated_request_before_anything_runs(
    version: str, tmp_path: Path
) -> None:
    for i, request in enumerate(_unvalidated(version)):
        store = LocalArtifactStore(base_dir=str(tmp_path / str(i)))
        with pytest.raises(RetiredContractVersionError) as raised:
            assess(request, store=store, job_id="retired2")
        _check_message(raised.value, version)
        assert not (tmp_path / str(i)).exists() or not any((tmp_path / str(i)).iterdir())


def test_assess_refuses_an_unvalidated_unknown_version() -> None:
    for request in _unvalidated("2.0"):
        with pytest.raises(ValueError, match="supports contract 1.3 only") as raised:
            assess(request)
        assert not isinstance(raised.value, RetiredContractVersionError)


def test_a_retired_version_is_refused_even_with_other_errors() -> None:
    """The version is the first thing a caller must fix: it is named even when the
    request has other problems too."""
    with pytest.raises(RetiredContractVersionError):
        AssessmentRequest.model_validate({"contract_version": "1.2", "database_name": ""})


@pytest.mark.parametrize("version", ["1.4", "2.0", "1", "1.3.0", "", 1.3, None])
def test_an_unknown_version_keeps_the_invalid_field_refusal(version: Any) -> None:
    payload = {"contract_version": version, "database_name": "shop", "collection": {"a": 1}}
    with pytest.raises(ValidationError) as raised:
        AssessmentRequest.model_validate(payload)
    assert not isinstance(raised.value, RetiredContractVersionError)
    assert [e["loc"] for e in raised.value.errors()] == [("contract_version",)]


@pytest.mark.parametrize("version", ["1.3", None])
def test_a_1_3_request_or_one_with_no_version_gets_1_3(version: str | None) -> None:
    assert AssessmentRequest.model_validate(_request(version)).contract_version == "1.3"
    if version is None:
        assert json.loads(_result_json())["contract_version"] == "1.3"


def test_a_request_with_a_retired_version_is_refused_by_the_published_schema() -> None:
    validator = Draft202012Validator(_schema("assess_request"))
    assert _schema("assess_request")["properties"]["contract_version"]["const"] == "1.3"
    minimal = {"database_name": "shop", "collection": {"a": 1}}
    validator.validate({**minimal, "contract_version": "1.3"})
    for version in RETIRED:
        with pytest.raises(SchemaError):
            validator.validate({**minimal, "contract_version": version})


# --------------------------------------------------------------------------- results


@pytest.mark.parametrize("version", RETIRED)
def test_a_result_naming_a_retired_version_is_refused(version: str) -> None:
    document = json.loads(_result_json())
    assert document["contract_version"] == "1.3"
    AssessmentResult.model_validate(document)
    document["contract_version"] = version
    with pytest.raises(RetiredContractVersionError) as raised:
        AssessmentResult.model_validate(document)
    _check_message(raised.value, version)
    with pytest.raises(RetiredContractVersionError):
        AssessmentResult.model_validate_json(json.dumps(document))
    with pytest.raises(SchemaError):
        Draft202012Validator(_schema("assess_result")).validate(document)


def test_a_result_is_written_as_1_3_with_every_field() -> None:
    """No field is dropped for an earlier version any more: the result writes every field
    of its model, and reads back byte for byte."""
    text = _result_json()
    result = AssessmentResult.model_validate_json(text)
    assert result.model_dump_json() == text
    document = json.loads(text)
    for statement in document["statements"]:
        assert set(statement) == set(type(result.statements[0]).model_fields)
    Draft202012Validator(_schema("assess_result")).validate(document)
    assert _schema("assess_result")["properties"]["contract_version"]["const"] == "1.3"
