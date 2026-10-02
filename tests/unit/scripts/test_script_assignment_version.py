"""Local scripts resolve the effective assignment version (ADR-028).

The Claude Code skills drive schema design and synthesis through these scripts
without passing ``--assignment-version``. Before this, schema design defaulted
to v1 and synthesis to 0, so after Reality Check wrote assignment v2 the skills
designed schemas against the pre-consolidation routing and synthesis found no
schema output at all. These pin the default to the effective version on the
LocalArtifactStore the scripts use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import run_schema_design, run_synthesis
from scripts.run_assessment import _surviving_engines
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


def _write_assignment(store: LocalArtifactStore, version: int, qas: list[tuple[str, str]]) -> None:
    store.write_json(
        f"{DB}/{JOB}/assignment/v{version}/assignment.json",
        {
            "query_assignments": [
                {"query_id": q, "assigned_engine": e, "in_scope": True} for q, e in qas
            ]
        },
    )


@pytest.fixture
def consolidated_store(tmp_path: Path) -> LocalArtifactStore:
    """v1 routes q2 to DocumentDB; Reality Check's v2 consolidates it into DynamoDB."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _write_assignment(store, 1, [("q1", "dynamodb"), ("q2", "documentdb"), ("q3", "aurora_mysql")])
    _write_assignment(store, 2, [("q1", "dynamodb"), ("q2", "dynamodb"), ("q3", "aurora_mysql")])
    return store


@pytest.mark.parametrize("script", [run_schema_design, run_synthesis])
class TestResolveVersion:
    def test_defaults_to_effective_version(
        self, script: object, consolidated_store: LocalArtifactStore
    ) -> None:
        assert script._resolve_version(consolidated_store, JOB, DB, None) == 2  # type: ignore[attr-defined]

    def test_explicit_version_wins(
        self, script: object, consolidated_store: LocalArtifactStore
    ) -> None:
        assert script._resolve_version(consolidated_store, JOB, DB, 1) == 1  # type: ignore[attr-defined]

    def test_no_assignment_coerces_to_v1(self, script: object, tmp_path: Path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        assert script._resolve_version(store, JOB, DB, None) == 1  # type: ignore[attr-defined]


class TestSurvivingEngines:
    def test_drops_consolidated_engine_and_keeps_order(
        self, consolidated_store: LocalArtifactStore
    ) -> None:
        selected = ["dynamodb", "documentdb", "aurora_mysql"]
        assert _surviving_engines(consolidated_store, JOB, DB, selected) == [
            "dynamodb",
            "aurora_mysql",
        ]

    def test_unreadable_assignment_keeps_selection(self, tmp_path: Path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        selected = ["dynamodb", "documentdb"]
        assert _surviving_engines(store, JOB, DB, selected) == selected


class TestSynthesisStatusPaths:
    def test_report_key_is_versioned(self) -> None:
        assert run_synthesis._report_key(DB, JOB, 2) == f"{DB}/{JOB}/synthesis/v2/report.json"

    def test_report_key_legacy_when_unversioned(self) -> None:
        assert run_synthesis._report_key(DB, JOB, 0) == f"{DB}/{JOB}/referee-synthesis/report.json"
