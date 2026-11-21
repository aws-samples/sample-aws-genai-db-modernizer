# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract 1.4 of the public ``assess()`` contract, and the 1.3 callers it keeps.

1.4 adds one optional request field, ``routine_sources`` (#2022); the result is
unchanged. The version a request naming none gets is derived, never kept by hand: the
newest version a release recorded (``src/assessment/released_contract.json``, written
with ``tests/contract/assess_released_since_1_3.json`` by the release step's
``--write-released <tag>``, run after the tag is pushed). Before that record (v0.4.0-beta.3's
1.3) 1.4 is read only when pinned, also in the release that first ships it; the record
step after v0.5.0-beta.1's tag records it, so it is the default with no code change.

- the default is the version the record names (``tests/released_record.py``), whatever
  it is; both states are simulated, so each stays covered: with a 1.3 record 1.4 is read
  only when pinned, and with a 1.4 record it is the default;
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
    pinned_only_contract_versions,
)
from tests.released_record import RECORDED_VERSION, recorded_as
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
    assert record["contract_version"] == released_version_of(released) == RECORDED_VERSION
    assert record["contract_version"] in SUPPORTED_CONTRACT_VERSIONS
    assert record["released"] == released["released"]
    assert default_contract_version() == record["contract_version"]
    for model in (AssessmentRequest, AssessmentResult):
        field = model.model_fields["contract_version"]
        assert field.get_default(call_default_factory=True) == record["contract_version"]
    assert CONTRACT_VERSION == SUPPORTED_CONTRACT_VERSIONS[-1] == "1.5"


@pytest.fixture
def recorded_1_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """The release that recorded 1.4: this tree's shape as --write-released records it
    for a tag, stamped 1.4 (the tree is 1.5 since #2478), with its released-version
    record, which the models then read."""
    document = {**released_document(None), "released": "v9.9.9"}
    for entry in document["contracts"].values():
        entry["version"] = "1.4"
    record = tmp_path / "released_contract.json"
    write_released_version(document, record)
    monkeypatch.setattr(models, "RELEASED_VERSION_PATH", record)
    models.released_contract_version.cache_clear()
    yield document
    models.released_contract_version.cache_clear()


@pytest.fixture
def recorded_1_3(tmp_path: Path) -> Iterator[None]:
    """Before the record: v0.4.0-beta.3's record, which names 1.3."""
    with recorded_as("1.3", tmp_path, released="v0.4.0-beta.3"):
        yield


@pytest.mark.passes_on_main
def test_before_the_record_1_4_is_read_only_when_pinned(recorded_1_3: None) -> None:
    assert default_contract_version() == "1.3"
    assert pinned_only_contract_versions() == ("1.4", "1.5")
    assert AssessmentRequest.model_validate(_request(None)).contract_version == "1.3"
    assert AssessmentRequest.model_validate(_request("1.4")).contract_version == "1.4"
    with pytest.raises(ValueError, match=r"1\.4 \(read only when pinned\)"):
        models.check_contract_version("1.6")
    # The public shape is the newest version's (1.5 since #2478).
    assert {e["version"] for e in build_public_snapshot().values()} == {CONTRACT_VERSION}


@pytest.mark.passes_on_main
def test_once_a_release_records_1_4_it_is_the_default(recorded_1_4: dict[str, Any]) -> None:
    assert released_version_of(recorded_1_4) == "1.4"
    assert default_contract_version() == "1.4"
    # 1.5 (#2478) is newer than the record: read only when pinned.
    assert pinned_only_contract_versions() == ("1.5",)
    assert AssessmentRequest.model_validate(_request(None)).contract_version == "1.4"
    assert assess(_request(None)).contract_version == "1.4"
    with pytest.raises(ValueError) as caught:
        models.check_contract_version("1.6")
    assert str(caught.value).endswith(
        "this code reads contracts 1.3, 1.4 and 1.5 (read only when pinned)"
    )
    assert "1.4 (read only when pinned)" not in str(caught.value)
    # A request naming no version may set a 1.4 field: it is a 1.4 request.
    AssessmentRequest.model_validate({**_request(None), "routine_sources": []})
    # A 1.3 caller is still served as before, and may not set a 1.4 field.
    assert AssessmentRequest.model_validate(_request("1.3")).contract_version == "1.3"
    with pytest.raises(RoutineSourcesError, match=r"field of contract 1\.4: pin"):
        AssessmentRequest.model_validate({**_request("1.3"), "routine_sources": []})


@pytest.mark.passes_on_main
def test_a_request_naming_no_version_gets_the_recorded_version() -> None:
    assert AssessmentRequest.model_validate(_request(None)).contract_version == RECORDED_VERSION
    assert json.loads(_result(None, "analysis"))["contract_version"] == RECORDED_VERSION


@pytest.mark.passes_on_main
def test_before_the_record_a_request_naming_no_version_gets_1_3(recorded_1_3: None) -> None:
    assert AssessmentRequest.model_validate(_request(None)).contract_version == "1.3"
    assert json.loads(assess(_request(None)).model_dump_json())["contract_version"] == "1.3"


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
    version: str | None, run_through: str, tmp_path: Path
) -> None:
    """The guard for 1.3 callers: their request and their result validate against the
    schemas v0.4.0-beta.3 shipped, unchanged, pinned, or unpinned while 1.3 is the
    default (a 1.3 record: once 1.4 is recorded, a request naming none is a 1.4 one)."""
    _shipped("assess_request.v1.schema.json").validate(_request(version, run_through))
    if version is None:
        with recorded_as("1.3", tmp_path, released="v0.4.0-beta.3"):
            result = json.loads(assess(_request(None, run_through)).model_dump_json())
        assert result["contract_version"] == "1.3"
    else:
        result = json.loads(_result(version, run_through))
    _shipped("assess_result.v1.schema.json").validate(result)


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


@pytest.mark.passes_on_main
def test_a_1_3_request_naming_routine_sources_is_refused(tmp_path: Path) -> None:
    request = {**_request("1.3"), "routine_sources": []}
    # From the field's own validator: the input-free RoutineSourcesError.
    with pytest.raises(
        RoutineSourcesError,
        match=r"routine_sources is a field of contract 1\.4: pin",
    ):
        AssessmentRequest.model_validate(request)
    # A request naming none is a 1.3 one while 1.3 is the default (a 1.3 record).
    with recorded_as("1.3", tmp_path, released="v0.4.0-beta.3"):
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


@pytest.mark.passes_on_main
def test_the_published_schemas_read_both_versions_and_document_routine_sources() -> None:
    request = json.loads(rcs.schema_path("assess_request").read_text(encoding="utf-8"))
    result = json.loads(rcs.schema_path("assess_result").read_text(encoding="utf-8"))
    for schema in (request, result):
        assert schema["x-contract-version"] == CONTRACT_VERSION
        assert schema["properties"]["contract_version"]["enum"][:2] == ["1.3", "1.4"]
        # No default in the schema: a request naming none gets the released version,
        # which the release step moves with no regeneration.
        assert "default" not in schema["properties"]["contract_version"]
    field = request["properties"]["routine_sources"]
    assert "routine_sources" not in request.get("required", [])
    assert "RoutineSource" in field["description"]
    Draft202012Validator(request).validate({**_request("1.4"), "routine_sources": []})
    Draft202012Validator(result).validate(json.loads(_result("1.4", "analysis")))
