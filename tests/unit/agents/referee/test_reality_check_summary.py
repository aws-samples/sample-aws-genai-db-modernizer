"""The reality-check executive summary must describe the final assignment (#236).

The summary is written before the LLM corrections, the re-run Aurora absorption, the
sanity sweep and the record reconciliation (external mode: by the LLM, from the
deterministic preview). Those steps can still consolidate an engine away, or keep one.
``finalize_executive_summary`` checks the summary against the final records and, on a
contradiction, replaces it with a deterministic summary built from them; the LLM text
is kept for audit.

The summaries below are the actual texts from headless WordPress runs: every
reality-check summary named OpenSearch as part of the architecture, while the final
records consolidated OpenSearch into Aurora MySQL. The synthesis summaries of the same
runs describe the final result, so the check must accept them.
"""

from __future__ import annotations

import pytest

from src.agents.referee.reality_check_summary import (
    build_final_summary,
    check_summary_against_records,
    finalize_executive_summary,
)

# Final records of the run-ui-wordpress run (reality-check/output.json).
BEFORE = {"dynamodb": 45, "aurora_mysql": 22, "elasticache": 31, "documentdb": 6, "opensearch": 3}
AFTER = {"dynamodb": 63, "aurora_mysql": 10, "elasticache": 34}
CONSOLIDATIONS = [
    {
        "from_engine": "aurora_mysql",
        "to_engine": "dynamodb",
        "query_count": 12,
        "action": "partial",
    },
    {
        "from_engine": "aurora_mysql",
        "to_engine": "elasticache",
        "query_count": 3,
        "action": "partial",
    },
    {"from_engine": "documentdb", "to_engine": "dynamodb", "query_count": 6, "action": "full"},
    {"from_engine": "opensearch", "to_engine": "aurora_mysql", "query_count": 3, "action": "full"},
]

# run-ui-wordpress: the reality-check summary the UI assignments page showed.
RUN_UI_RC_SUMMARY = (
    "Your workload consolidates onto Amazon DynamoDB as the primary engine, with Amazon "
    "ElastiCache serving hot lookups and Amazon OpenSearch Service serving text search, "
    "fed from DynamoDB through OpenSearch Ingestion zero-ETL. Seven multi-table JOIN, "
    "subquery, and text-search queries do not fit a key-value model and remain on Amazon "
    "Aurora MySQL, so a single right-sized Aurora cluster stays in the architecture. The "
    "remaining single-table lookups, configuration reads, and low-frequency writes move "
    "cleanly to DynamoDB and ElastiCache, removing the Amazon DocumentDB cluster entirely."
)

STALE_RC_SUMMARIES = {
    "run-ui": RUN_UI_RC_SUMMARY,
    "run-both": (
        "Your workload is dominated by key-value and metadata access, which Amazon DynamoDB "
        "serves with predictable single-digit millisecond latency, with Amazon ElastiCache "
        "covering hot-path reads and Amazon OpenSearch Service covering text search. Seven "
        "multi-table JOIN, subquery and aggregation queries do not fit a non-relational "
        "engine, so Aurora MySQL stays in the architecture for those and the remaining 15 "
        "Aurora queries and the 6 DocumentDB queries consolidate into DynamoDB, ElastiCache "
        "and OpenSearch. Search data can flow from DynamoDB to OpenSearch through zero-ETL "
        "via OpenSearch Ingestion, which keeps a single write path and removes the dedicated "
        "DocumentDB cluster."
    ),
    "run2": (
        "Your WordPress and WooCommerce workload is dominated by key-value lookups, option "
        "and metadata reads, and session access, which DynamoDB and ElastiCache serve well "
        "at this scale. Seven multi-table join, aggregation and subquery queries cannot run "
        "on DynamoDB or ElastiCache and remain on Aurora MySQL, so a small Aurora cluster "
        "stays in the architecture. Full-text search is served by OpenSearch, with DynamoDB "
        "to OpenSearch zero-ETL ingestion through OpenSearch Ingestion keeping the search "
        "index in sync, and the remaining DocumentDB queries fold into DynamoDB."
    ),
    "run3": (
        "The analysis shows that most of your workload, including key-value lookups, "
        "metadata reads, and low-frequency writes, fits Amazon DynamoDB and Amazon "
        "ElastiCache. Seven multi-table JOIN, subquery, and text-search queries remain on "
        "Amazon Aurora MySQL because DynamoDB cannot serve them without a known partition "
        "key. Amazon OpenSearch Service handles the search workload, and the consolidation "
        "of DocumentDB into DynamoDB removes one engine from operations."
    ),
    "run4": (
        "The deterministic consolidation moved all 22 Aurora MySQL queries and 6 DocumentDB "
        "queries into DynamoDB, ElastiCache and OpenSearch. Seven Aurora MySQL queries that "
        "rely on multi-table JOINs, aggregations, subqueries or text search are returned to "
        "Aurora MySQL, because DynamoDB cannot execute them without full scans and "
        "application-side joins. The remaining key-value, metadata and write workload fits "
        "DynamoDB, with ElastiCache serving hot-path reads and OpenSearch serving full-text "
        "search through DynamoDB zero-ETL integration with OpenSearch Ingestion."
    ),
    "run5": (
        "The workload shifts from five engines to three, with DynamoDB taking the key-value "
        "and metadata traffic, ElastiCache serving leaderboard and session patterns, and "
        "OpenSearch handling text search. Seven Aurora MySQL queries that rely on "
        "multi-table JOINs, subqueries and cross-table aggregation remain on Aurora MySQL, "
        "because DynamoDB, ElastiCache and OpenSearch cannot execute them without heavy "
        "denormalization. DocumentDB is fully retired into DynamoDB, and DynamoDB to "
        "OpenSearch zero-ETL through OpenSearch Ingestion keeps the search index current "
        "without custom pipelines."
    ),
}

# Synthesis summaries of the same runs: they describe the final records.
ACCURATE_SUMMARIES = {
    "run-ui": (
        "The WordPress and WooCommerce workload of 107 query patterns moves to a hybrid "
        "architecture built on DynamoDB, Aurora MySQL and ElastiCache. DynamoDB takes 63 "
        "queries, the largest share, covering key-based lookups such as wp_options reads by "
        "option name and the writes that go with them. ElastiCache takes 34 queries that fit "
        "cache and leaderboard access, and Aurora MySQL keeps the 10 queries that need "
        "relational joins and aggregation, plus the text search work that a dedicated "
        "OpenSearch Service domain would not justify. The migration removes full table scans "
        "and join-heavy reads from the hot path, lets DynamoDB scale key lookups without "
        "capacity planning on a single database, and confines relational workload to a small "
        "Aurora MySQL footprint."
    ),
    "run-both": (
        "The WordPress and WooCommerce database moves to a hybrid target of Amazon DynamoDB, "
        "Amazon ElastiCache and Amazon Aurora MySQL. DynamoDB takes 63 of 107 analyzed "
        "queries, the key-based reads and writes that dominate the workload, so they run at "
        "single-digit millisecond latency without a relational server. ElastiCache serves 34 "
        "queries that fit cache and leaderboard access patterns, and Aurora MySQL keeps 10 "
        "queries that need multi-table joins and text search, which is why no separate "
        "OpenSearch Service domain or DocumentDB cluster is needed. The split removes "
        "join-heavy work from the hot path and lets each workload scale on the service built "
        "for it."
    ),
    "run2": (
        "The WordPress workload moves to a hybrid architecture with a cache tier: DynamoDB "
        "serves the majority of queries, including option, post, post meta and term lookups, "
        "while ElastiCache handles the high-frequency leaderboard and cached reads. Aurora "
        "MySQL keeps the multi-table joins with aggregation and the text search queries that "
        "DynamoDB and ElastiCache cannot serve, which removes the need for a separate "
        "OpenSearch Service deployment. Each engine takes the queries it fits best, so "
        "key-value reads scale without row-locking contention on the relational store. The "
        "migration leaves Aurora MySQL with a small, well-defined set of relational queries "
        "and gives the hot read paths single-digit millisecond access."
    ),
    "run3": (
        "The WordPress and WooCommerce workload moves to a hybrid target built on DynamoDB, "
        "ElastiCache, and Aurora MySQL. DynamoDB serves the largest share of the workload, 63 "
        "of 107 queries, covering key-based reads and writes such as option lookups, post "
        "reads, and metadata access. ElastiCache takes 34 queries that benefit from in-memory "
        "latency, and Aurora MySQL keeps the 10 queries that need relational joins and text "
        "search, so no query is forced onto an engine that fits it poorly. The migration "
        "removes the single relational bottleneck, lets each access pattern scale on the "
        "service built for it, and leaves the relational core only where joins are required."
    ),
    "run5": (
        "The WordPress and WooCommerce workload moves to a hybrid of Amazon DynamoDB, Amazon "
        "ElastiCache, and Aurora MySQL. DynamoDB takes 63 of 107 queries, including option "
        "reads and writes against wp_options, post lookups by ID, and WooCommerce API key "
        "maintenance, because these are single-key operations that scale without connection "
        "limits. ElastiCache takes 34 queries that fit cached, leaderboard-style access. "
        "Aurora MySQL keeps the 10 queries that need multi-table joins and aggregation, and "
        "it also serves the text search that a dedicated Amazon OpenSearch Service deployment "
        "would have carried. The migration moves most of the traffic off a single relational "
        "instance and keeps relational behavior only where the queries require it."
    ),
}


# Correct descriptions of the final records written by the PR #260 reviewer; the
# first version of the check rejected all of them.
REVIEWER_CORRECT_SUMMARIES = [
    # Comma lists: the elimination verb applies to every listed engine.
    "DocumentDB, OpenSearch are consolidated away, and DynamoDB, ElastiCache and "
    "Aurora MySQL carry the workload.",
    "DocumentDB, OpenSearch and part of Aurora MySQL consolidate into DynamoDB, "
    "ElastiCache and Aurora MySQL.",
    "DocumentDB, OpenSearch drop out, and the target keeps DynamoDB, ElastiCache and "
    "Aurora MySQL.",
    # "no <engine>" / "<engine> is not required / not part of / not needed".
    "The target runs on DynamoDB, ElastiCache and Aurora MySQL, with no OpenSearch domain "
    "and no DocumentDB cluster.",
    "No OpenSearch domain is required: Aurora MySQL serves the text search.",
    "OpenSearch is not required, because Aurora MySQL serves the three text search queries.",
    "OpenSearch is not part of the target architecture.",
    "DocumentDB, OpenSearch are not needed: DynamoDB and Aurora MySQL take their queries.",
    "DocumentDB and OpenSearch are both retired.",
    # "drops out", "goes away".
    "OpenSearch drops out of the architecture, and DocumentDB goes away.",
    # History and possessives are neutral.
    "Aurora MySQL keeps the text search that previously ran on OpenSearch.",
    "The text search queries formerly assigned to OpenSearch run on Aurora MySQL.",
    "The workload moves from five engines (DynamoDB, Aurora MySQL, ElastiCache, DocumentDB "
    "and OpenSearch) to three.",
    "Aurora MySQL takes over OpenSearch's text search.",
    # An engine that was never in play is not a claim about the final records.
    "Neptune is not part of the target, and DynamoDB serves the key lookups.",
]


def _check(summary: str) -> list[str]:
    return check_summary_against_records(summary, BEFORE, AFTER, CONSOLIDATIONS)


class TestCheckSummaryAgainstRecords:
    @pytest.mark.parametrize("run", sorted(STALE_RC_SUMMARIES))
    def test_stale_reality_check_summaries_are_rejected(self, run):
        findings = _check(STALE_RC_SUMMARIES[run])
        assert findings, f"{run}: summary keeping OpenSearch passed the check"
        assert any("OpenSearch" in f for f in findings)

    @pytest.mark.parametrize("run", sorted(ACCURATE_SUMMARIES))
    def test_summaries_matching_the_final_records_pass(self, run):
        assert _check(ACCURATE_SUMMARIES[run]) == []

    def test_kept_engine_described_as_eliminated_is_rejected(self):
        findings = _check(
            "DynamoDB and ElastiCache serve the workload, and Aurora MySQL is removed from "
            "the architecture."
        )
        assert len(findings) == 1
        assert "Aurora MySQL" in findings[0]
        assert "10 queries" in findings[0]

    def test_eliminated_engine_named_as_consolidated_passes(self):
        assert _check("OpenSearch is consolidated into Aurora MySQL.") == []
        assert _check("DynamoDB replaces DocumentDB for the document queries.") == []

    def test_partial_consolidation_is_not_an_elimination(self):
        assert _check("Aurora MySQL is partially consolidated into DynamoDB.") == []

    def test_query_level_moves_are_neutral(self):
        assert _check("The 6 DocumentDB queries consolidate into DynamoDB.") == []

    def test_generic_aurora_matches_the_aurora_family(self):
        assert _check("A single right-sized Aurora cluster stays.") == []
        assert _check("The Aurora cluster is retired.") != []

    def test_elasticache_aliases(self):
        # Redis is ElastiCache; it keeps 34 queries.
        assert _check("Redis is eliminated.") != []

    def test_no_engine_named_passes(self):
        assert _check("The migration removes join-heavy reads from the hot path.") == []

    @pytest.mark.parametrize("summary", REVIEWER_CORRECT_SUMMARIES)
    def test_reviewer_phrasings_of_the_final_records_pass(self, summary):
        # PR #260 review: correct summaries the first version of the check rejected.
        assert _check(summary) == []

    @pytest.mark.parametrize(
        "summary",
        [
            "DocumentDB, Aurora MySQL are consolidated away.",
            "With no Aurora MySQL cluster, DynamoDB serves the workload.",
            "Aurora MySQL is not required.",
            "ElastiCache drops out of the architecture.",
            "The workload runs on DynamoDB and OpenSearch.",
            # PR #260 re-review: history only covers the preposition it governs, and a
            # possessive followed by a keep word still keeps the engine.
            "Search queries that were formerly on Aurora MySQL move to OpenSearch.",
            "OpenSearch's search index stays in the architecture.",
            "OpenSearch's domain remains for full-text search.",
        ],
    )
    def test_the_new_cues_still_catch_contradictions(self, summary):
        assert _check(summary) != []


class TestBuildFinalSummary:
    def test_describes_the_final_records(self):
        text = build_final_summary(BEFORE, AFTER, CONSOLIDATIONS)
        assert text.startswith("The initial assignment placed 107 queries on 5 engines")
        assert (
            "DynamoDB (63 queries), ElastiCache (34 queries) and Aurora MySQL (10 queries)" in text
        )
        assert "DocumentDB" in text and "OpenSearch" in text
        assert "aurora_mysql" not in text and "opensearch" not in text
        assert "$" not in text and "confidence" not in text.lower()
        assert "—" not in text

    def test_generated_summary_passes_its_own_check(self):
        text = build_final_summary(BEFORE, AFTER, CONSOLIDATIONS)
        assert check_summary_against_records(text, BEFORE, AFTER, CONSOLIDATIONS) == []

    def test_no_consolidation(self):
        before = {"dynamodb": 4, "opensearch": 2}
        text = build_final_summary(before, before, [])
        assert "DynamoDB (4 queries) and OpenSearch (2 queries)" in text
        assert "No engine is consolidated" in text
        assert check_summary_against_records(text, before, before, []) == []

    def test_single_query_wording(self):
        before = {"dynamodb": 1, "opensearch": 1}
        after = {"dynamodb": 2}
        cons = [
            {
                "from_engine": "opensearch",
                "to_engine": "dynamodb",
                "query_count": 1,
                "action": "full",
            }
        ]
        text = build_final_summary(before, after, cons)
        assert "2 queries" in text and "1 query " in text
        assert check_summary_against_records(text, before, after, cons) == []


class TestFinalizeExecutiveSummary:
    def _result(self, summary):
        return {
            "executive_summary": summary,
            "before_distribution": dict(BEFORE),
            "after_distribution": dict(AFTER),
            "consolidations": [dict(c) for c in CONSOLIDATIONS],
        }

    def test_mismatch_replaces_summary_and_keeps_llm_text(self):
        result = self._result(RUN_UI_RC_SUMMARY)
        finalize_executive_summary(result)
        assert result["executive_summary"] == build_final_summary(BEFORE, AFTER, CONSOLIDATIONS)
        assert result["executive_summary_source"] == "deterministic_fallback"
        assert result["executive_summary_llm"] == RUN_UI_RC_SUMMARY
        assert any("OpenSearch" in w for w in result["executive_summary_validation_warnings"])

    def test_match_keeps_llm_summary(self):
        text = ACCURATE_SUMMARIES["run-ui"]
        result = self._result(text)
        finalize_executive_summary(result)
        assert result["executive_summary"] == text
        assert result["executive_summary_source"] == "llm"
        assert result["executive_summary_llm"] == text
        assert result["executive_summary_validation_warnings"] == []

    def test_a_new_llm_summary_replaces_the_audit_copy(self):
        # Finalizing twice: the second LLM summary must be checked, not the first.
        result = self._result(RUN_UI_RC_SUMMARY)
        finalize_executive_summary(result)
        from src.agents.referee.reality_check_handler import apply_reality_check_llm_output

        text = ACCURATE_SUMMARIES["run-ui"]
        apply_reality_check_llm_output(result, {"executive_summary": text})
        finalize_executive_summary(result)
        assert result["executive_summary"] == text
        assert result["executive_summary_source"] == "llm"
        assert result["executive_summary_llm"] == text

    def test_a_kept_llm_summary_is_not_resurrected_from_the_audit_field(self):
        result = self._result(ACCURATE_SUMMARIES["run-ui"])
        finalize_executive_summary(result)
        result["executive_summary"] = ACCURATE_SUMMARIES["run2"]
        finalize_executive_summary(result)
        assert result["executive_summary"] == ACCURATE_SUMMARIES["run2"]
        assert result["executive_summary_llm"] == ACCURATE_SUMMARIES["run2"]

    def test_refinalizing_a_fallback_keeps_the_original_llm_text(self):
        result = self._result(RUN_UI_RC_SUMMARY)
        finalize_executive_summary(result)
        finalize_executive_summary(result)
        assert result["executive_summary_llm"] == RUN_UI_RC_SUMMARY
        assert result["executive_summary_source"] == "deterministic_fallback"

    def test_no_summary_stays_none(self):
        result = self._result(None)
        finalize_executive_summary(result)
        assert result["executive_summary"] is None
        assert result["executive_summary_source"] is None
        assert result["executive_summary_llm"] is None


# ---------------------------------------------------------------------------
# Handler wiring: both LLM paths ship a summary of the final records
# ---------------------------------------------------------------------------


def _rc_output(store) -> dict:
    return next(v for k, v in store._written.items() if k.endswith("reality-check/output.json"))


class TestHandlerShipsFinalSummary:
    """Fixture: DOC-AP-1 is the only DocumentDB query; the deterministic pass moves it
    to DynamoDB, which eliminates DocumentDB."""

    def test_external_finalize_replaces_a_summary_the_final_records_contradict(self):
        from src.agents.referee.reality_check_handler import (
            finalize_reality_check,
            run_reality_check_handler,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        store = _mock_store()
        run_reality_check_handler("job-1", "mydb", store, assignment_version=1, llm_mode="external")
        # The LLM wrote its summary from the preview (DocumentDB eliminated), then
        # reversed the only DocumentDB move: DocumentDB stays.
        stale = "DynamoDB serves the workload and DocumentDB is retired."
        finalize_reality_check(
            store,
            "job-1",
            "mydb",
            {
                "consolidation_corrections": [
                    {"query_id": "DOC-AP-1", "original_engine": "documentdb", "reason": "nested"}
                ],
                "executive_summary": stale,
            },
            assignment_version=1,
        )
        out = _rc_output(store)
        assert out["after_distribution"] == {"dynamodb": 2, "documentdb": 1}
        assert out["executive_summary_source"] == "deterministic_fallback"
        assert out["executive_summary_llm"] == stale
        assert "DocumentDB" in out["executive_summary_validation_warnings"][0]
        assert out["executive_summary"] == build_final_summary(
            out["before_distribution"], out["after_distribution"], out["consolidations"]
        )
        assert "DocumentDB" in out["executive_summary"]

    def test_external_finalize_keeps_a_consistent_summary(self):
        from src.agents.referee.reality_check_handler import finalize_reality_check
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        store = _mock_store()
        summary = "DynamoDB serves every query, and DocumentDB is consolidated into DynamoDB."
        finalize_reality_check(
            store, "job-1", "mydb", {"executive_summary": summary}, assignment_version=1
        )
        out = _rc_output(store)
        assert out["executive_summary"] == summary
        assert out["executive_summary_source"] == "llm"
        assert out["executive_summary_validation_warnings"] == []

    def test_bedrock_summary_is_written_from_the_corrected_records(self):
        from unittest.mock import patch

        from src.agents.referee.reality_check_handler import run_reality_check_handler
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        store = _mock_store()
        correction = [{"query_id": "DOC-AP-1", "original_engine": "documentdb", "reason": "x"}]
        summary = "DynamoDB serves key lookups and DocumentDB keeps the nested documents."
        with (
            patch(
                "src.agents.referee.reality_check_handler.validate_consolidations",
                return_value=correction,
            ),
            patch(
                "src.agents.referee.reality_check_handler._generate_executive_summary",
                return_value=summary,
            ) as gen,
        ):
            run_reality_check_handler("job-1", "mydb", store, assignment_version=1)
        context = gen.call_args.kwargs
        # The model sees the outcome after the correction, not the preview.
        assert context["after_distribution"] == {"dynamodb": 2, "documentdb": 1}
        assert context["consolidations"] == []
        out = _rc_output(store)
        assert out["executive_summary"] == summary
        assert out["executive_summary_source"] == "llm"

    def test_none_mode_has_no_summary(self):
        from src.agents.referee.reality_check_handler import run_reality_check_handler
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        store = _mock_store()
        run_reality_check_handler("job-1", "mydb", store, assignment_version=1, llm_mode="none")
        out = _rc_output(store)
        assert out["executive_summary"] is None
        assert out["executive_summary_source"] is None

    def test_settling_the_records_is_idempotent(self):
        from copy import deepcopy

        from src.agents.referee.reality_check_handler import (
            _settle_records,
            run_reality_check_deterministic,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        det = run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1)
        _settle_records(det)
        once = deepcopy({k: det[k] for k in ("consolidations", "recommendations")})
        _settle_records(det)
        assert {k: det[k] for k in ("consolidations", "recommendations")} == once

    def test_settling_is_idempotent_with_an_override_and_a_correction(self):
        from copy import deepcopy

        from src.agents.referee.reality_check_handler import (
            _settle_records,
            apply_reality_check_llm_output,
            run_reality_check_deterministic,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        det = deepcopy(run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1))
        # The customer pinned DDB-AP-2 to DynamoDB; a later step moved it anyway.
        for qa in det["assignment"]["query_assignments"]:
            if qa["query_id"] == "DDB-AP-2":
                qa["customer_override"] = True
        for qa in det["revised_assignments"]:
            if qa["query_id"] == "DDB-AP-2":
                qa["assigned_engine"] = "documentdb"
        apply_reality_check_llm_output(
            det,
            {
                "consolidation_corrections": [
                    {"query_id": "DOC-AP-1", "original_engine": "documentdb", "reason": "x"}
                ]
            },
        )
        keys = ("consolidations", "recommendations", "revised_assignments", "after_distribution")
        _settle_records(det)
        once = deepcopy({k: det[k] for k in keys})
        assert once["after_distribution"] == {"dynamodb": 2, "documentdb": 1}
        _settle_records(det)
        assert {k: det[k] for k in keys} == once


class TestAbsorptionCandidates:
    def test_small_engines_are_flagged_when_aurora_is_in_play(self):
        from src.agents.referee.reality_check_handler import absorption_candidates

        # The run-ui-wordpress preview (reality-check/llm_input.json): Aurora MySQL fully
        # moved away, OpenSearch left with 4 queries.
        before = dict(BEFORE)
        preview_after = {"dynamodb": 69, "elasticache": 34, "opensearch": 4}
        # DocumentDB (6 in the input) is empty in the preview, but a correction could
        # restore it and Aurora could then absorb it.
        assert absorption_candidates(before, preview_after) == ["documentdb", "opensearch"]

    def test_an_engine_emptied_by_the_preview_is_still_a_candidate(self):
        from src.agents.referee.reality_check_handler import absorption_candidates

        before = {"aurora_mysql": 20, "dynamodb": 30, "opensearch": 4}
        assert absorption_candidates(before, {"dynamodb": 54}) == ["opensearch"]
        assert absorption_candidates({"aurora_mysql": 3, "dynamodb": 30}, {"dynamodb": 33}) == []

    def test_no_aurora_no_candidates(self):
        from src.agents.referee.reality_check_handler import absorption_candidates

        assert absorption_candidates({"dynamodb": 5, "opensearch": 2}, {"opensearch": 2}) == []

    def test_llm_input_carries_the_flag(self):
        from src.agents.referee.reality_check_handler import (
            prepare_reality_check_llm_input,
            run_reality_check_deterministic,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        det = run_reality_check_deterministic("job-1", "mydb", _mock_store(), 1)
        payload = prepare_reality_check_llm_input(det)
        assert payload["executive_summary"]["absorption_candidates"] == []
