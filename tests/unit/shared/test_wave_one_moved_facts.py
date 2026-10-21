"""What later waves move off wave 1's Aurora engine, as a report fact (#1284, ADR-032).

Wave 1's rationale said "N of these tables and views are mapped to a target engine
in a later wave", with N every migration-map row, including the rows mapped to
Aurora itself, which stay. The tables a later wave moves are now one figure,
``facts.waves[].moved_by_later_waves`` on wave 1's row, computed by the facts layer
from the waves (the migrating waves' own "moves" rule), and the rationale states
that figure.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.report.waves import resolve_migration_waves
from src.shared.report_facts import compute_report_facts, wave_one_moved_by_later_waves

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


def _wave(n: int, engines: list[str], tables: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "wave": n,
        "engines": engines,
        "tables": tables,
        "table_count": len(tables),
        "moves_from": extra.pop("moves_from", []),
        **extra,
    }


WAVE_1 = _wave(
    1,
    ["aurora_mysql"],
    ["a", "b", "c", "d", "v"],
    moves_from=["mysql"],
    homogeneity="homogeneous",
)
CACHE = _wave(2, ["elasticache"], [])
DYNAMO = _wave(
    3,
    ["dynamodb"],
    ["a", "b", "c"],
    moves_from=["aurora_mysql"],
    # "c" is served here but owned by OpenSearch's owner outside the wave: kept in
    # sync, not moved.
    table_owners=[{"table": "c", "owner": "aurora_mysql"}],
)
SEARCH = _wave(4, ["opensearch"], ["d"], table_owners=[{"table": "d", "owner": "aurora_mysql"}])


class TestWaveOneMovedByLaterWaves:
    def test_counts_only_what_a_migrating_wave_moves(self) -> None:
        # a, b move to DynamoDB; c is kept in sync; d is only read by the search
        # read model; v never leaves Aurora.
        assert wave_one_moved_by_later_waves([WAVE_1, CACHE, DYNAMO, SEARCH]) == 2

    def test_no_later_migrating_wave_moves_nothing(self) -> None:
        assert wave_one_moved_by_later_waves([WAVE_1, CACHE, SEARCH]) == 0

    def test_none_when_wave_1_names_no_aurora_engine(self) -> None:
        hetero = {**WAVE_1, "engines": [], "homogeneity": "heterogeneous"}
        assert wave_one_moved_by_later_waves([hetero, DYNAMO]) is None
        assert wave_one_moved_by_later_waves([DYNAMO]) is None
        assert wave_one_moved_by_later_waves([]) is None

    def test_a_cross_engine_wave_1_counts_the_same_way(self) -> None:
        cross = {**WAVE_1, "moves_from": ["sqlserver"], "homogeneity": "cross_engine"}
        assert wave_one_moved_by_later_waves([cross, DYNAMO]) == 2


def _report() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(
        (FIXTURES / "wordpress_model_run_1_39_report.json").read_text()
    )
    return report


class TestTheFactsBlock:
    def test_only_wave_1s_row_carries_it(self) -> None:
        report = _report()
        rows = compute_report_facts(report)["waves"]
        waves = report["migration_waves"]
        assert rows[0]["moved_by_later_waves"] == wave_one_moved_by_later_waves(waves)
        assert isinstance(rows[0]["moved_by_later_waves"], int)
        assert all(r["moved_by_later_waves"] is None for r in rows[1:])
        # The contract keeps the figure on a round trip (WaveFacts ignores unknown keys).
        dumped = ReportFacts.model_validate(compute_report_facts(report)).model_dump()
        assert dumped["waves"][0]["moved_by_later_waves"] == rows[0]["moved_by_later_waves"]

    def test_a_block_stored_before_the_field_gains_it_when_read(self) -> None:
        report = _report()
        stored = compute_report_facts(report)
        for row in stored["waves"]:
            del row["moved_by_later_waves"]
        report["facts"] = copy.deepcopy(stored)
        rows = report_facts(report)["waves"]
        assert rows[0]["moved_by_later_waves"] == wave_one_moved_by_later_waves(
            resolve_migration_waves(report)
        )
        assert all(r["moved_by_later_waves"] is None for r in rows[1:])

    def test_a_wave_row_that_is_not_a_record_is_kept_as_stored(self) -> None:
        report = _report()
        stored = compute_report_facts(report)
        for row in stored["waves"]:
            del row["moved_by_later_waves"]
        stored["waves"].append("not a row")
        report["facts"] = stored
        rows = report_facts(report)["waves"]
        assert rows[-1] == "not a row"
        assert len(rows) == len(stored["waves"])
        assert isinstance(rows[0]["moved_by_later_waves"], int)

    def test_a_stored_figure_is_read_as_written(self) -> None:
        report = _report()
        stored = compute_report_facts(report)
        stored["waves"][0]["moved_by_later_waves"] = 7
        report["facts"] = stored
        assert report_facts(report)["waves"][0]["moved_by_later_waves"] == 7

    def test_an_old_report_with_the_old_sentence_still_loads(self) -> None:
        """A report synthesized before #1284 keeps its stored rationale, and its
        facts gain the figure."""
        report = json.loads((FIXTURES / "wordpress_model_run_1_39_report.json").read_text())
        assert (
            "mapped to a target engine in a later wave" in report["migration_waves"][0]["rationale"]
        )
        rows = report_facts(report)["waves"]
        assert isinstance(rows[0]["moved_by_later_waves"], int)
        ReportFacts.model_validate(report_facts(report))
