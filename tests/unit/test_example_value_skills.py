# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every designer skill that writes example values states the same rule (#2053)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.example_values import EXAMPLE_CREDENTIAL_RULE, EXAMPLE_VALUE_RULE

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "src" / "skills"
COMMANDS = REPO / ".claude" / "commands"
# The designers whose output holds sample documents or example values.
DESIGNER_SKILLS = {
    "documentdb": "documentdb-data-modeling.md",
    "elasticache": "elasticache-data-modeling.md",
    "opensearch": "opensearch-index-modeling.md",
}


@pytest.mark.parametrize("skill", sorted(DESIGNER_SKILLS.values()))
def test_the_skill_states_the_rule_word_for_word(skill: str) -> None:
    assert EXAMPLE_VALUE_RULE in (SKILLS / skill).read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", sorted(DESIGNER_SKILLS.values()))
def test_the_skill_never_asks_for_realistic_or_plausible_values(skill: str) -> None:
    text = (SKILLS / skill).read_text(encoding="utf-8").lower()
    assert "realistic" not in text and "plausible values" not in text


@pytest.mark.parametrize("engine", sorted(DESIGNER_SKILLS))
def test_the_rule_sits_where_the_designer_writes_example_values(engine: str) -> None:
    """In the skill and in the design command's step 3, word for word, with the
    credential-column sentence right after it (#2053)."""
    both = f"{EXAMPLE_VALUE_RULE} {EXAMPLE_CREDENTIAL_RULE}"
    assert both in (SKILLS / DESIGNER_SKILLS[engine]).read_text(encoding="utf-8")
    command = (COMMANDS / f"design-schema-{engine}.md").read_text(encoding="utf-8")
    step3 = command[command.index("3. **Design the schema**") : command.index("4. **")]
    assert both in step3


def test_the_documentdb_output_field_states_the_rule_itself() -> None:
    text = (SKILLS / DESIGNER_SKILLS["documentdb"]).read_text(encoding="utf-8")
    field_line = next(
        line for line in text.splitlines() if line.startswith("- `document_examples`")
    )
    assert EXAMPLE_VALUE_RULE in field_line and EXAMPLE_CREDENTIAL_RULE in field_line
