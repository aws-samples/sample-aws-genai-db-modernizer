"""One saving figure per removed engine, from its analysed cost (#793).

The reality check used to store ``ENGINE_BASE_COST + EXTRA_ENGINE_BURDEN_MONTHLY``
($500) on the first consolidation record of a removed engine and log it, while the
deliverables quote the engine's analysed cost (``tco_analysis.eliminated_engine_costs``,
$367.39 for DocumentDB on the wordpress sample). ADR-031 Decision 7 stays: a saving
is claimed only for an engine the first assignment justified, and counted once.
"""

from __future__ import annotations

from src.agents.referee.reality_check import _build_recommendations
from src.agents.referee.reality_check_records import removal_log_lines
from tests.unit.agents.referee.ledger_fakes import records_from_moves

WORDPRESS_DOCDB_COST = 367.39


def _qa(qid: str, engine: str) -> dict:
    return {"query_id": qid, "assigned_engine": engine, "assignment_reason": "assigned"}


def _record(src: str, dst: str, n: int, **extra) -> dict:
    return {
        "from_engine": src,
        "to_engine": dst,
        "query_count": n,
        "reason": f"{src} provides no unique capabilities",
        "saved_cost_estimate": 500.0,
        "action": "full",
        "queries_retained": [],
        "retention_reason": None,
        **extra,
    }


def _wordpress_like() -> tuple[list[dict], list[dict], list[dict]]:
    """DocumentDB removed through two moves: 2 queries to Aurora MySQL, 5 to DynamoDB."""
    before = [_qa(f"q{i}", "documentdb") for i in range(7)]
    after = [_qa(f"q{i}", "aurora_mysql" if i < 2 else "dynamodb") for i in range(7)]
    records = [
        _record("documentdb", "aurora_mysql", 2),
        _record("documentdb", "dynamodb", 5, saved_cost_estimate=0.0),
    ]
    return before, after, records


class TestReconcileUsesTheAnalysedCost:
    def test_saving_is_the_analysed_cost_not_the_placeholder(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(
            before, after, records, engine_costs={"documentdb": WORDPRESS_DOCDB_COST}
        )
        assert sum(c["saved_cost_estimate"] for c in final) == WORDPRESS_DOCDB_COST
        assert all(c["saved_cost_estimate"] != 500.0 for c in final)

    def test_every_record_of_the_engine_marks_the_removal_as_claimed(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(
            before, after, records, engine_costs={"documentdb": WORDPRESS_DOCDB_COST}
        )
        assert [c["removal_saving_claimed"] for c in final] == [True, True]

    def test_no_analysed_cost_claims_no_dollar_figure(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(before, after, records, engine_costs={})
        assert [c["saved_cost_estimate"] for c in final] == [0.0, 0.0]
        assert [c["removal_saving_claimed"] for c in final] == [True, True]

    def test_unjustified_engine_claims_nothing(self) -> None:
        before = [_qa("a", "opensearch"), _qa("b", "opensearch")]
        after = [_qa("a", "aurora_postgresql"), _qa("b", "aurora_postgresql")]
        final = records_from_moves(
            before,
            after,
            [_record("opensearch", "aurora_postgresql", 2, workload_justified=False)],
            engine_costs={"opensearch": 240.96},
        )
        assert final[0]["saved_cost_estimate"] == 0
        assert final[0]["removal_saving_claimed"] is False

    def test_partial_move_claims_nothing(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb")]
        after = [_qa("a", "dynamodb"), _qa("b", "documentdb")]
        final = records_from_moves(
            before,
            after,
            [_record("documentdb", "dynamodb", 1)],
            engine_costs={"documentdb": WORDPRESS_DOCDB_COST},
        )
        assert final[0]["action"] == "partial"
        assert final[0]["saved_cost_estimate"] == 0
        assert final[0]["removal_saving_claimed"] is False


class TestRecommendationWording:
    def _recs(self) -> list[str]:
        before, after, records = _wordpress_like()
        final = records_from_moves(
            before, after, records, engine_costs={"documentdb": WORDPRESS_DOCDB_COST}
        )
        return _build_recommendations(after, final, [], {}, {"documentdb": WORDPRESS_DOCDB_COST})

    def test_first_move_states_the_figure_first_and_later_moves_refer_back(self) -> None:
        first, later = self._recs()
        detail = first.split(": ", 1)[1]
        assert (
            "Removes $367.39/mo by retiring DocumentDB (2 queries to Aurora MySQL, 5 to DynamoDB)"
            in detail
        )
        # The deck clips a card's detail at 170 characters (RC_DETAIL_CLIP): the figure survives.
        assert "$367.39" in detail[:170]
        assert "$" not in later.split("DynamoDB: ", 1)[1]
        assert later.endswith("Part of retiring DocumentDB (saving counted on its first move).")
        for rec in (first, later):
            assert "counted once for the engine" not in rec
            assert "leaves the architecture" not in rec

    def test_the_figure_appears_once_across_the_engines_moves(self) -> None:
        assert sum(r.count("$367.39") for r in self._recs()) == 1

    def test_a_single_move_keeps_its_wording(self) -> None:
        before = [_qa("a", "opensearch")]
        after = [_qa("a", "aurora_mysql")]
        final = records_from_moves(
            before,
            after,
            [_record("opensearch", "aurora_mysql", 1)],
            engine_costs={"opensearch": 240.96},
        )
        rec = _build_recommendations(after, final, [], {}, {"opensearch": 240.96})[0]
        assert "Removes $240.96/mo of infrastructure cost" in rec
        assert "counted once" not in rec

    def test_a_multi_move_engine_without_a_cost_names_no_figure(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(before, after, records, engine_costs={})
        recs = _build_recommendations(after, final, [], {}, {})
        assert all("$" not in r for r in recs)
        assert "Retiring DocumentDB (2 queries to Aurora MySQL, 5 to DynamoDB)" in recs[0]
        assert recs[1].endswith("Part of retiring DocumentDB.")


class TestLogLines:
    def test_one_saving_line_per_removed_engine_with_the_same_figure(self) -> None:
        before, after, records = _wordpress_like()
        final = records_from_moves(
            before, after, records, engine_costs={"documentdb": WORDPRESS_DOCDB_COST}
        )
        lines = removal_log_lines(final)
        assert lines == [
            "Consolidated 2 queries: documentdb → aurora_mysql",
            "Consolidated 5 queries: documentdb → dynamodb",
            "Removed documentdb: saves ~$367.39/mo (its analysed cost, counted once "
            "across 2 moves)",
        ]

    def test_unjustified_removal_says_no_saving_claimed(self) -> None:
        records = [
            {
                **_record("opensearch", "aurora_postgresql", 2),
                "saved_cost_estimate": 0,
                "removal_saving_claimed": False,
            }
        ]
        assert removal_log_lines(records)[-1] == (
            "Removed opensearch: no saving claimed (never justified by the workload)"
        )


class TestOldRecordsLoad:
    def test_a_record_written_before_1_12_that_claimed_a_saving_reads_as_claimed(self) -> None:
        from src.contracts.reality_check_output import Consolidation

        old = _record("documentdb", "dynamodb", 3)
        assert Consolidation(**old).removal_saving_claimed is True
        assert Consolidation(**{**old, "saved_cost_estimate": 0}).removal_saving_claimed is False
        explicit = Consolidation(**{**old, "removal_saving_claimed": False})
        assert explicit.removal_saving_claimed is False
