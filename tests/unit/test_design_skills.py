"""Tripwires for the schema-design skills (#625).

The designer skills are the system prompts of unattended runs (headless
`/modernize --auto` subagents and Bedrock structured-output agents), and
ElastiCache is a cache layer only. These guard the fixes that removed text
contradicting both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "skills"
DESIGNER_SKILLS = sorted(
    [*SKILLS_DIR.glob("*-data-modeling.md"), *SKILLS_DIR.glob("*-index-modeling.md")]
)


def test_designer_skills_are_found() -> None:
    names = {p.name for p in DESIGNER_SKILLS}
    assert "dynamodb-data-modeling.md" in names
    assert "elasticache-data-modeling.md" in names


@pytest.mark.parametrize("path", DESIGNER_SKILLS, ids=lambda p: p.name)
def test_designer_skill_has_no_clarify_phase_or_questions(path: Path) -> None:
    # Nobody can answer a designer's questions: every caller is unattended.
    text = path.read_text()
    assert not re.search(r"^#+ .*\bClarify\b", text, re.MULTILINE), path.name
    assert not re.search(r"\bask\b[^.\n]*\bquestions?\b", text, re.IGNORECASE), path.name


def test_elasticache_skill_never_treats_the_cache_as_a_primary_store() -> None:
    # ElastiCache is cache-only here: every key has a TTL, no exception for
    # a "primary data store".
    text = " ".join((SKILLS_DIR / "elasticache-data-modeling.md").read_text().split())
    assert not re.search(r"unless[^.]*primary data store", text, re.IGNORECASE)
    assert "never the system of record" in text


DYNAMODB_SKILLS = ("dynamodb-data-modeling.md", "dynamodb-pe-review.md")


@pytest.mark.parametrize("name", DYNAMODB_SKILLS)
@pytest.mark.parametrize(
    "rule",
    [
        # #1483: uniqueness through a sentinel item in the record's transaction.
        r"sentinel item[^.]*TransactWriteItems",
        r"attribute_not_exists",
        r"Count items, not records",
        # Read-your-writes from the update itself, not a second read.
        r"ReturnValues=ALL_NEW",
        # The tenant leads the partition key: LeadingKeys matches it only.
        r"dynamodb:LeadingKeys",
    ],
)
def test_dynamodb_skills_state_each_design_rule(name: str, rule: str) -> None:
    text = " ".join((SKILLS_DIR / name).read_text().split())
    assert re.search(rule, text, re.IGNORECASE), f"{name}: {rule}"


def test_dynamodb_designer_prices_the_keys_only_fetch_and_states_the_sparse_threshold() -> None:
    text = " ".join((SKILLS_DIR / "dynamodb-data-modeling.md").read_text().split())
    # The fetch after a KEYS_ONLY index is a read per returned item, counted.
    assert "design_rps × avg_items_returned" in text
    # One sparse-GSI threshold, with its reason; the unexplained >90% is gone.
    assert "50% or more excluded" in text and "the 50% is ours, for this reason" in text
    assert ">90%" not in text
    # Callers with only the child's id are the identifying relationship's exception.
    assert "only the child's id" in text
