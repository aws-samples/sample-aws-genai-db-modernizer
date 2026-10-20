"""``build_tco_analysis`` must show a source baseline, or say it is unknown,
instead of a bare 0.0 / 0% that reads as "the source costs nothing" (#380).

Also: a retired engine's own analysed cost, when the caller has it, is
carried into the TCO facts (``retired_engine_savings``, #949: only an engine the
source runs today) so a saving a recommendation names for it (e.g. "$271.80/mo")
is traceable there too, not only in prose. ``eliminated_engine_costs`` is no
longer written.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_tco_analysis


def _data(rds_meta: dict | None = None) -> SynthesisData:
    collector: dict = {"metadata": {"source_database": {}}}
    if rds_meta is not None:
        collector["metadata"]["source_database"]["rds_instance_metadata"] = rds_meta
    return SynthesisData(
        job_id="j",
        database_name="db",
        collector=collector,
        engines={
            "dynamodb": EngineArtifacts(
                "dynamodb", analysis={"cost_estimate": {"monthly_cost_usd": 15.2}}
            ),
        },
    )


class TestCurrentCostKnown:
    def test_no_rds_metadata_marks_the_baseline_unknown(self) -> None:
        tco = build_tco_analysis(_data(rds_meta=None))
        assert tco["current_cost_known"] is False
        # The figure itself stays 0.0 for back-compat with any reader that
        # only looks at the number; current_cost_known is what tells a
        # renderer to say "source cost not provided" instead of "$0.00".
        assert tco["current_monthly_cost"] == 0

    def test_empty_rds_metadata_also_marks_the_baseline_unknown(self) -> None:
        tco = build_tco_analysis(_data(rds_meta={}))
        assert tco["current_cost_known"] is False

    def test_real_rds_metadata_marks_the_baseline_known(self) -> None:
        tco = build_tco_analysis(_data(rds_meta={"instance_class": "db.t3.medium"}))
        assert tco["current_cost_known"] is True
        assert tco["current_monthly_cost"] == 65


_RDS_ESTIMATE_NOTE = "Current RDS cost estimated from instance class (actual billing may differ)"


class TestBaselineAssumption:
    """#1247: the footnote claimed an estimate the report never showed."""

    def test_no_baseline_drops_the_estimate_note(self) -> None:
        tco = build_tco_analysis(_data(rds_meta=None))
        assert tco["current_cost_known"] is False
        assert not any("Current RDS cost" in a for a in tco["assumptions"])

    def test_empty_metadata_drops_the_estimate_note(self) -> None:
        tco = build_tco_analysis(_data(rds_meta={}))
        assert not any("Current RDS cost" in a for a in tco["assumptions"])

    @pytest.mark.passes_on_main
    def test_known_baseline_keeps_the_estimate_note(self) -> None:
        tco = build_tco_analysis(_data(rds_meta={"instance_class": "db.t3.medium"}))
        assert _RDS_ESTIMATE_NOTE in tco["assumptions"]

    @pytest.mark.passes_on_main
    def test_the_other_assumptions_stay_either_way(self) -> None:
        for meta in (None, {"instance_class": "db.t3.medium"}):
            assumptions = build_tco_analysis(_data(rds_meta=meta))["assumptions"]
            assert "On-demand capacity mode for all target databases" in assumptions
            assert "Does not include data transfer, backups, or global tables" in assumptions


class TestRetiredEngineSavings:
    def test_no_retired_costs_omits_the_field(self) -> None:
        tco = build_tco_analysis(_data(), retired_costs=None)
        assert "retired_engine_savings" not in tco
        assert "eliminated_engine_costs" not in tco

    def test_empty_retired_costs_omits_the_field(self) -> None:
        tco = build_tco_analysis(_data(), retired_costs={})
        assert "retired_engine_savings" not in tco
        assert "eliminated_engine_costs" not in tco

    def test_retired_costs_are_carried_into_the_tco_facts(self) -> None:
        tco = build_tco_analysis(
            _data(), retired_costs={"documentdb": 271.80, "opensearch": 240.96}
        )
        assert tco["retired_engine_savings"] == [
            {"database": "documentdb", "monthly_cost_usd": 271.80},
            {"database": "opensearch", "monthly_cost_usd": 240.96},
        ]
        assert "eliminated_engine_costs" not in tco
