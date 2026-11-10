# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each PE review skill asks only for what its contract accepts (#2010).

A reviewer that follows its skill writes the categories, severities and verdict
strings the skill names. When one is not in the engine's ``PEReviewResult``
enums, the review fails validation: a structured-output retry on Bedrock (and a
design accepted with no review if that fails too), a spent ``--finalize``
attempt on the headless path. The skill's stated review count must be the
runner's. The contract and the code are the source of truth; these tests read
the skills against them. Nothing here calls a model.
"""

from __future__ import annotations

import re
import types
import typing
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from src.agents.schema_design.pe_review_external import review_contract, review_skill
from src.tools.schema import (
    aurora_mysql_schema_agent,
    aurora_postgresql_schema_agent,
    base_schema_agent,
    documentdb_schema_agent,
    dynamodb_schema_agent,
    elasticache_schema_agent,
    opensearch_schema_agent,
)

REPO = Path(__file__).resolve().parents[4]

#: Every engine with a PE reviewer, and its designer module: the module's
#: ``_format_pe_feedback`` builds the revision turn.
DESIGNER_MODULES: dict[str, ModuleType] = {
    "dynamodb": dynamodb_schema_agent,
    "documentdb": documentdb_schema_agent,
    "opensearch": opensearch_schema_agent,
    "elasticache": elasticache_schema_agent,
    "aurora_postgresql": aurora_postgresql_schema_agent,
    "aurora_mysql": aurora_mysql_schema_agent,
}
#: The module whose MAX_PE_ITERATIONS caps each engine's loop (OpenSearch runs its own
#: loop; the others run SchemaDesignRunner).
LOOP_MODULES: dict[str, ModuleType] = {
    engine: opensearch_schema_agent if engine == "opensearch" else base_schema_agent
    for engine in DESIGNER_MODULES
}
ENGINES = tuple(DESIGNER_MODULES)
#: The engines /review-schema reviews headless: every engine (#2081).
HEADLESS_ENGINES = ENGINES

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
#: Spellings a skill once used that no contract accepts.
LEGACY_VALUES = {"approved", "changes_needed", "CHANGES_NEEDED", "REJECTED", "rejected"}


def _engines(right_on_main: tuple[str, ...] = ()) -> list[Any]:
    """The engines, with ``passes_on_main`` on those whose skill already passed the check."""
    mark = pytest.mark.passes_on_main
    return [
        pytest.param(engine, marks=mark) if engine in right_on_main else engine
        for engine in sorted(ENGINES)
    ]


def _enum_values(annotation: Any) -> set[str]:
    enum = next(a for a in (annotation, *typing.get_args(annotation)) if isinstance(a, type))
    assert issubclass(enum, Enum), enum
    return {member.value for member in enum}


def _contract_sets(engine: str) -> dict[str, set[str]]:
    review = review_contract(engine)
    change = typing.get_args(review.model_fields["change_requests"].annotation)[0]
    return {
        "verdict": _enum_values(review.model_fields["verdict"].annotation),
        "category": _enum_values(change.model_fields["category"].annotation),
        "severity": _enum_values(change.model_fields["severity"].annotation),
    }


SETS = {engine: _contract_sets(engine) for engine in ENGINES}
#: Every value any PE contract publishes, plus the legacy spellings: a skill token in
#: here is a contract value, and must be one of its own engine's.
VOCABULARY = set().union(*(s for sets in SETS.values() for s in sets.values())) | LEGACY_VALUES


def _skill(engine: str) -> str:
    return (REPO / review_skill(engine)).read_text(encoding="utf-8")


def _allowed(engine: str) -> set[str]:
    return set().union(*SETS[engine].values())


def _contract_tokens(text: str) -> set[str]:
    """Code-span tokens and verdict-like words that are PE contract values."""
    spans = set(re.findall(r"`([A-Za-z_]+)`", text))
    verdict_words = set(re.findall(r"\b(?:CHANGES_[A-Z]+|APPROVED|REJECTED)\b", text))
    after_verdict = {
        word
        for word in re.findall(r"(?i)\bverdict\b\W{1,4}([A-Za-z_]+)", text)
        if word in VOCABULARY
    }
    return (spans & VOCABULARY) | verdict_words | after_verdict


def _declared(text: str, field: str) -> set[str]:
    """The values the skill's Output contract lists for ``field``."""
    assert "\n## Output contract\n" in text, "the skill has an Output contract section"
    section = text.split("## Output contract", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in section.splitlines() if ln.startswith(f"- `{field}`:")]
    assert len(lines) == 1, f"the Output contract declares `{field}` once"
    return set(re.findall(r"`([A-Za-z_]+)`", lines[0].split(":", 1)[1]))


def _stated_counts(text: str) -> list[tuple[str, int]]:
    """Each "N reviews / iterations / rounds" the text states, with its number."""
    pattern = (
        r"(?i)\b(\d+|one|two|three|four|five)\s+(?:PE\s+)?"
        r"(iterations?|reviews?|review rounds?|rounds?)\b"
        r"|\breview(?:ed|s)?\s+(\d+|one|two|three|four|five)\s+times\b"
    )
    counts = []
    for match in re.finditer(pattern, text):
        word = (match.group(1) or match.group(3)).lower()
        counts.append((match.group(0), int(NUMBER_WORDS.get(word, word))))
    return counts


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_the_output_contract_lists_exactly_the_contract_values(engine: str) -> None:
    text = _skill(engine)
    for field in ("verdict", "category", "severity"):
        assert _declared(text, field) == SETS[engine][field], (engine, field)


@pytest.mark.parametrize("engine", _engines(("aurora_mysql", "aurora_postgresql", "elasticache")))
def test_every_contract_value_the_skill_names_is_its_engines(engine: str) -> None:
    named = _contract_tokens(_skill(engine))
    assert named, engine
    assert named <= _allowed(engine), f"{engine}: {sorted(named - _allowed(engine))}"


@pytest.mark.parametrize(
    "engine", _engines(("aurora_mysql", "aurora_postgresql", "dynamodb", "elasticache"))
)
def test_a_category_or_severity_the_skill_asks_for_is_one_of_that_kind(engine: str) -> None:
    text = _skill(engine)
    for kind in ("category", "severity"):
        asked = re.findall(rf"(?<![\w_]){kind}[`*:\s]{{1,5}}`([A-Za-z_]+)`", text)
        wrong = [value for value in asked if value not in SETS[engine][kind]]
        assert not wrong, f"{engine}: {kind} {wrong}"


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_a_stated_review_count_is_the_runners(engine: str) -> None:
    cap = LOOP_MODULES[engine].MAX_PE_ITERATIONS
    stated = _stated_counts(_skill(engine))
    assert stated, f"{engine}: the skill says how many reviews a design gets"
    assert all(number == cap for _, number in stated), (engine, cap, stated)


@pytest.mark.passes_on_main  # main's command already names only these, now for all six (#2081)
def test_review_schema_names_only_values_every_headless_engine_accepts() -> None:
    command = (REPO / ".claude/commands/review-schema.md").read_text(encoding="utf-8")
    common = set.intersection(*(_allowed(engine) for engine in HEADLESS_ENGINES))
    named = _contract_tokens(command)
    assert named and named <= common, sorted(named - common)
    assert not [n for _, n in _stated_counts(command) if n != 1], "one review, no revision"


@pytest.mark.passes_on_main  # the extraction must see the values it checks
def test_the_checks_catch_the_spellings_the_skills_once_used() -> None:
    old = (
        "issue `CHANGES_REQUESTED` with category `scope_challenge`\n"
        "- **severity**: `blocker` (must fix), `major` (should fix), `minor`\n"
        "- **Verdict: changes_needed** — fix it\n"
        "- **Max 3 iterations.** If you've reviewed 3 times, approve."
    )
    assert {"scope_challenge", "major", "minor", "changes_needed"} <= _contract_tokens(old)
    assert not {"scope_challenge", "major", "minor"} <= _allowed("documentdb")
    assert [n for _, n in _stated_counts(old)] == [3, 3]


# --- What the skills say about the loop is what the runner does ---------------------------


class _Out(BaseModel):
    trade_offs: list[Any] = []


def _request(engine: str, target: str) -> dict:
    request = {
        "category": "indexing" if engine.startswith("aurora") else "access_pattern",
        "severity": "blocker",
        "target": target,
        "requested_change": "add the index",
        "rationale": "the hot read scans",
    }
    if engine == "dynamodb":
        request["current_state"] = "no index"
    return request


def _review(engine: str, verdict: str, **extra: Any) -> BaseModel:
    return review_contract(engine).model_validate({"verdict": verdict, "summary": "s", **extra})


def _runner(reviews: list[BaseModel]) -> tuple[base_schema_agent.SchemaDesignRunner, list[str]]:
    prompts: list[str] = []

    def designer(prompt: str, **_kwargs: Any) -> Any:
        prompts.append(prompt)
        return types.SimpleNamespace(structured_output=_Out())

    queue = iter(reviews)
    runner = base_schema_agent.SchemaDesignRunner(
        target_type="dynamodb",
        output_model=_Out,
        model=types.SimpleNamespace(),  # type: ignore[arg-type]
        designer_agent=designer,  # type: ignore[arg-type]
        pe_reviewer_fn=lambda *_args: next(queue),
    )
    return runner, prompts


@pytest.mark.passes_on_main  # the behaviour the skills now describe
def test_a_design_gets_at_most_max_pe_iterations_reviews() -> None:
    reviews = [
        _review("dynamodb", "CHANGES_REQUESTED", change_requests=[_request("dynamodb", f"t{i}")])
        for i in range(base_schema_agent.MAX_PE_ITERATIONS + 2)
    ]
    runner, prompts = _runner(reviews)
    runner.run("design it")
    reviewed = [e for e in runner.trace.entries if e["type"] == "pe_review"]
    assert len(reviewed) == base_schema_agent.MAX_PE_ITERATIONS
    assert len(prompts) == base_schema_agent.MAX_PE_ITERATIONS  # the first design plus one revision


@pytest.mark.passes_on_main  # the behaviour the skills now describe
def test_an_approved_review_applies_no_change_request_and_keeps_its_notes() -> None:
    approved = _review(
        "dynamodb",
        "APPROVED",
        change_requests=[{**_request("dynamodb", "t"), "severity": "minor"}],
        pe_notes=["watch the hot key"],
    )
    runner, prompts = _runner([approved])
    output: Any = runner.run("design it")[0]
    assert prompts == ["design it"]  # no revision
    assert [t.description for t in output.trade_offs] == ["[PE note] watch the hot key"]


@pytest.mark.passes_on_main  # the behaviour the skills now describe
@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_the_revision_turn_carries_the_change_requests_and_strengths(engine: str) -> None:
    review = _review(
        engine,
        "CHANGES_REQUESTED",
        change_requests=[_request(engine, "posts")],
        strengths=["the tenant leads every key"],
    )
    feedback = DESIGNER_MODULES[engine]._format_pe_feedback(review)
    for text in ("posts", "add the index", "the hot read scans", "the tenant leads every key"):
        assert text in feedback, (engine, text)
