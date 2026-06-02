"""Tests that LLM advisor classes load their system prompts from skill files."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class TestDynamoDBAdvisorLoadsFromFile:
    """LlmAdvisor must read its system prompt from src/skills/dynamodb-analysis-advisor.md."""

    def test_skill_file_exists(self):
        path = PROJECT_ROOT / "src" / "skills" / "dynamodb-analysis-advisor.md"
        assert path.exists(), f"Skill file missing: {path}"

    def test_loaded_prompt_contains_critical_rules(self):
        from src.tools.analysis.dynamodb_analysis_tools import load_advisor_prompt

        prompt = load_advisor_prompt()
        assert "MUST list the specific query_ids" in prompt
        assert "Do NOT hallucinate" in prompt

    def test_advisor_uses_loaded_prompt(self):
        from src.tools.analysis.dynamodb_analysis_tools import LlmAdvisor, load_advisor_prompt

        advisor = LlmAdvisor(enabled=False)
        expected = load_advisor_prompt()
        assert advisor.system_prompt == expected


class TestDocumentDBAdvisorLoadsFromFile:
    """LlmDocumentDBAdvisor must read its system prompt from src/skills/documentdb-analysis-advisor.md."""

    def test_skill_file_exists(self):
        path = PROJECT_ROOT / "src" / "skills" / "documentdb-analysis-advisor.md"
        assert path.exists(), f"Skill file missing: {path}"

    def test_loaded_prompt_contains_critical_rules(self):
        from src.tools.analysis.documentdb_analysis_tools import load_docdb_advisor_prompt

        prompt = load_docdb_advisor_prompt()
        assert "embed, reference, or hybrid" in prompt
        assert "Do NOT produce generic advice" in prompt

    def test_advisor_uses_loaded_prompt(self):
        from src.tools.analysis.documentdb_analysis_tools import (
            LlmDocumentDBAdvisor,
            load_docdb_advisor_prompt,
        )

        advisor = LlmDocumentDBAdvisor(enabled=False)
        expected = load_docdb_advisor_prompt()
        assert advisor.system_prompt == expected
