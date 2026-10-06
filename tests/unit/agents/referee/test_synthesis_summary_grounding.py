"""The executive summary must agree with the effective table assignment (#205).

(a) the synthesis LLM input carries the effective per-engine table list,
(b) /synthesize tells the model every engine/table claim must match it,
(c) finalize runs a deterministic post-check: a sentence that attributes a table to
    an engine none of whose in-scope queries touch it rejects the LLM summary. The customer-facing
    ``summary`` falls back to the deterministic one; the LLM text and the warnings are
    kept for audit.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.referee.synthesis_grounding import (
    build_effective_architecture,
    build_fallback_summary,
    check_summary_grounding,
)
from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    apply_synthesis_llm_output,
    prepare_synthesis_llm_input,
    run_synthesis_deterministic,
)
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.test_synthesis_effective_risks import DB, JOB, _seed

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_summary_grounding.json"
REPO = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="module")
def wordpress() -> dict:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def _check(summary: str, wordpress: dict, scope: dict | None = None) -> list[dict]:
    return check_summary_grounding(
        summary, scope or wordpress["engine_tables"], wordpress["database_name"]
    )


def _high(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["high_confidence"]]


# ---------------------------------------------------------------------------
# (c) the deterministic post-check
# ---------------------------------------------------------------------------


class TestRealEvidence:
    """The judged wordpress run (fixture reconstructed from its report.json)."""

    def test_judged_summary_is_not_flagged_under_the_assignment_scope(self, wordpress) -> None:
        """ "DynamoDB serves ... post meta lookups" is TRUE for this run.

        The judge compared the summary with table_mappings, whose single
        recommended_database for wp_postmeta is aurora_mysql (confidence 81 vs 79). But
        in-scope DynamoDB queries touch wp_postmeta (DynamoDB's schema design has in-scope
        "Post meta reads"/"Post meta writes" access patterns over it), so the claim matches
        the assignment and the post-check keeps the summary.
        """
        assert _check(wordpress["llm_executive_summary"], wordpress) == []

    def test_recommended_database_alone_would_only_warn(self, wordpress) -> None:
        """Against recommended_database alone "post meta" mismatches, but it sits in a
        list clause that inherits DynamoDB, so it is low confidence: a warning, not a
        rejection."""
        by_recommended: dict[str, list[str]] = {}
        for table, engine in wordpress["recommended_engine"]:
            by_recommended.setdefault(engine, []).append(table)
        findings = _check(wordpress["llm_executive_summary"], wordpress, by_recommended)
        assert [f["table"] for f in findings] == ["wordpress.wp_postmeta"]
        assert _high(findings) == []

    def test_effective_architecture_stays_compact(self, wordpress) -> None:
        mappings = [
            {"source_table": t, "recommended_database": e}
            for t, e in wordpress["recommended_engine"]
        ]
        eff = build_effective_architecture(
            wordpress["engine_tables"],
            mappings,
            wordpress["ranking"],
            wordpress["query_groups"],
            wordpress["database_name"],
            wordpress["eliminated"],
        )
        size = len(json.dumps(eff))
        # Measured 4,091 bytes on this run (llm_input.json was 96 KB); 2x headroom.
        assert size < 8_000, size
        assert {e["engine"] for e in eff["engines"]} == {"dynamodb", "elasticache", "aurora_mysql"}


class TestPostCheckSynthetic:
    """Synthetic summaries against the reconstructed wordpress scope."""

    def test_wrong_engine_is_high_confidence(self, wordpress) -> None:
        findings = _check("ElastiCache serves the post meta lookups.", wordpress)
        assert len(_high(findings)) == 1
        message = findings[0]["message"]
        assert "wordpress.wp_postmeta" in message and "ElastiCache" in message
        assert "DynamoDB" in message  # names the engines that do serve it

    def test_one_word_stem_is_only_low_confidence(self, wordpress) -> None:
        findings = _check("ElastiCache serves comments lookups.", wordpress)
        assert len(findings) == 1 and _high(findings) == []

    def test_correct_summary_is_kept(self, wordpress) -> None:
        summary = (
            "DynamoDB serves option, post, post meta and term lookups, while ElastiCache "
            "keeps the order item rollups hot. Aurora MySQL keeps user meta queries, "
            "including the text search that DynamoDB and ElastiCache cannot serve, which "
            "removes the need for a separate OpenSearch Service deployment."
        )
        assert _check(summary, wordpress) == []

    @pytest.mark.parametrize("name", ["wp_usermeta", "wordpress.wp_usermeta", "user meta"])
    def test_table_matched_with_and_without_db_prefix(self, wordpress, name) -> None:
        assert _high(_check(f"DynamoDB stores {name} rows.", wordpress))
        assert _check(f"Aurora MySQL stores {name} rows.", wordpress) == []

    def test_a_table_served_by_several_engines_is_fine_under_each(self, wordpress) -> None:
        for engine in ("DynamoDB", "ElastiCache", "Aurora MySQL"):
            assert _check(f"{engine} serves wp_posts.", wordpress) == [], engine

    def test_clause_order_does_not_matter(self, wordpress) -> None:
        ok = [
            "While Aurora MySQL keeps wp_usermeta, DynamoDB serves wp_posts.",
            "wp_usermeta stays on Aurora MySQL, while wp_posts moves to DynamoDB.",
            "Aurora MySQL keeps wp_usermeta, while DynamoDB takes wp_posts and wp_options.",
        ]
        for summary in ok:
            assert _check(summary, wordpress) == [], summary
        bad = "DynamoDB keeps wp_usermeta, while ElastiCache takes wp_comments."
        assert len(_high(_check(bad, wordpress))) == 2

    def test_any_engine_in_the_clause_may_serve_it(self, wordpress) -> None:
        assert _check("DynamoDB or Aurora MySQL serves wp_usermeta.", wordpress) == []

    def test_list_continuation_is_low_confidence(self, wordpress) -> None:
        findings = _check("DynamoDB serves wp_options, wp_posts and wp_usermeta.", wordpress)
        assert [f["table"] for f in findings] == ["wordpress.wp_usermeta"]
        assert _high(findings) == []

    def test_engine_named_after_the_table_in_the_same_clause(self, wordpress) -> None:
        assert _high(_check("User meta lookups move to DynamoDB.", wordpress))
        assert _check("User meta lookups stay on Aurora MySQL.", wordpress) == []

    def test_only_not_or_no_directly_before_the_engine_negates(self, wordpress) -> None:
        assert _check("Aurora MySQL, not DynamoDB, keeps wp_usermeta.", wordpress) == []
        # "cannot" no longer negates; Aurora MySQL in the clause serves the table.
        assert (
            _check("User meta queries that DynamoDB cannot serve stay on Aurora MySQL.", wordpress)
            == []
        )

    def test_history_clauses_attribute_nothing(self, wordpress) -> None:
        for summary in (
            "wp_usermeta moved from DynamoDB to Aurora MySQL.",
            "Text search was consolidated from OpenSearch into Aurora MySQL, which serves wp_posts.",
            "Previously OpenSearch indexed wp_posts.",
        ):
            assert _check(summary, wordpress) == [], summary

    def test_respectively_keeps_the_sentence_as_one_unit(self, wordpress) -> None:
        summary = "DynamoDB and Aurora MySQL serve wp_options and wp_usermeta, respectively."
        assert _check(summary, wordpress) == []

    def test_eliminated_engine_claim_is_high_confidence(self, wordpress) -> None:
        assert _high(_check("OpenSearch Service indexes wp_posts for search.", wordpress))

    def test_generic_words_users_and_options(self, wordpress) -> None:
        assert _check("With ElastiCache, users see cached pages instantly.", wordpress) == []
        assert _check("DynamoDB options for scaling are simple.", wordpress) == []
        # "options ... reads" reads as wp_options, which ElastiCache does not serve: a
        # one-word stem, so only a warning.
        findings = _check("ElastiCache gives users and options faster reads.", wordpress)
        assert findings and _high(findings) == []

    def test_sentence_without_an_engine_is_not_checked(self, wordpress) -> None:
        assert _check("User meta keeps its relational shape.", wordpress) == []

    def test_text_search_capability_claims_are_not_checked(self, wordpress) -> None:
        """Documented limit: only table-to-engine claims are checked, not capabilities."""
        assert _check("DynamoDB handles the text search.", wordpress) == []


# ---------------------------------------------------------------------------
# (a) llm_input + finalize wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def det(tmp_path) -> dict:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed(store, eliminated=True)
    store.write_json(
        f"{DB}/{JOB}/schema-aurora_mysql/v2/schema_output.json",
        {
            "source_database": DB,
            "table_definitions": [{"table_name": "products", "columns": []}],
            "access_patterns": [
                {
                    "pattern_id": "AM-AP-1",
                    "pattern_group": "Product search",
                    "table_name": "products",
                    "design_rps": 12,
                    "query_ids": ["q-search"],
                }
            ],
        },
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v2/assignment.json",
        {
            "version": 2,
            "status": "reality_checked",
            "query_assignments": [
                {
                    "query_id": "q-orders",
                    "assigned_engine": "dynamodb",
                    "in_scope": True,
                    "source_tables": [f"{DB}.orders", f"{DB}.products"],
                },
                {
                    "query_id": "q-search",
                    "assigned_engine": "aurora_mysql",
                    "in_scope": True,
                    "source_tables": [f"{DB}.products", "unknown"],
                },
                {
                    "query_id": "q-old",
                    "assigned_engine": "aurora_mysql",
                    "in_scope": False,
                    "source_tables": [f"{DB}.orders"],
                },
            ],
        },
    )
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    result["_store"] = store
    return result


class TestLlmInput:
    def test_effective_per_engine_tables(self, det) -> None:
        eff = prepare_synthesis_llm_input(det)["effective_architecture"]
        engines = {e["engine"]: e for e in eff["engines"]}
        assert set(engines) == {"dynamodb", "aurora_mysql"}
        # Scope = tables the engine's in-scope queries touch; a table can be on several
        # engines, out-of-scope queries and non-table names ("unknown") do not count.
        assert engines["dynamodb"]["tables"] == ["orders", "products"]
        assert engines["aurora_mysql"]["tables"] == ["products"]
        assert eff["recommended_engine_by_table"] == {
            "orders": "dynamodb",
            "products": "aurora_mysql",
        }
        assert engines["aurora_mysql"]["top_query_groups"] == ["Product search"]
        assert eff["source_database"] == DB
        assert eff["eliminated_engines"] == [
            {"engine": "opensearch", "absorbed_by": "aurora_mysql"}
        ]
        assert "must match" in eff["rule"]

    def test_effective_block_is_compact(self, det) -> None:
        eff = prepare_synthesis_llm_input(det)["effective_architecture"]
        assert len(json.dumps(eff)) < 2000


class TestFinalize:
    def test_wrong_attribution_falls_back_to_deterministic(self, det) -> None:
        llm = "Aurora MySQL serves the shop.orders table and the product lookups."
        out = apply_synthesis_llm_output(det, {"executive_summary": llm})
        assert out["executive_summary"] == build_fallback_summary(det["effective_architecture"])
        assert out["summary_llm"] == llm
        assert out["summary_source"] == "deterministic_fallback"
        assert out["summary_validation_warnings"]

    def test_correct_attribution_is_used(self, det) -> None:
        llm = (
            "DynamoDB serves the order and product lookups; Aurora MySQL keeps the products table."
        )
        out = apply_synthesis_llm_output(det, {"executive_summary": llm})
        assert out["executive_summary"] == llm
        assert out["summary_source"] == "llm"
        assert out["summary_validation_warnings"] == []

    def test_internal_field_name_rejects_the_summary(self, det) -> None:
        """#380: a summary that points the customer at one of this codebase's
        own JSON field names (e.g. "see tco_analysis's cost_breakdown for
        documentdb's own figure before removal") is rejected, even though it
        names no table at all -- there is no table/engine mismatch for
        ``check_summary_grounding`` to catch, so this needs its own check."""
        llm = (
            "DynamoDB serves the order and product lookups; see tco_analysis's "
            "cost_breakdown for the figure."
        )
        out = apply_synthesis_llm_output(det, {"executive_summary": llm})
        assert out["executive_summary"] == build_fallback_summary(det["effective_architecture"])
        assert out["summary_source"] == "deterministic_fallback"
        assert any("tco_analysis" in w for w in out["summary_validation_warnings"])

    def test_low_confidence_finding_keeps_the_llm_text(self, det) -> None:
        llm = "Aurora MySQL keeps the orders table hot."  # one-word stem: warning only
        out = apply_synthesis_llm_output(det, {"executive_summary": llm})
        assert out["executive_summary"] == llm
        assert out["summary_source"] == "llm"
        assert out["summary_validation_warnings"][0].startswith("[low confidence]")

    def test_bedrock_path_rejects_end_to_end(self, det) -> None:
        from src.agents.referee.synthesis_handler import run_synthesis

        store = det["_store"]
        with patch(
            "src.agents.referee.synthesis_handler.generate_executive_summary",
            return_value="Aurora MySQL serves the shop.orders table for every order lookup.",
        ):
            run_synthesis(JOB, DB, store, assignment_version=2, llm_mode="bedrock")
        report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
        assert report["summary_source"] == "deterministic_fallback"
        assert report["summary_llm"].startswith("Aurora MySQL serves the shop.orders")
        assert report["summary_validation_warnings"][0].startswith("[high confidence]")
        assert "shop.orders" not in report["summary"]

    def test_report_keeps_llm_text_for_audit(self, det) -> None:
        llm = "Aurora MySQL serves the shop.orders table."
        apply_synthesis_llm_output(det, {"executive_summary": llm})
        store = det["_store"]
        _write_synthesis_report(store, det, 2)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
        assert report["summary"] == build_fallback_summary(det["effective_architecture"])
        assert report["summary_deterministic"] == det["summary"]
        assert report["summary_llm"] == llm
        assert report["summary_source"] == "deterministic_fallback"
        assert report["summary_validation_warnings"]

    def test_deterministic_only_report(self, det) -> None:
        store = det["_store"]
        _write_synthesis_report(store, det, 2)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
        assert report["summary_source"] == "deterministic"
        assert report["summary_llm"] is None


# ---------------------------------------------------------------------------
# (b) the instructions given to the model
# ---------------------------------------------------------------------------


def test_synthesize_command_requires_matching_the_effective_list() -> None:
    text = (REPO / ".claude" / "commands" / "synthesize.md").read_text()
    assert "effective_architecture" in text
    assert "deterministic summary" in text.lower()


def test_bedrock_prompt_carries_the_effective_architecture(det) -> None:
    from src.agents.referee import synthesis_report

    captured: dict = {}

    def fake_agent(*_a, **_k):
        agent = MagicMock()
        agent.side_effect = lambda prompt: captured.setdefault("prompt", prompt) and "x" * 50
        return agent

    with (
        patch("strands.Agent", side_effect=fake_agent),
        patch("strands.models.bedrock.BedrockModel"),
    ):
        synthesis_report.generate_executive_summary(
            det["summary"],
            det["ranking"],
            det["query_groups"],
            det["tco_analysis"],
            det["risk_assessment"],
            det["table_mappings"],
            det["trade_offs"],
            effective_architecture=det["effective_architecture"],
        )
    assert '"products"' in captured["prompt"]
    assert "must match" in captured["prompt"]


@pytest.mark.parametrize(
    ("summary", "source"),
    [
        ("Aurora MySQL serves the shop.orders table.", "deterministic_fallback"),
        ("Aurora MySQL keeps the products table.", "llm"),
    ],
)
def test_finalize_script_reports_the_post_check(
    det, tmp_path, monkeypatch, capsys, summary, source
) -> None:
    import sys

    from scripts import run_synthesis

    det["_store"].write_json(
        f"{DB}/{JOB}/synthesis/v2/llm_response.json", {"executive_summary": summary}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_synthesis.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--finalize",
            "--assignment-version",
            "2",
            "--artifact-root",
            str(tmp_path),
        ],
    )
    run_synthesis.main()
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["status"] == "complete"
    assert status["summary_source"] == source
    assert bool(status["summary_validation_warnings"]) == (source != "llm")
    report = det["_store"].read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
    assert report["summary_llm"] == summary


# ---------------------------------------------------------------------------
# Fallback summary quality and the decision-report note
# ---------------------------------------------------------------------------


class TestFallbackSummary:
    def _eff(self, wordpress) -> dict:
        mappings = [
            {"source_table": t, "recommended_database": e}
            for t, e in wordpress["recommended_engine"]
        ]
        return build_effective_architecture(
            wordpress["engine_tables"],
            mappings,
            wordpress["ranking"],
            wordpress["query_groups"],
            wordpress["database_name"],
            wordpress["eliminated"],
        )

    def test_wordpress_fallback_reads_as_a_narrative(self, wordpress) -> None:
        text = build_fallback_summary(self._eff(wordpress))
        assert text is not None
        assert text.startswith(
            "The target architecture combines DynamoDB, ElastiCache and Aurora MySQL"
        )
        assert "DynamoDB serves 63 queries (58.9% of the workload), led by" in text
        assert (
            "The reality check consolidated DocumentDB into DynamoDB and OpenSearch "
            "into Aurora MySQL" in text
        )

    def test_fallback_has_no_cost_confidence_or_mapping_counts(self, wordpress) -> None:
        text = build_fallback_summary(self._eff(wordpress)) or ""
        for banned in ("$", "confidence", "source tables mapped", "dynamodb (", "aurora_mysql"):
            assert banned not in text, banned
        assert "ungrouped" not in text
        assert text.count("tables mapped") == 0

    def test_single_engine_and_empty(self) -> None:
        eff = {"engines": [{"engine": "dynamodb", "tables": ["a", "b"]}]}
        assert build_fallback_summary(eff) == (
            "The target architecture runs on DynamoDB. DynamoDB serves 2 source tables."
        )
        assert build_fallback_summary({"engines": []}) is None


def test_decision_report_notes_the_withheld_narrative(det) -> None:
    from src.report.renderers import render_decision_report_html

    apply_synthesis_llm_output(det, {"executive_summary": "Aurora MySQL serves shop.orders."})
    store = det["_store"]
    _write_synthesis_report(store, det, 2)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
    html = render_decision_report_html(report)
    assert "named a table under an engine that does not serve it" in html
    assert "The target architecture combines" in html

    report["summary_source"] = "llm"
    assert "named a table under an engine" not in render_decision_report_html(report)
