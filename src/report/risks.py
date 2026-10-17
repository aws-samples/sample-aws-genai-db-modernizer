"""Migration risks as the deliverables show them: open, resolved and severity counts."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.report_facts import open_risks, resolved_risk_list
from src.shared.report_facts import risk_engine_and_body as _risk_engine_and_body
from src.shared.report_facts import risk_has_content as _risk_has_content

__all__ = [
    "NO_OPEN_RISKS_CAVEAT",
    "_RISK_POSTURE_SEVERITIES",
    "_repeats",
    "_risk_engine_and_body",
    "_risk_has_content",
    "_severity_counts_text",
    "filtered_risks",
    "resolved_risk_label",
    "resolved_risks",
]


def filtered_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The one open-risk list every deliverable renders (#201); its counts are
    facts (``report_facts(report)["open_risk_count"]`` and friends, #615)."""
    return open_risks(report)


def _repeats(mitigation: Any, description: Any) -> bool:
    """True when ``mitigation`` is already contained in ``description`` (#222).

    Some risks (e.g. DynamoDB unsupported patterns carrying only ``recommendation``)
    use the same text for both; the engineering report then states it once.
    Compared case-insensitively with whitespace collapsed.
    """
    m = " ".join(str(mitigation or "").split()).casefold()
    d = " ".join(str(description or "").split()).casefold()
    return bool(m) and m in d


def resolved_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Analysis risks the effective assignment resolved (``risk_assessment.resolved_risks``)."""
    return resolved_risk_list(report)


def resolved_risk_label(risk: dict[str, Any]) -> str:
    """``aurora_mysql`` resolved onto ``dynamodb`` -> ``Aurora MySQL -> DynamoDB``;
    resolved onto the same engine it started on -> ``Aurora MySQL (kept)`` (#434).

    A resolved risk whose queries never left its own engine (the risk was
    resolved some other way -- a schema change, a different access pattern)
    used to render as ``Aurora MySQL -> Aurora MySQL``, naming the same engine
    twice as if something moved. The engineering report's "Resolved by the
    assignment" section prints it.
    """
    engine = str(risk.get("engine") or "")
    label = display_engine(engine) if engine else "(general)"
    resolved_on = risk.get("resolved_on")
    if not resolved_on:
        return label
    if str(resolved_on) == engine:
        return f"{label} (kept)"
    return f"{label} → {display_engine(str(resolved_on))}"


# The headline reminder every deliverable's no-open-risk state carries (#434
# review): "0 open risks" must never read as "0 migration risk" when the
# analysis never modeled production traffic, peak load or compliance in the
# first place. The engineering report and the executive report state it; the UI's
# no-open-risk state does not yet (#996).
NO_OPEN_RISKS_CAVEAT = (
    "No open risks is not the same as no migration risk: this analysis reads schema and "
    "query patterns only and does not model traffic bursts, peak load or compliance "
    "requirements."
)


_RISK_POSTURE_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def _severity_counts_text(counts: dict[str, int]) -> str:
    """ "N critical, N high, ..." for the risk posture line, one term per
    non-zero severity, in CRITICAL/HIGH/MEDIUM/LOW order, with any unrated
    risks named last.

    ``counts`` is the facts layer's ``open_risks_by_severity`` (#615),
    counted once by ``count_by_severity`` (``src.shared.risk_severity``,
    #536 S1): every risk exactly once, case-insensitively, an unrecognised
    severity "unrated" rather than folded into LOW (#270 round-1 review) --
    so the terms always sum to the open-risk total.
    """
    terms = [f"{counts[lvl]} {lvl.lower()}" for lvl in _RISK_POSTURE_SEVERITIES if counts.get(lvl)]
    unrated = counts.get("UNRATED", 0)
    if unrated:
        terms.append(f"{unrated} unrated")
    return ", ".join(terms)
