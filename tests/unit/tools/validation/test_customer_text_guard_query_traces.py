"""The customer-text guard on report.json's ``query_traces`` (#339).

A trace mixes machine fields (engine ids, rule codes, pass and actor codes,
the comparison basis) with the reason renderer's sentences
(``src/shared/move_reasons.py``). The machine fields are exempt by name or by
schema path; every sentence and display name is still scanned. The test below
keeps that split complete: a string field added to a trace model later must
be listed as one or the other.
"""

from __future__ import annotations

import enum
import json
import types
import typing
from pathlib import Path

from pydantic import BaseModel

from src.contracts.query_trace import (
    CloseCall,
    QueryTraceEntry,
    QueryTraceListing,
    QueryTraceResponse,
    TraceCandidate,
    TraceEngineScore,
    TraceOutscored,
    TraceRuleCount,
    TraceRunnerUp,
    TraceStep,
    UnservableGate,
    UnservableQuery,
)
from src.shared.query_trace import query_trace_digest
from src.tools.validation.customer_text_guard import ID_FIELD_PATHS, ID_FIELDS, scan_json

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "wordpress_move_trace.json"

# Each trace model and the JSON keys the guard's walk reaches it through.
TRACE_MODELS: dict[type[BaseModel], tuple[str, ...]] = {
    QueryTraceResponse: ("queries",),
    QueryTraceEntry: ("queries",),
    QueryTraceListing: ("more_listed", "listed_queries"),
    TraceStep: ("first_assignment", "moves"),
    TraceCandidate: ("candidates",),
    TraceEngineScore: ("runner_up",),
    TraceRunnerUp: ("runner_up",),
    TraceOutscored: ("outscored_by",),
    TraceRuleCount: ("unlisted_rules",),
    CloseCall: ("close_calls",),
    UnservableQuery: ("unservable",),
    UnservableGate: ("blocking_gates",),
}
# Display names and the reason renderer's sentences: scanned like any prose.
SCANNED = frozenset(
    {
        "engine_name",
        "from_engine_name",
        "to_engine_name",
        "final_engine_name",
        "tied_engine_names",
        "gate_text",
        "rule",
        "label",
        "explanation",
        "summary",
        "rule_label",
    }
)


def _digest() -> dict:
    sample = json.loads(FIXTURE.read_text())
    versions = {1: sample["v1"], 2: sample["v2"]}
    return {
        "query_traces": query_trace_digest(sample["v2"], versions.get, 2).model_dump(
            mode="json", by_alias=True
        )
    }


def _is_text(annotation: object) -> bool:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType, list):
        return any(_is_text(a) for a in typing.get_args(annotation))
    if origin is typing.Literal:
        return True
    return annotation is str or (isinstance(annotation, type) and issubclass(annotation, enum.Enum))


def test_a_real_digest_has_no_finding() -> None:
    assert scan_json(_digest()) == []


def test_the_sentences_are_still_scanned() -> None:
    report = _digest()
    trace = report["query_traces"]["queries"][0]
    trace["summary"] = "Ends on aurora_mysql."
    trace["first_assignment"]["explanation"] = "Moved to dynamodb."
    paths = {f.path for f in scan_json(report)}
    assert "$.query_traces.queries[0].summary" in paths
    assert "$.query_traces.queries[0].first_assignment.explanation" in paths


def test_every_string_field_of_a_trace_is_exempt_or_scanned() -> None:
    unlisted = []
    for model, parents in TRACE_MODELS.items():
        for name, field in model.model_fields.items():
            key = field.alias or name
            if not _is_text(field.annotation) or key in SCANNED or key in ID_FIELDS:
                continue
            for parent in parents:
                if (parent, key) not in ID_FIELD_PATHS:
                    unlisted.append(f"{model.__name__}.{key} under {parent}")
    assert unlisted == []
