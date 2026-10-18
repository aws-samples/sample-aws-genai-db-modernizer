"""The plain-prose style list and its helpers (#1034)."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.shared.prose_style import (
    FILLER_WORDS,
    HEDGES,
    clean_model_prose,
    collector_identifiers,
    find_style_hits,
    replace_dashes,
)


def _kinds(text: str, identifiers: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    return [(h.kind, h.term) for h in find_style_hits(text, identifiers)]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("DocumentDB adds nothing — 25 queries move.", ("dash", "—")),
        ("Read capacity—not write—is the limit.", ("dash", "—")),
        ("OpenSearch was never justified -- none of its queries", ("dash", "--")),
        ("(— see below)", ("dash", "—")),
        ("Pass -- before the file name.", ("dash", "--")),
        ("A robust design.", ("filler", "robust")),
        ("This ensures the reads stay fast.", ("filler", "ensures")),
        ("We leverage DynamoDB.", ("filler", "leverage")),
        ("Furthermore, the cache helps.", ("filler", "furthermore")),
        ("One path, not the only one.", ("hedge", "not the only one")),
        ("Reads typically hit the cache.", ("hedge", "typically")),
        ("Reads generally hit the cache.", ("hedge", "generally")),
        ("It might fail.", ("hedge", "might")),
        ("It could  potentially fail.", ("hedge", "could potentially")),
        ("Not just faster but cheaper.", ("construction", "not just/not only ... but")),
        ("It not only scales, but it costs less.", ("construction", "not just/not only ... but")),
        ("It's worth noting that reads dominate.", ("construction", "it's worth noting")),
        ("Here's the plan.", ("construction", "here's")),
        ("Let's start with DynamoDB.", ("construction", "let's")),
        ("Open CRITICAL/HIGH risks: 0", ("severity_caps", "CRITICAL/HIGH")),
        ("the tables behind the HIGH risks", ("severity_caps", "HIGH")),
        ("a HIGH-severity finding", ("severity_caps", "HIGH")),
        ("Severity HIGH applies.", ("severity_caps", "HIGH")),
        ("CRITICAL or HIGH severity", ("severity_caps", "CRITICAL or HIGH")),
        ("severity CRITICAL/HIGH only", ("severity_caps", "CRITICAL/HIGH")),
    ],
)
def test_each_item_of_the_list_is_found(text: str, expected: tuple[str, str]) -> None:
    assert expected in _kinds(text)


@pytest.mark.parametrize(
    "text",
    [
        "DocumentDB provides no unique capabilities: 25 queries can be served by Aurora MySQL.",
        # Ranges, compounds and minus signs (#1047 review).
        "Increase the refresh interval to 30s–60s.",
        "Costs drop by $100–$200 a month.",
        "p50–p99 latency",
        "us-east-1–us-west-2",
        "–5 degrees",
        "Pages 1–3",
        # The en dash is the range mark, spaced or not (#1047 review round 2).
        "9 am – 5 pm",
        "Monday – Friday",
        "10 GB – 50 GB",
        "p50 – p99 latency",
        "Q1 – Q3 2027",
        "17 Oct – 20 Oct",
        "us-east-1 – us-west-2",
        "10 – –5",
        "DocumentDB adds nothing – 25 queries move.",
        "Keep - this hyphen.",
        # Placeholders and badges.
        "—",
        "| Engine | — |",
        "| a | - |",
        "| a | -- |",
        "| HIGH |",
        "RISK-001 · HIGH · migration complexity: schema design gap",
        # Code, URLs, paths and identifiers.
        "Use the `ensure_index` helper.",
        "Run `SELECT 1 -- note` first.",
        "```sql\nSELECT 1 -- a comment\nUNLOCK TABLES;\n```",
        "```sql\nSELECT 1 -- note — robust\n",  # unclosed fence: runs to the end
        "~~~sql\nSELECT 1 -- note\n~~~",
        "````\n```\nSELECT 1 -- note\n```\n````",
        "``a ` b -- c``",
        "Run `aws s3 sync --dryrun -- src dst` and `--dry-run`.",
        "--flag",
        "See https://example.com/ensure-robust-setup for details.",
        "Logs are in /var/log/journey/robust.log.",
        "The might_fail column.",
        "The journey_events and journey-events tables.",
        "The mightCache flag and the RobustQueue class.",
        "Read wordpress.journeys first.",
        # Ordinary technical wording.
        "Set the page to landscape orientation.",
        "Elevated latency means elevated privileges are not needed.",
        "The feature is generally available in every Region.",
        "DynamoDB gives LOW latency and HIGH throughput.",
        # Not severity labels: a technical noun follows (#1047 maintainer call).
        "Set the HIGH/LOW watermark on the queue.",
        "Tune the HIGH/LOW water mark first.",
        "The HIGH/LOW threshold is 80%.",
        "LOW, MEDIUM and HIGH load levels.",
        # A row lock, not a filler word.
        "The row is unlocked after commit; UNLOCK TABLES releases it.",
        "Open critical or high risks: 0",
        "Not only reads. The writes move too; but later.",
        "",
    ],
)
def test_plain_text_has_no_hits(text: str) -> None:
    assert find_style_hits(text) == []


def test_collected_table_and_column_names_are_data_not_words() -> None:
    collector = {
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.journeys",
                    "table_name": "journeys",
                    "columns": [{"column_name": "landscape"}, {"column_name": "robust"}],
                }
            ]
        }
    }
    names = collector_identifiers(collector)
    assert {"shop.journeys", "journeys", "robust"} <= names
    assert find_style_hits("Journeys move to DynamoDB; robust is a flag column.", names) == []
    # Only the name is skipped: the same filler elsewhere still counts.
    assert ("filler", "ensures") in _kinds("Journeys move, which ensures speed.", tuple(names))


def test_the_list_holds_the_issue_items() -> None:
    for word in ("robust", "seamless", "leverage", "ensure", "comprehensive", "crucial"):
        assert word in FILLER_WORDS
    for dropped in ("landscape", "elevated", "unlock", "unlocked"):
        assert dropped not in FILLER_WORDS
    assert "not the only one" in HEDGES


@pytest.mark.parametrize(
    "text, expected",
    [
        # One em dash in a sentence: a comma, or a colon before a number or a list.
        (
            "DynamoDB serves reads — Aurora keeps joins.",
            "DynamoDB serves reads, Aurora keeps joins.",
        ),
        ("DocumentDB adds nothing — 25 queries move.", "DocumentDB adds nothing: 25 queries move."),
        (
            "Three engines — DynamoDB, Aurora and ElastiCache.",
            "Three engines: DynamoDB, Aurora and ElastiCache.",
        ),
        ("never justified -- none of its queries", "never justified, none of its queries"),
        ("Read capacity—the limit.", "Read capacity, the limit."),
        # Two in one sentence on one line: parentheses.
        ("An aside — a short one — here.", "An aside (a short one) here."),
        ("Read capacity—not write—is the limit.", "Read capacity (not write) is the limit."),
        # Each line and each side of a semicolon is its own sentence (round 2).
        (
            "- DynamoDB — 12 queries\n- Aurora — 3 queries",
            "- DynamoDB: 12 queries\n- Aurora: 3 queries",
        ),
        ("DynamoDB — fast; Aurora — relational.", "DynamoDB, fast; Aurora, relational."),
        # Any other case is left as it is (and is rejected at the seam).
        ("A — b — c — d.", "A — b — c — d."),
        ("Trailing dash —", "Trailing dash —"),
        ("— Leading dash", "— Leading dash"),
        ("(— see below)", "(— see below)"),
        # En dashes, hyphens and placeholders are never touched.
        ("9 am – 5 pm", "9 am – 5 pm"),
        ("Costs drop by $100–$200 a month.", "Costs drop by $100–$200 a month."),
        ("us-east-1 – us-west-2", "us-east-1 – us-west-2"),
        ("10 – –5", "10 – –5"),
        ("| a | — |", "| a | — |"),
        ("| a | -- |", "| a | -- |"),
        ("Keep - this.", "Keep - this."),
        # Code is never touched, by CommonMark's rules (round 2).
        ("`SELECT 1 -- x`", "`SELECT 1 -- x`"),
        ("```sql\nSELECT 1 -- note\n```", "```sql\nSELECT 1 -- note\n```"),
        ("```sql\nSELECT 1 -- note\n", "```sql\nSELECT 1 -- note\n"),
        ("~~~sql\nSELECT 1 -- note\n~~~", "~~~sql\nSELECT 1 -- note\n~~~"),
        ("````\n```\nSELECT 1 -- note\n```\n````", "````\n```\nSELECT 1 -- note\n```\n````"),
        ("``a ` b -- c``", "``a ` b -- c``"),
        ("Run `a — b` — then stop.", "Run `a — b`, then stop."),
        ("See https://x.example/a—b — then stop.", "See https://x.example/a—b, then stop."),
    ],
)
def test_replace_dashes(text: str, expected: str) -> None:
    assert replace_dashes(text) == expected


def test_a_dash_it_cannot_rewrite_is_rejected_at_the_seam() -> None:
    result = clean_model_prose("A — b — c — d.", "fallback")
    assert not result.accepted and result.text == "fallback"


_TOKEN = re.compile(r"[A-Za-z0-9$%]+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


@given(
    st.lists(
        st.sampled_from(
            [
                "10",
                "20",
                "$100",
                "$200",
                "5%",
                "30s",
                "60s",
                "ms",
                "GB",
                "p50",
                "p99",
                "reads",
                "Aurora",
                "DynamoDB",
                "x",
                "—",
                "–",
                " — ",
                " – ",
                " -- ",
                "(",
                ")",
                ".",
                ",",
                ":",
                " ",
                "`SELECT 1 -- y`",
                "|",
                "-",
            ]
        ),
        max_size=14,
    )
)
def test_replace_dashes_never_drops_or_reorders_a_figure_or_word(parts: list[str]) -> None:
    text = "".join(parts)
    out = replace_dashes(text)
    assert _tokens(out) == _tokens(text)
    assert re.findall(r"[0-9$%]", out) == re.findall(r"[0-9$%]", text)
    assert out.count("–") == text.count("–")  # the range mark is never touched
    assert out.count("`") == text.count("`")


def test_clean_model_prose_keeps_text_whose_only_hits_are_dashes() -> None:
    result = clean_model_prose("DynamoDB serves reads — Aurora keeps joins.", "fallback")
    assert result.accepted
    assert result.text == "DynamoDB serves reads, Aurora keeps joins."
    assert result.reason == ""


def test_clean_model_prose_rejects_other_hits_back_to_the_fallback() -> None:
    result = clean_model_prose("A robust design — it ensures speed.", "fallback")
    assert not result.accepted
    assert result.text == "fallback"
    assert "filler:robust" in result.reason and "filler:ensures" in result.reason


CUSTOMER_PROSE_SKILLS = (
    "executive-summary.md",
    "consolidation-review.md",
    "aurora_mysql-data-modeling.md",
    "aurora_postgresql-data-modeling.md",
    "documentdb-data-modeling.md",
    "dynamodb-data-modeling.md",
    "elasticache-data-modeling.md",
    "opensearch-index-modeling.md",
)


@pytest.mark.parametrize("name", CUSTOMER_PROSE_SKILLS)
def test_every_skill_that_writes_customer_prose_quotes_the_style_list(name: str) -> None:
    from src.shared.prose_style import style_list_markdown
    from src.shared.skill_sections import skill_path

    assert style_list_markdown() in skill_path(name).read_text()


def test_the_style_list_prompt_names_every_word() -> None:
    from src.shared.prose_style import style_list_markdown

    text = style_list_markdown()
    for word in ("robust", "ensure", "journey", "truly", "might", "could potentially"):
        assert word in text
