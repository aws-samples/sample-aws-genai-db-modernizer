"""The executive summary must agree with the deterministic wave order (#393).

``migration_waves`` is the one deterministic rule for the incremental roadmap's
sequence (``src.agents.referee.migration_waves``) -- a model may explain a wave, it
never decides the order. Evidence (judged WordPress run, #375 review): the executive
summary stated "the migration moves the cache first, then Aurora MySQL, then
DynamoDB", while the roadmap's own waves move Aurora MySQL first (wave 1), the cache
second (wave 2) and DynamoDB third (wave 3) -- a customer reading only the summary
would plan the rollout backwards.

``check_summary_wave_order`` finds that contradiction; ``apply_synthesis_llm_output``
folds it into the same accept/reject decision as the table-attribution and
internal-field-name checks (#205, #380).
"""

from __future__ import annotations

from src.agents.referee.synthesis_grounding import check_summary_wave_order
from src.agents.referee.synthesis_handler import apply_synthesis_llm_output

WAVES = [
    {"wave": 1, "title": "Move to Aurora MySQL", "engines": ["aurora_mysql"]},
    {"wave": 2, "title": "Cache hot reads with ElastiCache", "engines": ["elasticache"]},
    {
        "wave": 3,
        "title": "Move key-value and point-lookup queries to DynamoDB",
        "engines": ["dynamodb"],
    },
]


class TestCheckSummaryWaveOrder:
    def test_real_evidence_sentence_is_flagged(self) -> None:
        """The exact judged sentence (#375 review, WordPress run)."""
        summary = (
            "Schema design has run for all three engines, and the migration moves the "
            "cache first, then Aurora MySQL, then DynamoDB, so the read pressure drops "
            "before the key-value tables move."
        )
        findings = check_summary_wave_order(summary, WAVES)
        assert findings
        assert all(f["high_confidence"] for f in findings)
        messages = " ".join(f["message"] for f in findings)
        assert "ElastiCache" in messages
        assert "Aurora MySQL" in messages
        assert "wave 1" in messages
        assert "wave 2" in messages

    def test_correct_order_is_not_flagged(self) -> None:
        summary = (
            "The migration moves Aurora MySQL first, then adds the cache, then moves to "
            "DynamoDB."
        )
        assert check_summary_wave_order(summary, WAVES) == []

    def test_no_sequencing_language_is_not_flagged(self) -> None:
        """Naming every engine in one sentence makes no ordering claim to contradict."""
        summary = "The workload splits across Aurora MySQL, DynamoDB and ElastiCache."
        assert check_summary_wave_order(summary, WAVES) == []

    def test_only_one_wave_engine_named_is_not_flagged(self) -> None:
        summary = "Aurora MySQL serves the relational core first, then handles overflow."
        assert check_summary_wave_order(summary, WAVES) == []

    def test_no_migration_waves_is_not_flagged(self) -> None:
        summary = "The migration moves the cache first, then Aurora MySQL, then DynamoDB."
        assert check_summary_wave_order(summary, None) == []
        assert check_summary_wave_order(summary, []) == []

    def test_empty_summary_is_not_flagged(self) -> None:
        assert check_summary_wave_order("", WAVES) == []

    def test_reversed_tail_order_is_flagged(self) -> None:
        """Aurora first is correct, but DynamoDB before the cache is still backwards."""
        summary = "The migration moves Aurora MySQL first, then DynamoDB, then the cache."
        findings = check_summary_wave_order(summary, WAVES)
        assert findings
        assert {tuple(f["engines"]) for f in findings} == {("dynamodb", "elasticache")}

    def test_dynamodb_before_aurora_is_flagged(self) -> None:
        """Another legitimate reject: DynamoDB (wave 3) stated ahead of Aurora (wave 1)."""
        summary = "The plan is to move DynamoDB first, then Aurora MySQL."
        findings = check_summary_wave_order(summary, WAVES)
        assert findings
        assert {tuple(f["engines"]) for f in findings} == {("dynamodb", "aurora_mysql")}

    def test_dynamodb_before_cache_is_flagged(self) -> None:
        """Another legitimate reject: DynamoDB (wave 3) stated ahead of the cache (wave 2)."""
        summary = "DynamoDB moves first, then the cache follows."
        findings = check_summary_wave_order(summary, WAVES)
        assert findings
        assert {tuple(f["engines"]) for f in findings} == {("dynamodb", "elasticache")}

    # ------------------------------------------------------------------
    # Review round 1 (#393): reproduced false rejections of correct or
    # unrelated prose from a looser "first ... then" co-occurrence check that
    # paired every engine mention in the sentence by position, regardless of
    # whether either cue was actually next to it.
    # ------------------------------------------------------------------

    def test_unrelated_schema_validation_sentence_is_not_flagged(self) -> None:
        summary = (
            "Validate the schema first, then roll out: Aurora MySQL serves relational "
            "joins, DynamoDB serves key-value lookups, and ElastiCache fronts hot reads."
        )
        assert check_summary_wave_order(summary, WAVES) == []

    def test_first_wave_phrasing_is_not_flagged(self) -> None:
        summary = "The cache improves latency; the first wave then moves Aurora MySQL into place."
        assert check_summary_wave_order(summary, WAVES) == []

    def test_hyphenated_first_party_is_not_flagged(self) -> None:
        summary = (
            "This keeps a first-party cache in place, then moves read-heavy tables to "
            "Aurora MySQL."
        )
        assert check_summary_wave_order(summary, WAVES) == []

    def test_first_and_then_in_different_clauses_is_not_flagged(self) -> None:
        summary = (
            "Validate schema compatibility first; then DynamoDB will take write-heavy "
            "tables once Aurora MySQL is in place."
        )
        assert check_summary_wave_order(summary, WAVES) == []

    # ------------------------------------------------------------------
    # Review round 2 (#393): unconditional clause splitting let a wrong order
    # escape entirely once "first" and "then" landed in separate ";"/":"
    # clauses, since each engine then sat alone with no partner to compare
    # against -- fixed by pairing a clause that ENDS with a bound "first"
    # across the boundary to the very next clause when it STARTS with a
    # bound "then".
    # ------------------------------------------------------------------

    def test_semicolon_split_wrong_order_is_flagged(self) -> None:
        summary = "DynamoDB moves first; then Aurora MySQL takes over."
        findings = check_summary_wave_order(summary, WAVES)
        assert findings
        assert {tuple(f["engines"]) for f in findings} == {("dynamodb", "aurora_mysql")}

    def test_colon_split_wrong_order_is_flagged(self) -> None:
        summary = "DynamoDB moves first: then Aurora MySQL takes over."
        findings = check_summary_wave_order(summary, WAVES)
        assert findings
        assert {tuple(f["engines"]) for f in findings} == {("dynamodb", "aurora_mysql")}

    def test_semicolon_split_correct_order_is_not_flagged(self) -> None:
        summary = "Aurora MySQL moves first; then DynamoDB takes the key-value reads."
        assert check_summary_wave_order(summary, WAVES) == []

    def test_first_wave_phrasing_across_a_semicolon_is_still_not_flagged(self) -> None:
        """Unconditional cross-boundary pairing would have mis-paired this one too --
        the "first" here is excluded as a wave reference, not an ordering cue, so
        there is nothing to pair across the ";" at all."""
        summary = "The cache improves latency; the first wave then moves Aurora MySQL into place."
        assert check_summary_wave_order(summary, WAVES) == []


class TestApplySynthesisLlmOutputWiring:
    def _det(self) -> dict:
        return {
            "table_mappings": [],
            "database_name": "wordpress",
            "effective_architecture": None,
            "summary": "deterministic fallback summary",
            "migration_waves": WAVES,
        }

    def test_wrong_wave_order_rejects_the_summary(self) -> None:
        llm_summary = "The migration moves the cache first, then Aurora MySQL, then DynamoDB."
        out = apply_synthesis_llm_output(self._det(), {"executive_summary": llm_summary})
        assert out["summary_source"] == "deterministic_fallback"
        assert out["executive_summary"] == "deterministic fallback summary"
        assert out["summary_llm"] == llm_summary
        assert any("wave" in w.lower() for w in out["summary_validation_warnings"])

    def test_correct_wave_order_keeps_the_llm_summary(self) -> None:
        llm_summary = (
            "The migration moves Aurora MySQL first, then adds the cache, then moves to "
            "DynamoDB."
        )
        out = apply_synthesis_llm_output(self._det(), {"executive_summary": llm_summary})
        assert out["summary_source"] == "llm"
        assert out["executive_summary"] == llm_summary
        assert out["summary_validation_warnings"] == []
