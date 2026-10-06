"""Consolidation records describe the moves the reality check actually made (#218).

The wordpress run (headless validation, job cf163e54) wrote records that did not
match its own distributions: "11 of 25 aurora_mysql queries moved to dynamodb; 7
redirected to aurora_mysql" (it was 12 of 18, and the reversed queries stayed on
Aurora MySQL), "Saves ~$550/mo ... avoiding a dedicated aurora_mysql cluster" while
Aurora MySQL stayed, and no record for the 3 aurora_mysql -> elasticache moves. The
external validator's corrections carry no ``failed_target``, so a correction from
aurora_mysql matched every aurora_mysql consolidation and deleted the smaller ones.

Invariant: applying the recorded moves to ``before_distribution`` gives
``after_distribution`` exactly, and savings are claimed only for an engine that ends
with no in-scope query.
"""

from __future__ import annotations

from collections import Counter

from src.agents.referee.consolidation_validator import apply_corrections
from src.agents.referee.reality_check import (
    _build_recommendations,
    _engine_infra_cost,
    reconcile_consolidations,
)
from src.agents.referee.reality_check_handler import write_reality_check_result
from src.report import renderers
from src.storage.local_store import LocalArtifactStore


def _qa(qid: str, engine: str, reason: str = "assigned", **extra) -> dict:
    return {"query_id": qid, "assigned_engine": engine, "assignment_reason": reason, **extra}


def _moved(qid: str, src: str, dst: str) -> dict:
    return _qa(qid, dst, f"reality check: consolidated from {src} → {dst} (no unique value)")


def _record(src: str, dst: str, n: int, saved: float = 0, action: str = "full") -> dict:
    return {
        "from_engine": src,
        "to_engine": dst,
        "query_count": n,
        "reason": f"{src} provides no unique capabilities — {n} queries can be served",
        "saved_cost_estimate": saved,
        "action": action,
        "queries_retained": [],
        "retention_reason": None,
    }


def _apply_records(before: dict[str, int], records: list[dict]) -> dict[str, int]:
    dist = Counter(before)
    for c in records:
        dist[c["from_engine"]] -= c["query_count"]
        dist[c["to_engine"]] += c["query_count"]
    return {e: n for e, n in dist.items() if n}


def _distribution(assignments: list[dict]) -> dict[str, int]:
    return dict(Counter(qa["assigned_engine"] for qa in assignments))


def _assert_invariant(before: list[dict], after: list[dict], records: list[dict]) -> None:
    assert _apply_records(_distribution(before), records) == _distribution(after)
    ending = {qa["assigned_engine"] for qa in after if qa.get("in_scope", True)}
    for c in records:
        if c["from_engine"] in ending:
            assert c["saved_cost_estimate"] == 0, c
            assert c["action"] == "partial", c


# ---------------------------------------------------------------------------
# The wordpress shape: aurora_mysql 22 -> dynamodb 18, elasticache 3, opensearch 1;
# 7 corrections (6 on dynamodb, 1 on opensearch), Aurora MySQL stays.
# ---------------------------------------------------------------------------


def _wordpress_pass2() -> tuple[list[dict], list[dict], list[dict]]:
    before = (
        [_qa(f"am-{i}", "aurora_mysql") for i in range(22)]
        + [_qa(f"doc-{i}", "documentdb") for i in range(6)]
        + [_qa(f"os-{i}", "opensearch") for i in range(3)]
    )
    revised = (
        [_moved(f"am-{i}", "aurora_mysql", "dynamodb") for i in range(18)]
        + [_moved(f"am-{i}", "aurora_mysql", "elasticache") for i in range(18, 21)]
        + [_moved("am-21", "aurora_mysql", "opensearch")]
        + [_moved(f"doc-{i}", "documentdb", "dynamodb") for i in range(6)]
        + [_qa(f"os-{i}", "opensearch") for i in range(3)]
    )
    records = [
        _record("aurora_mysql", "dynamodb", 18, saved=550),
        _record("aurora_mysql", "elasticache", 3),
        _record("aurora_mysql", "opensearch", 1),
        _record("documentdb", "dynamodb", 6, saved=500),
    ]
    return before, revised, records


def _external_corrections() -> list[dict]:
    # The external validator's response shape: no failed_target.
    ids = [f"am-{i}" for i in range(6)] + ["am-21"]
    return [{"query_id": q, "original_engine": "aurora_mysql", "reason": "joins"} for q in ids]


class TestApplyCorrections:
    def _run(self) -> tuple[list[dict], list[dict]]:
        _, revised, records = _wordpress_pass2()
        return apply_corrections(
            _external_corrections(),
            revised,
            records,
            surviving_engines={"dynamodb", "elasticache", "opensearch"},
            all_original_engines={"aurora_mysql", "dynamodb", "elasticache", "opensearch"},
        )

    def test_a_correction_only_touches_the_consolidation_its_query_was_in(self) -> None:
        _, records = self._run()
        pairs = {(c["from_engine"], c["to_engine"]): c["query_count"] for c in records}
        assert pairs == {
            ("aurora_mysql", "dynamodb"): 12,
            ("aurora_mysql", "elasticache"): 3,
            ("documentdb", "dynamodb"): 6,
        }

    def test_denominator_is_the_number_proposed_and_reversed_queries_stay(self) -> None:
        _, records = self._run()
        ddb = next(
            c for c in records if c["to_engine"] == "dynamodb" and c["from_engine"] != "documentdb"
        )
        assert "12 of 18 aurora_mysql queries moved to dynamodb" in ddb["reason"]
        assert "6 stay on aurora_mysql" in ddb["reason"]
        assert "redirected to aurora_mysql" not in ddb["reason"]
        assert "redirected" not in (ddb["retention_reason"] or "")
        assert ddb["action"] == "partial"
        assert ddb["saved_cost_estimate"] == 0

    def test_reversed_query_reason_does_not_claim_a_redirect(self) -> None:
        assignments, _ = self._run()
        q = next(qa for qa in assignments if qa["query_id"] == "am-0")
        assert q["assigned_engine"] == "aurora_mysql"
        assert "redirected to aurora_mysql" not in q["assignment_reason"]

    def test_redirect_to_a_different_engine_is_described_as_a_redirect(self) -> None:
        revised = [
            _moved("doc-0", "documentdb", "dynamodb"),
            _moved("doc-1", "documentdb", "dynamodb"),
        ]
        _, records = apply_corrections(
            [{"query_id": "doc-0", "original_engine": "documentdb", "reason": "joins"}],
            revised,
            [_record("documentdb", "dynamodb", 2, saved=500)],
            all_original_engines={"documentdb", "dynamodb", "aurora_postgresql"},
        )
        assert "1 of 2 documentdb queries moved to dynamodb" in records[0]["reason"]
        assert "1 redirected to aurora_postgresql" in records[0]["reason"]

    def test_a_single_reversal_reads_singular(self) -> None:
        revised = [_moved(q, "documentdb", "dynamodb") for q in ("d0", "d1", "d2")]
        _, records = apply_corrections(
            [{"query_id": "d0", "original_engine": "documentdb", "reason": "joins"}],
            revised,
            [_record("documentdb", "dynamodb", 3, saved=500)],
        )
        assert "1 stays on documentdb" in records[0]["reason"]


class TestReconcile:
    def test_records_reproduce_the_final_distribution(self) -> None:
        before, revised, records = _wordpress_pass2()
        revised, records = apply_corrections(
            _external_corrections(),
            revised,
            records,
            all_original_engines={"aurora_mysql", "dynamodb", "elasticache", "opensearch"},
        )
        # Re-run absorption moved OpenSearch's 3 queries into Aurora MySQL.
        for qa in revised:
            if qa["assigned_engine"] == "opensearch":
                qa["assigned_engine"] = "aurora_mysql"
        records.append(_record("opensearch", "aurora_mysql", 3, saved=450))

        final = reconcile_consolidations(before, revised, records)

        _assert_invariant(before, revised, final)
        assert _distribution(revised) == {"dynamodb": 18, "aurora_mysql": 10, "elasticache": 3}
        by_pair = {(c["from_engine"], c["to_engine"]): c for c in final}
        assert by_pair[("aurora_mysql", "elasticache")]["action"] == "partial"
        assert by_pair[("opensearch", "aurora_mysql")]["saved_cost_estimate"] == 450
        assert by_pair[("documentdb", "dynamodb")]["saved_cost_estimate"] == 500

    def test_unrecorded_move_gets_a_record(self) -> None:
        before = [_qa("a", "aurora_mysql"), _qa("b", "aurora_mysql"), _qa("c", "opensearch")]
        after = [_qa("a", "dynamodb"), _qa("b", "aurora_mysql"), _qa("c", "aurora_mysql")]
        final = reconcile_consolidations(before, after, [])
        _assert_invariant(before, after, final)
        am = next(c for c in final if c["from_engine"] == "aurora_mysql")
        assert am["query_count"] == 1 and am["action"] == "partial"
        os_ = next(c for c in final if c["from_engine"] == "opensearch")
        assert os_["action"] == "full" and os_["saved_cost_estimate"] > 0

    def test_overstated_count_is_corrected_and_net_zero_moves_dropped(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb"), _qa("c", "opensearch")]
        # c went opensearch -> documentdb -> back: no net move.
        after = [_qa("a", "dynamodb"), _qa("b", "dynamodb"), _qa("c", "opensearch")]
        records = [
            _record("documentdb", "dynamodb", 5, saved=500),
            _record("opensearch", "documentdb", 1, saved=450),
        ]
        final = reconcile_consolidations(before, after, records)
        _assert_invariant(before, after, final)
        assert [(c["from_engine"], c["query_count"]) for c in final] == [("documentdb", 2)]
        assert final[0]["saved_cost_estimate"] == 500

    def test_savings_counted_once_per_eliminated_engine(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb")]
        after = [_qa("a", "dynamodb"), _qa("b", "aurora_mysql")]
        records = [
            _record("documentdb", "dynamodb", 1, saved=500),
            _record("documentdb", "aurora_mysql", 1, saved=500),
        ]
        final = reconcile_consolidations(before, after, records)
        assert sum(c["saved_cost_estimate"] for c in final) == 500

    def test_out_of_scope_leftovers_do_not_keep_an_engine(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb", in_scope=False)]
        after = [_qa("a", "dynamodb"), _qa("b", "documentdb", in_scope=False)]
        final = reconcile_consolidations(before, after, [_record("documentdb", "dynamodb", 1, 500)])
        assert final[0]["action"] == "full" and final[0]["saved_cost_estimate"] == 500

    def test_customer_override_left_on_an_emptied_engine_keeps_it(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "documentdb", customer_override=True)]
        after = [_qa("a", "dynamodb"), _qa("b", "documentdb", customer_override=True)]
        final = reconcile_consolidations(before, after, [_record("documentdb", "dynamodb", 1, 500)])
        assert final[0]["action"] == "partial" and final[0]["saved_cost_estimate"] == 0

    def test_engine_with_no_in_scope_query_in_the_input_claims_no_savings(self) -> None:
        before = [_qa("a", "opensearch", in_scope=False), _qa("b", "dynamodb")]
        after = [_qa("a", "dynamodb", in_scope=False), _qa("b", "dynamodb")]
        final = reconcile_consolidations(before, after, [_record("opensearch", "dynamodb", 1, 450)])
        assert final[0]["action"] == "partial" and final[0]["saved_cost_estimate"] == 0
        _assert_invariant(before, after, final)

    def test_reason_is_refreshed_when_the_retained_queries_change(self) -> None:
        before = [_qa(q, "aurora_mysql") for q in ("a", "b", "c", "d")]
        # b was retained on aurora_mysql, then the sweep moved it to elasticache.
        after = [_qa("a", "dynamodb"), _qa("b", "elasticache"), _qa("c", "aurora_mysql")]
        after.append(_qa("d", "aurora_mysql"))
        record = {
            **_record("aurora_mysql", "dynamodb", 1, action="partial"),
            "reason": "Partial consolidation: 1 of 3 aurora_mysql queries moved to dynamodb; "
            "2 stay on aurora_mysql (unserviceable on dynamodb)",
            "queries_retained": ["b", "c"],
        }
        final = reconcile_consolidations(before, after, [record])
        ddb = next(c for c in final if c["to_engine"] == "dynamodb")
        assert ddb["queries_retained"] == ["c"]
        assert "2 stay on" not in ddb["reason"]
        assert "1 aurora_mysql query moved to dynamodb" in ddb["reason"]
        assert "aurora_mysql stays for 2 queries" in ddb["reason"]

    def test_cost_justification_survives_the_reconciler(self) -> None:
        """Review finding 5: the justification-floor reason (#326, #167) must survive
        reconcile_consolidations, which otherwise overwrites the reason with a generic
        "N queries moved; no in-scope query remains" when anything else moved too."""
        before = [_qa(q, "opensearch") for q in ("a", "b", "c")]
        after = [_qa(q, "aurora_mysql") for q in ("a", "b", "c")]
        unique_value_assessment = {
            "opensearch": {
                "cost_justification": ("3 queries use LIKE/prefix matching, not relevance ranking")
            }
        }
        final = reconcile_consolidations(before, after, [], unique_value_assessment)
        record = next(c for c in final if c["from_engine"] == "opensearch")
        assert "LIKE/prefix" in record["reason"]

    def test_no_unique_value_assessment_is_fine(self) -> None:
        """The parameter is optional -- existing callers that don't pass it keep working."""
        before = [_qa("a", "opensearch")]
        after = [_qa("a", "aurora_mysql")]
        final = reconcile_consolidations(before, after, [])
        assert final[0]["from_engine"] == "opensearch"

    def test_cost_justification_only_attaches_to_the_floors_own_pair(self) -> None:
        """Review of #375, finding 5's wording problem: a review found the floor's
        reason attached to a 119-query opensearch->aurora_postgresql record and to a
        separate 29-query opensearch->dynamodb record the floor had no part in. Only
        a record whose own net-moved queries overlap cost_justification_query_ids
        gets the floor's text."""
        before = [_qa(q, "opensearch") for q in ("a", "b", "c", "d")]
        # a, b, c moved by ordinary consolidation (unrelated to the floor); d is the
        # floor's own move, to a *different* target engine.
        after = [_qa(q, "aurora_postgresql") for q in ("a", "b", "c")] + [_qa("d", "dynamodb")]
        unique_value_assessment = {
            "opensearch": {
                "cost_justification": "1 of 1 queries have no search predicate",
                "cost_justification_query_ids": ["d"],
            }
        }
        final = reconcile_consolidations(before, after, [], unique_value_assessment)
        to_aurora = next(c for c in final if c["to_engine"] == "aurora_postgresql")
        to_dynamodb = next(c for c in final if c["to_engine"] == "dynamodb")
        assert "no search predicate" not in to_aurora["reason"]
        assert "no search predicate" in to_dynamodb["reason"]

    def test_cost_justification_attaches_when_the_pair_matches(self) -> None:
        before = [_qa(q, "opensearch") for q in ("a", "b")]
        after = [_qa(q, "aurora_mysql") for q in ("a", "b")]
        unique_value_assessment = {
            "opensearch": {
                "cost_justification": "2 of 2 queries have no search predicate",
                "cost_justification_query_ids": ["a", "b"],
            }
        }
        final = reconcile_consolidations(before, after, [], unique_value_assessment)
        record = next(c for c in final if c["from_engine"] == "opensearch")
        assert "no search predicate" in record["reason"]

    def test_cost_justification_states_how_many_of_the_record_it_covers(self) -> None:
        """A second review of #375: the floor's text used to read as if it applied
        to every query in the record (e.g. a 119-query record saying "no search
        predicate" with no hint that the floor itself only moved a handful of
        those 119). The reason must say how many of the record's own queries the
        floor actually moved."""
        before = [_qa(q, "opensearch") for q in ("a", "b", "c", "d", "e")]
        # a..d all move opensearch -> aurora_postgresql; only c and d are the
        # floor's own moves (the other two moved for an unrelated reason).
        after = [_qa(q, "aurora_postgresql") for q in ("a", "b", "c", "d")] + [
            _qa("e", "opensearch")
        ]
        unique_value_assessment = {
            "opensearch": {
                "cost_justification": "2 of 2 queries have no search predicate",
                "cost_justification_query_ids": ["c", "d"],
            }
        }
        final = reconcile_consolidations(before, after, [], unique_value_assessment)
        record = next(c for c in final if c["from_engine"] == "opensearch")
        assert "2 queries of these, moved by the justification floor" in record["reason"]
        assert "no search predicate" in record["reason"]

    def test_counts_of_one_are_singular(self) -> None:
        before = [_qa("a", "documentdb"), _qa("b", "aurora_mysql"), _qa("c", "aurora_mysql")]
        after = [_qa("a", "dynamodb"), _qa("b", "dynamodb"), _qa("c", "aurora_mysql")]
        final = reconcile_consolidations(before, after, [])
        text = " ".join(c["reason"] for c in final) + " ".join(
            _build_recommendations([], final, [], {})
        )
        assert "1 documentdb query moved" in text
        assert "stays for 1 query" in text
        assert "Consolidated 1 query from" in text
        assert "1 queries" not in text


class TestEngineInfraCost:
    """Review of #375: the real per-engine cost tco_analysis's own cost_breakdown
    is built from (``build_tco_analysis`` in synthesis_report.py reads the same
    ``cost_estimate.monthly_cost_usd`` field)."""

    def test_reads_monthly_cost_usd_per_engine(self) -> None:
        analysis_outputs = {
            "opensearch": {"cost_estimate": {"monthly_cost_usd": 240.96}},
            "dynamodb": {"cost_estimate": {"monthly_cost_usd": 15.2}},
        }
        assert _engine_infra_cost(analysis_outputs) == {"opensearch": 240.96, "dynamodb": 15.2}

    def test_engine_with_no_cost_estimate_is_absent(self) -> None:
        assert _engine_infra_cost({"documentdb": {}}) == {}

    def test_none_or_empty_input_is_fine(self) -> None:
        assert _engine_infra_cost(None) == {}
        assert _engine_infra_cost({}) == {}


class TestRecommendations:
    def test_partial_consolidation_claims_no_avoided_cluster(self) -> None:
        rec = _build_recommendations(
            [], [_record("aurora_mysql", "dynamodb", 12, saved=0, action="partial")], [], {}
        )[0]
        assert "avoiding a dedicated aurora_mysql cluster" not in rec
        assert "Saves" not in rec
        assert "does not remove aurora_mysql" in rec

    def test_full_consolidation_with_no_real_cost_estimate_stays_qualitative(self) -> None:
        """Review of #375: with no analysed cost estimate for the removed engine,
        the sentence must not fall back to the ENGINE_BASE_COST heuristic -- no
        dollar figure is claimed at all, only the qualitative overhead reduction."""
        rec = _build_recommendations([], [_record("documentdb", "dynamodb", 6, saved=500)], [], {})[
            0
        ]
        assert "Saves" not in rec
        assert "$" not in rec
        assert "Avoids running a dedicated documentdb cluster" in rec

    def test_full_consolidation_grounds_its_cost_in_the_real_analysed_estimate(self) -> None:
        """Review of #375: when the removed engine's own analysed cost estimate
        is available, the sentence states that real figure (traceable to
        tco_analysis's cost_breakdown), not the ENGINE_BASE_COST +
        EXTRA_ENGINE_BURDEN_MONTHLY operational-overhead heuristic."""
        rec = _build_recommendations(
            [],
            [_record("opensearch", "aurora_postgresql", 119, saved=450)],
            [],
            {},
            {"opensearch": 240.96},
        )[0]
        assert "$240.96" in rec
        assert "$450" not in rec
        assert "Saves ~$" not in rec


class TestWrittenOutput:
    def test_output_records_reconcile_with_the_shipped_distributions(self, tmp_path) -> None:
        before, revised, records = _wordpress_pass2()
        # Simulate the run-3 drift: two records lost, one overstated.
        records = [records[0], records[3]]
        result = {
            "revised_assignments": revised,
            "consolidations": records,
            "architectural_patterns": [],
            "recommendations": [],
            "unique_value_assessment": {},
            "executive_summary": None,
            "before_distribution": _distribution(before),
            "after_distribution": _distribution(revised),
            "lightweight_recommendations": [],
            "assignment": {"query_assignments": before},
            "collector_output": {},
            "analysis_outputs": {},
        }
        store = LocalArtifactStore(base_dir=str(tmp_path))
        write_reality_check_result(store, "job-1", "mydb", result, 1, write_revision=False)
        out = store.read_json("mydb/job-1/reality-check/output.json")
        assert (
            _apply_records(out["before_distribution"], out["consolidations"])
            == out["after_distribution"]
        )
        _assert_invariant(before, revised, out["consolidations"])


class TestEngineeringReport:
    def test_trade_off_whose_impact_repeats_its_title_renders_once(self) -> None:
        line = "Consolidated 6 queries from documentdb → dynamodb: no unique capabilities."
        md = renderers.render_engineering_report_md(
            {"trade_offs": [{"description": line, "impact": line, "engine": "reality-check"}]}
        )
        assert md.count("no unique capabilities") == 1
