# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cutover advice and readiness claims in model-written design text: the one rule (#2107, #2197).

The modernizer gives no cutover advice: where the cutover comes up, the text says only
``CUTOVER_MESSAGE``. Model-written design rationale and review text (a designer's
trade-off, an access pattern's description, a PE review) speaks of data, not of the
move, so only these phrases count there: cutover timing, "safe to cut over", the
replication-finished phrases, and "in sync", "proven" or "ready" as a claim about the
move (``cutover_claims``).

Decommissioning the source database is the customer's too (#2265): the only text about it,
in any text, is ``DECOMMISSION_MESSAGE``. Any other "decommission", or retiring, shutting
down or turning off the source, counts, whatever its timing.

Two places use it: the shared schema-design prose pass
(``synthesis_prose.clean_schema_design_prose``), which sends a field with a claim back to
its deterministic sentence, and the deliverables guard
(``tests/unit/docs/test_no_cutover_advice.py``, ``tests/e2e/test_no_cutover_advice.py``),
which adds the stricter words it checks in the modernizer's own text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.shared.cutover_message import DECOMMISSION_MESSAGE

# What the shared prose pass does with a field that breaks the cutover rule: every
# designer skill says it after the rule, word for word
# (``tests/unit/agents/referee/test_cutover_advice_in_model_prose.py``).
CUTOVER_FALLBACK_RULE = (
    "Synthesis swaps a field that says when to cut over, that advises on decommissioning, "
    "or that uses readiness wording, for a short deterministic sentence."
)

#: Each phrase model design text never uses, by label.
MODEL_TEXT_BANNED: dict[str, str] = {
    "caught up": r"\b(?:caught|catch(?:es|ing)?)[ _-]?up\b",
    "in sync": r"\bin\s+sync\b",  # RDS's own apply status `in-sync` is a status value, not a claim
    "proven": r"\bproven\b",
    "ready": r"\bready\b",
    "safe to cut over": r"\bsafe\s+to\s+cut\s*over\b",
    "applied the last change": r"\bapplied\s+the\s+last\s+change\b",
    "lag is zero": r"\blag\s+(?:is|reaches|hits|drops\s+to|gets\s+to)\s+zero\b",
    "cutover timing": r"\b(?:at|after|before|until|once|during|prior\s+to|ahead\s+of)\s+"
    r"(?:the\s+|your\s+|their\s+)?cut\s*over\b(?!-)"
    r"|\b(?:after|before|once|until|when|as\s+soon\s+as)\s+(?:you|they|the\s+customer)\s+"
    r"cut\s*over\b",
    # #2265: every word about decommissioning the source but DECOMMISSION_MESSAGE itself.
    # (The constant's own name, DECOMMISSION_MESSAGE, is code, not text.)
    "decommission": r"\bdecommission(?!_message\b)\w*"
    r"|\b(?:retir(?:e|es|ed|ing)|shut(?:s|ting)?\s+down|turn(?:s|ed|ing)?\s+off|"
    r"switch(?:es|ed|ing)?\s+off)\s+(?:the\s+|your\s+|their\s+)?"
    r"(?:legacy\s+|old\s+|original\s+)?(?:\w+\s+)?source\b",
}
MODEL_TEXT_LABELS: tuple[str, ...] = tuple(MODEL_TEXT_BANNED)

# Timing words model design text writes next to "in sync" about the plan's own waves or
# CDC, not the cutover: a later wave or its gate ("dual-read by OpenSearch queries until
# wave 4", "via CDC until then", "before this gate", "Before the Wave 3 gate", "before wave
# 3 starts", "remain on Aurora MySQL once later waves have moved their share", "once the
# cache wave has absorbed the read pressure") and replication's own delay ("sees a write
# only after CDC applies it").
CDC_TIMING = re.compile(
    "|".join(
        (
            r"\b(?:until|before|after)\s+wave\s+\d+(?:\s+starts)?\b",
            r"\bbefore\s+(?:this|the\s+Wave\s+\d+)\s+gate\b",
            r"\bonce\s+(?:every\s+)?later\s+waves?\b",
            r"\bonce\s+the\s+[\w-]+\s+wave\b",
            r"\bvia\s+CDC\s+until\s+then\b",
            r"\bonly\s+after\s+CDC\s+applies\s+it\b",
        )
    ),
    re.I,
)
TIMING_WORD = re.compile(r"\b(?:until|once|when|before|after|wait|switch|cut\s*over)\b", re.I)
# Wave 1's headline counts the whole workload it moves in one cutover ("all 107 queries
# at cutover"): what the wave moves, not when to cut over.
WAVE_ONE_COUNT = re.compile(r"\b(?:query|queries) at cutover$", re.I)
# "proven" and "ready" count only as a claim about the move, the target or the switch.
READINESS_CLAIM = re.compile(
    "|".join(
        (
            # "proven to work", "ready to switch", "ready for your applications"
            r"\b(?:proven|ready)\s+(?:to|for)\s+(?:(?:the|your|a|its|their)\s+)?(?:\w+\s+)?"
            r"(?:work|works|cut\s*over|cutover|switch|go\s+live|production|migrat\w*|"
            r"decommission\w*|retire\w*|traffic|applications?|users|use)\b",
            # "the move is proven.", "Aurora is ready", "is now ready"
            r"\b(?:is|are|was|were|has\s+been|have\s+been|now)\s+(?:now\s+)?(?:fully\s+)?"
            r"(?:proven|ready)\s*(?:[.,;:!?)]|$)",
            r"\b(?:proven|ready)\s+(?:in\s+production|end[- ]to[- ]end)\b",
        )
    ),
    re.I,
)


@dataclass(frozen=True)
class CutoverClaim:
    """One claim in a text: its label, and its span in the text."""

    label: str
    start: int
    end: int
    text: str


def _sentence(line: str, end: int) -> str:
    starts = [m.end() for m in re.finditer(r"[.!?]\s+", line[:end])]
    first = starts[-1] if starts else 0
    stop = re.search(r"[.!?](?:\s|$)", line[end:])
    return line[first : end + stop.start() + 1 if stop else len(line)]


def model_text_flags(label: str, line: str, start: int, end: int) -> bool:
    """Whether a match of ``label`` at ``start:end`` in ``line`` is a cutover or
    readiness claim, in model-written design text."""
    if label in ("proven", "ready"):
        return any(m.start() <= start and end <= m.end() for m in READINESS_CLAIM.finditer(line))
    if label == "in sync":
        return bool(TIMING_WORD.search(CDC_TIMING.sub("", _sentence(line, end))))
    if label == "cutover timing":
        return not WAVE_ONE_COUNT.search(line[:end])
    if label == "decommission":
        return not any(
            m.start() <= start and end <= m.end()
            for m in re.finditer(re.escape(DECOMMISSION_MESSAGE), line)
        )
    return True


def cutover_claims(text: str) -> list[CutoverClaim]:
    """Each cutover or readiness claim in model-written ``text``, in label order."""
    found = []
    for label, pattern in MODEL_TEXT_BANNED.items():
        for m in re.finditer(pattern, text, re.IGNORECASE):
            first = text.rfind("\n", 0, m.start()) + 1
            last = text.find("\n", m.end())
            line = text[first : None if last < 0 else last]
            if model_text_flags(label, line, m.start() - first, m.end() - first):
                found.append(CutoverClaim(label, m.start(), m.end(), m.group(0)))
    return found
