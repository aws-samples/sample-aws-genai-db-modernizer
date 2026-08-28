"""Schema-design absence is classified using the source engine.

Upstream's ``run_schema_design`` dispatches on ``target_type`` alone. It has
designers for dynamodb, documentdb, opensearch and elasticache; other targets
take a ``case _:`` branch that writes ``status: "not_implemented"``. Because it
never reads ``metadata.source_database.engine``, it cannot distinguish a target
that needs no redesign from one this report does not cover.

``run_schema_design_core`` supplies that distinction. These tests pin the four
branches and, importantly, which channel each lands in: ``notes`` for a normal
outcome, ``warnings`` only when the reader needs to act. A warning that fires on
every same-engine job would stop being read.

They also assert the wording stays free of fault language, because these strings
can reach the customer-facing deliverable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.atx_orchestrator.core import run_schema_design_core

JOB, DB = "j1", "discourse"


class FakeStore:
    """Minimal ArtifactStore stand-in. Records writes, serves seeded reads."""

    def __init__(self, seeded: dict[str, dict]):
        self.data = dict(seeded)

    def exists(self, path: str) -> bool:
        return path in self.data

    def read_json(self, path: str) -> dict:
        return self.data[path]

    def write_json(self, path: str, data: dict) -> None:
        self.data[path] = data


def _store(source_engine: str | None, schema_output: dict) -> FakeStore:
    """Seed prerequisites plus the artifact upstream would have written."""
    collector: dict = {"metadata": {}}
    if source_engine is not None:
        collector["metadata"]["source_database"] = {"engine": source_engine}
    return FakeStore(
        {
            f"{DB}/{JOB}/collector/output.json": collector,
            f"{DB}/{JOB}/assignment/v1/assignment.json": {"version": 1},
            f"{DB}/{JOB}/schema-TARGET/v1/schema_output.json": schema_output,
        }
    )


def _run(target: str, store: FakeStore) -> dict:
    """Invoke the core with upstream's handler stubbed out.

    The artifact is pre-seeded under a placeholder key that is remapped to the
    real target, so these tests exercise our classification rather than upstream's
    designer or Bedrock.
    """
    key = f"{DB}/{JOB}/schema-{target}/v1/schema_output.json"
    store.data[key] = store.data.pop(f"{DB}/{JOB}/schema-TARGET/v1/schema_output.json")
    with patch("src.agents.schema_design.handler.run_schema_design"):
        return run_schema_design_core(
            job_id=JOB,
            database_name=DB,
            target_type=target,
            assignment_version=1,
            store=store,
        )


class TestSameFamily:
    """Source and target in one family: a normal outcome, not a warning."""

    @pytest.mark.parametrize(
        ("source", "target"),
        [("postgresql", "aurora_postgresql"), ("mysql", "aurora_mysql")],
    )
    def test_reported_as_a_note_not_a_warning(self, source: str, target: str) -> None:
        s = _run(target, _store(source, {"target_type": target, "status": "not_implemented"}))
        assert "warnings" not in s
        assert len(s["notes"]) == 1
        note = s["notes"][0]
        assert "no schema design required" in note
        assert source in note

    def test_status_from_upstream_is_relayed_not_rewritten(self) -> None:
        """We classify alongside upstream's value; we do not overwrite it."""
        s = _run(
            "aurora_postgresql",
            _store("postgresql", {"target_type": "aurora_postgresql", "status": "not_implemented"}),
        )
        assert s["status"] == "not_implemented"


class TestHeterogeneousSource:
    """A conversion this report does not cover: the reader needs to act."""

    @pytest.mark.parametrize("source", ["oracle", "sqlserver", "mysql"])
    def test_reported_as_a_warning_naming_the_source(self, source: str) -> None:
        s = _run(
            "aurora_postgresql",
            _store(source, {"target_type": "aurora_postgresql", "status": "not_implemented"}),
        )
        assert "notes" not in s
        assert len(s["warnings"]) == 1
        w = s["warnings"][0]
        assert "not included in this report" in w
        assert source in w
        assert "schema conversion assessment" in w

    def test_wording_carries_no_fault_language(self) -> None:
        """These strings can reach the customer deliverable."""
        s = _run(
            "aurora_postgresql",
            _store("oracle", {"target_type": "aurora_postgresql", "status": "not_implemented"}),
        )
        w = s["warnings"][0].lower()
        for word in ("owed", "failed", "should have", "instead", "not yet", "gap", "placeholder"):
            assert word not in w, f"fault language {word!r} in customer-facing string"


class TestUpstreamSkip:
    """Upstream's own deliberate no-op already carries a reason. Relay it."""

    def test_skipped_reason_is_relayed_as_a_note(self) -> None:
        s = _run(
            "documentdb",
            _store(
                "postgresql",
                {
                    "target_type": "documentdb",
                    "status": "skipped",
                    "reason": "No queries or tables assigned to this engine",
                },
            ),
        )
        assert "warnings" not in s
        assert "No queries or tables assigned" in s["notes"][0]


class TestMissingSourceEngine:
    """Absent metadata must not fail a phase whose real work already succeeded."""

    def test_falls_back_to_a_warning_without_naming_an_engine(self) -> None:
        s = _run(
            "aurora_postgresql",
            _store(None, {"target_type": "aurora_postgresql", "status": "not_implemented"}),
        )
        assert len(s["warnings"]) == 1
        assert "not covered here" in s["warnings"][0]

    def test_unreadable_collector_does_not_raise(self) -> None:
        store = _store("postgresql", {"target_type": "aurora_postgresql", "status": "x"})
        key = f"{DB}/{JOB}/schema-aurora_postgresql/v1/schema_output.json"
        store.data[key] = store.data.pop(f"{DB}/{JOB}/schema-TARGET/v1/schema_output.json")

        def boom(path: str) -> dict:
            if path.endswith("collector/output.json"):
                raise RuntimeError("S3 unavailable")
            return store.data[path]

        with (
            patch("src.agents.schema_design.handler.run_schema_design"),
            patch.object(store, "read_json", side_effect=boom),
        ):
            s = run_schema_design_core(JOB, DB, "aurora_postgresql", 1, store=store)
        assert s["designs"] == 0


class TestRealDesign:
    """A completed design reports counts and neither notes nor warnings."""

    def test_counts_reported_and_no_annotations(self) -> None:
        s = _run(
            "dynamodb",
            _store(
                "postgresql",
                {
                    "target_type": "dynamodb",
                    "status": "completed",
                    "table_definitions": [{"table_name": "Users"}, {"table_name": "Posts"}],
                    "access_patterns": [{"id": 1}, {"id": 2}, {"id": 3}],
                    "unsupported_patterns": [{"id": 9}],
                },
            ),
        )
        assert s["designs"] == 2
        assert s["design_unit"] == "target tables"
        assert s["access_patterns"] == 3
        assert s["unsupported_patterns"] == 1
        assert "notes" not in s
        assert "warnings" not in s


class TestPrerequisites:
    """Fail with the missing key named, rather than deep inside upstream."""

    def test_missing_collector_output(self) -> None:
        store = FakeStore({})
        with pytest.raises(FileNotFoundError, match="Collector output not found"):
            run_schema_design_core(JOB, DB, "dynamodb", 1, store=store)

    def test_missing_assignment_at_the_requested_version(self) -> None:
        store = FakeStore({f"{DB}/{JOB}/collector/output.json": {"metadata": {}}})
        with pytest.raises(FileNotFoundError, match="Assignment not found"):
            run_schema_design_core(JOB, DB, "dynamodb", 1, store=store)

    def test_upstream_writing_no_artifact_is_an_error(self) -> None:
        """Silence from upstream must not read as an empty design."""
        store = FakeStore(
            {
                f"{DB}/{JOB}/collector/output.json": {"metadata": {}},
                f"{DB}/{JOB}/assignment/v1/assignment.json": {"version": 1},
            }
        )
        with patch("src.agents.schema_design.handler.run_schema_design"):
            with pytest.raises(FileNotFoundError, match="no output exists"):
                run_schema_design_core(JOB, DB, "dynamodb", 1, store=store)


class TestAgentTypeMapping:
    """Six agent types, each mapping to the target_type upstream expects."""

    def test_all_six_targets_present_and_correct(self) -> None:
        from src.atx_orchestrator.subagents.schema import SCHEMA_TARGETS

        assert SCHEMA_TARGETS == {
            "schema-dynamodb": "dynamodb",
            "schema-documentdb": "documentdb",
            "schema-elasticache": "elasticache",
            "schema-opensearch": "opensearch",
            "schema-aurora-pg": "aurora_postgresql",
            "schema-aurora-mysql": "aurora_mysql",
        }

    def test_every_target_is_in_the_entrypoint_table(self) -> None:
        """A target with no _AGENTS row could never be deployed."""
        from src.atx_orchestrator.atx_entrypoint import _AGENTS
        from src.atx_orchestrator.subagents.schema import SCHEMA_TARGETS

        assert set(SCHEMA_TARGETS) <= set(_AGENTS)


class TestNonTabularEngineIsNotMistakenForNoDesign:
    """The regression that shipped: a DynamoDB-shaped test on four engine shapes.

    DocumentDB, ElastiCache and OpenSearch never emit ``table_definitions``. On
    job ``v2-e2e-08`` they produced 20 collections, 10 key designs and 5 index
    designs and were each reported as needing "a separate schema conversion
    assessment", attributed to the PostgreSQL source — while those designs were in
    the artifacts the same report was built from.
    """

    @pytest.mark.parametrize(
        "target,field,count,unit",
        [
            ("documentdb", "collections", 20, "collections"),
            ("elasticache", "key_designs", 10, "key designs"),
            ("opensearch", "index_designs", 5, "index designs"),
        ],
    )
    def test_design_in_engine_field_produces_no_warning(
        self, target: str, field: str, count: int, unit: str
    ) -> None:
        s = _run(
            target,
            _store(
                "postgresql",
                {
                    "target_type": target,
                    "status": "completed",
                    field: [{"name": f"n{i}"} for i in range(count)],
                    "access_patterns": [{"id": 1}],
                },
            ),
        )
        assert s["designs"] == count
        assert s["design_unit"] == unit
        assert "warnings" not in s, (
            f"{target} designed {count} {unit} and must not be reported as "
            f"needing a separate schema conversion assessment"
        )
        assert "notes" not in s

    def test_aurora_still_warns_when_source_family_differs(self) -> None:
        """The warning must keep firing where it is true — this is what it is for."""
        s = _run(
            "aurora_postgresql",
            _store(
                "oracle",
                {"target_type": "aurora_postgresql", "status": "not_implemented"},
            ),
        )
        assert s["designs"] == 0
        assert "warnings" in s
        assert "separate schema conversion assessment" in s["warnings"][0]
