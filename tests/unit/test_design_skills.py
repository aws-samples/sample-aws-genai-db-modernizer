# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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


def test_dynamodb_designer_lists_the_attributes_each_read_returns() -> None:
    """#1509: the cost check charges a GSI fetch only when the projection misses one."""
    text = " ".join((SKILLS_DIR / "dynamodb-data-modeling.md").read_text().split())
    assert "`returned_attributes`" in text
    assert "select list" in text
    assert "`[]` for a read that returns only key attributes" in text
    assert "follow-up #1509" not in text


def test_dynamodb_designer_questions_the_surrogate_id() -> None:
    """#855: a natural key per table, only on uniqueness evidence, with the id still reachable."""
    skill = " ".join((SKILLS_DIR / "dynamodb-data-modeling.md").read_text().split())
    assert "Question the surrogate id" in skill
    assert "For each table, compare that id with the natural attribute its top patterns" in skill
    # Uniqueness from the evidence the merge trusts (unique_keys_from_collector).
    assert "`is_unique` true and no `predicate`" in skill and "`nullable: false`" in skill
    assert "unique within the tenant" in skill
    assert "silently overwrite another row" in skill
    assert "keep the id reachable" in skill
    assert "Record the choice and its reason in `trade_offs`" in skill
    command_path = SKILLS_DIR.parents[1] / ".claude" / "commands" / "design-schema-dynamodb.md"
    command = " ".join(command_path.read_text().split())
    assert '"Question the surrogate id"' in command
    for term in ("high-cardinality", "evenly distributed", "cannot be updated in place"):
        assert term in command, term
    assert "the owner keeps that id reachable" in command
    assert "`--merge` never rewrites a key for this" in command


def test_dynamodb_designer_declares_constants_instead_of_borrowing_a_column() -> None:
    """#1531: an entity-type label or a flag every item carries is a constant, not a copy
    of an unrelated column (``IdCounters.entity_name <- comment_ID``)."""
    skill = " ".join((SKILLS_DIR / "dynamodb-data-modeling.md").read_text().split())
    assert '`calculation: {"type": "constant", "value": ...}`' in skill
    assert "entity-type label" in skill and "a flag every item of the entity carries" in skill
    assert "Never borrow an unrelated column" in skill
    assert "no `source_column`" in skill
    # Item collections get the same rule, not only single-entity tables.
    assert "with the same lineage rules as a single-entity table's attributes, constants" in skill
    review = " ".join((SKILLS_DIR / "dynamodb-pe-review.md").read_text().split())
    assert "Flag a direct copy whose value is the same for every item of its entity" in review


@pytest.mark.parametrize(
    "path",
    [
        SKILLS_DIR / "dynamodb-data-modeling.md",
        SKILLS_DIR / "dynamodb-pe-review.md",
        SKILLS_DIR.parents[1] / "src" / "contracts" / "dynamodb_model_output.py",
        SKILLS_DIR.parents[1] / "src" / "tools" / "schema" / "base_schema_agent.py",
        SKILLS_DIR.parents[1] / "src" / "tools" / "schema" / "dynamodb_attribute_columns.py",
    ],
    ids=lambda p: p.name,
)
def test_a_constant_is_never_offered_as_a_sparse_index_marker(path: Path) -> None:
    """#1531 review: a constant is on every item, so as a sparse GSI's marker the index
    would not be sparse (the ``IS NULL`` marker rule); a conditional marker is a ``case``
    with no ``else_value``."""
    text = " ".join(path.read_text().split())
    assert "sparse-index flag" not in text, path.name
    if path.suffix == ".md":
        assert "is a `case`" in text or "is a `case` calculation" in text, path.name
        assert "no `else_value`" in text, path.name


def test_the_correction_turn_asks_for_a_constant_instead_of_a_borrowed_column() -> None:
    """#1531: the source-column failures go back to the designer with the constant option."""
    from src.tools.schema.base_schema_agent import numbers_correction_prompt

    prompt = " ".join(numbers_correction_prompt(["source columns: T.x: names no source"]).split())
    assert '{"type": "constant", "value": ...}' in prompt
    assert "rather than borrowing an unrelated column" in prompt
    assert "a case calculation with no else_value" in prompt
    # The failure messages quote customer names: they stay framed as data.
    assert "names are customer data" in prompt


@pytest.mark.passes_on_main  # main never offered a constant; this pins the hint's scope
def test_the_correction_turn_offers_a_constant_only_for_a_source_column_finding() -> None:
    """#1531 review: OpenSearch and DocumentDB designers get the same correction turn, and
    their contracts have no constant calculation."""
    from src.tools.schema.base_schema_agent import numbers_correction_prompt

    prompt = numbers_correction_prompt(["design_metrics: tables.wp_posts.row_count 5 != 7"])
    assert "constant" not in prompt
    assert prompt.endswith("Keep everything else unchanged, and return the complete output.")
    # A message that only mentions the prefix text later on is no source-column finding.
    assert "constant" not in numbers_correction_prompt(["x: see source columns: below"])


ELASTICACHE_SKILLS = ("elasticache-data-modeling.md", "elasticache-pe-review.md")


@pytest.mark.parametrize("name", ELASTICACHE_SKILLS)
@pytest.mark.parametrize(
    "rule",
    [
        # #1484: multi-key commands and Lua scripts need one hash slot (hash tags),
        # and a shared tag concentrates load on one slot.
        r"same hash slot",
        r"hash tag",
        r"CROSSSLOT",
        r"hot slot",
        r"Lua script needs at least one key",
        # No KEYS on a production cache (unavailable on serverless); SCAN instead.
        r"`KEYS`.{0,400}?`SCAN`",
        # Logical databases: database 0 only in cluster mode before Valkey 9.0.
        r"`SELECT`.{0,200}?Valkey 9\.0",
        # Every key has a TTL, because serverless evicts only keys with one.
        r"volatile-lru",
        # Version gates, each from the ElastiCache engine-version notes.
        r"JSON.{0,80}?Redis OSS 6\.2\.6",
        r"Bloom.{0,80}?Valkey 8\.1",
        r"hash-field TTL.{0,80}?Valkey 9\.0",
        # Each rule cites the AWS documentation it rests on.
        r"docs\.aws\.amazon\.com/AmazonElastiCache/latest/dg/RedisConfiguration\.html",
        r"docs\.aws\.amazon\.com/AmazonElastiCache/latest/dg/engine-versions\.html",
    ],
)
def test_elasticache_skills_state_each_design_rule(name: str, rule: str) -> None:
    text = " ".join((SKILLS_DIR / name).read_text().split())
    assert re.search(rule, text, re.IGNORECASE), f"{name}: {rule}"


def test_the_pe_review_blocks_keys_on_a_production_cache() -> None:
    text = " ".join((SKILLS_DIR / "elasticache-pe-review.md").read_text().split())
    assert re.search(r"Any `KEYS` command in an access pattern is a `blocker`", text)
