"""ATX schema/synthesis default to the resolved assignment version, not v1 (issue #189).

When a caller omits ``assignment_version``, the subagents pass ``None`` and the
core resolves the effective version from the store
(``resolve_downstream_assignment_version``), so a re-entry + second Reality
Check (v3 -> v4) is read at v4 rather than a pinned v1.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.atx_orchestrator.subagents import schema as schema_subagent
from src.atx_orchestrator.subagents import synthesis as synthesis_subagent

JOB, DB = "j1", "mydb"


class FakeStore:
    def __init__(self, seeded: dict[str, dict]):
        self.data = dict(seeded)

    def exists(self, path: str) -> bool:
        return path in self.data

    def read_json(self, path: str) -> dict:
        return self.data[path]

    def write_json(self, path: str, data: dict) -> None:
        self.data[path] = data

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.data if k.startswith(prefix)]


def _store_with_versions(n: int) -> FakeStore:
    seeded: dict[str, dict] = {f"{DB}/{JOB}/collector/output.json": {"metadata": {}}}
    for v in range(1, n + 1):
        seeded[f"{DB}/{JOB}/assignment/v{v}/assignment.json"] = {"version": v}
    return FakeStore(seeded)


class TestSubagentParams:
    @pytest.mark.parametrize(
        ("params", "expected"),
        [({}, None), ({"assignment_version": 3}, 3), ({"assignment_version": "2"}, 2)],
    )
    def test_schema_passes_through_or_defers(self, params, expected) -> None:
        with patch("src.atx_orchestrator.core.run_schema_design_core") as core:
            schema_subagent._work(
                {"job_id": JOB, "database_name": DB, "target_type": "dynamodb", **params}
            )
        assert core.call_args.kwargs["assignment_version"] == expected

    @pytest.mark.parametrize(
        ("params", "expected"),
        [({}, None), ({"assignment_version": 4}, 4)],
    )
    def test_synthesis_passes_through_or_defers(self, params, expected) -> None:
        with patch("src.atx_orchestrator.core.run_synthesis_core") as core:
            synthesis_subagent._work({"job_id": JOB, "database_name": DB, **params})
        assert core.call_args.kwargs["assignment_version"] == expected


class TestCoreResolvesAbsentVersion:
    def test_schema_design_core_reads_newest_version(self) -> None:
        from src.atx_orchestrator.core import run_schema_design_core

        store = _store_with_versions(4)
        store.data[f"{DB}/{JOB}/schema-dynamodb/v4/schema_output.json"] = {
            "status": "completed",
            "table_definitions": [{"table_name": "t"}],
        }
        with patch("src.agents.schema_design.handler.run_schema_design_auto") as design:
            summary = run_schema_design_core(JOB, DB, "dynamodb", store=store)

        assert design.call_args.kwargs["assignment_version"] == 4
        assert summary["assignment_version"] == 4

    def test_synthesis_core_reads_newest_version(self) -> None:
        from src.atx_orchestrator import core

        store = _store_with_versions(4)
        store.data[f"{DB}/{JOB}/referee-triage/triage.json"] = {}

        with (
            patch("src.agents.referee.synthesis_handler.run_synthesis") as synth,
            patch("src.atx_orchestrator.core._build_graph"),
            # Stop right after the version-dependent call; the report guards are
            # covered elsewhere.
            pytest.raises(FileNotFoundError, match="synthesis/v4/report.json"),
        ):
            core.run_synthesis_core(JOB, DB, store=store)

        assert synth.call_args.kwargs["assignment_version"] == 4

    def test_no_versions_coerces_to_one(self) -> None:
        from src.atx_orchestrator.core import run_schema_design_core

        store = _store_with_versions(0)
        with pytest.raises(FileNotFoundError, match="assignment/v1/assignment.json"):
            run_schema_design_core(JOB, DB, "dynamodb", store=store)
