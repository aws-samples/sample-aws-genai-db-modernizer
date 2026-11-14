# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Design trade-offs grouped by a structured topic (#1221).

Each ``TradeOff`` carries a ``topic`` from a closed set the schema designer picks
(``src/contracts/schema_design_output.py``), and what the pipeline appends of its own
(the design review's notes, the schema agents' warnings) a ``note_kind`` no designer
is offered; a design saved before the fields reads as ``other``, its pipeline notes
by their text markers. Synthesis carries it into ``report.json``'s ``trade_offs``, and the
facts layer groups them once, into ``facts.trade_off_topics`` (ADR-032). The
engineering report lists each group under its label and count, and the results page
does the same (``src/ui/src/components/__tests__/TradeOffTopicGroups.test.js``, from
the same fixture).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from src.agents.referee.synthesis_handler import _collect_trade_offs
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.contracts.schema_design_output import TradeOff
from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report.engineering_report import _trade_offs_md
from src.report.facts import report_facts
from src.shared.contract_versioning import current_version
from src.shared.report_facts import compute_report_facts, trade_off_topic_facts
from src.shared.trade_off_topics import (
    TOPIC_LABELS,
    TOPICS_SINCE,
    TRADE_OFF_GROUPS,
    TRADE_OFF_TOPICS,
    predates_topics,
    trade_off_group,
    trade_off_note_kind,
    trade_off_topic,
    with_default_topics,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "src/ui/src/components/__tests__/fixtures/tradeOffTopics.json"
OLD_DESIGN = (
    ROOT / "tests/fixtures/real_runs/wordpress-both/job/schema-dynamodb/v2/schema_output.json"
)
# A model run's report synthesized (contract 1.60) before the topics, with its facts block.
OLD_REPORT = ROOT / "tests/unit/report/fixtures/wordpress_model_run_1_60_report.json"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _section(lines: list[str], heading: str) -> list[str]:
    """The lines under ``heading`` up to the next heading of the same or a higher level."""
    level = heading.split(" ", 1)[0]
    start = lines.index(heading)
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#") and len(line.split(" ", 1)[0]) <= len(level):
            break
        out.append(line)
    return out


# --- the contract ---------------------------------------------------------------


def test_every_group_has_a_label_and_other_is_the_default() -> None:
    assert set(TOPIC_LABELS) == set(TRADE_OFF_GROUPS)
    assert TRADE_OFF_GROUPS == (*TRADE_OFF_TOPICS, "warning", "review_note")
    assert TRADE_OFF_TOPICS[-1] == "other"
    t = TradeOff(description="d", impact="i")
    assert (t.topic, t.note_kind) == ("other", None)


@pytest.mark.parametrize("topic", TRADE_OFF_TOPICS)
def test_a_trade_off_round_trips_its_topic(topic: str) -> None:
    t = TradeOff(description="d", impact="i", engine="dynamodb", topic=topic)  # type: ignore[arg-type]
    dumped = t.model_dump(mode="json")
    assert dumped["topic"] == topic
    assert TradeOff.model_validate(dumped) == t


@pytest.mark.parametrize("kind", ["review", "warning"])
def test_a_pipeline_note_round_trips_its_kind(kind: str) -> None:
    t = TradeOff(description="d", impact="i", note_kind=kind)  # type: ignore[arg-type]
    assert TradeOff.model_validate(t.model_dump(mode="json")).note_kind == kind


@pytest.mark.parametrize("topic", ["latency", "review_note", "warning"])
def test_a_topic_outside_the_designers_set_is_rejected(topic: str) -> None:
    """``review_note`` and ``warning`` are groups the pipeline's own notes are listed
    in, never a topic a designer can write (a design decision would otherwise move
    into the collapsed review notes)."""
    with pytest.raises(ValidationError):
        TradeOff.model_validate({"description": "Use a GSI", "impact": "x", "topic": topic})


def test_a_null_topic_reads_as_other() -> None:
    t = TradeOff.model_validate({"description": "d", "impact": "i", "topic": None})
    assert t.topic == "other"


def test_the_designer_schema_offers_only_the_designers_topics() -> None:
    props = TradeOff.model_json_schema()["properties"]
    # Null is listed too (read as other): Bedrock refuses a nullable-typed enum.
    assert props["topic"]["enum"] == [*TRADE_OFF_TOPICS, None]
    assert props["topic"]["default"] == "other"
    assert "note_kind" not in props
    # Nor anywhere in a designer's tool schema.
    schema = json.dumps(DynamoDBModelOutputContract.model_json_schema())
    assert "review_note" not in schema and "note_kind" not in schema


def test_a_design_saved_before_the_topic_loads_with_every_trade_off_as_other() -> None:
    raw = json.loads(OLD_DESIGN.read_text(encoding="utf-8"))
    assert raw["trade_offs"] and all("topic" not in t for t in raw["trade_offs"])
    design = DynamoDBModelOutputContract.model_validate(raw)
    assert {(t.topic, t.note_kind) for t in design.trade_offs} == {("other", None)}


def test_report_json_carries_the_topic_and_the_note_kind() -> None:
    """``report.json``'s ``trade_offs`` are the same ``TradeOff``, so both survive."""
    assert SynthesisOutputContract.model_fields["trade_offs"].annotation == list[TradeOff]
    rows = [TradeOff.model_validate(t) for t in _fixture()["trade_offs"][:5]]
    assert [(t.topic, t.note_kind) for t in rows] == [
        ("item_size", None),
        ("indexing", None),
        ("item_size", None),
        ("other", "review"),
        ("other", "warning"),
    ]


# --- which files are read by their text markers -----------------------------------------


@pytest.mark.parametrize("module", sorted(TOPICS_SINCE))
def test_topics_since_is_each_contracts_1221_version(module: str) -> None:
    """``TOPICS_SINCE`` names the version whose Version History entry is #1221's, so
    renumbering the bump (a rebase over another one) cannot leave it stale."""
    source = (ROOT / "src/contracts" / f"{module}.py").read_text(encoding="utf-8")
    entries = re.findall(r"^\s*- (\d+\.\d+) \([^)]*#1221[^)]*\)", source, re.M)
    assert entries == [TOPICS_SINCE[module]], module


def test_only_a_file_older_than_the_field_predates_it() -> None:
    assert predates_topics("dynamodb_model_output", "1.0")
    assert predates_topics("dynamodb_model_output", None) and predates_topics(
        "dynamodb_model_output", "x"
    )
    assert not predates_topics("dynamodb_model_output", TOPICS_SINCE["dynamodb_model_output"])
    assert not predates_topics(
        "dynamodb_model_output", current_version(DynamoDBModelOutputContract)
    )
    assert not predates_topics("synthesis_output", current_version(SynthesisOutputContract))
    assert predates_topics("synthesis_output", "1.72")
    assert not predates_topics("not_a_contract", "1.0")
    # Every engine's design output is listed under its contract module, as synthesis
    # looks it up.
    designed = {p.stem for p in (ROOT / "src/contracts").glob("*_model_output.py")}
    assert designed | {"synthesis_output"} == set(TOPICS_SINCE)


def test_a_marker_is_read_only_in_a_file_older_than_the_field() -> None:
    note = {"description": "[PE note] Check the index.", "impact": "x"}
    warning = {"description": "[WARNING] Revision failed.", "impact": "x"}
    assert trade_off_note_kind(note, legacy=True) == "review"
    assert trade_off_note_kind(warning, legacy=True) == "warning"
    # A design saved since the field: the same words a model wrote are only text.
    assert trade_off_note_kind(note, legacy=False) is None
    assert trade_off_note_kind(warning, legacy=False) is None
    # Its own kind always wins, and a designer's topic is kept apart from it.
    assert trade_off_note_kind({"description": "x", "note_kind": "warning"}, False) == "warning"
    assert trade_off_topic({"description": "[PE note] x", "topic": "cost"}) == "cost"
    assert trade_off_topic({"description": "x", "topic": "review_note"}) == "other"


def test_a_saved_design_always_carries_its_topic() -> None:
    out = with_default_topics(
        {"trade_offs": [{"description": "a"}, {"description": "b", "topic": None}, "c"]}
    )
    assert out["trade_offs"] == [
        {"description": "a", "topic": "other"},
        {"description": "b", "topic": "other"},
        "c",
    ]


# --- synthesis --------------------------------------------------------------------


def _collect(schema: dict[str, Any], engine: str = "dynamodb") -> list[dict[str, Any]]:
    data = SimpleNamespace(engines={engine: SimpleNamespace(schema_design=schema)})
    return _collect_trade_offs(data)  # type: ignore[no-any-return]


NEW = current_version(DynamoDBModelOutputContract)


def test_synthesis_carries_each_design_trade_offs_topic_and_note_kind() -> None:
    out = _collect(
        {
            "contract_version": NEW,
            "trade_offs": [
                {"description": "A.", "impact": "a", "topic": "item_size"},
                {"description": "B.", "impact": "b", "topic": "consistency"},
                {"description": "[PE note] C.", "impact": "C.", "note_kind": "review"},
                {"description": "[WARNING] D.", "impact": "D.", "note_kind": "warning"},
            ],
        }
    )
    assert [(t["topic"], t["note_kind"]) for t in out] == [
        ("item_size", None),
        ("consistency", None),
        ("other", "review"),
        ("other", "warning"),
    ]


LEGACY_TRADE_OFFS = [
    {"description": "Keys follow the source.", "impact": "a"},
    {"description": "[PE note] Check the index.", "impact": "Check the index."},
    {"description": "[WARNING] Schema design produced from partial LLM output.", "impact": "w"},
    "A legacy string trade-off.",
    "[PE note] A legacy string note.",
]


def test_synthesis_reads_a_design_saved_before_the_field_by_its_markers() -> None:
    out = _collect({"contract_version": "1.0", "trade_offs": LEGACY_TRADE_OFFS})
    assert [(t["topic"], t["note_kind"]) for t in out] == [
        ("other", None),
        ("other", "review"),
        ("other", "warning"),
        ("other", None),
        ("other", "review"),
    ]


def test_synthesis_never_reads_a_newer_designs_text() -> None:
    """An external-mode design saved since the field, whose model wrote the marker's
    words itself: it stays a design trade-off."""
    out = _collect({"contract_version": NEW, "trade_offs": LEGACY_TRADE_OFFS})
    assert {(t["topic"], t["note_kind"]) for t in out} == {("other", None)}


# --- the facts layer -----------------------------------------------------------------


def test_the_facts_group_the_fixture_once() -> None:
    fixture = _fixture()
    report = {"contract_version": fixture["contract_version"], "trade_offs": fixture["trade_offs"]}
    rc = fixture["facts"]["reality_check_trade_offs"]
    assert trade_off_topic_facts(report, rc) == fixture["facts"]["trade_off_topics"]


def test_groups_follow_the_topic_order_and_leave_the_reality_check_out() -> None:
    fixture = _fixture()
    groups = fixture["facts"]["trade_off_topics"]
    assert [(g["engine"], g["topic"], g["trade_off_count"]) for g in groups] == [
        ("dynamodb", "item_size", 2),
        ("dynamodb", "indexing", 1),
        ("dynamodb", "warning", 1),
        ("dynamodb", "review_note", 1),
        ("elasticache", "other", 1),
        ("elasticache", "review_note", 1),
    ]
    assert groups[0]["indexes"] == [0, 2]
    assert all(g["engine"] != "reality-check" for g in groups)


def test_compute_report_facts_writes_the_groups_through_the_contract() -> None:
    fixture = _fixture()
    report = {
        "contract_version": fixture["contract_version"],
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-11-03T00:00:00Z",
        "ranking": [{"target": "dynamodb", "confidence_score": 70, "workload_percent": 100.0}],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "trade_offs": fixture["trade_offs"],
    }
    facts = compute_report_facts(report)
    assert facts["trade_off_topics"] == fixture["facts"]["trade_off_topics"]
    dumped = ReportFacts.model_validate(facts).model_dump(mode="json")
    assert dumped["trade_off_topics"] == fixture["facts"]["trade_off_topics"]


def test_no_trade_offs_means_no_groups() -> None:
    assert trade_off_topic_facts({"trade_offs": []}) == []
    assert trade_off_topic_facts({}) == []


def test_an_old_report_gets_the_groups_from_the_same_function() -> None:
    report = json.loads(OLD_REPORT.read_text(encoding="utf-8"))
    assert "trade_off_topics" not in (report.get("facts") or {})
    facts = report_facts(report)
    groups = facts["trade_off_topics"]
    assert groups == trade_off_topic_facts(report, facts.get("reality_check_trade_offs"))
    assert groups and {g["topic"] for g in groups} <= {"other", "warning", "review_note"}
    # Every design trade-off is in exactly one group.
    design = [
        i
        for i, t in enumerate(report["trade_offs"])
        if isinstance(t, dict) and t.get("engine") != "reality-check"
    ]
    assert sorted(i for g in groups for i in g["indexes"]) == design


def _old_and_new_reports() -> tuple[dict[str, Any], dict[str, Any]]:
    """The same review note and warning, in a report synthesized before #1221 (no
    topic, no note kind: the schema agents' text markers) and in one synthesized since."""
    old = {
        "contract_version": "1.72",
        "trade_offs": [
            {"description": "Keys follow the source.", "impact": "a", "engine": "dynamodb"},
            {"description": "[PE note] Check the index.", "impact": "x", "engine": "dynamodb"},
            {"description": "[WARNING] Revision failed.", "impact": "y", "engine": "dynamodb"},
        ],
    }
    new = {
        "contract_version": current_version(SynthesisOutputContract),
        "trade_offs": [
            {**old["trade_offs"][0], "topic": "other", "note_kind": None},  # type: ignore[dict-item]
            {**old["trade_offs"][1], "topic": "other", "note_kind": "review"},  # type: ignore[dict-item]
            {**old["trade_offs"][2], "topic": "other", "note_kind": "warning"},  # type: ignore[dict-item]
        ],
    }
    return old, new


def test_an_old_and_a_new_report_file_the_pipelines_notes_alike() -> None:
    old, new = _old_and_new_reports()
    expected = [
        ("dynamodb", "other", [0]),
        ("dynamodb", "warning", [2]),
        ("dynamodb", "review_note", [1]),
    ]
    for report in (old, new):
        groups = trade_off_topic_facts(report)
        assert [(g["engine"], g["topic"], g["indexes"]) for g in groups] == expected
    # In a new report the words alone file nothing.
    words_only = {**new, "trade_offs": [{**t, "note_kind": None} for t in new["trade_offs"]]}
    assert [g["topic"] for g in trade_off_topic_facts(words_only)] == ["other"]


def test_an_old_reports_warnings_stay_with_the_design_decisions() -> None:
    """A warning is a topic group of the design's own tab, never a review note."""
    old, _ = _old_and_new_reports()
    assert trade_off_group(old["trade_offs"][2], legacy=True) == "warning"
    lines = _trade_offs_md(old, {"trade_off_topics": trade_off_topic_facts(old)})
    ddb = _section(lines, "### DynamoDB")
    warnings = _section(lines, "#### Design warnings (1)")
    assert any("Revision failed" in line for line in warnings)
    assert "#### Design review notes (1)" in ddb


# --- the engineering report ------------------------------------------------------------


def test_the_engineering_report_lists_each_group_under_its_label_and_count() -> None:
    fixture = _fixture()
    lines = _trade_offs_md(fixture, fixture["facts"])
    ddb = _section(lines, "### DynamoDB")
    headings = [line for line in ddb if line.startswith("#### ")]
    assert headings == [
        "#### Item and value size (2)",
        "#### Indexes (1)",
        "#### Design warnings (1)",
        "#### Design review notes (1)",
    ]
    size = _section(lines, "#### Item and value size (2)")
    bullets = [line for line in size if line.startswith("- ")]
    assert len(bullets) == 2
    assert "711 bytes" in bullets[0] and "360 bytes" in bullets[1]


def test_an_engine_with_only_other_lists_its_trade_offs_without_a_topic_heading() -> None:
    fixture = _fixture()
    lines = _trade_offs_md(fixture, fixture["facts"])
    cache = _section(lines, "### ElastiCache")
    assert [line for line in cache if line.startswith("#### ")] == ["#### Design review notes (1)"]
    assert any("one hour TTL" in line or "one-hour TTL" in line for line in cache)


def test_the_reality_check_keeps_its_own_heading_ungrouped() -> None:
    fixture = _fixture()
    lines = _trade_offs_md(fixture, fixture["facts"])
    rc = _section(lines, "### Reality check")
    assert not [line for line in rc if line.startswith("#### ")]
    assert any("Event-Driven Sync" in line for line in rc)


def test_a_report_before_trade_offs_carried_an_engine_keeps_its_reality_check_lines() -> None:
    """A report written before trade-offs carried an engine files every line under
    "(general)": its reality-check consolidation shares the bucket with the design's
    lines, and no topic group lists it, yet it is printed."""
    report: dict[str, Any] = {
        "trade_offs": [
            {"description": "Posts keep their keys.", "impact": "No change for reads."},
            {
                "description": "Consolidated 3 queries from DynamoDB → Aurora PostgreSQL: "
                "DynamoDB provides no unique capabilities.",
                "impact": "Consolidated 3 queries from DynamoDB → Aurora PostgreSQL: "
                "DynamoDB provides no unique capabilities.",
            },
        ]
    }
    facts = compute_report_facts(report)
    assert [t["index"] for t in facts["reality_check_trade_offs"]] == [1]
    assert facts["trade_off_topics"] == [
        {"engine": "", "topic": "other", "label": "Other", "trade_off_count": 1, "indexes": [0]}
    ]
    lines = _trade_offs_md(report, facts)
    assert lines[0] == f"## Migration trade-offs ({facts['trade_off_count']})"
    bullets = [line for line in lines if line.startswith("- ")]
    assert len(bullets) == facts["trade_off_count"] == 2
    assert any("Consolidated 3 queries from DynamoDB" in line for line in bullets)
    assert any("Posts keep their keys" in line for line in bullets)


def test_the_heading_text_is_the_facts_label_escaped() -> None:
    fixture = copy.deepcopy(_fixture())
    fixture["facts"]["trade_off_topics"][0]["label"] = "Size *and* [limits]"
    lines = _trade_offs_md(fixture, fixture["facts"])
    assert "#### Size \\*and\\* \\[limits\\] (2)" in lines


def test_the_results_page_names_the_review_notes_as_the_report_does() -> None:
    """One label: the results page prints the facts' own, and its fallback string
    (a tab with no topic groups) has the same words."""
    en = json.loads((ROOT / "src/ui/src/locales/en.json").read_text(encoding="utf-8"))
    assert en["analysis-results.trade-offs.pe-notes-header"] == (
        f"{TOPIC_LABELS['review_note']} ({{{{count}}}})"
    )
    assert en["analysis-results.tradeoffs.topic-heading"] == "{{label}} ({{count}})"


# --- the pipeline's own trade-offs ------------------------------------------------------


def test_the_design_reviews_notes_and_the_agents_warnings_are_marked() -> None:
    from src.tools.schema.base_schema_agent import SchemaDesignRunner

    out = SimpleNamespace(trade_offs=[])
    agent = SimpleNamespace(target_type="dynamodb")
    SchemaDesignRunner._append_trade_offs(agent, out, ["[PE note] Check the index."])  # type: ignore[arg-type]
    SchemaDesignRunner._append_trade_offs(agent, out, ["[WARNING] Partial output."], "warning")  # type: ignore[arg-type]
    assert [(t.note_kind, t.topic, t.engine) for t in out.trade_offs] == [
        ("review", "other", "dynamodb"),
        ("warning", "other", "dynamodb"),
    ]


# --- the designer skills ------------------------------------------------------------------

_SKILLS = [
    "src/skills/aurora_mysql-data-modeling.md",
    "src/skills/aurora_postgresql-data-modeling.md",
    "src/skills/documentdb-data-modeling.md",
    "src/skills/dynamodb-data-modeling.md",
    "src/skills/elasticache-data-modeling.md",
    "src/skills/opensearch-index-modeling.md",
]


@pytest.mark.parametrize("skill", _SKILLS)
def test_each_designer_skill_lists_exactly_the_designers_topics(skill: str) -> None:
    text = (ROOT / skill).read_text(encoding="utf-8")
    [paragraph] = [line for line in text.splitlines() if line.startswith("`topic` files")]
    named = re.findall(r"`([a-z_]+)`", paragraph)
    assert [n for n in named if n != "topic"] == list(TRADE_OFF_TOPICS), skill
    assert "review_note" not in text and "note_kind" not in text, skill


def test_the_contracts_topic_guide_names_exactly_the_designers_topics() -> None:
    from src.contracts.schema_design_output import TRADE_OFF_TOPIC_GUIDE

    named = re.findall(r"\b([a-z_]+) \(", TRADE_OFF_TOPIC_GUIDE)
    assert named == list(TRADE_OFF_TOPICS)
    assert "review_note" not in TRADE_OFF_TOPIC_GUIDE


# --- the Bedrock designer path ------------------------------------------------------------


class _Design(BaseModel):
    trade_offs: list[TradeOff]


def _model_sent_a_note_kind() -> _Design:
    """A design whose model sent ``note_kind`` anyway (it is not in its schema)."""
    return _Design(trade_offs=[TradeOff(description="Use a GSI.", impact="x", note_kind="review")])


@pytest.mark.parametrize("structured", [True, False])
def test_the_bedrock_designer_clears_a_note_kind_the_model_sent(structured: bool) -> None:
    """The Bedrock path does what ``finalize_schema_design`` does by refusing it: a model's
    ``note_kind`` never files a design decision as a review note or a warning."""
    from src.tools.schema.base_schema_agent import SchemaDesignRunner

    sent = _model_sent_a_note_kind()
    result = (
        SimpleNamespace(structured_output=sent) if structured else _Text(sent.model_dump_json())
    )
    runner = SimpleNamespace(
        target_type="dynamodb", output_model=_Design, designer=lambda p: result
    )
    out: Any = SchemaDesignRunner._invoke_designer(runner, "prompt")  # type: ignore[arg-type]
    assert [(t.description, t.note_kind) for t in out.trade_offs] == [("Use a GSI.", None)]
    # The pipeline's own note appended afterwards keeps its kind.
    SchemaDesignRunner._append_trade_offs(runner, out, ["[PE note] Check it."])  # type: ignore[arg-type]
    assert [t.note_kind for t in out.trade_offs] == [None, "review"]


class _Text:
    """A designer result with no structured output, only its text."""

    def __init__(self, text: str) -> None:
        self.structured_output = None
        self._text = text

    def __str__(self) -> str:
        return self._text


def test_the_opensearch_designer_clears_a_note_kind_the_model_sent(monkeypatch) -> None:
    from src.contracts.opensearch_model_output import OpenSearchModelOutputContract
    from src.tools.schema import opensearch_schema_agent as osa

    sent = SimpleNamespace(
        trade_offs=[TradeOff(description="Denormalize.", impact="x", note_kind="warning")]
    )
    monkeypatch.setattr(osa, "OpenSearchModelOutputContract", type(sent))
    out = osa._invoke_designer(lambda p: SimpleNamespace(structured_output=sent), "prompt")  # type: ignore[arg-type]
    assert [t.note_kind for t in out.trade_offs] == [None]
    assert OpenSearchModelOutputContract  # the real contract is untouched by the stub
