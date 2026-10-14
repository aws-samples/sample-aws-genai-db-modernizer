"""#628: each model decision reads one prompt source in both LLM modes.

The consolidation review (``/reality-check`` in Claude Code, the consolidation
validator on Bedrock) and the synthesis executive summary (``/synthesize``,
``generate_executive_summary``) each used two independently written prompts,
which disagreed: the headless reality check pushed toward reversals, the
Bedrock one against them, and the Bedrock summary banned percentages the
headless one told the model to quote. Each decision now has one skill file the
command names and the Bedrock code loads; these tests pin that.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.referee import consolidation_validator as cv
from src.agents.referee import synthesis_report
from src.agents.referee.synthesis_grounding import _INTERNAL_FIELD_NAMES
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, PLACEHOLDER_ENGINES
from src.shared.skill_sections import skill_sections, skill_subsections

REPO = Path(__file__).resolve().parents[4]
COMMANDS = REPO / ".claude" / "commands"
# Display-name keys that are never a routing target: the placeholder engines,
# plus the generic "aurora" label (the real targets are aurora_mysql and
# aurora_postgresql). A new target engine without a capability entry fails here.
NON_ROUTING_ENGINE_KEYS = PLACEHOLDER_ENGINES | {"aurora"}
TARGET_ENGINES = tuple(sorted(set(ENGINE_DISPLAY_NAMES) - NON_ROUTING_ENGINE_KEYS))

# Wording that told one mode to lean one way on reversals: the headless
# command's "CRITICAL REVIEWER ... almost always FLAG ... Do not default to
# empty" against the validator's "Be conservative ... Most queries can be
# served by any engine".
BIASED_REVIEW_WORDING = (
    "critical reviewer",
    "almost always flag",
    "do not default to empty",
    "rare for aurora",
    "be conservative",
    "most queries can be served by any engine",
    "rubber-stamp",
)


# ---------------------------------------------------------------------------
# Consolidation review
# ---------------------------------------------------------------------------


class TestConsolidationReviewHasOneSource:
    def test_every_target_engine_has_a_capability_entry(self):
        assert set(TARGET_ENGINES) <= set(cv.ENGINE_CONTEXT)

    def test_capability_text_comes_from_the_skill(self):
        capabilities = skill_subsections("consolidation-review.md", "Engine Capabilities")
        assert cv.ENGINE_CONTEXT == capabilities

    def test_bedrock_system_prompt_carries_the_skill_instruction_verbatim(self):
        sections = skill_sections("consolidation-review.md")
        for heading in ("Instruction", "Flag When", "Do Not Flag"):
            assert sections[heading] in cv.VALIDATOR_SYSTEM_PROMPT

    @pytest.mark.parametrize("engine", TARGET_ENGINES)
    def test_bedrock_prompt_carries_the_target_capabilities_verbatim(self, engine):
        query = {
            "query_id": "q-1",
            "type": "SELECT",
            "cps": 1.0,
            "tables": ["t"],
            "signals": [],
            "sql": "SELECT 1",
        }
        prompt = cv._build_validation_prompt("aurora_mysql", engine, [query])
        assert cv.ENGINE_CONTEXT[engine] in prompt

    def test_command_reads_the_skill_and_carries_no_copy_of_it(self):
        command = (COMMANDS / "reality-check.md").read_text()
        assert "src/skills/consolidation-review.md" in command
        assert "CAN DO" not in command and "CANNOT DO" not in command
        assert "Flag Criteria" not in command

    def test_instruction_says_what_finalize_does_with_a_flag(self):
        """#712: a flag is evidence; the scorer re-routes among the kept engines
        and never revives a removed one (maintainer decision, 2026-10-14)."""
        instruction = skill_sections("consolidation-review.md")["Instruction"]
        assert "never names where the query goes" in instruction
        assert "among the engines the reality check kept" in instruction
        assert "never brings back an engine the reality check removed" in instruction
        assert "it stays where it is, and its record says why" in instruction
        assert "records it as ignored" in instruction
        # The pre-#712 redirect is gone from the text both modes read.
        assert "Aurora engine when one is in the architecture" not in instruction
        assert "back to the engine it came from" not in instruction

    def test_response_section_is_the_shared_contract(self):
        from src.contracts.model_flag import FlagReason, ModelFlag

        response = skill_sections("consolidation-review.md")["Response"]
        assert "RealityCheckModelResponse" in response
        for field in ModelFlag.model_fields:
            assert f"`{field}`" in response
        for reason in FlagReason:
            assert f"`{reason.value}`" in response
        assert response in cv.VALIDATOR_SYSTEM_PROMPT

    def test_reality_check_summary_rules_live_only_in_the_skill(self):
        """#628 (maintainer decision): one summary instruction for both modes."""
        command = (COMMANDS / "reality-check.md").read_text()
        assert "Executive Summary section" in command
        for copied_rule in ("em dash", "zero-ETL", "first person", "absorption_candidates`:"):
            assert copied_rule not in command
        handler = (REPO / "src/agents/referee/reality_check_handler.py").read_text()
        assert "briefing the CTO" not in handler and "STRICT STYLE RULES" not in handler

    def test_documentdb_target_is_stated(self):
        instruction = skill_sections("consolidation-review.md")["Instruction"]
        assert "DocumentDB 8.0 on an instance-based cluster" in instruction

    def test_skill_loads_lazily_so_deterministic_runs_do_not_need_it(self, tmp_path):
        """Importing the reality check with no skill files must still work."""
        code = (
            "import src.shared.skill_sections as s, pathlib, sys\n"
            f"s.SKILLS_DIR = pathlib.Path({str(tmp_path)!r})\n"
            "import src.agents.referee.reality_check_handler\n"
            "import src.agents.referee.consolidation_validator as cv\n"
            "try:\n    cv.engine_context()\nexcept FileNotFoundError:\n    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        result = subprocess.run(  # nosec B603 - fixed interpreter and code
            [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_neither_mode_carries_a_reversal_bias(self):
        texts = {
            "reality-check.md": (COMMANDS / "reality-check.md").read_text(),
            "consolidation-review.md": (REPO / "src/skills/consolidation-review.md").read_text(),
            "VALIDATOR_SYSTEM_PROMPT": cv.VALIDATOR_SYSTEM_PROMPT,
            "validation prompt": cv._build_validation_prompt("aurora_mysql", "dynamodb", []),
        }
        for name, text in texts.items():
            lowered = text.lower()
            for phrase in BIASED_REVIEW_WORDING:
                assert phrase not in lowered, f"{name} says {phrase!r}"


# ---------------------------------------------------------------------------
# Executive summary
# ---------------------------------------------------------------------------


class _CapturingAgent:
    last_prompt = ""
    last_system_prompt = ""

    def __init__(self, *args, **kwargs) -> None:
        type(self).last_system_prompt = kwargs.get("system_prompt", "")

    def __call__(self, prompt: str):
        type(self).last_prompt = prompt
        return "A concise executive summary that is long enough to pass the length gate."


RANKINGS = {
    "all_designed": [
        {
            "target": "dynamodb",
            "confidence_score": 90,
            "assigned_queries": 10,
            "workload_percent": 77.7,
            "schema_design_available": True,
            "target_tables": 3,
            "access_patterns": 10,
        },
    ],
    "some_designed": [
        {
            "target": "dynamodb",
            "confidence_score": 90,
            "assigned_queries": 10,
            "schema_design_available": True,
        },
        {"target": "opensearch", "confidence_score": 60, "assigned_queries": 2},
    ],
    "none_designed": [
        {"target": "dynamodb", "confidence_score": 90, "assigned_queries": 10},
    ],
}


def _llm_input(ranking: list[dict]) -> dict:
    """A minimal ``prepare_synthesis_llm_input`` payload for ``ranking``."""
    designed, not_designed = synthesis_report.designed_and_not_designed_engines(ranking)
    return {
        "deterministic_summary": "The deterministic summary.",
        "ranking": ranking,
        "source_database": "relational database",
        "schema_design_status": {
            "case": synthesis_report.schema_design_case(designed, not_designed),
            "designed": designed,
            "not_designed": not_designed,
        },
    }


def _generate(ranking: list[dict]) -> tuple[str, str]:
    with (
        patch("strands.Agent", _CapturingAgent),
        patch("src.shared.role_bedrock_model.RoleBedrockModel", lambda *a, **k: object()),
    ):
        synthesis_report.generate_executive_summary(_llm_input(ranking))
    return _CapturingAgent.last_prompt, _CapturingAgent.last_system_prompt


def _context(prompt: str) -> dict:
    """The JSON inside the prompt's untrusted-data block."""
    from src.agents.prompt_framing import _CLOSE, _OPEN

    body = prompt.split(_OPEN + "\n", 1)[1].split("\n" + _CLOSE, 1)[0]
    parsed: dict = json.loads(body)
    return parsed


class TestExecutiveSummaryHasOneSource:
    @pytest.mark.parametrize("case", sorted(RANKINGS))
    def test_bedrock_prompt_is_built_from_the_skill(self, case):
        prompt, system_prompt = _generate(RANKINGS[case])
        sections = skill_sections("executive-summary.md")
        for heading in synthesis_report._SUMMARY_PROMPT_SECTIONS:
            assert sections[heading] in prompt
        assert sections["Role"] in system_prompt
        assert "{designed}" not in prompt and "{not_designed}" not in prompt
        assert "{" + "it}" not in system_prompt

    @pytest.mark.parametrize("case", sorted(RANKINGS))
    def test_bedrock_context_is_the_request_payload(self, case):
        """Payload parity (#628): Bedrock sees exactly the headless request."""
        prompt, _ = _generate(RANKINGS[case])
        context = _context(prompt)
        assert context == _llm_input(RANKINGS[case])
        assert context["schema_design_status"]["case"] == case

    def test_source_engine_is_data(self):
        structure = skill_sections("executive-summary.md")["Structure"]
        assert "`source_database`" in structure
        assert "MySQL, MariaDB" not in structure

    def test_grounding_is_not_delegated_to_framed_data(self):
        """effective_architecture sits inside frame_untrusted on Bedrock, so the
        skill states the rule itself instead of saying to follow its text."""
        assert (
            "effective_architecture.rule"
            not in (REPO / "src/skills/executive-summary.md").read_text()
        )

    def test_case_selection(self):
        case = synthesis_report.schema_design_case
        assert case(["dynamodb"], []) == "all_designed"
        assert case(["dynamodb"], ["opensearch"]) == "some_designed"
        assert case([], ["opensearch"]) == "none_designed"
        # #370 recheck: both empty is not proof everything was designed
        assert case([], []) == "none_designed"

    def test_workload_share_rule_is_the_same_in_both_modes(self):
        """The Bedrock prompt used to ban percentages the headless one quoted."""
        prompt, _ = _generate(RANKINGS["all_designed"])
        guardrails = skill_sections("executive-summary.md")["Guardrails"]
        assert "quoted, exactly as given" in guardrails
        assert "percentages" not in prompt.lower().split("assessment context")[0]
        assert _context(prompt)["ranking"][0]["workload_percent"] == 77.7

    def test_command_reads_the_skill_and_carries_no_copy_of_it(self):
        command = (COMMANDS / "synthesize.md").read_text()
        assert "src/skills/executive-summary.md" in command
        assert "schema_design_status.case" in command
        for copied_rule in ("em dash", "77.7", "hedging", "SOLVED"):
            assert copied_rule not in command

    def test_skill_names_every_field_the_leak_check_rejects(self):
        grounding = skill_sections("executive-summary.md")["Grounding"]
        for name in _INTERNAL_FIELD_NAMES:
            assert f"`{name}`" in grounding
