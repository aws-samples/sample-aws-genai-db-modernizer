"""Single assignment-version resolver + source provenance (ADR-028).

Pins the one resolver that replaced the six ad-hoc ``max(vN)`` scans, and the
optional ``source`` provenance field on the Assignment contract (defaulted so
legacy artifacts written before the field existed still validate).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from src.contracts.assignment_models import Assignment, AssignmentSource, AssignmentStatus
from src.storage.assignment_versioning import (
    assignment_artifact_path,
    next_assignment_version,
    resolve_effective_assignment_version,
)


class _FakeStore:
    """Minimal store exposing only ``list_prefix`` (what the resolver needs)."""

    def __init__(self, keys: Iterable[str]) -> None:
        self._keys = list(keys)

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self._keys if k.startswith(prefix)]


def _keys(db: str, job: str, versions: Iterable[int]) -> list[str]:
    return [f"{db}/{job}/assignment/v{v}/assignment.json" for v in versions]


# =============================================================================
# resolve_effective_assignment_version


class TestResolveEffectiveAssignmentVersion:
    def test_returns_highest_version(self) -> None:
        store = _FakeStore(_keys("db", "job", [1, 2, 3]))
        assert resolve_effective_assignment_version(store, "db", "job") == 3

    def test_returns_zero_when_none(self) -> None:
        assert resolve_effective_assignment_version(_FakeStore([]), "db", "job") == 0

    def test_order_independent(self) -> None:
        store = _FakeStore(_keys("db", "job", [2, 10, 1]))
        assert resolve_effective_assignment_version(store, "db", "job") == 10

    def test_ignores_non_version_and_legacy_keys(self) -> None:
        # Legacy flat assignments.json and validation files must not be counted
        # as versions.
        store = _FakeStore(
            [
                "db/job/assignment/assignments.json",
                "db/job/assignment/v1/assignment.json",
                "db/job/assignment/v1/validation.json",
            ]
        )
        assert resolve_effective_assignment_version(store, "db", "job") == 1

    def test_scoped_to_db_and_job(self) -> None:
        store = _FakeStore(_keys("db", "job", [1, 2]) + _keys("db", "other", [5]))
        assert resolve_effective_assignment_version(store, "db", "job") == 2


class TestNextAssignmentVersion:
    def test_first_write_is_v1(self) -> None:
        assert next_assignment_version(_FakeStore([]), "db", "job") == 1

    def test_increments_highest(self) -> None:
        store = _FakeStore(_keys("db", "job", [1, 2]))
        assert next_assignment_version(store, "db", "job") == 3


class TestAssignmentArtifactPath:
    def test_path_shape(self) -> None:
        assert assignment_artifact_path("db", "job", 2) == "db/job/assignment/v2/assignment.json"


# =============================================================================
# source provenance on the Assignment contract


class TestAssignmentSourceProvenance:
    def _assignment(self, **overrides: object) -> Assignment:
        base: dict = {
            "job_id": "j",
            "version": 1,
            "status": AssignmentStatus.AUTO_GENERATED,
            "timestamp": datetime.now(UTC),
            "query_assignments": [],
            "table_assignments": [],
            "co_dependency_groups": [],
            "validation_warnings": [],
        }
        base.update(overrides)
        return Assignment(**base)

    def test_source_defaults_to_none(self) -> None:
        assert self._assignment().source is None

    def test_source_roundtrips_through_json(self) -> None:
        a = self._assignment(source=AssignmentSource.CUSTOMER_GATE)
        dumped = a.model_dump(mode="json")
        assert dumped["source"] == "customer_gate"
        assert Assignment.model_validate(dumped).source is AssignmentSource.CUSTOMER_GATE

    def test_legacy_artifact_without_source_still_validates(self) -> None:
        # An artifact written before the field existed has no `source` key.
        d = self._assignment(source=AssignmentSource.REALITY_CHECK).model_dump(mode="json")
        d.pop("source")
        assert Assignment.model_validate(d).source is None
