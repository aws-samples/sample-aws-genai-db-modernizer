# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract 1.4 of the public ``assess()`` contract, and the 1.3 callers it keeps.

1.4 adds one optional request field, ``routine_sources`` (#2022); the result is
unchanged. The version a request naming none gets is derived, never kept by hand: the
newest version a release recorded (``src/assessment/released_contract.json``, written
with ``tests/contract/assess_released_since_1_3.json`` by the release step's
``--write-released <tag>``). Today that is v0.4.0-beta.3's 1.3, so 1.4 is not in a
release yet and is read only when pinned; once the next release records it, it is the
default with no code change.

- the default is the recorded version; before the record 1.4 is read only when pinned,
  and after a simulated record it is the default;
- a 1.3 request and its result validate against the schemas v0.4.0-beta.3 shipped
  (``tests/fixtures/assess_{request,result}_v1_3.schema.json``, the tag's bytes);
- a 1.3 request that names a 1.4 field is refused.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404 -- git show of this repository's own tag, fixed argv
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from scripts import render_contract_schemas as rcs
from scripts.contract_schema_snapshot import (
    RELEASED_SNAPSHOT_PATH,
    RELEASED_VERSION_PATH,
    build_public_snapshot,
    released_document,
    released_version_of,
    write_released_version,
)
from src.assessment import (
    CONTRACT_VERSION,
    AssessmentRequest,
    AssessmentResult,
    RoutineSourcesError,
    assess,
    models,
)
from src.assessment.models import (
    SUPPORTED_CONTRACT_VERSIONS,
    default_contract_version,
    unreleased_contract_versions,
)
from tests.unit.assessment.conftest import sample_collection

REPO = Path(__file__).resolve().parents[2]
SHIPPED_TAG = "v0.4.0-beta.3"
_FIXTURES = REPO / "tests" / "fixtures"
#: The schemas v0.4.0-beta.3 shipped, copied byte for byte, with each file's SHA-256 (so
#: the copies are checked where the tag is not fetched).
SHIPPED_1_3 = {
    "assess_request.v1.schema.json": _FIXTURES / "assess_request_v1_3.schema.json",
    "assess_result.v1.schema.json": _FIXTURES / "assess_result_v1_3.schema.json",
}
SHIPPED_SHA256 = {
    "assess_request.v1.schema.json": "a853561b7ac0ef489d7e6f519f66784f47e0cc225fe82f4b30f5218fea5f1e09",
    "assess_result.v1.schema.json": "da1f4fd686280f7bc73321a02f76a3430752be2af89ee752f5c5c154db203595",
}


def _request(version: str | None, run_through: str = "analysis") -> dict[str, Any]:
    request: dict[str, Any] = {
        "database_name": "wordpress",
        "collection": sample_collection("wordpress"),
        "options": {"run_through": run_through},
    }
    if version is not None:
        request["contract_version"] = version
    return request


@cache
def _result(version: str | None, run_through: str) -> str:
    return assess(_request(version, run_through)).model_dump_json()


def _shipped(name: str) -> Draft202012Validator:
    schema = json.loads(SHIPPED_1_3[name].read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# --------------------------------------------------------------------------- the default


def test_the_default_is_the_version_the_release_recorded() -> None:
    released = json.loads(RELEASED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    record = json.loads(RELEASED_VERSION_PATH.read_text(encoding="utf-8"))
    assert models.RELEASED_VERSION_PATH == RELEASED_VERSION_PATH
    # The record agrees with the released snapshot (--write-released writes both).
    assert record["contract_version"] == released_version_of(released) == "1.3"
    assert record["released"] == released["released"]
    assert default_contract_version() == record["contract_version"]
    for model in (AssessmentRequest, AssessmentResult):
        field = model.model_fields["contract_version"]
        assert field.get_default(call_default_factory=True) == record["contract_version"]
    assert CONTRACT_VERSION == SUPPORTED_CONTRACT_VERSIONS[-1] == "1.4"


@pytest.fixture
def recorded_1_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """The next release recorded: this tree's shape as --write-released records it for a
    tag, with its released-version record, which the models then read."""
    document = {**released_document(None), "released": "v9.9.9"}
    record = tmp_path / "released_contract.json"
    write_released_version(document, record)
    monkeypatch.setattr(models, "RELEASED_VERSION_PATH", record)
    models.released_contract_version.cache_clear()
    yield document
    models.released_contract_version.cache_clear()


def test_before_the_record_1_4_is_read_only_when_pinned() -> None:
    assert unreleased_contract_versions() == ("1.4",)
    assert AssessmentRequest.model_validate(_request(None)).contract_version == "1.3"
    assert AssessmentRequest.model_validate(_request("1.4")).contract_version == "1.4"
    with pytest.raises(ValueError, match=r"1\.4 \(not in a release yet\)"):
        models.check_contract_version("1.5")
    # The public shape is 1.4's: against the released 1.3 it is the MINOR rise.
    assert {e["version"] for e in build_public_snapshot().values()} == {"1.4"}


def test_once_a_release_records_1_4_it_is_the_default(recorded_1_4: dict[str, Any]) -> None:
    assert released_version_of(recorded_1_4) == "1.4"
    assert default_contract_version() == "1.4"
    assert unreleased_contract_versions() == ()
    assert AssessmentRequest.model_validate(_request(None)).contract_version == "1.4"
    assert assess(_request(None)).contract_version == "1.4"
    with pytest.raises(ValueError) as caught:
        models.check_contract_version("1.5")
    assert "not in a release yet" not in str(caught.value)
    # A 1.3 caller is still served as before, and may not set a 1.4 field.
    assert AssessmentRequest.model_validate(_request("1.3")).contract_version == "1.3"
    with pytest.raises(RoutineSourcesError, match=r"field of contract 1\.4: pin"):
        AssessmentRequest.model_validate({**_request("1.3"), "routine_sources": []})


def test_a_request_naming_no_version_gets_1_3() -> None:
    assert AssessmentRequest.model_validate(_request(None)).contract_version == "1.3"
    assert json.loads(_result(None, "analysis"))["contract_version"] == "1.3"


# --------------------------------------------------------------------------- 1.3 callers


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_a_1_4_request_gets_the_1_3_values_stamped_1_4(run_through: str) -> None:
    old = json.loads(_result("1.3", run_through))
    new = json.loads(_result("1.4", run_through))
    assert old["contract_version"] == "1.3" and new["contract_version"] == "1.4"
    assert {**new, "contract_version": "1.3"} == old
    # Read back as a 1.4 result, and written back the same.
    again = AssessmentResult.model_validate_json(_result("1.4", run_through))
    assert again.contract_version == "1.4"
    assert json.loads(again.model_dump_json()) == new


@pytest.mark.passes_on_main
@pytest.mark.parametrize("version", ["1.3", None])
@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_a_1_3_caller_validates_with_the_schemas_1_3_shipped(
    version: str | None, run_through: str
) -> None:
    """The guard for 1.3 callers: their request and their result validate against the
    schemas v0.4.0-beta.3 shipped, unchanged, pinned or not."""
    _shipped("assess_request.v1.schema.json").validate(_request(version, run_through))
    _shipped("assess_result.v1.schema.json").validate(json.loads(_result(version, run_through)))


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", sorted(SHIPPED_1_3))
def test_the_shipped_1_3_schemas_are_the_tags_bytes(name: str) -> None:
    data = SHIPPED_1_3[name].read_bytes()
    assert hashlib.sha256(data).hexdigest() == SHIPPED_SHA256[name]
    shown = subprocess.run(  # nosec B603 B607 -- fixed git argv on this repository
        ["git", "-C", str(REPO), "show", f"{SHIPPED_TAG}:docs/contracts/schemas/{name}"],
        capture_output=True,
        check=False,
    )
    if shown.returncode == 0:  # where the tag is fetched, the bytes are the tag's own
        assert shown.stdout == data
    assert json.loads(data)["x-contract-version"] == "1.3"


def test_a_1_3_request_naming_routine_sources_is_refused() -> None:
    request = {**_request("1.3"), "routine_sources": []}
    # From the field's own validator: the input-free RoutineSourcesError.
    with pytest.raises(
        RoutineSourcesError,
        match=r"routine_sources is a field of contract 1\.4 \(not in a release yet\)",
    ):
        AssessmentRequest.model_validate(request)
    with pytest.raises(RoutineSourcesError, match="field of contract 1.4"):
        AssessmentRequest.model_validate({**_request(None), "routine_sources": []})
    AssessmentRequest.model_validate({**_request("1.4"), "routine_sources": []})


def test_an_unvalidated_1_3_request_naming_routine_sources_is_refused_before_it_runs(
    tmp_path: Path,
) -> None:
    from src.storage.local_store import LocalArtifactStore

    built = AssessmentRequest.model_validate(_request("1.3"))
    for request in (
        AssessmentRequest.model_construct(**{**dict(built), "routine_sources": []}),
        built.model_copy(update={"routine_sources": []}),
    ):
        with pytest.raises(ValueError, match=r"routine_sources is a field of contract 1\.4"):
            assess(request, store=LocalArtifactStore(base_dir=str(tmp_path)), job_id="v13rs001")
        assert not any(tmp_path.iterdir())


def test_the_published_schemas_read_both_versions_and_document_routine_sources() -> None:
    request = json.loads(rcs.schema_path("assess_request").read_text(encoding="utf-8"))
    result = json.loads(rcs.schema_path("assess_result").read_text(encoding="utf-8"))
    for schema in (request, result):
        assert schema["x-contract-version"] == "1.4"
        assert schema["properties"]["contract_version"]["enum"] == ["1.3", "1.4"]
        # No default in the schema: a request naming none gets the released version,
        # which the release step moves with no regeneration.
        assert "default" not in schema["properties"]["contract_version"]
    field = request["properties"]["routine_sources"]
    assert "routine_sources" not in request.get("required", [])
    assert "RoutineSource" in field["description"]
    Draft202012Validator(request).validate({**_request("1.4"), "routine_sources": []})
    Draft202012Validator(result).validate(json.loads(_result("1.4", "analysis")))
