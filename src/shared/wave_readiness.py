# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether the plan suggests each migration wave (#1992, decision 4 on #1987).

A wave is suggested only when its design exists, was reviewed, and no deterministic
check holds it. "Not suggested yet" is a valid outcome: the plan says why, and what to
do next, instead of carrying the wave with its risk as a follow-up action. No model
decides it; every hold names the fact it reads.

Each wave gets one ``readiness``:

- ``held``: a check below holds it ("Not suggested yet");
- ``not_designed``: no check holds it, but one of its engines has no schema design, so
  there is nothing to endorse yet ("Not designed yet"): neither suggested nor held;
- ``not_reviewed``: no check holds it, but the last review of its design failed to run
  ("Not reviewed"): neither suggested nor held;
- ``suggested``: none of the above.

The checks, in the order a wave's holds list them:

- ``open_high_risk``: an open critical or high risk belongs to the wave (the risk's own
  ``wave``, else the first wave of the engine it is raised for, :func:`risk_wave`, the
  placement the executive summary's exposures use). A risk with neither holds no wave.
  A risk another engine's analysis raised on queries since moved here
  (``reattributed_from``) holds the wave only when this engine's own analysis or design
  flags one of the same queries; the hold names the engine that raised it. A risk of a
  rule in :data:`NON_HOLDING_RULES` never holds a wave (maintainer decision on #1938):
  the routines the pipeline could not translate are a to-do for the customer, listed in
  the wave's line (``to_dos``) instead.
- ``slower_and_pricier``: the source's monthly cost is known and the plan costs more,
  and the wave carries an open medium performance risk (a high one already holds it):
  the plan never suggests a target it rates slower and pricier than the source.
- ``unserved_patterns``: the wave's engine design leaves access patterns unserved (the
  DynamoDB group merge's unresolved conflicts).
- ``open_review``: the last design review of the wave's engine (of a design, or of any
  of its design groups) did not approve it.
- ``earlier_wave``: wave 1 moves the source to Aurora and is not suggested, and every
  later wave builds on it.

Next steps, one per engine and kind: run the engine's schema design when it has none,
else revise the design and review it again (review it again, for a failed review); wait
until wave 1 is suggested; and, when a rate-driven risk holds the wave while the rate
window check failed, collect again over a longer window.

The figures are computed once here, into ``facts.waves[]`` (ADR-032); every deliverable
prints ``readiness_line`` as it is, except the executive report, which prints
``readiness_line_short`` (#2075): the same line with its review warnings as a count that
points to the engineering report, and a failed redesign round without its reason, so it
names no collected table or column.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.shared.engine_names import display_engine
from src.shared.risk_severity import severity_of

#: The checks that can hold a wave, in the order the holds list them.
HOLD_CHECKS = (
    "open_high_risk",
    "slower_and_pricier",
    "unserved_patterns",
    "unsupported_queries",
    "open_review",
    "earlier_wave",
)
#: The next steps a wave that is not suggested can get.
NEXT_STEPS = ("design", "revise_design", "review_again", "collect_longer", "earlier_wave")
#: Each wave's readiness.
READINESS = ("suggested", "held", "not_designed", "not_reviewed")

#: The risk rules whose risks never hold a wave, whatever their severity (#1938): a
#: to-do for the customer, which the wave's line lists.
NON_HOLDING_RULES = frozenset({"routines.migrate_yourself"})
#: The risk rules whose severity the measured rates drive: only these get "collect over
#: a longer window" when the rate window check fails.
RATE_DRIVEN_RULES = frozenset({"design.hot_partition"})

#: Where the short readiness line sends the reader for the warnings it counts (#2075).
WARNINGS_LISTED_IN = "listed in the engineering report"

#: The rule, stated once next to the waves by every deliverable.
READINESS_RULE_NOTE = (
    "A wave is suggested only when its design exists, was reviewed, and no check holds "
    "it: an open critical or high risk on it, an access pattern its design leaves "
    "unserved, a design review that did not approve its design, a plan that costs more "
    "than the source while the wave carries an open performance risk, or a wave 1 that "
    "is not suggested yet"
)

SUGGESTED_LINE = "Suggested: no check holds it."

COLLECT_LONGER_LINE = (
    "collect again over at least 7 days of usual load, without resetting the statement "
    "statistics first"
)

_PERFORMANCE = "PERFORMANCE_DEGRADATION"
_WAVE_ONE_MOVES = ("homogeneous", "cross_engine", "heterogeneous")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _usd(x: float) -> str:
    return f"${int(x + 0.5):,}"


def _risk_phrase(risk_type: Any) -> str:
    return str(risk_type or "risk").replace("_", " ").lower()


def _warning_phrase(w: Mapping[str, Any]) -> str:
    """A design review warning, briefly: its engine, target and requested change."""
    target = f" {w.get('target')}" if w.get("target") else ""
    change = f": {w.get('change')}" if w.get("change") else ""
    return f"{display_engine(str(w.get('engine') or ''))}{target}{change}".rstrip(".")


_WARNING_FIELDS = ("engine", "category", "target", "change")


def _listed(w: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(w.get(k) or "") for k in _WARNING_FIELDS)


def wave_edit_facts(
    decisions: Iterable[Mapping[str, Any]] | None, log: Mapping[str, Any] | None
) -> dict[int, dict[str, Any]]:
    """Per wave, its latest ``/wave-edit`` decision (#2067) and how many of that decision's
    redesign rounds kept a new design, or are still open with it in place (``kept_rounds``,
    from the redesign log)."""
    out: dict[int, dict[str, Any]] = {}
    for d in decisions or []:
        if not isinstance(d, Mapping):
            continue
        wave = int(d.get("wave") or 0)
        if int(d.get("decision_version") or 0) >= int(
            (out.get(wave) or {}).get("decision", {}).get("decision_version") or 0
        ):
            out[wave] = {"decision": d, "kept_rounds": 0}
    for rnd in (log or {}).get("rounds") or []:
        if not isinstance(rnd, Mapping) or rnd.get("wave_edit") is None:
            continue
        edit = out.get(int(rnd.get("wave_edit") or 0))
        if not edit or rnd.get("decision_version") != edit["decision"].get("decision_version"):
            continue
        outcome = rnd.get("outcome")
        # A round still open when synthesis ran: the design in place is its new one (a
        # round that puts it back asks for another synthesis).
        if outcome is None or (
            isinstance(outcome, Mapping)
            and any(v in ("fixed", "improved") for v in outcome.values())
        ):
            edit["kept_rounds"] += 1
    return out


def _engines_next_step(engines: Iterable[str]) -> str:
    return "; ".join(
        f"revise the {display_engine(e)} schema design in the modernization session and "
        "review it again"
        for e in dict.fromkeys(engines)
    )


def _decided_line(
    warnings: list[dict[str, Any]], edit: Mapping[str, Any] | None
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]] | None:
    """A suggested wave's line once the customer decided on its warnings with /wave-edit
    (#2067): ``(line, short line, accepted, rejected)``, or None to state the warnings as
    to accept (#1992). A decision speaks only for the review it was made on: its warnings,
    each by its position and its own text, are the review's list exactly (nothing was
    redesigned since, or the earlier design came back). After a redesign kept a new
    design, the new review's warnings are a new list for the customer to decide on; they
    are never matched to the old ones."""
    if not edit:
        return None
    d = edit["decision"]
    decided = sorted(
        [*(d.get("accepted") or []), *(d.get("rejected") or [])],
        key=lambda w: int(w.get("n") or 0),
    )
    accepted = [dict(w) for w in d.get("accepted") or []]
    rejected = [dict(w) for w in d.get("rejected") or []]
    if decided and [_listed(w) for w in decided] == [_listed(w) for w in warnings]:
        noun = _plural(len(warnings), "design review warning", "design review warnings")
        parts = []
        if accepted:
            parts.append(
                f"The customer accepted {len(accepted)}: "
                + "; ".join(map(_warning_phrase, accepted))
                + "."
            )
        if rejected:
            parts.append(
                f"The customer did not accept {len(rejected)}, still open: "
                + "; ".join(map(_warning_phrase, rejected))
                + "; the next step is to "
                + _engines_next_step(str(w.get("engine") or "") for w in rejected)
                + "."
            )
        line = f"Suggested, with {noun}. " + " ".join(parts)
        short = (
            f"Suggested, with {noun} ({WARNINGS_LISTED_IN}): the customer accepted "
            f"{len(accepted)}"
            + (f" and did not accept {len(rejected)}, still open" if rejected else "")
            + "."
        )
        return line, short, accepted, rejected
    kept = int(edit.get("kept_rounds") or 0)
    if not kept or not rejected:
        return None
    head = (
        f"Suggested: redesigned {_times(kept)} after the customer did not accept "
        f"{_plural(len(rejected), 'design review warning', 'design review warnings')}; "
    )
    if not warnings:
        line = head + "the new review approved it with no warnings."
        return line, line, [], []
    lists = (
        f"the new review lists "
        f"{_plural(len(warnings), 'design review warning', 'design review warnings')} "
        "to accept"
    )
    then = (
        " If the customer does not accept them: "
        + _engines_next_step(str(w.get("engine") or "") for w in warnings)
        + "."
    )
    line = head + lists + ": " + "; ".join(map(_warning_phrase, warnings)) + "." + then
    short = head + lists + f" ({WARNINGS_LISTED_IN})." + then
    return line, short, [], []


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _names(engines: Sequence[str]) -> str:
    names = [display_engine(e) for e in engines]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def risk_wave(
    risk: Mapping[str, Any], engine: str | None, waves: Sequence[Mapping[str, Any]]
) -> int | None:
    """The wave a risk belongs to: its own ``wave``, else the first wave of ``engine``
    (the engine it is raised for); None with neither. The one placement the readiness
    holds and the executive summary's exposures share."""
    wave = risk.get("wave")
    if isinstance(wave, int) and not isinstance(wave, bool):
        return wave
    if engine:
        for w in waves:
            if engine in (w.get("engines") or []):
                return int(w.get("wave") or 0) or None
    return None


def _risk_hold(check: str, r: Mapping[str, Any], engine: str | None, line: str) -> dict:
    return {
        "check": check,
        "engine": engine,
        "risk_id": r.get("risk_id"),
        "severity": severity_of(dict(r)),
        "risk_type": r.get("risk_type"),
        "flagged_by": r.get("reattributed_from") or None,
        "line": line,
    }


def _holds_for(
    wave: Mapping[str, Any],
    waves: Sequence[Mapping[str, Any]],
    risks: Sequence[Mapping[str, Any]],
    unserved_by_engine: Mapping[str, int],
    open_reviews_by_engine: Mapping[str, int],
    pricier: tuple[float, float] | None,
    unsupported_by_engine: Mapping[str, int],
) -> list[dict[str, Any]]:
    n = int(wave.get("wave") or 0)
    engines = [str(e) for e in wave.get("engines") or [] if e]
    holds: list[dict[str, Any]] = []
    mine = [
        r
        for r in risks
        if risk_wave(r, r.get("engine"), waves) == n and r.get("rule") not in NON_HOLDING_RULES
        # Another engine's flag holds here only when this engine flags the query too.
        and (not r.get("reattributed_from") or r.get("receiving_engine_flags"))
    ]
    for r in mine:
        severity = severity_of(dict(r))
        if severity not in ("CRITICAL", "HIGH"):
            continue
        # A general risk placed on the wave by its number names no engine (#2051 review).
        engine = str(r.get("engine") or "") or None
        by = r.get("reattributed_from")
        flagged = f", flagged by the {display_engine(str(by))} analysis" if by else ""
        on = f" on {display_engine(engine)}" if engine else ""
        holds.append(
            _risk_hold(
                "open_high_risk",
                r,
                engine,
                f"an open {severity.lower()} risk{on}{flagged} "
                f"({r.get('risk_id') or 'unnumbered'}, {_risk_phrase(r.get('risk_type'))})",
            )
        )
    if pricier is not None:
        projected, source = pricier
        for r in mine:
            if r.get("risk_type") != _PERFORMANCE or severity_of(dict(r)) != "MEDIUM":
                continue
            engine = str(r.get("engine") or "") or None
            carrier = display_engine(engine) if engine else "the wave"
            holds.append(
                _risk_hold(
                    "slower_and_pricier",
                    r,
                    engine,
                    f"the plan costs more than the source ({_usd(projected)} against "
                    f"{_usd(source)} a month) and {carrier} carries "
                    f"an open performance risk ({r.get('risk_id') or 'unnumbered'})",
                )
            )
    for engine in engines:
        unserved = int(unserved_by_engine.get(engine) or 0)
        if unserved > 0:
            holds.append(
                {
                    "check": "unserved_patterns",
                    "engine": engine,
                    "risk_id": None,
                    "severity": None,
                    "risk_type": None,
                    "flagged_by": None,
                    "line": (
                        f"the {display_engine(engine)} design leaves "
                        f"{_plural(unserved, 'access pattern', 'access patterns')} unserved"
                    ),
                }
            )
    for engine in engines:
        unsupported = int(unsupported_by_engine.get(engine) or 0)
        if unsupported > 0:
            # A query the engine still serves that its own design says it cannot
            # (ADR-031 Decision 11): the router acts on it, never this rule; until then
            # the wave is not suggested.
            holds.append(
                {
                    "check": "unsupported_queries",
                    "engine": engine,
                    "risk_id": None,
                    "severity": None,
                    "risk_type": None,
                    "flagged_by": None,
                    "line": (
                        f"the {display_engine(engine)} design marks "
                        f"{_plural(unsupported, 'query', 'queries')} it is assigned as "
                        "unsupported"
                    ),
                }
            )
    for engine in engines:
        reviews = int(open_reviews_by_engine.get(engine) or 0)
        if reviews > 0:
            holds.append(
                {
                    "check": "open_review",
                    "engine": engine,
                    "risk_id": None,
                    "severity": None,
                    "risk_type": None,
                    "flagged_by": None,
                    "line": (
                        f"the last {display_engine(engine)} design review did not approve "
                        f"{_plural(reviews, 'design', 'designs')}"
                    ),
                }
            )
    return holds


def _step(step: str, engine: str | None, line: str) -> dict[str, Any]:
    return {"step": step, "engine": engine, "line": line}


def _next_steps(
    holds: list[dict[str, Any]],
    risks_by_id: Mapping[Any, Mapping[str, Any]],
    designed: set[str],
    window_representative: bool | None,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    def add(step: str, engine: str | None, line: str) -> None:
        if (step, engine) not in seen:
            seen.add((step, engine))
            steps.append(_step(step, engine, line))

    for h in holds:
        engine = h.get("engine")
        if h["check"] == "earlier_wave":
            add("earlier_wave", None, "wait until wave 1 is suggested")
            continue
        name = display_engine(str(engine or ""))
        if engine and engine not in designed:
            add("design", engine, f"run the {name} schema design, then check the wave again")
        else:
            add(
                "revise_design",
                engine,
                f"revise the {name} design and review it again until no check holds it",
            )
    if window_representative is False and any(
        (risks_by_id.get(h.get("risk_id")) or {}).get("rule") in RATE_DRIVEN_RULES
        for h in holds
        if h.get("risk_id")
    ):
        add("collect_longer", None, COLLECT_LONGER_LINE)
    return steps


def wave_readiness(
    waves: Sequence[Mapping[str, Any]],
    *,
    risks: Sequence[Mapping[str, Any]],
    designed_engines: Iterable[str],
    unserved_by_engine: Mapping[str, int] | None = None,
    open_reviews_by_engine: Mapping[str, int] | None = None,
    failed_reviews_by_engine: Mapping[str, int] | None = None,
    window_representative: bool | None = None,
    projected_cost: float | None = None,
    source_cost: float | None = None,
    redesign_by_engine: Mapping[str, Mapping[str, Any]] | None = None,
    unsupported_by_engine: Mapping[str, int] | None = None,
    review_warnings_by_engine: Mapping[str, list[Mapping[str, Any]]] | None = None,
    wave_edits: Mapping[int, Mapping[str, Any]] | None = None,
    to_dos_by_wave: Mapping[int, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """One row per wave, in order: ``wave``, ``readiness``, ``suggested`` (True when
    suggested, False when held, None otherwise), ``holds``, ``next_steps`` and the
    ``readiness_line`` every deliverable prints.

    ``risks`` are the open risks, each with its ``risk_id``, ``severity``, ``risk_type``,
    ``rule``, ``engine`` (None for a general risk), ``wave`` (None when it names none),
    ``reattributed_from`` (the engine whose analysis raised it, when another) and
    ``receiving_engine_flags`` (whether ``engine``'s own analysis or design flags one of
    its queries). ``designed_engines`` are the engines whose schema design completed. A
    source cost is compared only when both costs are given. ``redesign_by_engine`` is
    :func:`redesign_facts` of the redesign log: a wave states the rounds its engines were
    redesigned in, and why no further round ran when a ceiling stopped it.
    ``wave_edits`` is :func:`wave_edit_facts` (#2067): a suggested wave's line states the
    customer's decision on its warnings. ``to_dos_by_wave`` (wave -> sentences, #1938)
    are the customer's to-dos a wave's line ends with ("To do: ..."); they hold nothing.
    """
    designed = {str(e) for e in designed_engines}
    unserved = unserved_by_engine or {}
    reviews = open_reviews_by_engine or {}
    failed = failed_reviews_by_engine or {}
    unsupported_q = unsupported_by_engine or {}
    by_id = {r.get("risk_id"): r for r in risks if r.get("risk_id")}
    pricier = (
        (float(projected_cost), float(source_cost))
        if projected_cost is not None
        and source_cost is not None
        and float(projected_cost) > float(source_cost)
        else None
    )
    rows: list[dict[str, Any]] = []
    wave_one_unsuggested = False
    for i, wave in enumerate(waves):
        engines = [str(e) for e in wave.get("engines") or [] if e]
        holds = _holds_for(wave, waves, risks, unserved, reviews, pricier, unsupported_q)
        missing = [e for e in engines if e not in designed]
        unreviewed = [e for e in engines if int(failed.get(e) or 0) > 0]
        if i > 0 and wave_one_unsuggested and (holds or not (missing or unreviewed)):
            holds.append(
                {
                    "check": "earlier_wave",
                    "engine": None,
                    "risk_id": None,
                    "severity": None,
                    "risk_type": None,
                    "flagged_by": None,
                    "line": "wave 1 is not suggested yet, and this wave builds on it",
                }
            )
        warnings = [
            {"engine": e, **dict(w)}
            for e in engines
            for w in (review_warnings_by_engine or {}).get(e) or []
            if isinstance(w, Mapping)
        ]
        redesign = [(redesign_by_engine or {}).get(e) or {} for e in engines]
        rounds = max([int(r.get("rounds") or 0) for r in redesign] or [0])
        stopped = next((str(r["note"]) for r in redesign if r.get("note")), None)
        # The same note with no free text in it, for the executive report (#2075).
        stopped_short = next(
            (str(r.get("short_note") or r["note"]) for r in redesign if r.get("note")), None
        )
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        short: str | None = None
        if holds:
            readiness = "held"
            steps = _next_steps(holds, by_id, designed, window_representative)
            redesigned = f" (redesigned {_times(rounds)}, still held)" if rounds else ""
            reasons = (
                ": "
                + "; ".join(h["line"] for h in holds)
                + ". Next step: "
                + "; ".join(s["line"] for s in steps)
                + "."
            )
            line, short = (
                "Not suggested yet" + redesigned + (f" ({note})" if note else "") + reasons
                for note in (stopped, stopped_short)
            )
        elif missing:
            readiness = "not_designed"
            steps = [
                _step("design", e, f"run the {display_engine(e)} schema design") for e in missing
            ]
            line = (
                f"Not designed yet: run the {_names(missing)} schema design, then check the "
                "wave again."
            )
        elif unreviewed:
            readiness = "not_reviewed"
            steps = [
                _step("review_again", e, f"review the {display_engine(e)} design again")
                for e in unreviewed
            ]
            line = (
                f"Not reviewed: the {_names(unreviewed)} design was not reviewed (the review "
                "failed). Next step: review it again, then check the wave again."
            )
        else:
            readiness, steps = "suggested", []
            line = (
                f"Suggested: no check holds it after it was redesigned {_times(rounds)}."
                if rounds
                else SUGGESTED_LINE
            )
            decided = _decided_line(warnings, (wave_edits or {}).get(int(wave.get("wave") or 0)))
            if decided is not None:
                # The customer decided on the warnings with /wave-edit (#2067).
                line, short, accepted, rejected = decided
            elif warnings:
                # A review with only warnings holds nothing (maintainer decision on
                # #2051): approving the wave accepts them. Each warning names the
                # reviewed table and its columns: the short line counts them (#2075).
                head = (
                    "Suggested, with "
                    f"{_plural(len(warnings), 'design review warning', 'design review warnings')}"
                    " to accept"
                )
                otherwise = (
                    " If you do not accept them: "
                    + "; ".join(
                        f"revise the {display_engine(e)} schema design in your modernization "
                        "session and review it again"
                        for e in dict.fromkeys(w["engine"] for w in warnings)
                    )
                    + "."
                )
                line = (
                    f"{head}: " + "; ".join(_warning_phrase(w) for w in warnings) + "." + otherwise
                )
                short = f"{head} ({WARNINGS_LISTED_IN})." + otherwise
        to_dos = [str(t) for t in (to_dos_by_wave or {}).get(int(wave.get("wave") or 0)) or []]
        if to_dos:
            # Counts only, so the short line takes it as it is.
            tail = " To do: " + "; ".join(to_dos) + "."
            short = (short or line) + tail
            line += tail
        if i == 0 and readiness != "suggested" and wave.get("homogeneity") in _WAVE_ONE_MOVES:
            wave_one_unsuggested = True
        rows.append(
            {
                "wave": int(wave.get("wave") or 0),
                "readiness": readiness,
                "suggested": {"suggested": True, "held": False}.get(readiness),
                "holds": holds,
                "next_steps": steps,
                "readiness_line": line,
                "readiness_line_short": short or line,
                "to_dos": to_dos,
                "review_warnings": warnings if readiness == "suggested" else [],
                "review_warning_count": len(warnings) if readiness == "suggested" else 0,
                "accepted_warnings": accepted,
                "accepted_warning_count": len(accepted),
                "rejected_warnings": rejected,
                "rejected_warning_count": len(rejected),
                "open_warning_count": (
                    len(warnings) - len(accepted) - len(rejected) if readiness == "suggested" else 0
                ),
                "redesign_rounds": rounds,
                "redesign_note": stopped,
            }
        )
    return rows


#: The words a review's change request is stated in, briefly, for a warning to accept.
_WARNING_CHARS = 160


def review_change_summary(change_requests: Any) -> dict[str, Any]:
    """A review's change requests split by the contract's severity (#1992): ``blockers``
    (``severity`` ``blocker``) counted, every other severity (``warning``, or DynamoDB's
    ``major``, ``minor``, ``suggestion``) a warning, each stated briefly from its
    ``category``, ``target`` and ``requested_change``. Read from the structured review
    only, never its prose."""
    blockers, warnings = 0, []
    for cr in change_requests if isinstance(change_requests, list) else []:
        if not isinstance(cr, Mapping):
            continue
        severity = str(getattr(cr.get("severity"), "value", cr.get("severity")) or "").lower()
        if severity == "blocker":
            blockers += 1
            continue
        warnings.append(
            {
                "category": str(getattr(cr.get("category"), "value", cr.get("category")) or ""),
                "target": str(cr.get("target") or ""),
                "change": str(cr.get("requested_change") or "")[:_WARNING_CHARS],
            }
        )
    return {"blockers": blockers, "warnings": warnings}


def _last_review(run: Mapping[str, Any]) -> dict[str, Any] | None:
    """The last review a design run records: ``{"verdict", "blockers", "warnings"}``
    (``blockers`` None when the record has no severities: an older trace), ``{"verdict":
    "FAILED"}`` for a review that failed to run, None for no review."""
    external = run.get("pe_review")
    if isinstance(external, Mapping) and external.get("error"):
        return {"verdict": "FAILED"}
    entries = [
        e
        for e in run.get("trace") or []
        if isinstance(e, Mapping) and e.get("type") in ("pe_review", "pe_error")
    ]
    if entries:
        last = entries[-1]
        if last.get("type") == "pe_error":
            return {"verdict": "FAILED"}
        if not last.get("verdict"):
            return None
        if isinstance(last.get("blockers"), int):
            return {
                "verdict": str(last["verdict"]),
                "blockers": int(last["blockers"]),
                "warnings": [w for w in last.get("warnings") or [] if isinstance(w, Mapping)],
            }
        if isinstance(external, Mapping) and isinstance(external.get("change_requests"), list):
            return {
                "verdict": str(last["verdict"]),
                **review_change_summary(external["change_requests"]),
            }
        return {"verdict": str(last["verdict"]), "blockers": None, "warnings": []}
    if isinstance(external, Mapping) and external.get("verdict"):
        if not isinstance(external.get("change_requests"), list):
            return {"verdict": str(external["verdict"]), "blockers": None, "warnings": []}
        return {
            "verdict": str(external["verdict"]),
            **review_change_summary(external["change_requests"]),
        }
    return None


def _holds_review(review: Mapping[str, Any]) -> bool:
    """A review holds its wave when it has a blocker, whatever its verdict (maintainer
    decision on #2051; #2081: an approval that lists a blocker holds too): a review with
    only warnings does not. An older record with no severities holds when it did not
    approve the design. A review that failed to run holds nothing."""
    verdict = str(review.get("verdict") or "").upper()
    if verdict == "FAILED":
        return False
    blockers = review.get("blockers")
    if blockers is None:
        return verdict != "APPROVED"
    return int(blockers) > 0


def _runs(trace: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(trace, Mapping):
        return []
    return [g for g in trace.get("groups") or [] if isinstance(g, Mapping)] or [trace]


def open_review_count(trace: Mapping[str, Any] | None) -> int:
    """How many designs a design trace records whose last review has a blocker: the
    design itself, or each of its groups (a grouped DynamoDB design). A review with only
    warnings, none, or one that failed to run counts none here."""
    return sum(1 for run in _runs(trace) if (r := _last_review(run)) and _holds_review(r))


def failed_review_count(trace: Mapping[str, Any] | None) -> int:
    """How many designs (or design groups) a design trace records whose last review
    failed to run: not reviewed, so neither passed nor held."""
    return sum(1 for run in _runs(trace) if (r := _last_review(run)) and r["verdict"] == "FAILED")


def redesign_facts(log: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per engine, from one assignment version's redesign log (``report.json``'s
    ``wave_redesign``, #1992): the rounds that redesigned it, its last outcome, and the
    note a wave states when the last round did not keep a new design (it failed, did not
    improve it, or a run ceiling stopped it), and ``short_note``, the same with no free
    text (a failed round's reason can name a table, #2075)."""
    out: dict[str, dict[str, Any]] = {}
    for rnd in (log or {}).get("rounds") or []:
        if not isinstance(rnd, Mapping):
            continue
        n = int(rnd.get("round") or 0)
        raw = rnd.get("outcome")
        outcome: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        for engine in rnd.get("engines") or []:
            row = out.setdefault(
                str(engine), {"rounds": 0, "last": None, "note": None, "short_note": None}
            )
            last = outcome.get(engine)
            row["last"] = last
            row["note"] = row["short_note"] = None
            if last == "stopped":
                row["note"] = f"no further redesign round ran: {rnd.get('stopped')}"
                row["short_note"] = "no further redesign round ran: a run ceiling stopped it"
                continue
            row["rounds"] += 1
            if last == "failed":
                row["note"] = (
                    f"{(rnd.get('failed') or {}).get(engine) or f'redesign round {n} failed'}; "
                    "the earlier design is kept"
                )
                row["short_note"] = f"redesign round {n} failed; the earlier design is kept"
            elif last == "not_reviewed":
                row["note"] = row["short_note"] = (
                    f"redesign round {n}: the review did not run, so the earlier design is kept"
                )
            elif last == "did_not_improve":
                row["note"] = row["short_note"] = (
                    f"redesign round {n} did not improve it; the earlier design is kept"
                )
    return out


def reviewed_count(trace: Mapping[str, Any] | None) -> int:
    """How many designs (or design groups) a design trace records with a review verdict:
    a redesign round counts as ``fixed`` only for a design that was reviewed."""
    return sum(1 for run in _runs(trace) if (r := _last_review(run)) and r["verdict"] != "FAILED")


def all_review_warnings(trace: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The warnings of each design's (or group's) last review that ran, a review with a
    blocker included: what ``/wave-edit`` lists for a held wave too (#2067)."""
    out: list[dict[str, Any]] = []
    for run in _runs(trace):
        r = _last_review(run)
        if r and r["verdict"] != "FAILED":
            out.extend(dict(w) for w in r.get("warnings") or [])
    return out


def review_warnings(trace: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The warnings of each design's (or group's) last review that has no blocker: the
    change requests the customer accepts by approving the wave (#1992)."""
    out: list[dict[str, Any]] = []
    for run in _runs(trace):
        r = _last_review(run)
        if r and r["verdict"] != "FAILED" and not _holds_review(r):
            out.extend(dict(w) for w in r.get("warnings") or [])
    return out
