# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The public ``assess()`` contract against the shape recorded at its last release (#1184).

Two snapshots, one per era (#1641):

- ``tests/contract/assess_released_snapshot.json`` is the historical record of the last
  release recorded before contract 1.3: v0.4.0-beta.1's contract 1.1 (v0.4.0-beta.2
  shipped 1.2, but its release step never recorded it). It is kept
  exactly as it was and never re-recorded. Every version it records is retired: this
  release supports contract 1.3 only, so a request or a result naming one is refused.
- ``tests/contract/assess_released_since_1_3.json`` (``RELEASED_SNAPSHOT_PATH``) is the
  released surface from 1.3 on. A change that breaks a caller -- a field removed or
  renamed, a type changed, an enum value removed or changed -- fails here unless
  ``CONTRACT_VERSION``'s MAJOR moved; once a release is recorded (``released`` names
  its tag; null until the first release that ships 1.3), an additive change needs a
  MINOR move. So the 1.3 surface only grows from here.

The rules are ``scripts/contract_schema_snapshot.py``'s, the same ones the internal
contracts' snapshot applies (``tests/contract/test_schema_snapshot.py``). This compares
with the branch's own copy; ``scripts/resolve_contract_versions.py --check --against
<main>`` makes the same comparison with main's copy, so a branch cannot pass by
re-recording it. The release step records the baseline from the tag itself:
``uv run python scripts/contract_schema_snapshot.py --write-released <tag>``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

import pytest

from scripts.contract_schema_snapshot import (
    HISTORICAL_RELEASED_SNAPSHOT_PATH,
    RELEASED_SNAPSHOT_PATH,
    WRITE_RELEASED_COMMAND,
    build_public_snapshot,
    evaluate_against_released,
    public_contract_models,
)
from src.assessment import CONTRACT_VERSION, AssessmentRequest, AssessmentResult
from src.assessment.models import RETIRED_CONTRACT_VERSIONS, RetiredContractVersionError

#: The historical snapshot's bytes, as v0.4.0-beta.1's release step recorded them
#: (commit 4b3032d28). It is the record of what shipped: never regenerated or edited.
HISTORICAL_SHA256 = "5300357285e42c3f52379fe030bb789f9fcbdbd295b1eba7ac835f5b0007f063"


def _load(path: Any) -> dict[str, Any]:
    assert path.exists(), f"missing; record it with {WRITE_RELEASED_COMMAND}"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _released() -> dict[str, Any]:
    return _load(RELEASED_SNAPSHOT_PATH)


def _historical() -> dict[str, Any]:
    return _load(HISTORICAL_RELEASED_SNAPSHOT_PATH)


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _versions_named(entry: dict[str, Any]) -> list[str]:
    """The ``contract_version`` values a snapshot entry's schema admits."""
    schema = entry["fields"]["contract_version"]["schema"]
    return list(schema["values"]) if schema["kind"] == "enum" else [schema["value"]]


# --------------------------------------------------------------------------- before 1.3


def test_the_historical_snapshot_is_kept_as_it_shipped() -> None:
    data = HISTORICAL_RELEASED_SNAPSHOT_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == HISTORICAL_SHA256
    historical = _historical()
    assert historical["released"] == "v0.4.0-beta.1"
    assert {key: e["version"] for key, e in historical["contracts"].items()} == {
        "assessment.AssessmentRequest": "1.1",
        "assessment.AssessmentResult": "1.1",
    }


@pytest.mark.parametrize("key", ["assessment.AssessmentRequest", "assessment.AssessmentResult"])
def test_every_version_the_historical_snapshot_records_is_now_refused(key: str) -> None:
    entry = _historical()["contracts"][key]
    named = _versions_named(entry)
    assert entry["version"] in named and named == ["1.0", "1.1"]
    assert set(named) <= set(RETIRED_CONTRACT_VERSIONS)
    for version in named:
        with pytest.raises(RetiredContractVersionError, match="supports contract 1.3 only"):
            AssessmentRequest.model_validate(
                {"contract_version": version, "database_name": "shop", "collection": {"a": 1}}
            )
        with pytest.raises(RetiredContractVersionError):
            AssessmentResult.model_validate({"contract_version": version})


def test_the_1_3_surface_is_the_shipped_one_grown() -> None:
    """Apart from the retired versions, 1.3 only added to what shipped: every field the
    historical snapshot records is still there with its type (``contract_version`` is
    the version's own move, never a field change)."""
    assert RELEASED_SNAPSHOT_PATH != HISTORICAL_RELEASED_SNAPSHOT_PATH
    problems = evaluate_against_released(_historical(), build_public_snapshot())
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------- from 1.3 on


def test_the_public_contract_keeps_the_released_shape_or_moves_its_version() -> None:
    """The 1.3 surface only grows from the baseline."""
    problems = evaluate_against_released(_released(), build_public_snapshot())
    assert not problems, "\n".join(problems)


def test_the_released_snapshot_covers_the_request_and_the_result() -> None:
    models = dict(public_contract_models())
    assert models == {
        "assessment.AssessmentRequest": AssessmentRequest,
        "assessment.AssessmentResult": AssessmentResult,
    }
    released = _released()
    assert set(released["contracts"]) == set(models)
    assert released["released"] is None or released["released"].startswith("v")
    for entry in released["contracts"].values():
        assert int(CONTRACT_VERSION.split(".")[0]) >= int(entry["version"].split(".")[0])


def test_the_baseline_starts_at_1_3_and_names_no_retired_version() -> None:
    for entry in _released()["contracts"].values():
        assert _version_tuple(entry["version"]) >= (1, 3)
        named = _versions_named(entry)
        assert entry["version"] in named
        assert not set(named) & set(RETIRED_CONTRACT_VERSIONS)
