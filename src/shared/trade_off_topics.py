# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The topics a design trade-off is about, and their labels (#1221).

A schema designer files each trade-off under one ``topic`` from this closed set
(``TradeOff.topic``, ``src/contracts/schema_design_output.py``), so trade-offs that
say the same kind of thing (four design groups each stating the 400 KB item limit)
are listed together. The set comes from what the saved designs' trade-offs say:
item sizes, generated ids, indexes, keys and item layout, rewritten queries,
consistency windows, uniqueness, data types, capacity, cost and operations.

What the pipeline appends as a trade-off of its own carries a ``note_kind``
instead, which no designer is offered: the design review's notes (``review``) and
the schema agents' warnings (``warning``). The deliverables list them as their own
groups, ``review_note`` and ``warning``.

A design saved before the field has no topic and reads as ``other``. Its pipeline
notes carry the marker the schema agents wrote at the start of the text (``[PE
note]``, ``[WARNING]``), and only such a design, one whose contract version predates
the field (``TOPICS_SINCE``, by contract module), is read by that marker. A design saved since always
carries its topic: ``write_schema_output`` stamps the default on each trade-off.

The facts layer groups the report's trade-offs (``report_facts.trade_off_topic_facts``)
with the labels below; the engineering report and the results page list each group
under its label and never read a topic out of the text (ADR-032 Decision 6).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, get_args

from src.contracts.schema_design_output import (
    TradeOffGroupTopic,
    TradeOffNoteKind,
    TradeOffTopic,
)

# The designer's topics, in the order the deliverables list them (``other`` last).
TRADE_OFF_TOPICS: tuple[str, ...] = get_args(TradeOffTopic)
# Every group the deliverables list: the topics, then the pipeline's warnings and the
# design review notes.
TRADE_OFF_GROUPS: tuple[str, ...] = get_args(TradeOffGroupTopic)
NOTE_KINDS: tuple[str, ...] = get_args(TradeOffNoteKind)

DEFAULT_TOPIC: Final = "other"
REVIEW_NOTE_TOPIC: Final = "review_note"
WARNING_TOPIC: Final = "warning"
REVIEW_NOTE: Final[Literal["review"]] = "review"
WARNING_NOTE: Final[Literal["warning"]] = "warning"

# The group a pipeline note is listed in.
NOTE_GROUPS: dict[str, str] = {REVIEW_NOTE: REVIEW_NOTE_TOPIC, WARNING_NOTE: WARNING_TOPIC}

TOPIC_LABELS: dict[str, str] = {
    "item_size": "Item and value size",
    "id_generation": "Generated IDs",
    "indexing": "Indexes",
    "key_design": "Keys and data layout",
    "query_translation": "Query translation",
    "consistency": "Consistency and freshness",
    "uniqueness": "Uniqueness and conditional writes",
    "data_types": "Data types",
    "capacity": "Capacity and hot keys",
    "cost": "Cost",
    "operational": "Operations",
    "other": "Other",
    "warning": "Design warnings",
    "review_note": "Design review notes",
}

# The first contract version of each design output and of the synthesis report, by
# contract module (``src/contracts/<module>.py``), that carries ``topic`` and ``note_kind``: its Version History
# entry for #1221 (``tests/unit/report/test_trade_off_topics.py`` checks that they
# agree). A saved file below its version is read by the pipeline's text markers.
TOPICS_SINCE: dict[str, str] = {
    "aurora_mysql_model_output": "1.1",
    "aurora_postgresql_model_output": "1.1",
    "documentdb_model_output": "1.2",
    "dynamodb_model_output": "1.6",
    "elasticache_model_output": "1.2",
    "opensearch_model_output": "1.2",
    "synthesis_output": "1.74",
}

# The markers the schema agents put at the start of the text of a note they appended
# before the field (``base_schema_agent._append_trade_offs``, ``opensearch_schema_agent``).
_LEGACY_MARKERS: tuple[tuple[str, str], ...] = (
    ("[PE note]", REVIEW_NOTE),
    ("[WARNING]", WARNING_NOTE),
)


def _version(v: object) -> tuple[int, int] | None:
    if not isinstance(v, str):
        return None
    major, _, minor = v.partition(".")
    return (int(major), int(minor)) if major.isdigit() and minor.isdigit() else None


def predates_topics(module: str, contract_version: object) -> bool:
    """Whether a file of contract ``module`` (``dynamodb_model_output``,
    ``synthesis_output``) at ``contract_version`` was written before trade-offs
    carried a topic.

    A file with no version, or an unparseable one, is from before. A module with no
    entry in ``TOPICS_SINCE`` (an engine with no design contract) never is.
    """
    since = _version(TOPICS_SINCE.get(module))
    if since is None:
        return False
    written = _version(contract_version)
    return written is None or written < since


def trade_off_topic(t: Mapping[str, Any]) -> str:
    """The designer's topic of ``t``: its own when it is one of the set, else ``other``."""
    topic = t.get("topic")
    return topic if isinstance(topic, str) and topic in TRADE_OFF_TOPICS else DEFAULT_TOPIC


def trade_off_note_kind(t: Mapping[str, Any], legacy: bool) -> str | None:
    """What the pipeline appended ``t`` as (``review``, ``warning``), or None.

    Its own ``note_kind`` when set. Only for a file saved before the field
    (``legacy``, see ``predates_topics``) is a note read by the marker its text
    starts with; a newer one never is, whatever its text says.
    """
    kind = t.get("note_kind")
    if isinstance(kind, str) and kind in NOTE_KINDS:
        return kind
    if legacy and kind is None:
        text = str(t.get("description") or "")
        return next((k for marker, k in _LEGACY_MARKERS if text.startswith(marker)), None)
    return None


def trade_off_group(t: Mapping[str, Any], legacy: bool = False) -> str:
    """The group the deliverables list ``t`` in: its pipeline note's, else its topic."""
    kind = trade_off_note_kind(t, legacy)
    return NOTE_GROUPS[kind] if kind else trade_off_topic(t)


def with_default_topics(output: dict[str, Any]) -> dict[str, Any]:
    """``output`` with every dict trade-off carrying its topic (``other`` when the
    design left it out or wrote null), as ``write_schema_output`` saves it, so a
    design saved since #1221 is never read by its text."""
    trade_offs = output.get("trade_offs")
    if not isinstance(trade_offs, list):
        return output
    return {
        **output,
        "trade_offs": [
            ({**t, "topic": DEFAULT_TOPIC} if isinstance(t, dict) and t.get("topic") is None else t)
            for t in trade_offs
        ],
    }


def designer_note_kind_errors(output: Mapping[str, Any]) -> list[str]:
    """One error per trade-off a designer marked as a pipeline note (``note_kind``):
    only the pipeline appends those."""
    return [
        f"trade_offs[{i}].note_kind: set by the pipeline for its own notes; a design "
        "trade-off leaves it out"
        for i, t in enumerate(output.get("trade_offs") or [])
        if isinstance(t, dict) and t.get("note_kind") is not None
    ]


def without_designer_note_kinds[T](output: T) -> T:
    """``output`` (a designer's contract object) with any ``note_kind`` a model sent
    cleared, before the pipeline appends its own notes (#1221): the Bedrock path does
    what ``finalize_schema_design`` does by refusing it, so a design decision is never
    listed as a review note or a warning. Returns ``output`` itself."""
    for t in getattr(output, "trade_offs", None) or []:
        if getattr(t, "note_kind", None) is not None:
            t.note_kind = None
    return output
