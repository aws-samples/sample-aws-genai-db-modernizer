"""Synthesis records the stay-on-source baseline (#949).

With no source billing in the collection the deliverables had nothing to
compare the plan's projected cost against. The source-compatible Aurora engine
(``retained_engine_for``: the homogeneous one, or the heterogeneous choice) is
analysed against the whole workload, so its own analysed cost is what keeping
every query on it costs. Synthesis writes it to ``tco_analysis.stay_on_source``.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.agents.referee.synthesis_report import build_tco_analysis
from src.contracts.synthesis_output import TCOAnalysis
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.test_synthesis_report_tco import _data

DB = "shop"
JOB = "job-949b"


def _store(
    tmp_path,
    source: str = "mysql",
    assigned: tuple[str, ...] = ("dynamodb", "aurora_mysql"),
    choice: str | None = None,
) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    engines = ("dynamodb", "aurora_mysql", "aurora_postgresql")
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines]},
    )
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": source}},
            "database_schema": {"tables": [{"table_id": "shop.orders"}]},
            "queries": {},
        },
    )
    for engine, cost in zip(engines, (15.2, 121.06, 140.5), strict=True):
        s.write_json(
            f"{DB}/{JOB}/analysis-{engine}/analysis.json",
            {"workload_analysis": {}, "cost_estimate": {"monthly_cost_usd": cost}},
        )
    assignment: dict = {
        "version": 2,
        "status": "reality_checked",
        "query_assignments": [
            {"query_id": f"q{i}", "assigned_engine": e, "in_scope": True}
            for i, e in enumerate(assigned)
        ],
    }
    if choice:
        assignment["aurora_engine_choice"] = {"engine": choice}
    s.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", assignment)
    return s


class TestBuildTcoAnalysis:
    def test_the_baseline_is_written_when_given(self) -> None:
        tco = build_tco_analysis(_data(), stay_on_source=("aurora_mysql", 121.06))
        assert tco["stay_on_source"] == {"database": "aurora_mysql", "monthly_cost_usd": 121.06}
        TCOAnalysis.model_validate(tco)

    def test_no_baseline_omits_the_field(self) -> None:
        assert "stay_on_source" not in build_tco_analysis(_data())
        assert "stay_on_source" not in build_tco_analysis(_data(), stay_on_source=None)


class TestSynthesis:
    def test_a_mysql_source_compares_against_aurora_mysql(self, tmp_path) -> None:
        tco = run_synthesis_deterministic(JOB, DB, _store(tmp_path), assignment_version=2)[
            "tco_analysis"
        ]
        assert tco["stay_on_source"] == {"database": "aurora_mysql", "monthly_cost_usd": 121.06}
        # The baseline is a comparison, never part of the projection.
        assert tco["projected_monthly_cost"] == pytest.approx(15.2 + 121.06, abs=0.01)

    def test_a_heterogeneous_source_compares_against_the_chosen_aurora_engine(
        self, tmp_path
    ) -> None:
        store = _store(
            tmp_path,
            source="sqlserver",
            assigned=("dynamodb", "aurora_postgresql"),
            choice="aurora_postgresql",
        )
        tco = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)["tco_analysis"]
        assert tco["stay_on_source"] == {
            "database": "aurora_postgresql",
            "monthly_cost_usd": 140.5,
        }

    def test_a_retained_engine_the_plan_dropped_is_still_the_baseline(self, tmp_path) -> None:
        """Every query moved off Aurora MySQL: staying on it is still the comparison,
        read from its own analysis."""
        store = _store(tmp_path, assigned=("dynamodb", "dynamodb"))
        tco = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)["tco_analysis"]
        assert tco["stay_on_source"] == {"database": "aurora_mysql", "monthly_cost_usd": 121.06}

    def test_an_unknown_source_has_no_baseline(self, tmp_path) -> None:
        store = _store(tmp_path, source="", assigned=("dynamodb", "dynamodb"))
        tco = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)["tco_analysis"]
        assert "stay_on_source" not in tco
