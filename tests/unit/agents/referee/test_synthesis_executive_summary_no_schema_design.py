"""#132: the executive summary prompt must not assert schema design happened
when it was skipped, or forbid the model from saying so.

``generate_executive_summary``'s prompt opened unconditionally with "You just
completed a full database modernization assessment. You designed the target
schemas, mapped every access pattern, and validated everything." -- even when
no engine has ``schema_design_available`` (#116 is a concrete instance: every
dispatched engine skipped while the pipeline still reported COMPLETED). The
prompt also told the model to report "how many target tables/indexes were
designed" and that capability gaps "are SOLVED by the schema design you
produced", neither of which exists in that case.

These tests patch the ``strands`` ``Agent``/``BedrockModel`` the function
imports internally so no real model call happens; the stand-in records the
prompt and system prompt it was called with (same technique as
``tests/unit/agents/test_prompt_framing.py``).
"""

from __future__ import annotations

from unittest.mock import patch

from src.agents.referee import synthesis_report


class _CapturingAgent:
    """Stand-in for strands.Agent recording both prompt and system prompt."""

    last_prompt: str = ""
    last_system_prompt: str = ""

    def __init__(self, *args, **kwargs) -> None:
        type(self).last_system_prompt = kwargs.get("system_prompt", "")

    def __call__(self, prompt: str):
        type(self).last_prompt = prompt
        return "A concise executive summary that is long enough to pass the length gate."


def _generate(ranking: list[dict], table_mappings: list[dict] | None = None) -> None:
    _CapturingAgent.last_prompt = ""
    _CapturingAgent.last_system_prompt = ""
    with (
        patch("strands.Agent", _CapturingAgent),
        patch("strands.models.bedrock.BedrockModel", lambda *a, **k: object()),
    ):
        synthesis_report.generate_executive_summary(
            deterministic_summary="fallback",
            ranking=ranking,
            query_groups=[],
            tco={},
            risks={"overall_risk_level": "LOW", "risks": []},
            table_mappings=table_mappings or [],
            trade_offs=[],
        )


class TestNoEngineHasASchemaDesign:
    """Every engine's design was skipped (#116's reproduction): confidence,
    workload and routing still exist, but no target schema does."""

    RANKING = [
        {"target": "dynamodb", "confidence_score": 90, "assigned_queries": 1486},
        {"target": "aurora_postgresql", "confidence_score": 70, "assigned_queries": 162},
    ]

    def test_premise_does_not_claim_a_design_was_produced(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert prompt, "Agent was not invoked / prompt not captured"
        assert "You designed the target schemas" not in prompt
        assert "validated everything" not in prompt

    def test_prompt_states_schema_design_did_not_run(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt.lower()
        assert "schema design" in prompt and (
            "has not run" in prompt or "not yet run" in prompt or "did not run" in prompt
        )

    def test_does_not_claim_capability_gaps_are_already_solved(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "SOLVED by the schema design you produced" not in prompt

    def test_does_not_ask_for_a_designed_table_count(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "how many target tables/indexes were designed" not in prompt

    def test_system_prompt_does_not_claim_schemas_were_designed(self) -> None:
        _generate(self.RANKING)
        system_prompt = _CapturingAgent.last_system_prompt
        assert "designed every target schema" not in system_prompt


class TestUnversionedRunWithNoSchemaDesign:
    """#370 recheck: an unversioned run (no assignment artifact) never adds
    assigned_queries to a ranking entry. designed_and_not_designed_engines()
    used to come back with BOTH lists empty here (gated on workload that
    doesn't exist yet), and an empty not_designed made the prompt fall into
    the fully assertive "all designed" branch with nothing to back it. It
    must land in the same honest, routing-only branch as the versioned
    none-designed case.
    """

    RANKING = [
        {"target": "dynamodb", "confidence_score": 90, "schema_design_available": False},
        {"target": "aurora_postgresql", "confidence_score": 70, "schema_design_available": False},
    ]

    def test_premise_does_not_claim_a_design_was_produced(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert prompt, "Agent was not invoked / prompt not captured"
        assert "You designed the target schemas" not in prompt
        assert "validated everything" not in prompt

    def test_prompt_states_schema_design_did_not_run(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt.lower()
        assert "schema design" in prompt and "has not run" in prompt

    def test_system_prompt_does_not_claim_schemas_were_designed(self) -> None:
        _generate(self.RANKING)
        assert "designed every target schema" not in _CapturingAgent.last_system_prompt


class TestAtLeastOneEngineHasASchemaDesign:
    """Regression guard: when real design work exists, the assertive premise
    the CTO reads must not regress to a hedging one."""

    RANKING = [
        {
            "target": "dynamodb",
            "confidence_score": 90,
            "assigned_queries": 1486,
            "schema_design_available": True,
            "target_tables": 12,
            "access_patterns": 30,
        },
    ]

    def test_premise_keeps_the_assertive_claim(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "You designed the target schemas" in prompt

    def test_system_prompt_keeps_the_assertive_claim(self) -> None:
        _generate(self.RANKING)
        assert "designed every target schema" in _CapturingAgent.last_system_prompt


class TestPartialSchemaDesign:
    """#132 review finding 1 (important): dynamodb has a design, opensearch's
    was skipped. The premise must name only dynamodb as designed, and say
    plainly that opensearch's has not run -- not narrate a design for it too."""

    RANKING = [
        {
            "target": "dynamodb",
            "confidence_score": 90,
            "assigned_queries": 1486,
            "schema_design_available": True,
            "target_tables": 12,
            "access_patterns": 30,
        },
        {
            "target": "opensearch",
            "confidence_score": 60,
            "assigned_queries": 6,
        },
    ]

    def test_premise_names_the_designed_engine(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "DynamoDB" in prompt

    def test_premise_says_the_other_engines_design_has_not_run(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "OpenSearch" in prompt
        assert "has not run" in prompt

    def test_does_not_claim_the_undesigned_engine_was_designed(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "designed the target schemas" not in prompt
        assert "validated everything" not in prompt

    def test_does_not_ask_for_a_table_count_on_the_undesigned_engine(self) -> None:
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "how many target tables/indexes were designed" not in prompt

    def test_tone_line_is_not_the_built_framing(self) -> None:
        """370-3: 'here is what we built' must not sit next to 'has not run'."""
        _generate(self.RANKING)
        prompt = _CapturingAgent.last_prompt
        assert "here is what we built" not in prompt
        assert "here is how the workload is routed" in prompt

    def test_system_prompt_names_the_designed_engine_only(self) -> None:
        _generate(self.RANKING)
        system_prompt = _CapturingAgent.last_system_prompt
        assert "DynamoDB" in system_prompt
        assert "designed every target schema" not in system_prompt
        assert "has not run yet for OpenSearch" in system_prompt
