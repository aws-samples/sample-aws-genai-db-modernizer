# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A gate's conditions: its sentences, each with the table lists it names (#2287).

A wave's gate (``gate_parts``, #1028) and a stage's gate (``stage_gate_parts``) are text
runs and item lists that join back to one string. Every deliverable states the gate as
that string; the results page shows it as a checklist, one condition per sentence. The
cut is made here, once, from the parts the producer wrote, so no renderer reads the
gate's prose (ADR-032).

Each condition is ``{"kind": "check" | "note", "parts": [{"text", "items"}]}``. A note is
a sentence that states whose a step is (``CUTOVER_MESSAGE``, ``DECOMMISSION_MESSAGE``), or
any other sentence about the cutover or the decommissioning (``cutover_claims``, the
wording an older release saved), so the checklist never shows it as something to check. The conditions joined with one
space are exactly the gate; a gate whose sentences cannot be cut that way is one
condition holding every part.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from src.contracts.synthesis_output import join_gate_parts
from src.shared.cutover_message import CUTOVER_MESSAGE, DECOMMISSION_MESSAGE
from src.shared.cutover_wording import cutover_claims

__all__ = ["NOTE_SENTENCES", "conditions_for", "gate_conditions", "join_gate_conditions"]

#: The sentences a gate states that are not conditions to check.
NOTE_SENTENCES = frozenset({CUTOVER_MESSAGE, DECOMMISSION_MESSAGE})

# A sentence ends at a full stop followed by white space and more text. A decimal
# ("82.1%") or a version ("16.10") has no space after its dot.
_SENTENCE_END = re.compile(r"(?<=\.)\s+(?=\S)")


def _clean(parts: Sequence[Any]) -> list[dict[str, Any]]:
    """The parts as dicts, each text-only run merged into the run after it, so a
    sentence end never falls between two runs."""
    out: list[dict[str, Any]] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        text = str(p.get("text") or "")
        items = [str(i) for i in p.get("items") or []]
        if out and not out[-1]["items"]:
            out[-1] = {"text": out[-1]["text"] + text, "items": items}
        else:
            out.append({"text": text, "items": items})
    return out


def _kind(parts: list[dict[str, Any]]) -> str:
    """``note`` for a sentence that states whose a step is, and for any sentence that
    speaks of the cutover or the decommissioning in other words (``cutover_claims``: a
    gate a release before #2107 or #2265 saved, such as "decommission the legacy source
    once replication lag is zero"), so no such sentence is ever shown as a step to check.
    Its words are kept, so the conditions still join back to the stored gate that every
    deliverable prints."""
    text = "".join(p["text"] for p in parts).strip()
    if text in NOTE_SENTENCES or cutover_claims(text):
        return "note"
    return "check"


def join_gate_conditions(conditions: Sequence[Any]) -> str:
    """The gate string ``conditions`` stand for: each condition's parts joined as a gate's
    parts are (``join_gate_parts``), the conditions with one space."""
    return " ".join(
        join_gate_parts(c.get("parts") or []) for c in conditions if isinstance(c, dict)
    )


def gate_conditions(parts: Sequence[Any] | None, gate: str | None = None) -> list[dict[str, Any]]:
    """The gate's conditions, one per sentence, from its parts (or from ``gate`` alone,
    one text run, when it has none). Empty for a gate with no text."""
    runs = _clean(parts or [])
    if not runs and gate:
        runs = [{"text": str(gate), "items": []}]
    whole = join_gate_parts(runs)
    if not whole.strip():
        return []
    sentences: list[list[dict[str, Any]]] = [[]]
    for run in runs:
        pieces = _SENTENCE_END.split(run["text"])
        for i, piece in enumerate(pieces):
            if i:
                sentences.append([])
            items = run["items"] if i == len(pieces) - 1 else []
            if piece or items:
                sentences[-1].append({"text": piece, "items": items})
    out = [{"kind": _kind(s), "parts": s} for s in sentences if s]
    if join_gate_conditions(out) != whole:
        # White space the cut cannot keep (two spaces, a line break): one condition.
        return [{"kind": "check", "parts": runs}]
    return out


def conditions_for(
    stored: Any, parts: Sequence[Any] | None, gate: str | None
) -> list[dict[str, Any]]:
    """The conditions a reader states for a gate: ``stored`` when it is a list that joins
    back to the gate (``parts``, or ``gate`` alone), else the gate cut again. A stored
    sentence about the cutover or the decommissioning is a note whatever its stored kind,
    so the rule above holds for a block written by any release."""
    whole = join_gate_parts(_clean(parts or [])) or str(gate or "")
    if (
        isinstance(stored, list)
        and all(isinstance(c, dict) for c in stored)
        and join_gate_conditions(stored) == whole
    ):
        return [
            {**c, "kind": "note"} if _kind(_clean(c.get("parts") or [])) == "note" else c
            for c in stored
        ]
    return gate_conditions(parts, gate)
