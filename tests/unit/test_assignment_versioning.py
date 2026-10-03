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
    RealityCheckRun,
    assignment_artifact_path,
    is_reality_check_produced,
    next_assignment_version,
    reality_check_is_current,
    reality_check_run_for_lineage,
    resolve_downstream_assignment_version,
    resolve_effective_assignment_version,
    resolve_reality_check_input_version,
    stale_schema_versions,
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
# stale_schema_versions


def _schema_key(db: str, job: str, engine: str, version: int) -> str:
    return f"{db}/{job}/schema-{engine}/v{version}/schema_output.json"


class TestStaleSchemaVersions:
    def test_empty_when_no_assignment(self) -> None:
        # Schema outputs exist but there is no assignment to be stale against.
        store = _FakeStore([_schema_key("db", "job", "dynamodb", 1)])
        assert stale_schema_versions(store, "db", "job") == {}

    def test_none_stale_when_all_at_effective(self) -> None:
        store = _FakeStore(
            _keys("db", "job", [2])
            + [_schema_key("db", "job", "dynamodb", 2), _schema_key("db", "job", "opensearch", 2)]
        )
        assert stale_schema_versions(store, "db", "job") == {}

    def test_reports_engines_behind_effective(self) -> None:
        # effective assignment v2; dynamodb designed at v2 (fresh), opensearch at v1 (stale).
        store = _FakeStore(
            _keys("db", "job", [1, 2])
            + [_schema_key("db", "job", "dynamodb", 2), _schema_key("db", "job", "opensearch", 1)]
        )
        assert stale_schema_versions(store, "db", "job") == {"opensearch": 1}

    def test_uses_newest_schema_version_per_engine(self) -> None:
        # dynamodb has both v1 and v2 outputs; the newest (v2) matches effective, so not stale.
        store = _FakeStore(
            _keys("db", "job", [2])
            + [_schema_key("db", "job", "dynamodb", 1), _schema_key("db", "job", "dynamodb", 2)]
        )
        assert stale_schema_versions(store, "db", "job") == {}

    def test_ignores_legacy_unversioned_output(self) -> None:
        store = _FakeStore(_keys("db", "job", [2]) + ["db/job/schema-dynamodb/schema_output.json"])
        assert stale_schema_versions(store, "db", "job") == {}


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


# =============================================================================
# Reality Check input/output resolution (issue #189)


class _DictStore:
    """Store backed by ``{key: json}``: list_prefix + read_json + exists."""

    def __init__(self, artifacts: dict[str, dict]) -> None:
        self.artifacts = dict(artifacts)

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.artifacts if k.startswith(prefix)]

    def read_json(self, path: str) -> dict:
        return self.artifacts[path]

    def exists(self, path: str) -> bool:
        return path in self.artifacts


def _versioned(*docs: dict) -> _DictStore:
    """Build a store whose v1..vN assignment artifacts are ``docs`` in order."""
    return _DictStore(
        {assignment_artifact_path("db", "job", i): d for i, d in enumerate(docs, start=1)}
    )


_RESOLUTION = {"source": "assignment_resolution"}
_RC_OUTPUT_KEY = "db/job/reality-check/output.json"


def _gate(previous: int) -> dict:
    return {"source": "customer_gate", "previous_version": previous, "reality_check_applied": True}


def _rc(previous: int) -> dict:
    return {"source": "reality_check", "previous_version": previous, "reality_check_applied": True}


def _legacy_dump(version: int) -> dict:
    """A pre-ADR-028 artifact: a full model dump without ``source``.

    ``model_dump`` always includes ``previous_version`` (null unless set), which
    is the shape the old Reality Check writer spread forward.
    """
    d = Assignment(
        job_id="job",
        version=version,
        status=AssignmentStatus.AUTO_GENERATED,
        timestamp=datetime.now(UTC),
        query_assignments=[],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    ).model_dump(mode="json")
    d.pop("source")
    return d


class TestIsRealityCheckProduced:
    def test_source_stamp_marks_reality_check(self) -> None:
        assert is_reality_check_produced(_rc(1)) is True

    def test_other_sources_are_not_reality_check(self) -> None:
        assert is_reality_check_produced(_RESOLUTION) is False
        # A customer-gate copy of a consolidated version carries the copied
        # reality_check_applied marker; the explicit source wins.
        assert is_reality_check_produced(_gate(2)) is False

    def test_legacy_reality_check_write_has_null_previous_version(self) -> None:
        # Pre-ADR-028 Reality Check spread a model dump (previous_version: null)
        # and added reality_check_applied.
        legacy = {**_legacy_dump(2), "reality_check_applied": True}
        assert "previous_version" in legacy and legacy["previous_version"] is None
        assert is_reality_check_produced(legacy) is True

    def test_legacy_override_of_consolidated_version_is_not_reality_check(self) -> None:
        # Legacy REST overrides always set previous_version and copied the marker.
        legacy_override = {**_legacy_dump(3), "reality_check_applied": True, "previous_version": 2}
        assert is_reality_check_produced(legacy_override) is False

    def test_legacy_resolution_without_marker(self) -> None:
        assert is_reality_check_produced(_legacy_dump(1)) is False


class TestResolveRealityCheckInputVersion:
    def test_fresh_job_reads_v1(self) -> None:
        assert resolve_reality_check_input_version(_versioned(_RESOLUTION), "db", "job") == 1

    def test_skips_its_own_consolidation(self) -> None:
        store = _versioned(_RESOLUTION, _rc(1))
        assert resolve_reality_check_input_version(store, "db", "job") == 1

    def test_re_resolution_above_consolidation_is_the_input(self) -> None:
        store = _versioned(_RESOLUTION, _rc(1), _RESOLUTION)
        assert resolve_reality_check_input_version(store, "db", "job") == 3

    def test_no_versions_coerces_to_one(self) -> None:
        # ADR-026 coercion: the caller's missing-artifact check on v1 then fires.
        assert resolve_reality_check_input_version(_DictStore({}), "db", "job") == 1

    def test_version_dir_without_assignment_is_skipped(self) -> None:
        store = _versioned(_RESOLUTION)
        store.artifacts["db/job/assignment/v2/validation.json"] = {}
        assert resolve_reality_check_input_version(store, "db", "job") == 1


class TestRealityCheckRunForLineage:
    def test_none_on_fresh_job(self) -> None:
        assert reality_check_run_for_lineage(_versioned(_RESOLUTION), "db", "job") is None
        assert reality_check_is_current(_versioned(_RESOLUTION), "db", "job") is False

    def test_newest_consolidation(self) -> None:
        run = reality_check_run_for_lineage(_versioned(_RESOLUTION, _rc(1)), "db", "job")
        assert run == RealityCheckRun(input_version=1, output_version=2)

    def test_customer_edit_after_consolidation_is_current(self) -> None:
        # RC -> gate ordering (ADR-028): the edit was made after Reality Check.
        store = _versioned(_RESOLUTION, _rc(1), _gate(2), _gate(3))
        assert reality_check_run_for_lineage(store, "db", "job") == RealityCheckRun(1, 2)
        assert reality_check_is_current(store, "db", "job") is True

    def test_re_resolution_starts_a_new_lineage(self) -> None:
        store = _versioned(_RESOLUTION, _rc(1), _gate(2), _RESOLUTION)
        assert reality_check_is_current(store, "db", "job") is False

    def test_run_that_consolidated_nothing_then_customer_edit(self) -> None:
        store = _versioned(_RESOLUTION, _gate(1))
        store.artifacts[_RC_OUTPUT_KEY] = {
            "source_assignment_version": 1,
            "output_assignment_version": None,
        }
        assert reality_check_run_for_lineage(store, "db", "job") == RealityCheckRun(1, None)

    def test_pending_external_preview_is_not_a_run(self) -> None:
        store = _versioned(_RESOLUTION)
        store.artifacts[_RC_OUTPUT_KEY] = {"source_assignment_version": 1}
        store.artifacts["db/job/reality-check/awaiting_llm.json"] = {"status": "awaiting_llm"}
        assert reality_check_is_current(store, "db", "job") is False

    def test_legacy_consolidation_and_legacy_override(self) -> None:
        legacy_rc = {**_legacy_dump(2), "reality_check_applied": True}
        legacy_edit = {**_legacy_dump(3), "reality_check_applied": True, "previous_version": 2}
        store = _versioned(_legacy_dump(1), legacy_rc, legacy_edit)
        store.artifacts[_RC_OUTPUT_KEY] = {"source_assignment_version": 1}
        assert reality_check_run_for_lineage(store, "db", "job") == RealityCheckRun(1, 2)

    def test_none_when_nothing_exists(self) -> None:
        assert reality_check_is_current(_DictStore({}), "db", "job") is False


class TestDownstreamReadsNewestVersion:
    def test_customer_edit_after_consolidation(self) -> None:
        store = _versioned(_RESOLUTION, _rc(1), _gate(2))
        assert resolve_downstream_assignment_version(store, "db", "job") == 3

    def test_consolidation_of_re_resolution(self) -> None:
        store = _versioned(_RESOLUTION, _rc(1), _RESOLUTION, _rc(3))
        assert resolve_downstream_assignment_version(store, "db", "job") == 4
