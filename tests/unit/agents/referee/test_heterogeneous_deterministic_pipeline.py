"""#381 review (blocker): the AdventureWorks SQL Server fixture must run through
the FULL deterministic pipeline -- triage, every analysis agent, assignment
resolution, reality check, and synthesis -- and the resulting report.json must
validate against ``SynthesisOutputContract``.

Before the review fix, ``MigrationWave.homogeneity``'s ``Literal`` lacked
``"cross_engine"``, so synthesis raised a ``ValidationError`` for every
heterogeneous source (SQL Server, Oracle, DB2) where the assignment resolver
still picked an Aurora engine -- this test is the regression guard for that.

It also renders every customer deliverable (decision/analysis HTML,
engineering Markdown, PDF) from the resulting report.json, since a
contract bump with no renderer regression is only half the fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.analysis.handler import run_analysis
from src.agents.referee.assignment_handler import run_assignment_resolver
from src.agents.referee.reality_check_handler import run_reality_check_handler
from src.agents.referee.synthesis_handler import run_synthesis
from src.agents.referee.triage_handler import run_triage
from src.contracts.synthesis_output import SynthesisOutputContract
from src.report import deliverables as dl
from src.report.architecture import _architecture_engines
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore

FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "adventureworks_trimmed_collection.json"
)
DB = "adventureworks"
JOB = "job-381"


@pytest.fixture(scope="module")
def pipeline_report(tmp_path_factory) -> tuple[LocalArtifactStore, dict, int]:
    """Run the deterministic pipeline end to end and return (store, report, version).

    Module-scoped: the whole chain (6 analysis agents, assignment, reality check,
    synthesis) is deterministic and reused by every test below instead of
    re-running it per test.
    """
    tmp_path = tmp_path_factory.mktemp("adventureworks-pipeline")
    store = LocalArtifactStore(base_dir=str(tmp_path))
    collector_output = json.loads(FIXTURE.read_text())
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector_output)

    run_triage(JOB, DB, store)
    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    selected = [a["agent_type"] for a in triage["selected_agents"]]
    assert "aurora_mysql" in selected and "aurora_postgresql" in selected

    for engine in selected:
        run_analysis(JOB, DB, engine, store, llm_mode="none")

    run_assignment_resolver(JOB, DB, store)
    run_reality_check_handler(JOB, DB, store, llm_mode="none")

    version = resolve_downstream_assignment_version(store, DB, JOB)
    run_synthesis(JOB, DB, store, version, llm_mode="none")
    report_key = f"{DB}/{JOB}/synthesis/v{version}/report.json"
    report = store.read_json(report_key)
    return store, report, version


class TestAdventureWorksSynthesisContract:
    def test_report_validates_against_the_synthesis_contract(self, pipeline_report) -> None:
        _, report, _ = pipeline_report
        SynthesisOutputContract.model_validate(report)

    def test_contract_version_is_at_least_1_7(self, pipeline_report) -> None:
        _, report, _ = pipeline_report
        major, minor = (int(p) for p in report["contract_version"].split("."))
        assert (major, minor) >= (1, 7)

    def test_wave_one_is_cross_engine_with_an_aurora_engine_named(self, pipeline_report) -> None:
        _, report, _ = pipeline_report
        waves = report.get("migration_waves") or []
        assert waves, "expected at least one migration wave"
        wave1 = waves[0]
        assert wave1["homogeneity"] == "cross_engine"
        assert wave1["engines"] in (["aurora_mysql"], ["aurora_postgresql"])
        assert "cross-engine" in wave1["rationale"]
        # #381 review round 2: the fixture's real SQL Server @@VERSION banner must
        # not duplicate the engine name in the rationale text ("SQL Server
        # Microsoft SQL Server ..."), and the short build number should appear.
        assert "SQL Server SQL Server" not in wave1["rationale"]
        assert "Microsoft" not in wave1["rationale"]
        assert "15.0.2000.5" in wave1["rationale"]
        # #743: the same opening clause as the executive summary (one shared helper).
        assert wave1["rationale"].startswith(
            "The source SQL Server database (15.0.2000.5) has no Aurora engine of its own dialect"
        )

    def test_assignment_summary_carries_the_aurora_engine_choice(self, pipeline_report) -> None:
        _, report, _ = pipeline_report
        choice = report["assignment_summary"]["aurora_engine_choice"]
        assert choice is not None
        assert choice["source_engine"] == "sqlserver"
        assert choice["engine"] in ("aurora_mysql", "aurora_postgresql")

    def test_chosen_aurora_engine_is_a_migration_target_not_retained(self, pipeline_report) -> None:
        """#287 round-1 review (C1): SQL Server has no Aurora dialect of its own, so
        whichever Aurora engine the heterogeneous resolver picked is a real
        migration target in every deliverable -- never "Retained", which would
        wrongly claim SQL Server's own dialect carries no migration.
        """
        _, report, _ = pipeline_report
        choice = report["assignment_summary"]["aurora_engine_choice"]
        roles = {e["engine"]: e["role"] for e in _architecture_engines(report)}
        assert roles[choice["engine"]] == "Migration target"
        assert "retained" not in report["recommended_architecture"]["rationale"].lower()

    def test_executive_summary_names_the_heterogeneous_choice(self, pipeline_report) -> None:
        _, report, _ = pipeline_report
        assert (
            "The source SQL Server database has no Aurora engine of its own dialect"
            in report["summary"]
        )


class TestAdventureWorksDeliverablesRender:
    def test_every_deliverable_renders_with_no_errors(self, pipeline_report) -> None:
        store, _, version = pipeline_report
        report_key = f"{DB}/{JOB}/synthesis/v{version}/report.json"
        out = dl.render_deliverables(store, JOB, DB, report_key, assignment_version=version)
        assert out.errors == []
        names = {d.name for d in out.items}
        assert names == {"engineering-report", "assessment-data", "executive-report"}

    @pytest.mark.passes_on_main  # the role guard, narrowed to the engine table (#1231)
    def test_the_chosen_aurora_engine_reads_migration_target(self, pipeline_report) -> None:
        store, _, version = pipeline_report
        report_key = f"{DB}/{JOB}/synthesis/v{version}/report.json"
        out = {
            d.name: d
            for d in dl.render_deliverables(
                store, JOB, DB, report_key, assignment_version=version
            ).items
        }
        md = out["engineering-report"].content.decode("utf-8")
        # #287 round-1 review (C1): the engine table renders each engine's role --
        # the chosen Aurora engine must read "Migration target" there, never
        # "Retained" (SQL Server has no Aurora dialect of its own).
        # The engine table's rows: an Aurora engine then its role (other tables, such as
        # the close calls, also start with an engine name since routing moved queries).
        from src.shared.report_facts import ROLES

        rows = [
            line
            for line in md.splitlines()
            if line.startswith("| Aurora ") and line.split("|")[2].strip() in ROLES
        ]
        assert rows and all("| Migration target |" in r for r in rows), rows
        assert "| Retained |" not in md

    def test_pdf_renders(self, pipeline_report) -> None:
        store, _, version = pipeline_report
        report_key = f"{DB}/{JOB}/synthesis/v{version}/report.json"
        out = {
            d.name: d
            for d in dl.render_deliverables(
                store, JOB, DB, report_key, assignment_version=version
            ).items
        }
        assert out["executive-report"].content.startswith(b"%PDF")
        assert "executive-summary-pptx" not in out
