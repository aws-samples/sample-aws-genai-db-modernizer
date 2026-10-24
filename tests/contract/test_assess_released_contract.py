"""The public ``assess()`` contract against the shape recorded at the last release (#1184).

``tests/contract/assess_released_snapshot.json`` records the request's and the result's
shape as the last release shipped them (``released`` names the tag; null until the first
release records it). A change that breaks a caller -- a field removed or renamed, a type
changed, an enum value removed or changed -- fails here unless ``CONTRACT_VERSION``'s
MAJOR moved; once a release is recorded, an additive change needs a MINOR move. The
rules are ``scripts/contract_schema_snapshot.py``'s, the same ones the internal contracts'
snapshot applies (``tests/contract/test_schema_snapshot.py``). This compares with the
branch's own copy; ``scripts/resolve_contract_versions.py --check --against <main>`` makes
the same comparison with main's copy, so a branch cannot pass by re-recording it. The
release step records a new baseline from the tag itself:
``uv run python scripts/contract_schema_snapshot.py --write-released <tag>``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from scripts.contract_schema_snapshot import (
    RELEASED_SNAPSHOT_PATH,
    WRITE_RELEASED_COMMAND,
    build_public_snapshot,
    evaluate_against_released,
    public_contract_models,
)
from src.assessment import CONTRACT_VERSION, AssessmentRequest, AssessmentResult


def _released() -> dict[str, Any]:
    assert RELEASED_SNAPSHOT_PATH.exists(), f"missing; record it with {WRITE_RELEASED_COMMAND}"
    return cast(dict[str, Any], json.loads(RELEASED_SNAPSHOT_PATH.read_text(encoding="utf-8")))


def test_the_public_contract_keeps_the_released_shape_or_moves_its_version() -> None:
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
