# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A dropped candidate engine is never a saving (#949).

The reality check and synthesis priced every engine the plan analysed and then
removed as if the customer paid for it today, so "retiring" it read as a saving:
"Removes $367.39/mo by retiring DocumentDB" on the wordpress sample, whose source
runs only MySQL. A saving, or a retired-engine line, may only name an engine the
source runs today: the source engine itself, or one the collection records in
``metadata.engines_in_use``. A candidate the plan considered and dropped is kept
in ``report.json`` as ``considered_not_recommended``, with no dollar figure.
"""

from __future__ import annotations

import pytest

from src.agents.referee.reality_check import _build_recommendations
from src.agents.referee.reality_check_records import removal_log_lines
from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.shared.engines_in_use import engines_in_use
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.ledger_fakes import records_from_moves

DOCDB_COST = 367.39


def _qa(qid: str, engine: str) -> dict:
    return {"query_id": qid, "assigned_engine": engine, "assignment_reason": "assigned"}


def _record(src: str, dst: str, n: int, **extra) -> dict:
    return {
        "from_engine": src,
        "to_engine": dst,
        "query_count": n,
        "reason": f"{src} provides no unique capabilities",
        "saved_cost_estimate": 0.0,
        "action": "full",
        "queries_retained": [],
        "retention_reason": None,
        **extra,
    }


def _wordpress_like() -> tuple[list[dict], list[dict], list[dict]]:
    """DocumentDB, a candidate, removed through two moves (the wordpress shape)."""
    before = [_qa(f"q{i}", "documentdb") for i in range(7)]
    after = [_qa(f"q{i}", "aurora_mysql" if i < 2 else "dynamodb") for i in range(7)]
    records = [_record("documentdb", "aurora_mysql", 2), _record("documentdb", "dynamodb", 5)]
    return before, after, records


class TestEnginesInUse:
    def test_the_source_engine_and_the_recorded_engines(self) -> None:
        collector = {
            "metadata": {
                "source_database": {"engine": "MySQL"},
                "engines_in_use": ["elasticache", "DocumentDB"],
            }
        }
        assert engines_in_use(collector) == {"mysql", "elasticache", "documentdb"}

    def test_a_collection_without_the_field_runs_only_its_source(self) -> None:
        assert engines_in_use({"metadata": {"source_database": {"engine": "mysql"}}}) == {"mysql"}

    def test_empty_or_missing_collection(self) -> None:
        assert engines_in_use({}) == frozenset()
        assert engines_in_use(None) == frozenset()
        assert engines_in_use({"metadata": {"engines_in_use": [None, "", 3]}}) == frozenset()


class TestRecordsClaimNoSavingForACandidate:
    def test_a_dropped_candidate_claims_nothing(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(
            before, after, records, engine_costs={"documentdb": DOCDB_COST}, engines_in_use=set()
        )
        assert [c["saved_cost_estimate"] for c in final] == [0.0, 0.0]
        assert [c["removal_saving_claimed"] for c in final] == [False, False]
        assert [c["action"] for c in final] == ["full", "full"]

    def test_no_in_use_set_means_nothing_runs_besides_the_source(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(before, after, records, engine_costs={"documentdb": DOCDB_COST})
        assert sum(c["saved_cost_estimate"] for c in final) == 0
        assert not any(c["removal_saving_claimed"] for c in final)

    def test_an_engine_the_source_runs_today_still_claims_its_saving(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(
            before,
            after,
            records,
            engine_costs={"documentdb": DOCDB_COST},
            engines_in_use={"mysql", "documentdb"},
        )
        assert [c["saved_cost_estimate"] for c in final] == [DOCDB_COST, 0.0]
        assert [c["removal_saving_claimed"] for c in final] == [True, True]

    def test_the_log_says_why_no_saving_is_claimed(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(before, after, records, engine_costs={"documentdb": DOCDB_COST})
        assert removal_log_lines(final)[-1] == (
            "Removed documentdb: no saving claimed (not running today; a candidate the plan "
            "considered)"
        )


class TestRecommendationWording:
    def test_a_dropped_candidate_names_no_figure_and_no_retirement(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(before, after, records, engine_costs={"documentdb": DOCDB_COST})
        recs = _build_recommendations(
            after, final, [], {}, {"documentdb": DOCDB_COST}, engines_in_use={"mysql"}
        )
        assert len(recs) == 2
        for rec in recs:
            assert "$" not in rec
            assert "retir" not in rec.lower()
            assert "saving" not in rec.lower() or "no saving is claimed" in rec.lower()
        assert recs[0].endswith(
            "No saving is claimed: DocumentDB does not run today; it was a candidate this "
            "plan considered and does not recommend."
        )
        assert recs[1].endswith("DocumentDB does not run today, so no saving is claimed.")

    def test_an_engine_the_source_runs_today_keeps_its_retirement_saving(self) -> None:
        before, after, records = _wordpress_like()
        in_use = {"mysql", "documentdb"}
        final = records_from_moves(
            before,
            after,
            records,
            engine_costs={"documentdb": DOCDB_COST},
            engines_in_use=in_use,
        )
        recs = _build_recommendations(
            after, final, [], {}, {"documentdb": DOCDB_COST}, engines_in_use=in_use
        )
        assert "Removes $367.39/mo by retiring DocumentDB" in recs[0]

    def test_never_justified_keeps_its_own_reason(self) -> None:
        """A candidate the workload never justified keeps the #475 wording (no drift)."""
        before = [_qa("a", "opensearch")]
        after = [_qa("a", "aurora_postgresql")]
        final = records_from_moves(
            before,
            after,
            [_record("opensearch", "aurora_postgresql", 1, workload_justified=False)],
            engine_costs={"opensearch": 240.96},
        )
        rec = _build_recommendations(after, final, [], {}, {"opensearch": 240.96})[0]
        assert "was never justified by the workload" in rec
        assert "$" not in rec

    def test_a_partial_move_keeps_its_wording(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb")]
        after = [_qa("a", "dynamodb"), _qa("b", "documentdb")]
        final = records_from_moves(before, after, [_record("documentdb", "dynamodb", 1)])
        rec = _build_recommendations(after, final, [], {}, {})[0]
        assert "does not remove DocumentDB from the architecture" in rec


# ---------------------------------------------------------------------------
# Synthesis: the TCO facts
# ---------------------------------------------------------------------------

DB = "shop"
JOB = "job-949"


def _store(tmp_path, engines_in_use_list: list[str] | None = None) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {
            "selected_agents": [
                {"agent_type": e} for e in ("dynamodb", "aurora_mysql", "opensearch")
            ]
        },
    )
    metadata: dict = {"source_database": {"engine": "mysql"}}
    if engines_in_use_list is not None:
        metadata["engines_in_use"] = engines_in_use_list
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": metadata,
            "database_schema": {"tables": [{"table_id": "shop.orders"}]},
            "queries": {},
        },
    )
    for engine, cost in (("dynamodb", 15.2), ("aurora_mysql", 121.06), ("opensearch", 240.96)):
        s.write_json(
            f"{DB}/{JOB}/analysis-{engine}/analysis.json",
            {"workload_analysis": {}, "cost_estimate": {"monthly_cost_usd": cost}},
        )
    s.write_json(
        f"{DB}/{JOB}/assignment/v2/assignment.json",
        {
            "version": 2,
            "status": "reality_checked",
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True},
                {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
            ],
        },
    )
    s.write_json(
        f"{DB}/{JOB}/reality-check/output.json",
        {
            "consolidations": [
                {
                    "from_engine": "opensearch",
                    "to_engine": "dynamodb",
                    "query_count": 1,
                    "reason": "1 opensearch query moved to dynamodb",
                    "saved_cost_estimate": 240.96,
                    "action": "full",
                }
            ],
            "architectural_patterns": [],
        },
    )
    return s


class TestSynthesisTco:
    def test_a_dropped_candidate_is_not_a_saving(self, tmp_path) -> None:
        result = run_synthesis_deterministic(JOB, DB, _store(tmp_path), assignment_version=2)
        tco = result["tco_analysis"]
        assert "retired_engine_savings" not in tco
        assert "eliminated_engine_costs" not in tco
        assert result["considered_not_recommended"] == [
            {"engine": "opensearch", "absorbed_by": "dynamodb"}
        ]
        assert tco["projected_monthly_cost"] == pytest.approx(15.2 + 121.06, abs=0.01)

    def test_an_engine_the_source_runs_today_is_a_saving(self, tmp_path) -> None:
        store = _store(tmp_path, engines_in_use_list=["opensearch"])
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        tco = result["tco_analysis"]
        assert tco["retired_engine_savings"] == [
            {"database": "opensearch", "monthly_cost_usd": 240.96}
        ]
        assert "eliminated_engine_costs" not in tco
        assert result["considered_not_recommended"] == []
        # The retired engine's cost is never part of the projection.
        assert tco["projected_monthly_cost"] == pytest.approx(15.2 + 121.06, abs=0.01)
