# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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

from src.shared.engine_names import (
    ENGINE_DISPLAY_NAMES,
    canonical_engine_id,
    display_engine,
    display_engine_ids,
)
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


def trade_off_placeholder(engine: str) -> str:
    """The description a rejected trade-off naming no table falls back to (#2324)."""
    return f"{display_engine(engine)} design trade-off."


def trade_off_review_line(engine: str) -> str:
    """The impact a rejected trade-off falls back to."""
    return (
        f"Review this trade-off with the {display_engine(engine)} schema design before migration."
    )


_ENGINE_NAMES = "|".join(
    re.escape(n) for n in sorted(set(ENGINE_DISPLAY_NAMES.values()), key=len, reverse=True)
)
_PLACEHOLDER_RE = re.compile(rf"^(?:{_ENGINE_NAMES}) design trade-off\.?$", re.IGNORECASE)
_REVIEW_LINE_RE = re.compile(
    rf"^Review this trade-off with the (?:{_ENGINE_NAMES}) schema design before migration\.?$",
    re.IGNORECASE,
)


def is_placeholder_trade_off(trade_off: Any) -> bool:
    """Whether a trade-off states nothing of its own (#2324): its description is the
    fallback that names no table ("Aurora MySQL design trade-off.") and its impact is
    empty, the same text or the fallback review line. The prose seam's fallback writes
    one; a model can too. It is never a decision, so synthesis drops it and a stored
    report reads without it. A trade-off that names a table, or whose impact is the
    model's own text, is kept."""
    if isinstance(trade_off, str):
        return bool(_PLACEHOLDER_RE.match(" ".join(trade_off.split())))
    if not isinstance(trade_off, Mapping):
        return False
    description = " ".join(str(trade_off.get("description") or "").split())
    if not _PLACEHOLDER_RE.match(description):
        return False
    impact = " ".join(str(trade_off.get("impact") or "").split())
    return not impact or impact == description or bool(_REVIEW_LINE_RE.match(impact))


def trade_off_risk_text(engine: str, count: int, wave: int | None) -> tuple[str, str]:
    """The ``design.trade_offs`` risk's description (without its ``[engine]`` prefix) and
    mitigation for an engine whose design records ``count`` trade-offs (#2324).

    Synthesis writes the risk with it (``migration_risks._trade_off_risks``) and the
    saved-report reader rewords a stored one with it after a placeholder leaves the list,
    so both say the same count."""
    name = display_engine(engine)
    noun = "trade-off" if count == 1 else "trade-offs"
    these = "this trade-off" if count == 1 else "these trade-offs"
    start = f"Wave {wave} starts" if wave else "cutover"
    return (
        f"The {name} schema design records {count} {noun} to review, listed under "
        "Migration trade-offs in the engineering report.",
        f"Review {these} with the team that builds {name} before {start}, and test each "
        "one the application depends on.",
    )


_ENGINE_PREFIX_RE = re.compile(r"^\[(?P<engine>[^\]]+)\] ")


def trade_offs_by_engine_title(
    trade_offs: list[Any] | None,
) -> dict[str, dict[str, Mapping[str, Any]]]:
    """Each engine's design trade-offs by title (``design_text_item`` of the
    description), the first of each title kept: what a ``design.trade_offs`` risk counts
    and lists. The engine is the catalog id (``canonical_engine_id``: a model can tag a
    trade-off "Aurora MySQL"); the reality check's lines and an empty title are left out.
    Synthesis builds the risk from it, and the saved-report reader counts it again after
    a placeholder leaves (#2324)."""
    by_engine: dict[str, dict[str, Mapping[str, Any]]] = {}
    for t in trade_offs or []:
        if not isinstance(t, Mapping):
            continue
        engine = canonical_engine_id(str(t.get("engine") or ""))
        title = design_text_item(str(t.get("description") or ""))
        if not engine or engine == "reality-check" or not title:
            continue
        by_engine.setdefault(engine, {}).setdefault(title, t)
    return by_engine


def _trade_off_risk_after(
    risk: dict[str, Any], titles: dict[str, dict[str, Mapping[str, Any]]], engines: set[str]
) -> dict[str, Any] | None:
    """A stored ``design.trade_offs`` risk for one of ``engines`` (those that lost a
    placeholder) listing the titles its engine keeps (``titles``, from
    :func:`trade_offs_by_engine_title`) with its count worded again
    (:func:`trade_off_risk_text`); None when none is left, as synthesis then raises no
    such risk. A risk for another engine comes back unchanged."""
    match = _ENGINE_PREFIX_RE.match(str(risk.get("description") or ""))
    engine = canonical_engine_id(match.group("engine")) if match else ""
    if engine not in engines:
        return risk
    kept = sorted(titles.get(engine, {}))
    if not kept:
        return None
    wave = risk.get("wave")
    description, mitigation = trade_off_risk_text(
        engine, len(kept), wave if isinstance(wave, int) and not isinstance(wave, bool) else None
    )
    return {
        **risk,
        "details": kept,
        "description": f"[{match.group('engine') if match else engine}] {description}",
        "mitigation": mitigation,
    }


def _shift_index(i: Any, dropped: list[int]) -> Any:
    if not isinstance(i, int) or isinstance(i, bool):
        return i
    return i - sum(1 for d in dropped if d < i)


def _facts_without(facts: Any, dropped: list[int]) -> Any:
    """A stored facts block with its trade-off positions and counts as they read once
    the ``dropped`` trade-offs are gone (the reality check's positions, the topic groups
    and the trade-off count)."""
    if not isinstance(facts, dict):
        return facts
    out = dict(facts)
    gone = set(dropped)
    for key in ("reality_check_trade_offs", "reality_check_decisions"):
        rows = facts.get(key)
        if isinstance(rows, list):
            out[key] = [
                (
                    {**r, "index": _shift_index(r.get("index"), dropped)}
                    if isinstance(r, dict) and "index" in r
                    else r
                )
                for r in rows
                if not (isinstance(r, dict) and r.get("index") in gone)
            ]
    groups = facts.get("trade_off_topics")
    if isinstance(groups, list):
        kept_groups = []
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("indexes"), list):
                kept_groups.append(g)
                continue
            indexes = [_shift_index(i, dropped) for i in g["indexes"] if i not in gone]
            if indexes:
                kept_groups.append({**g, "indexes": indexes, "trade_off_count": len(indexes)})
        out["trade_off_topics"] = kept_groups
    count = facts.get("trade_off_count")
    if isinstance(count, int) and not isinstance(count, bool):
        out["trade_off_count"] = max(count - len(dropped), 0)
    tabs = facts.get("results_tab_counts")
    if isinstance(tabs, dict) and isinstance(tabs.get("risks"), int):
        # #2363: the Risks and decisions tab counts the trade-offs too.
        n = max(tabs["risks"] - len(dropped), 0)
        rest = {k: v for k, v in tabs.items() if k != "risks"}
        out["results_tab_counts"] = {**rest, **({"risks": n} if n > 0 else {})}
    return out


def without_placeholder_trade_offs(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with no placeholder trade-off (:func:`is_placeholder_trade_off`, #2324).

    Synthesis never writes one now; a report saved before reads the same way: the
    placeholder leaves ``trade_offs``, each ``schema_designs[engine].trade_offs`` and each
    ``design.trade_offs`` risk's ``details``, and the stored facts' positions into
    ``trade_offs`` follow. The reality check's own lines are never touched. A report
    with none comes back unchanged (the same object); the input is not changed.
    """
    out = report
    designs = report.get("schema_designs")
    if isinstance(designs, dict):
        new_designs = {}
        for engine, summary in designs.items():
            listed = summary.get("trade_offs") if isinstance(summary, dict) else None
            if isinstance(listed, list) and any(map(is_placeholder_trade_off, listed)):
                summary = {
                    **summary,
                    "trade_offs": [t for t in listed if not is_placeholder_trade_off(t)],
                }
            new_designs[engine] = summary
        if new_designs != designs:
            out = {**out, "schema_designs": new_designs}
    trade_offs = report.get("trade_offs")
    if not isinstance(trade_offs, list):
        return out
    # A model can tag a trade-off with the engine's name ("Aurora MySQL"): every
    # deliverable and the results page group by the catalog id, as synthesis tags it.
    named = [_with_engine_id(t) for t in trade_offs]
    renamed = named != trade_offs
    dropped = [
        i
        for i, t in enumerate(named)
        if not (isinstance(t, dict) and t.get("engine") == "reality-check")
        and is_placeholder_trade_off(t)
    ]
    if not dropped and not renamed:
        return out
    kept = [t for i, t in enumerate(named) if i not in dropped]
    out = {**out, "trade_offs": kept}
    engines = {
        canonical_engine_id(str(named[i].get("engine") or "") if isinstance(named[i], dict) else "")
        for i in dropped
    }
    assessment = report.get("risk_assessment")
    gone: list[str] = []
    if dropped and isinstance(assessment, dict) and isinstance(assessment.get("risks"), list):
        titles = trade_offs_by_engine_title(kept)
        risks = []
        for r in assessment["risks"]:
            if isinstance(r, dict) and r.get("rule") == "design.trade_offs":
                after = _trade_off_risk_after(r, titles, engines)
                if after is None:
                    gone.append(str(r.get("risk_id") or ""))
                    continue
                r = after
            risks.append(r)
        out["risk_assessment"] = {**assessment, "risks": risks}
        if gone:
            # #2324 review: a risk left with no trade-off leaves the register, so each
            # wave's gate line and the strategies stop naming it (the helpers the risk
            # backfill uses).
            from src.shared.migration_risk_rules import with_gate_lines_for

            out = with_gate_lines_for(report, out)
    if isinstance(report.get("facts"), dict):
        facts = _facts_without(report["facts"], dropped)
        if renamed and "trade_off_topics" in facts:
            # Grouped again by engine id, with the same function synthesis uses.
            from src.shared.report_facts import trade_off_topic_facts

            facts["trade_off_topics"] = trade_off_topic_facts(
                out, facts.get("reality_check_trade_offs")
            )
        if gone:
            # Every risk figure counted again from the same function synthesis uses.
            from src.shared.report_facts import refresh_risk_facts

            facts = refresh_risk_facts(facts, out)
        out["facts"] = facts
    return out


def _with_engine_id(t: Any) -> Any:
    """A trade-off tagged with its engine's catalog id (``canonical_engine_id``)."""
    if not isinstance(t, dict) or not isinstance(t.get("engine"), str):
        return t
    engine = canonical_engine_id(t["engine"])
    return t if engine == t["engine"] else {**t, "engine": engine}


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
    them by index) and their text. The sentences sibling design entries share are recorded
    once (#2165, ``shared_sentences.grouped_report``). A report with nothing to change
    comes back unchanged (the same object).
    """
    from src.shared.shared_sentences import grouped_report

    # #2324: a placeholder trade-off saved before synthesis dropped it is not shown.
    report = grouped_report(without_placeholder_trade_offs(report))
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
