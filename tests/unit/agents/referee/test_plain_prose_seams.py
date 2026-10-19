"""Model-written customer prose passes the plain-prose style list at its seam (#1034).

Each LLM seam that writes customer prose replaces dashes mechanically and sends
any other style-list hit back to the deterministic text, recording why in the
trace (``summary_validation_warnings``, ``executive_summary_validation_warnings``,
``prose_style_rejections``), never in the deliverable text.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.reality_check_summary import (
    build_final_summary,
    finalize_executive_summary,
)
from src.agents.referee.synthesis_handler import apply_synthesis_llm_output
from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.report.formatting import fallback_withheld_reason
from src.shared.prose_style import find_style_hits
from tests.unit.agents.referee.test_reality_check_summary import (
    ACCURATE_SUMMARIES,
    AFTER,
    BEFORE,
    CONSOLIDATIONS,
)
from tests.unit.agents.referee.test_synthesis_migration_waves import (
    DB,
    JOB,
)
from tests.unit.agents.referee.test_synthesis_migration_waves import store as migration_waves_store

DET_SUMMARY = "deterministic fallback summary"


def _det() -> dict:
    return {
        "table_mappings": [],
        "database_name": "wordpress",
        "effective_architecture": None,
        "summary": DET_SUMMARY,
        "migration_waves": None,
    }


class TestSynthesisSummary:
    def test_dashes_are_replaced_and_the_summary_kept(self) -> None:
        llm = "DynamoDB serves the key lookups — Aurora MySQL keeps the joins."
        out = apply_synthesis_llm_output(_det(), {"executive_summary": llm})
        assert out["summary_source"] == "llm"
        assert (
            out["executive_summary"]
            == "DynamoDB serves the key lookups, Aurora MySQL keeps the joins."
        )
        assert out["summary_llm"] == llm  # the audit copy stays as received
        assert out["summary_validation_warnings"] == []

    def test_a_filler_word_sends_the_summary_back_to_the_deterministic_text(self) -> None:
        llm = "DynamoDB delivers a robust, seamless key-value layer."
        out = apply_synthesis_llm_output(_det(), {"executive_summary": llm})
        assert out["summary_source"] == "deterministic_fallback"
        assert out["executive_summary"] == DET_SUMMARY
        assert out["summary_llm"] == llm
        warnings = out["summary_validation_warnings"]
        assert any(w.startswith("[high confidence] plain-prose style list") for w in warnings)
        assert any("robust" in w and "seamless" in w for w in warnings)

    def test_a_hedge_or_construction_is_rejected_too(self) -> None:
        for llm in (
            "Aurora MySQL might keep the joins.",
            "Not just faster but cheaper, DynamoDB takes the reads.",
            "Here's the plan: DynamoDB takes the reads.",
        ):
            out = apply_synthesis_llm_output(_det(), {"executive_summary": llm})
            assert out["summary_source"] == "deterministic_fallback", llm

    def test_the_withheld_note_names_the_style_reason(self) -> None:
        out = apply_synthesis_llm_output(
            _det(), {"executive_summary": "We leverage DynamoDB for reads."}
        )
        reason = fallback_withheld_reason(out)
        assert reason == "used wording outside the report's plain-language style"
        assert find_style_hits(reason) == []


class TestRealityCheckSummary:
    def _result(self, summary: str) -> dict:
        return {
            "executive_summary": summary,
            "before_distribution": dict(BEFORE),
            "after_distribution": dict(AFTER),
            "consolidations": [dict(c) for c in CONSOLIDATIONS],
        }

    def test_dashes_are_replaced_and_the_summary_kept(self) -> None:
        text = ACCURATE_SUMMARIES["run-ui"]
        dashed = text.replace(". ", " — ", 1)
        result = self._result(dashed)
        finalize_executive_summary(result)
        assert result["executive_summary_source"] == "llm"
        assert "—" not in result["executive_summary"]
        assert result["executive_summary_llm"] == dashed

    def test_a_filler_word_sends_the_summary_back_to_the_final_records(self) -> None:
        text = "Furthermore, " + ACCURATE_SUMMARIES["run-ui"]
        result = self._result(text)
        finalize_executive_summary(result)
        assert result["executive_summary_source"] == "deterministic_fallback"
        assert result["executive_summary"] == build_final_summary(BEFORE, AFTER, CONSOLIDATIONS)
        assert result["executive_summary_llm"] == text
        assert any("furthermore" in w for w in result["executive_summary_validation_warnings"])

    def test_the_deterministic_summary_has_no_hits(self) -> None:
        assert find_style_hits(build_final_summary(BEFORE, AFTER, CONSOLIDATIONS)) == []


class TestSchemaDesignProse:
    def _schema(self, **trade_off: str) -> dict:
        return {
            "trade_offs": [
                {
                    "description": trade_off.get("description", "Posts keyed by topic_id."),
                    "impact": trade_off.get("impact", "Reads by topic are one Query."),
                    "source_tables": ["discourse.posts"],
                }
            ],
            "unsupported_patterns": [
                {"pattern_type": "aggregation", "reason": trade_off.get("reason", "No GROUP BY.")}
            ],
        }

    def test_clean_text_is_unchanged(self) -> None:
        schema = self._schema()
        cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
        assert cleaned == schema
        assert rejections == []

    def test_dashes_are_replaced(self) -> None:
        cleaned, rejections = clean_schema_design_prose(
            "dynamodb", self._schema(impact="Reads by topic are one Query — no scan.")
        )
        assert cleaned["trade_offs"][0]["impact"] == "Reads by topic are one Query, no scan."
        assert rejections == []

    def test_a_filler_word_falls_back_to_deterministic_text_and_is_recorded(self) -> None:
        schema = self._schema(
            impact="This ensures reads stay fast.", reason="DynamoDB typically lacks GROUP BY."
        )
        cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
        impact = cleaned["trade_offs"][0]["impact"]
        assert "ensures" not in impact and find_style_hits(impact) == []
        assert "DynamoDB" in impact
        # An unsupported pattern's reason falls back to empty: the risk builder then
        # states the problem from the queries (synthesis_report).
        assert cleaned["unsupported_patterns"][0]["reason"] == ""
        assert len(rejections) == 2
        assert any("trade_offs[0].impact" in r and "ensures" in r for r in rejections)
        assert any("unsupported_patterns[0].reason" in r and "typically" in r for r in rejections)
        # The input is not mutated: the schema output on disk keeps the model text.
        assert schema["trade_offs"][0]["impact"] == "This ensures reads stay fast."

    def test_legacy_string_trade_offs_are_cleaned(self) -> None:
        cleaned, rejections = clean_schema_design_prose(
            "documentdb", {"trade_offs": ["Embed comments — bounded at 50."]}
        )
        assert cleaned["trade_offs"] == ["Embed comments, bounded at 50."]
        assert rejections == []

    def test_no_schema_design(self) -> None:
        assert clean_schema_design_prose("dynamodb", None) == (None, [])


store = migration_waves_store


def test_synthesis_reads_every_schema_design_through_the_cleaner(store) -> None:
    """The seam is wired: a saved design's model text reaches report.json cleaned,
    the rejection is in ``prose_style_rejections``, and the design on disk keeps
    the model's text."""
    from src.agents.referee.synthesis_handler import (
        _write_synthesis_report,
        run_synthesis_deterministic,
    )

    key = f"{DB}/{JOB}/schema-dynamodb/v2/schema_output.json"
    store.write_json(
        key,
        {
            "trade_offs": [
                {
                    "description": "Users keyed by user_id — one GetItem per profile.",
                    "impact": "This ensures profile reads stay fast.",
                    "source_tables": ["users"],
                }
            ]
        },
    )
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    _write_synthesis_report(store, result, assignment_version=2)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
    trade_off = next(t for t in report["trade_offs"] if t.get("engine") == "dynamodb")
    assert trade_off["description"] == "Users keyed by user_id, one GetItem per profile."
    assert trade_off["impact"] == (
        "Review this trade-off with the DynamoDB schema design before migration."
    )
    assert report["prose_style_rejections"] == [
        'dynamodb trade_offs[0].impact: plain-prose style list ("ensures")'
    ]
    assert store.read_json(key)["trade_offs"][0]["impact"] == (
        "This ensures profile reads stay fast."
    )


class TestSchemaDesignProseRoundTwo:
    """#1047 review: the other model-written fields that reach report.json."""

    def test_non_aurora_access_pattern_descriptions_are_cleaned(self) -> None:
        schema = {
            "access_patterns": [
                {"operation": "Query", "table_name": "Posts", "description": "Reads — by topic."},
                {
                    "operation": "GetItem",
                    "table_name": "Users",
                    "description": "This ensures fast profile reads.",
                    "key_condition": "user_id = :id",
                },
            ]
        }
        cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
        assert cleaned["access_patterns"][0]["description"] == "Reads, by topic."
        assert cleaned["access_patterns"][1]["description"] == "GetItem on Users"
        assert cleaned["access_patterns"][1]["key_condition"] == "user_id = :id"
        assert rejections == [
            'dynamodb access_patterns[1].description: plain-prose style list ("ensures")'
        ]

    def test_aurora_access_pattern_descriptions_are_this_codes_own_text(self) -> None:
        schema = {"access_patterns": [{"operation": "SELECT", "description": "SELECT on t — x"}]}
        cleaned, rejections = clean_schema_design_prose("aurora_mysql", schema)
        assert cleaned == schema and rejections == []

    def test_aurora_optimizations_are_cleaned(self) -> None:
        schema = {
            "optimizations": [
                {
                    "category": "read_replica",
                    "target": "cluster",
                    "recommendation": "Leverage a reader endpoint.",
                    "rationale": "Reads dominate — 90% of calls.",
                }
            ]
        }
        cleaned, rejections = clean_schema_design_prose("aurora_postgresql", schema)
        opt = cleaned["optimizations"][0]
        assert opt["recommendation"] == "Read replica optimization on cluster."
        assert opt["rationale"] == "Reads dominate: 90% of calls."
        assert len(rejections) == 1 and "leverage" in rejections[0]

    def test_a_collected_table_name_is_not_a_word(self) -> None:
        schema = {"trade_offs": [{"description": "Journeys keyed by trip_id.", "impact": "x"}]}
        cleaned, rejections = clean_schema_design_prose("dynamodb", schema, {"shop.journeys"})
        assert cleaned == schema and rejections == []
        _, rejections = clean_schema_design_prose("dynamodb", schema)
        assert rejections  # without the identifiers, "journeys" is the filler word

    def test_code_in_model_text_is_never_rewritten(self) -> None:
        text = "Run `SELECT id FROM t -- note` once — then cache it."
        cleaned, _ = clean_schema_design_prose(
            "documentdb", {"unsupported_patterns": [{"reason": text}]}
        )
        assert cleaned["unsupported_patterns"][0]["reason"] == (
            "Run `SELECT id FROM t -- note` once, then cache it."
        )

    def test_a_rejected_trade_off_names_its_tables(self) -> None:
        schema = {
            "trade_offs": [
                {
                    "description": "A robust merge.",
                    "impact": "fine",
                    "source_tables": ["users", "user_emails"],
                    "target_tables": ["Users"],
                }
            ]
        }
        cleaned, _ = clean_schema_design_prose("dynamodb", schema)
        assert cleaned["trade_offs"][0]["description"] == (
            "DynamoDB design trade-off: users, user_emails maps to Users."
        )


def test_the_reality_check_warning_reads_as_a_plain_clause() -> None:
    result: dict[str, Any] = {
        "executive_summary": "Furthermore, " + ACCURATE_SUMMARIES["run-ui"],
        "before_distribution": dict(BEFORE),
        "after_distribution": dict(AFTER),
        "consolidations": [dict(c) for c in CONSOLIDATIONS],
    }
    finalize_executive_summary(result)
    assert any(
        w.startswith("The summary used wording outside the report's plain-language style")
        for w in result["executive_summary_validation_warnings"]
    )


def test_a_summary_naming_a_collected_table_is_kept() -> None:
    class _Data:
        collector = {"database_schema": {"tables": [{"table_name": "journeys"}]}}

    det = {**_det(), "data": _Data()}
    out = apply_synthesis_llm_output(det, {"executive_summary": "DynamoDB serves journeys."})
    assert out["summary_source"] == "llm"


class TestSchemaDesignProseRoundThree:
    """#1047 review round 2."""

    def test_aurora_app_layer_note_recommendations_are_cleaned(self) -> None:
        schema = {
            "app_layer_notes": [
                {
                    "feature": "trigger",
                    "source_object": "t1",
                    "recommendation": "Ensure the app recomputes.",
                },
                {
                    "feature": "trigger",
                    "source_object": "t2",
                    "recommendation": "Recompute — on write.",
                },
            ]
        }
        cleaned, rejections = clean_schema_design_prose("aurora_mysql", schema)
        assert cleaned["app_layer_notes"][0]["recommendation"] == ""
        assert cleaned["app_layer_notes"][1]["recommendation"] == "Recompute, on write."
        assert len(rejections) == 1 and "app_layer_notes[0].recommendation" in rejections[0]

    def test_a_rejected_unsupported_fix_keeps_a_deterministic_mitigation(self) -> None:
        from src.shared.unsupported_pattern import unsupported_pattern_mitigation

        schema = {
            "unsupported_patterns": [
                {"pattern_type": "aggregation", "recommendation": "Leverage Streams."}
            ]
        }
        cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
        assert unsupported_pattern_mitigation(cleaned["unsupported_patterns"][0]) == (
            "Review these queries with the DynamoDB schema design before migration."
        )
        assert rejections

    def test_short_labels_only_get_the_dash_rewrite(self) -> None:
        schema = {
            "optimizations": [
                {"category": "index", "target": "orders — created_at", "recommendation": "Add it."}
            ],
            "unsupported_patterns": [{"pattern_type": "window—functions"}],
        }
        cleaned, rejections = clean_schema_design_prose("aurora_postgresql", schema)
        assert cleaned["optimizations"][0]["target"] == "orders, created_at"
        assert cleaned["unsupported_patterns"][0]["pattern_type"] == "window, functions"
        assert rejections == []

    def test_an_en_dash_range_in_model_text_is_kept_as_written(self) -> None:
        schema = {
            "trade_offs": [{"description": "Peak 9 am – 5 pm, 10 GB – 50 GB.", "impact": "x"}]
        }
        cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
        assert cleaned == schema and rejections == []


JUDGE_HEDGES = (
    ("Failover can take a moment.", "a moment"),
    ("The cache is briefly stale.", "briefly"),
    ("Add a replica if that matters.", "if that matters"),
    ("Readers see old data for a short time.", "for a short time"),
    ("Batch writes where possible.", "where possible"),
    ("Typical values are far smaller.", "typical values are far smaller"),
    ("A reader may see the old value.", "may see the old value"),
    ("Writes cost roughly more.", "roughly"),
    ("Reads usually hit the cache.", "usually"),
    ("The hot key is likely to throttle.", "likely"),
    ("Reads typically hit the cache.", "typically"),
)
BOUNDED_TEXT = "A reader may see the old value until the TTL expires, roughly 2x slower."


class TestJudgeHedgesAtEverySeam:
    def test_synthesis_summary(self) -> None:
        for hedge, term in JUDGE_HEDGES:
            llm = f"DynamoDB serves the key lookups. {hedge}"
            out = apply_synthesis_llm_output(_det(), {"executive_summary": llm})
            assert out["summary_source"] == "deterministic_fallback", hedge
            assert out["executive_summary"] == DET_SUMMARY
            assert any(
                "plain-prose style list" in w and f'"{term}"' in w
                for w in out["summary_validation_warnings"]
            ), hedge
        ok = apply_synthesis_llm_output(
            _det(), {"executive_summary": f"DynamoDB serves the key lookups. {BOUNDED_TEXT}"}
        )
        assert ok["summary_source"] == "llm"

    def test_reality_check_summary(self) -> None:
        base = ACCURATE_SUMMARIES["run-ui"]
        for hedge, term in JUDGE_HEDGES:
            result = TestRealityCheckSummary()._result(base + " " + hedge)
            finalize_executive_summary(result)
            assert result["executive_summary_source"] == "deterministic_fallback", hedge
            assert any(
                f'"{term}"' in w for w in result["executive_summary_validation_warnings"]
            ), hedge
        result = TestRealityCheckSummary()._result(base + " " + BOUNDED_TEXT)
        finalize_executive_summary(result)
        assert result["executive_summary_source"] == "llm"

    def test_schema_design_trade_offs_and_unsupported_fixes(self) -> None:
        for hedge, term in JUDGE_HEDGES:
            schema = {
                "trade_offs": [{"description": "Posts keyed by topic_id.", "impact": hedge}],
                "unsupported_patterns": [
                    {"pattern_type": "aggregation", "workaround": hedge, "reason": "No GROUP BY."}
                ],
            }
            cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
            assert find_style_hits(cleaned["trade_offs"][0]["impact"]) == [], hedge
            fix = cleaned["unsupported_patterns"][0]["workaround"]
            assert fix != hedge and find_style_hits(fix) == []
            assert len(rejections) == 2 and all(f'("{term}")' in r for r in rejections), hedge
            assert schema["trade_offs"][0]["impact"] == hedge  # the design on disk is unchanged
        schema = {"trade_offs": [{"description": "x", "impact": BOUNDED_TEXT}]}
        cleaned, rejections = clean_schema_design_prose("dynamodb", schema)
        assert cleaned["trade_offs"][0]["impact"] == BOUNDED_TEXT and not rejections
