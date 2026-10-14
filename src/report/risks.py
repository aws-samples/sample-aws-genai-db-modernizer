"""Migration risks as the deliverables show them: open, resolved and severity counts."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.risk_severity import count_by_severity


def _risk_engine_and_body(desc: Any) -> tuple[str, str]:
    """Split a risk description into its ``[engine]`` prefix and the remaining text.

    Risks carry no explicit engine field; the engine is encoded as a leading
    ``[documentdb]`` / ``[elasticache]`` tag in the description. Returns
    ``(engine, body)`` with ``engine`` defaulting to ``"(general)"``.
    """
    s = str(desc or "").strip()
    engine = "(general)"
    if s.startswith("["):
        j = s.find("]")
        if j != -1:
            engine = s[1:j].strip() or "(general)"
            s = s[j + 1 :].strip()
    return engine, s


def _risk_has_content(desc: Any) -> bool:
    """Defensive guard against a risk description with no real text after its
    ``[engine]`` prefix.

    This is no longer the primary line of defense it once was: before #210,
    synthesis could emit ``[engine] unknown:`` with nothing after it for an
    unsupported-pattern risk on an engine whose contract has neither
    ``pattern_type`` nor ``recommendation`` (documentdb/elasticache carry
    ``reason``/``workaround`` instead, which that code didn't read).
    ``build_risk_assessment`` now reads whichever fields a contract actually
    has (``src.shared.unsupported_pattern``), so a genuinely contentless
    risk should no longer occur there in practice. This filter remains as a
    general-purpose guard -- against any other producer emitting a risk with
    no body, and against the historical ``unknown:`` pattern surviving in
    already-generated ``report.json`` files.
    """
    _, body = _risk_engine_and_body(desc)
    if body.lower().startswith("unknown:"):
        body = body[len("unknown:") :].strip()
    return bool(body)


def filtered_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The one risk list every deliverable renders and counts from.

    ``risk_assessment.risks`` can in principle contain contentless entries
    (see ``_risk_has_content``); the decision report and engineering report
    dropped those before counting while the PDF/PPTX deck counted the raw
    list, so a report with any such entries produced disagreeing risk totals
    across deliverables (#201: "9 migration risks identified" in the decision
    report vs. 12 in ``report.json`` and the PDF, because 3 of the 12 were
    the contentless unsupported-pattern risks #210 also fixes at the source).
    Every deliverable must build its risk count and its risk list from this
    one function instead of re-deriving the filter.
    """
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("risks") or [])
        if isinstance(r, dict) and _risk_has_content(r.get("description"))
    ]


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
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("resolved_risks") or [])
        if isinstance(r, dict) and _risk_has_content(r.get("description"))
    ]


def resolved_risk_label(risk: dict[str, Any]) -> str:
    """``aurora_mysql`` resolved onto ``dynamodb`` -> ``Aurora MySQL -> DynamoDB``;
    resolved onto the same engine it started on -> ``Aurora MySQL (kept)`` (#434).

    A resolved risk whose queries never left its own engine (the risk was
    resolved some other way -- a schema change, a different access pattern)
    used to render as ``Aurora MySQL -> Aurora MySQL``, naming the same engine
    twice as if something moved. Shared by the deck
    (``src.report.pptx_report``) and this module's own engineering report
    section so the two agree on the same risk.
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
# first place. Same wording in the deck/PDF (``pptx_report``), this module's
# Decision Report, and the interactive Analysis Report's no-open-risk state
# (``src/ui/src/utils/ExportReport.js`` -- JavaScript cannot import this
# constant, so that copy is kept in sync by hand).
NO_OPEN_RISKS_CAVEAT = (
    "No open risks is not the same as no migration risk: this analysis reads schema and "
    "query patterns only and does not model traffic bursts, peak load or compliance "
    "requirements."
)


_RISK_POSTURE_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def _severity_counts_text(risks: list[dict[str, Any]]) -> str:
    """ "N critical, N high, ..." for the risk posture line, one term per
    non-zero severity, in CRITICAL/HIGH/MEDIUM/LOW order, with any unrated
    risks named last.

    #270: the posture line used to fold CRITICAL into "high" and drop LOW
    entirely, so its parenthetical count did not add up to the open-risk
    total whenever a risk register held either. ``count_by_severity``
    (``src.shared.risk_severity``, #536 S1) counts every risk exactly once,
    case-insensitively, naming an unrecognised severity "unrated" rather than
    folding it into LOW (#270 round-1 review: an unrated risk is not known to
    be low severity) -- the same counter the deck's own ``UNRATED`` handling
    (``pptx_report.py``) and the summary grounding check (``synthesis_
    grounding.check_summary_risk_counts``) use, so the terms always sum to
    ``len(risks)``.
    """
    counts = count_by_severity(risks)
    terms = [f"{counts[lvl]} {lvl.lower()}" for lvl in _RISK_POSTURE_SEVERITIES if counts.get(lvl)]
    unrated = counts.get("UNRATED", 0)
    if unrated:
        terms.append(f"{unrated} unrated")
    return ", ".join(terms)
