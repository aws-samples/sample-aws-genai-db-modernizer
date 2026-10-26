"""Design texts as a risk's ``details`` list them (#950, #1410).

Synthesis (``src.agents.referee.migration_risks``) writes each design trade-off's
title and each unsupported cache pattern into a risk's ``details`` with these
functions; the engineering report uses the same ones to tell which details it
already lists in their own section, by their text, so the risk register does not
print them a second time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.shared.engine_names import display_engine_ids
from src.shared.unsupported_pattern import unsupported_pattern_label

# A leading review marker a designer puts on a line: "[PE note] ...", "PE note: ...".
_MARKER_RE = re.compile(r"^(?:\[[^\]]{1,30}\]\s*|(?:PE|review) note:\s*)+", re.IGNORECASE)


def design_text_item(text: str) -> str:
    """One detail line in full: one line, its review marker and final full stop gone."""
    return _MARKER_RE.sub("", " ".join(str(text or "").split())).rstrip(" .;")


def unsupported_pattern_detail(up: Mapping[str, Any]) -> str:
    """One unsupported pattern in full: its label, reason and workaround."""
    label = unsupported_pattern_label(dict(up))
    reason = design_text_item(str(up.get("reason") or "")) or "not served by the cache design"
    fix = design_text_item(str(up.get("workaround") or up.get("recommendation") or ""))
    return f"{label}: {reason}" + (f". Workaround: {fix}" if fix else "")


_CARRIED_WRITE_RE = re.compile(r"^(?P<lead>.+? \(score [^)]*\)): (?P<approach>.+)$")


def carried_write_detail(
    statement: str, table: str, intent: str, score: str | float, approach: str
) -> str:
    """One carried write in full (#1362): ``DELETE of t, retention delete (score 20):
    <approach>``. ``score`` is the written figure (an int-valued float reads ``20``).
    The engineering report (#1454) reads it back with ``split_carried_write_detail``
    to list the statements that share an approach under it once."""
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    return (
        f"{(statement or 'Statement').upper()} of {str(table).rsplit('.', 1)[-1]}, "
        f"{str(intent).lower()} (score {score}): {design_text_item(approach)}"
    )


def split_carried_write_detail(text: str) -> tuple[str, str] | None:
    """``(statement lead, approach)`` for a detail ``carried_write_detail`` wrote, or
    ``None`` for any other detail text."""
    m = _CARRIED_WRITE_RE.match(text)
    return (m.group("lead"), m.group("approach")) if m else None


# A close-call risk's description (#1220): "<wave and counts>: <engine pairs>. A small
# change in the scores moves them." The engine pairs are the rows of the engineering
# report's Close calls table, so the register names that table instead of listing them.
_CLOSE_CALL_TAIL = "A small change in the scores moves "
_CLOSE_CALL_RE = re.compile(
    rf"^(?P<lead>.+?): (?P<pairs>[^:]+? (?:keeps|takes) [^:]+?)\. "
    rf"(?P<tail>{_CLOSE_CALL_TAIL}(?:it|them)\.)$"
)


def close_call_description(lead: str, pairs: list[str], moved: str) -> str:
    """A close-call risk's description: its counts (``lead``), each engine pair
    (``"Aurora MySQL keeps 14 close calls over DynamoDB"``) and ``moved`` ("it" or
    "them"). The engineering report reads it back with ``split_close_call_description``."""
    return f"{lead}: {'; '.join(pairs)}. {_CLOSE_CALL_TAIL}{moved}."


def split_close_call_description(text: str) -> tuple[str, str, str] | None:
    """``(lead, engine pairs, last sentence)`` for a description
    ``close_call_description`` wrote, or ``None`` for any other text."""
    m = _CLOSE_CALL_RE.match(text)
    return (m.group("lead"), m.group("pairs"), m.group("tail")) if m else None


def severe_risk_strategy(risk_id: str, label: str, scope: str, mitigation: str) -> str:
    """A CRITICAL or HIGH risk's line in ``mitigation_strategies``: its id, severity and
    engine (``label``), scope and own mitigation (one sentence). The engineering report
    reads it back with ``split_severe_risk_strategy``: its Risk register prints that
    mitigation already, so the strategy refers to it (#1220)."""
    return f"{risk_id} ({label}; {scope}): {mitigation}"


_SEVERE_STRATEGY_RE = re.compile(r"^(?P<head>(?P<rid>RISK-\d+) \([^:]*\)): (?P<body>.+)$")


def split_severe_risk_strategy(text: str) -> tuple[str, str, str] | None:
    """``(risk id, head, mitigation)`` for a line ``severe_risk_strategy`` wrote, or
    ``None`` for any other strategy."""
    m = _SEVERE_STRATEGY_RE.match(text)
    return (m.group("rid"), m.group("head"), m.group("body")) if m else None


# A sentence this long is stated once across the design trade-offs synthesis collects
# (#1574); the length the engineering report's said-once ledger and the report guard
# (``tests/report_concision.py``) use.
ONCE_MIN_WORDS = 15
# What a trade-off field says when every sentence in it was stated by an earlier one.
ALREADY_STATED = "As stated in an earlier trade-off."
_SENTENCE_END = re.compile(r"((?<=[.!?])[:;]?\s+)")


def _sentence_key(sentence: str) -> str:
    return " ".join(sentence.replace("**", "").split()).strip(" -*:,.;")


def said_once_trade_offs(trade_offs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The trade-offs with each long sentence stated once (#1574).

    A split design runs as independent parts, and two of them can word the same rule
    the same way ("A GSI is eventually consistent: ..."). The first trade-off to state
    a sentence of at least ``ONCE_MIN_WORDS`` words keeps it; a later trade-off keeps
    its own text and drops that sentence, in its ``description`` and its ``impact``.
    A field left with nothing of its own reads ``ALREADY_STATED``. Sentences are cut
    where the report guard cuts them. The input is not changed.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for t in trade_offs:
        if not isinstance(t, dict):
            out.append(t)
            continue
        entry = dict(t)
        # A legacy string trade-off carries its text as both fields: state it once.
        same = entry.get("impact") == entry.get("description")
        for field in ("description",) if same else ("description", "impact"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            parts = _SENTENCE_END.split(value.strip())
            kept: list[str] = []
            dropped = False
            for i in range(0, len(parts), 2):
                sentence = parts[i]
                key = _sentence_key(sentence)
                if len(key.split()) >= ONCE_MIN_WORDS:
                    if key in seen:
                        dropped = True
                        continue
                    seen.add(key)
                kept.append(sentence.strip())
            if dropped:
                entry[field] = " ".join(s for s in kept if s) or ALREADY_STATED
        if same:
            entry["impact"] = entry.get("description")
        out.append(entry)
    return out


# The model-written prose fields of a report's per-engine design summary
# (``schema_designs[engine]``) that the synthesis prose seam cleans.
_DESIGN_PROSE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("trade_offs", ("description", "impact")),
    ("unsupported_patterns", ("reason", "description", "recommendation", "workaround")),
    ("migration_notes", ("application_logic_required",)),
)


def _with_display_ids(entry: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(entry, dict):
        return entry
    out = dict(entry)
    for field in fields:
        if isinstance(out.get(field), str):
            out[field] = display_engine_ids(out[field])
    return out


def design_prose_report(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with its design prose as synthesis now writes it (#1574): every raw
    engine id written as the engine's name (``display_engine_ids``), in the trade-offs and
    the per-engine design summaries, and the trade-offs stating each long sentence once
    (:func:`said_once_trade_offs`), each ``design.trade_offs`` risk's ``details`` listing
    the same texts.

    Synthesis writes a report that way; the deliverables and the results payload read a
    report through this too, so one synthesized before it shows the same text. The reality
    check's own trade-offs keep their place (``facts.reality_check_trade_offs`` points at
    them by index) and their text. A report with nothing to change comes back unchanged
    (the same object).
    """
    out = report
    designs = report.get("schema_designs")
    if isinstance(designs, dict):
        new_designs = {}
        for engine, summary in designs.items():
            if not isinstance(summary, dict):
                new_designs[engine] = summary
                continue
            new_summary = dict(summary)
            for key, fields in _DESIGN_PROSE_FIELDS:
                if isinstance(summary.get(key), list):
                    new_summary[key] = [_with_display_ids(e, fields) for e in summary[key]]
            new_designs[engine] = new_summary
        if new_designs != designs:
            out = {**out, "schema_designs": new_designs}
    trade_offs = report.get("trade_offs")
    if not isinstance(trade_offs, list):
        return out
    design = [
        i
        for i, t in enumerate(trade_offs)
        if isinstance(t, dict) and t.get("engine") != "reality-check"
    ]
    named = [_with_display_ids(trade_offs[i], ("description", "impact")) for i in design]
    once = said_once_trade_offs(named)
    changed = {i: t for i, t in zip(design, once, strict=True) if t != trade_offs[i]}
    if not changed:
        return out
    renamed = {
        design_text_item(str(trade_offs[i].get("description") or "")): design_text_item(
            str(t.get("description") or "")
        )
        for i, t in changed.items()
    }
    out = {**out, "trade_offs": [changed.get(i, t) for i, t in enumerate(trade_offs)]}
    assessment = report.get("risk_assessment")
    if isinstance(assessment, dict) and isinstance(assessment.get("risks"), list):
        risks = []
        for r in assessment["risks"]:
            if isinstance(r, dict) and r.get("rule") == "design.trade_offs" and r.get("details"):
                r = {**r, "details": sorted({renamed.get(d, d) for d in r["details"]})}
            risks.append(r)
        out["risk_assessment"] = {**assessment, "risks": risks}
    return out
