# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive summary's three parts: recommendation, risk, decision (#1403, ADR-032).

The executive summary leads with what an executive reader acts on, in plain language:

- **Recommendation:** the target engines, the share of queries each takes, the wave
  order, and the projected monthly cost with its price basis. With no current cost
  collected for the source, it says so and claims no saving.
- **Risk:** the overall risk level and the one or two open risks driving it, with the
  wave to act before.
- **Decision:** what to approve next (normally wave 1), and why it is safe to approve.

Engineering detail (table and key-design counts, TTLs, consistency settings) stays in
the engineering report's body.

:func:`executive_brief_text` is the deterministic summary (``--llm-mode none``, and the
fallback for a rejected model summary): it only words ``report.json``'s ``facts``
(``facts.executive_brief`` and the headline figures), so it computes nothing.
:func:`check_summary_brief` holds a model's summary to the same structure: the projected
cost as the facts state it, the overall risk level, a decision sentence, no saving
claimed without a source cost, and the word budget.

``src/shared`` imports nothing from ``src/agents`` or ``src/report``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.shared.engine_names import SOURCE_ENGINE_DISPLAY_NAMES, display_engine
from src.shared.engines_in_use import retired_in_use_statement
from src.shared.report_facts import whole_dollars

# The longest summary a reader meets first: three short parts.
SUMMARY_WORD_BUDGET = 200

_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
# A risk type as the summary names it, in plain words.
_RISK_KINDS = {
    "PERFORMANCE_DEGRADATION": "risk of slower performance",
    "DATA_CONSISTENCY": "risk to data consistency",
    "MIGRATION_COMPLEXITY": "migration complexity risk",
}

# Each migration-risk rule as the summary names it: (what it is, what to do about it).
# A risk with no rule here is named by its severity, type and engine.
_EXPOSURES: dict[str, tuple[str, str]] = {
    "sync.wave": (
        "the tables kept current by change data capture (CDC)",
        "test the CDC replication",
    ),
    "routing.close_calls": (
        "the queries where two engines scored almost the same",
        "confirm where those queries run",
    ),
    "routing.tie_open": (
        "the queries tied between two engines",
        "choose their engine",
    ),
    "design.redesign": (
        "the queries whose plan relies on a schema redesign",
        "confirm the redesign in the schema design",
    ),
    "design.carried_writes": (
        "the writes the target engine serves poorly",
        "review those writes",
    ),
    "relational.gaps": (
        "the SQL features that need a rewrite or behave differently",
        "fix them",
    ),
}


def _count_word(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def whole_usd(x: int) -> str:
    """A cost as the executive summary states it, in whole dollars: ``$1,234``."""
    return f"${x:,}"


def _whole(brief: Mapping[str, Any], key: str, exact: Any) -> str | None:
    """The whole-dollar figure ``brief[key]`` the facts computed, else the exact one."""
    whole = brief.get(key)
    if isinstance(whole, int) and not isinstance(whole, bool):
        return whole_usd(whole)
    if isinstance(exact, (int, float)) and not isinstance(exact, bool):
        return usd(float(exact))
    return None


def usd(x: float) -> str:
    """A cost as every deliverable prints it: ``$1,234.56``."""
    return f"${x:,.2f}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _source_name(engine: str | None) -> str:
    if not engine:
        return "source"
    return SOURCE_ENGINE_DISPLAY_NAMES.get(engine, display_engine(engine))


def _price_basis(price_list: Any) -> str:
    """ "at AWS Price List on-demand prices for us-east-1, retrieved 2026-10-19"."""
    if not isinstance(price_list, Mapping):
        return "at on-demand list prices"
    text = (
        f"at {price_list.get('source') or 'list'} {price_list.get('pricing_model') or ''} "
        f"prices for {price_list.get('region') or 'the priced Region'}"
    )
    text = re.sub(r"\s+", " ", text)
    if price_list.get("retrieved_on"):
        text += f", retrieved {price_list['retrieved_on']}"
    return text


def _wave_clause(
    wave: Mapping[str, Any],
    engines: Mapping[str, Mapping[str, Any]],
    of_all: Mapping[str, Any],
) -> str:
    n = wave.get("wave")
    kind = wave.get("kind")
    names = [str(e) for e in wave.get("engines") or []]

    def share(e: str) -> str | None:
        # #1329: of every collected query when some have no engine in the scope.
        pct = of_all.get(e) if of_all else (engines.get(e) or {}).get("workload_percent")
        return f"{pct}%" if isinstance(pct, (int, float)) else None

    if kind in ("homogeneous", "cross_engine") and names:
        verb = "moves it" if kind == "homogeneous" else "converts it"
        pct = share(names[0])
        keeps = f", which ends up serving {pct} of the queries" if pct else ""
        return f"wave {n} {verb} to {display_engine(names[0])}{keeps}"
    if kind == "heterogeneous" or not names:
        return f"wave {n} plans no Aurora move"
    if kind == "cache":
        parts = []
        for e in names:
            row = engines.get(e) or {}
            reads, calls = row.get("cached_queries"), row.get("cached_call_share_percent")
            if isinstance(reads, int):
                hot = f"{reads} hot {'read' if reads == 1 else 'reads'}"
                if isinstance(calls, (int, float)):
                    hot += f" ({calls}% of calls)"
                parts.append(f"{display_engine(e)} in front of {hot}")
            else:
                parts.append(f"{display_engine(e)} as a cache")
        return f"wave {n} puts {_join(parts)}"
    shares = [(share(e), display_engine(e)) for e in names]
    if kind == "migration":
        moved = [f"{s} of the queries to {name}" if s else name for s, name in shares]
        if len(moved) > 1 and all(s for s, _ in shares):
            moved = [moved[0]] + [f"{s} to {name}" for s, name in shares[1:]]
        return f"wave {n} moves {_join(moved)}"
    added = [f"{name} for {s} of the queries" if s else name for s, name in shares]
    return f"wave {n} adds {_join(added)}"


def _recommendation(facts: Mapping[str, Any], brief: Mapping[str, Any], database_name: str) -> str:
    engines = {str(e.get("engine")): e for e in facts.get("engines") or [] if isinstance(e, dict)}
    waves = [w for w in brief.get("waves") or [] if isinstance(w, dict)]
    n_engines = int(facts.get("engine_count") or 0)
    n_waves = int(facts.get("migration_wave_count") or 0)
    source = brief.get("source_engine")
    named = " ".join(x for x in (database_name, _source_name(source) if source else "") if x)
    in_waves = (
        f" in {_count_word(n_waves)} {'wave' if n_waves == 1 else 'waves'}" if n_waves else ""
    )
    head = (
        f"Recommendation: move the {named + ' ' if named else ''}database to "
        f"{_count_word(n_engines)} AWS {'service' if n_engines == 1 else 'services'}"
        f"{in_waves}."
    )
    sentences = [head]
    if waves:
        of_all = brief.get("share_of_all_queries_percent") or {}
        clauses = [_wave_clause(w, engines, of_all) for w in waves]
        first = clauses[0][0].upper() + clauses[0][1:]
        sentences.append("; ".join([first, *clauses[1:]]) + ".")
    no_engine = facts.get("no_engine_in_scope_query_count")
    total = facts.get("query_total_count")
    if isinstance(no_engine, int) and no_engine > 0 and isinstance(total, int):
        them = "it" if no_engine == 1 else "them"
        sentences.append(
            f"{no_engine} of the {total} queries {'has' if no_engine == 1 else 'have'} no "
            f"engine: no engine in the customer's engine scope can serve {them}, so the waves "
            f"leave {them} out and the risk register lists {them} as blocking."
        )
    cost = facts.get("projected_monthly_cost_usd")
    shown = _whole(brief, "projected_monthly_cost_whole_usd", cost)
    if isinstance(cost, (int, float)) and cost > 0 and shown:
        sentences.append(
            f"The projected cost of the full modernization is {shown} per month "
            f"{_price_basis(facts.get('price_list'))}."
        )
    current = brief.get("source_monthly_cost_usd")
    savings = brief.get("savings_percent")
    if brief.get("source_cost_known") and isinstance(current, (int, float)):
        today = _whole(brief, "source_monthly_cost_whole_usd", current)
        line = f"The source database costs an estimated {today} per month today"
        if isinstance(savings, (int, float)) and savings > 0:
            line += f", so the plan saves {savings}%"
        elif isinstance(savings, (int, float)) and savings < 0:
            line += f", so the plan costs {abs(savings)}% more"
        sentences.append(line + ".")
    else:
        sentences.append(
            "No current cost was collected for the source database, so the plan claims "
            "no saving against it."
        )
    retired = [
        r
        for r in facts.get("retired_in_use_engines") or []
        if isinstance(r, dict) and r.get("engine")
    ]
    if retired:
        whole = brief.get("retired_saving_whole_usd") or {}
        sentences.append(
            "Retired by this plan: " + "; ".join(_retired(r, whole) for r in retired) + "."
        )
    return " ".join(sentences)


def _retired(row: Mapping[str, Any], whole: Mapping[str, Any]) -> str:
    """ "DocumentDB (runs today), $307/mo": the shared statement, in whole dollars."""
    saved = whole.get(str(row.get("engine")))
    if isinstance(saved, int) and not isinstance(saved, bool):
        return f"{display_engine(str(row['engine']))} (runs today), {whole_usd(saved)}/mo"
    return retired_in_use_statement(row)


def _exposure_phrase(exposure: Mapping[str, Any]) -> tuple[str, str, Any]:
    """(what the exposure is, what to do about it, the wave to do it before)."""
    rule = exposure.get("rule")
    wave = exposure.get("wave")
    engine = exposure.get("engine")
    if rule in _EXPOSURES:
        what, act = _EXPOSURES[str(rule)]
    else:
        severity = str(exposure.get("severity") or "").lower()
        severity = "" if severity == "unrated" else f"{severity}-severity "
        risk_type = str(exposure.get("risk_type") or "")
        kind = _RISK_KINDS.get(risk_type) or (
            f"{risk_type.replace('_', ' ').lower()} risk" if risk_type else "risk"
        )
        on = f" on {display_engine(str(engine))}" if engine else ""
        what, act = f"a {severity}{kind}{on}", "review it in the risk register"
    # The wave is named once, in the action ("... before wave 3 starts").
    return what, act, wave


def _before(waves: list[Any]) -> str:
    """ "before wave 3 starts", "before waves 1 and 3 start", or nothing with no wave."""
    named = [str(w) for w in waves if w]
    if not named:
        return ""
    if len(named) == 1:
        return f" before wave {named[0]} starts"
    return f" before waves {_join(named)} start"


def _wave_order(wave: Any) -> tuple[int, int]:
    return (0, wave) if isinstance(wave, int) and not isinstance(wave, bool) else (1, 0)


def _actions(exposures: list[tuple[str, str, Any]]) -> str:
    """Each wave's actions once, joined, then the wave they come before, in wave order.
    When every exposure is only to be reviewed, one clause names every wave (#2125:
    never "review them ... before wave 3 starts, and review them ... before wave 1")."""
    by_wave: dict[Any, list[str]] = {}
    for _, act, wave in sorted(exposures, key=lambda e: _wave_order(e[2])):
        acts = by_wave.setdefault(wave, [])
        if act not in acts:
            acts.append(act)
    several = len(exposures) > 1  # facts: presentation (verb agreement)
    if several and all(a == "review it in the risk register" for _, a, _ in exposures):
        return "review them in the risk register" + _before(list(by_wave))
    parts = [_join(acts) + _before([wave]) for wave, acts in by_wave.items()]
    return ", and ".join(parts)


def _risk(brief: Mapping[str, Any], open_risk_count: int) -> str:
    level = str(brief.get("overall_risk_level") or "LOW").lower()
    if not open_risk_count:
        return f"Risk: overall risk is {level}, with no open risks."
    exposures = [_exposure_phrase(e) for e in brief.get("exposures") or [] if isinstance(e, dict)]
    out = f"Risk: overall risk is {level}."
    if not exposures:
        return out
    whats = _join([w for w, _, _ in exposures])
    one = len(exposures) == 1  # facts: presentation (verb agreement)
    verb = "is" if one else "are"
    noun = "exposure" if one else "exposures"
    return f"{out} The main {noun} {verb} {whats}: {_actions(exposures)}."


_SUMMARY_STEPS = {
    "design": "run its schema design",
    "revise_design": "revise its design",
    "collect_longer": "collect over a longer window",
}


def _hold_phrase(hold: Mapping[str, Any]) -> str:
    """A hold in the summary's few words (the roadmap states it in full)."""
    engine = display_engine(str(hold.get("engine") or ""))
    rid = f" ({hold['risk_id']})" if hold.get("risk_id") else ""
    if hold.get("flagged_by"):
        # A re-attributed flag: name the engine whose analysis raised it.
        engine += f", flagged by the {display_engine(str(hold['flagged_by']))} analysis"
    check = hold.get("check")
    if check == "open_high_risk":
        on = f" on {engine}" if hold.get("engine") else ""
        return f"an open {str(hold.get('severity') or 'high').lower()} risk{on}{rid}"
    if check == "slower_and_pricier":
        return f"a plan costlier than the source with an open performance risk on {engine}{rid}"
    if check == "unserved_patterns":
        return f"access patterns the {engine} design leaves unserved"
    if check == "open_review":
        return f"an open {engine} design review"
    if check == "unsupported_queries":
        return f"assigned queries the {engine} design marks unsupported"
    return str(hold.get("line") or "")


def _unsuggested_wave_sentences(facts: Mapping[str, Any]) -> list[str]:
    """Each wave the plan does not suggest, in the summary's few words from the facts'
    readiness, hold and step codes (#1992): a held wave with what holds it and the next
    step, the waves not designed or not reviewed said once each, and the waves held only
    because wave 1 is said once."""
    rows = [w for w in facts.get("waves") or [] if isinstance(w, dict)]
    out: list[str] = []
    builds_on: list[int] = []
    for row in rows:
        if row.get("readiness") != "held":
            continue
        n = int(row.get("wave") or 0)
        holds = [h for h in row.get("holds") or [] if isinstance(h, dict)]
        own = [h for h in holds if h.get("check") != "earlier_wave"]
        if not own:
            builds_on.append(n)
            continue
        phrases = list(dict.fromkeys(_hold_phrase(h) for h in own))
        steps = list(
            dict.fromkeys(
                _SUMMARY_STEPS[str(s.get("step"))]
                for s in row.get("next_steps") or []
                if isinstance(s, dict) and str(s.get("step")) in _SUMMARY_STEPS
            )
        )
        rounds = int(row.get("redesign_rounds") or 0)
        after = {1: " after one redesign round", 2: " after two redesign rounds"}.get(
            rounds, f" after {rounds} redesign rounds" if rounds else ""
        )
        if row.get("redesign_note"):
            after += f" ({row['redesign_note']})"
        text = f"Wave {n} is not suggested yet{after}: {_join(phrases)}."
        if steps:
            text += f" Next, {_join(steps)}."
        out.append(text)
    if len(builds_on) == 1:  # facts: presentation (verb agreement)
        out.append(f"Wave {builds_on[0]} is not suggested yet: it builds on wave 1.")
    elif builds_on:
        out.append(
            f"Waves {_join([str(n) for n in builds_on])} are not suggested yet: they build "
            "on wave 1."
        )
    for state, said in (
        ("not_designed", "not designed yet: run {pron} schema design"),
        ("not_reviewed", "not reviewed (the review failed): review {pron} design again"),
    ):
        waves = [str(int(r.get("wave") or 0)) for r in rows if r.get("readiness") == state]
        if len(waves) == 1:  # facts: presentation (verb agreement)
            out.append(f"Wave {waves[0]} is {said.format(pron='its')}.")
        elif waves:
            out.append(f"Waves {_join(waves)} are {said.format(pron='their')}.")
    return out


def decision_paragraph(facts: Mapping[str, Any] | None) -> str | None:
    """The executive summary's Decision, rendered from the facts only (#1992): the wave
    to approve, why it is safe, and every wave the plan does not suggest. Every summary
    ends with it, a model's included (:func:`compose_summary`). None when ``facts`` has
    no ``executive_brief``."""
    if not isinstance(facts, Mapping):
        return None
    brief = facts.get("executive_brief")
    if not isinstance(brief, Mapping):
        return None
    return _decision(facts, brief)


def _warning_sentences(facts: Mapping[str, Any]) -> list[str]:
    """Each suggested wave's design review warnings, as the customer accepts them by
    approving it (#1992); the roadmap lists each one."""
    out = []
    for row in facts.get("waves") or []:
        if not isinstance(row, dict) or row.get("readiness") != "suggested":
            continue
        n = int(row.get("review_warning_count") or 0)
        took = int(row.get("accepted_warning_count") or 0)
        refused = int(row.get("rejected_warning_count") or 0)
        wave = int(row.get("wave") or 0)
        if not n:
            continue
        if not (took or refused):
            noun = "design review warning" if n == 1 else "design review warnings"
            out.append(f"Approving wave {wave} accepts its {n} {noun}, listed in the roadmap.")
        elif not refused:
            # #2067: the customer accepted every one with /wave-edit.
            out.append(
                f"The customer accepted wave {wave}'s {n} design review "
                f"{'warning' if n == 1 else 'warnings'}, listed in the roadmap."
            )
        else:
            # #2067: the customer did not accept some; approving the wave as it stands
            # accepts them too.
            out.append(
                f"The customer accepted {took} of wave {wave}'s {n} design review warnings "
                f"and did not accept {refused}, which are still open: approving wave {wave} "
                "as it stands accepts them too. All are listed in the roadmap."
            )
    return out


def _decision(facts: Mapping[str, Any], brief: Mapping[str, Any]) -> str:
    rest = [*_warning_sentences(facts), *_unsuggested_wave_sentences(facts)]
    text = _decision_head(facts, brief)
    return " ".join([text, *rest]) if rest else text


def _decision_head(facts: Mapping[str, Any], brief: Mapping[str, Any]) -> str:
    wave_n = brief.get("decision_wave")
    waves = [w for w in brief.get("waves") or [] if isinstance(w, dict)]
    if wave_n is None and any(isinstance(w.get("readiness"), str) for w in waves):
        return "Decision: no wave is suggested yet."
    if wave_n is None or not waves:
        return "Decision: approve the plan's first step."
    # The decision wave's own facts say why it is safe (#1992: not always wave 1).
    first = next((w for w in waves if int(w.get("wave") or 0) == int(wave_n)), waves[0])
    names = [display_engine(str(e)) for e in first.get("engines") or []]
    source = _source_name(brief.get("source_engine"))
    kind = first.get("kind")
    head = f"Decision: approve wave {wave_n}."
    if kind == "homogeneous" and names:
        why = (
            f"It is safe to approve: it moves the database 1:1 to {names[0]}, the same engine "
            f"family as {source}, with its current data model, and its cutover waits until "
            "schema and data parity is validated against the source."
        )
    elif kind == "cross_engine" and names:
        fact_rows = {
            int(w.get("wave") or 0): w for w in facts.get("waves") or [] if isinstance(w, dict)
        }
        gaps = bool((fact_rows.get(int(wave_n)) or {}).get("lists_relational_gaps"))
        why = (
            f"It is safe to approve: it converts the SQL dialect and data types from {source} "
            f"to {names[0]} and keeps the current data model, and its cutover waits until the "
            "schema and query translation is validated"
            + ("; the SQL compatibility check lists what to rewrite first." if gaps else ".")
        )
    elif kind == "cache" and names:
        why = f"It is safe to approve: {_join(names)} moves no data and can be switched off again."
    else:
        why = "Each wave cuts over only once its validation gate passes."
    return f"{head} {why}"


# #1992: a sentence that puts a wave, a step or the plan up for approval or for starting.
# A model's summary never states the decision: any sentence with one of these is
# stripped, whatever wave it names, and the Decision is rendered from the facts.
_DECISION_WORDING = re.compile(
    r"\bapprov\w*|\bproceed\w*|\bgo[- ]ahead\b|\bgreen[- ]?light\w*|\bsign[- ]?off\b"
    r"|\bsign (?:it )?off\b|\bauthori[sz]\w*|\bcommit to\b"
    r"|\bready to (?:start|begin|go|proceed|run|launch|move)\b"
    r"|\b(?:start|begin|launch|kick(?:s|ed)? off|move on to|moving on to|continue with)\s+"
    r"(?:with\s+)?(?:the\s+)?(?:wave|waves|plan|migration|modernization|first|next|full)\b"
    r"|\bnext wave\b",
    re.IGNORECASE,
)


def strip_decision_wording(text: str) -> tuple[str, list[str]]:
    """``text`` without a ``Decision:`` paragraph and without any sentence that states an
    approval, a go-ahead or a next wave (:data:`_DECISION_WORDING`), and the sentences
    taken out. Paragraph breaks are kept."""
    stripped: list[str] = []
    kept_paragraphs: list[str] = []
    # Each line is a paragraph, as the deliverables split the summary.
    for paragraph in re.split(r"\n+", text or ""):
        if not paragraph.strip():
            continue
        if re.match(r"\s*decision\s*:", paragraph, re.IGNORECASE):
            stripped.append(paragraph.strip())
            continue
        kept: list[str] = []
        for sentence in _sentences(paragraph):
            if _DECISION_WORDING.search(sentence):
                stripped.append(sentence)
            else:
                kept.append(sentence)
        if kept:
            kept_paragraphs.append(" ".join(kept))
    return "\n\n".join(kept_paragraphs), stripped


def compose_summary(text: str, facts: Mapping[str, Any] | None) -> str:
    """A model's summary as the customer sees it (#1992): its own text without any
    decision wording (:func:`strip_decision_wording`), then the Decision rendered from
    the facts. ``text`` as it is when the facts have no ``executive_brief``. Running it
    again on its own output gives the same text."""
    decision = decision_paragraph(facts)
    if decision is None:
        return text
    body, _ = strip_decision_wording(text)
    return f"{body}\n\n{decision}" if body else decision


def executive_brief_text(facts: Mapping[str, Any] | None, database_name: str = "") -> str | None:
    """The deterministic executive summary, three paragraphs, from ``facts`` (#1403).

    None when ``facts`` has no ``executive_brief`` (a report written before it).
    """
    if not isinstance(facts, Mapping):
        return None
    brief = facts.get("executive_brief")
    if not isinstance(brief, Mapping):
        return None
    open_count = int(facts.get("open_risk_count") or 0)
    return "\n\n".join(
        [
            _recommendation(facts, brief, database_name),
            _risk(brief, open_count),
            _decision(facts, brief),
        ]
    )


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


_NEGATION = re.compile(r"\b(no|not|none|without|never|cannot)\b|n't\b", re.IGNORECASE)
# A money claim: a saving (the noun), saving with a money cue, a lower or reduced bill, a
# cheaper plan. "ElastiCache saves round trips" is not one.
_SAVING_NOUN = re.compile(r"\bsavings?\b", re.IGNORECASE)
_SAVE_VERB = re.compile(r"\bsav(?:e|es|ed)\b", re.IGNORECASE)
_MONEY_CUE = re.compile(
    r"\$|\bUSD\b|\bmoney\b|\bcosts?\b|\bbills?\b|\bspend\w*|\bprices?\b|\bbudget\b",
    re.IGNORECASE,
)
_LOWER = re.compile(r"\b(?:reduc\w*|lower\w*|cuts?|smaller)\b", re.IGNORECASE)
_LOWER_WHAT = re.compile(r"\b(?:bills?|costs?|spend\w*|budget|prices?)\b", re.IGNORECASE)
_CHEAPER = re.compile(r"\bcheaper\b|\bcosts? less\b", re.IGNORECASE)
# A dollar amount however it is written: "$505", "$ 505", "US$505", "USD 505", "$1,234",
# "$505.02".
_AMOUNT = re.compile(
    r"(?:US\$|\$|\bUSD)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\d)", re.IGNORECASE
)


# #1474: the sentence that states the projected cost says it is the whole plan's.
_FULL_MODERNIZATION = re.compile(r"\bfull[\s-]+modernization\b", re.IGNORECASE)


def _amounts(text: str) -> list[tuple[str, int]]:
    """Each dollar amount in ``text``: (as written, rounded half up to whole dollars)."""
    out = []
    for m in _AMOUNT.finditer(text):
        value = float(m.group(1).replace(",", "") + "." + (m.group(2) or "0"))
        out.append((m.group(0), whole_dollars(value)))
    return out


def _fact_dollars(facts: Mapping[str, Any]) -> set[int]:
    """Every cost the facts state, in whole dollars: the projected and engine costs, the
    engines' total, retired savings, the source cost (each a figure a summary may quote)."""
    brief = facts.get("executive_brief") or {}
    values = [
        facts.get("projected_monthly_cost_usd"),
        facts.get("engine_cost_total_usd"),
        brief.get("source_monthly_cost_usd"),
        *(e.get("monthly_cost_usd") for e in facts.get("engines") or [] if isinstance(e, dict)),
        *(
            r.get("monthly_cost_usd")
            for r in facts.get("retired_in_use_engines") or []
            if isinstance(r, dict)
        ),
    ]
    whole = {
        whole_dollars(float(v))
        for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    for key in ("projected_monthly_cost_whole_usd", "source_monthly_cost_whole_usd"):
        if isinstance(brief.get(key), int):
            whole.add(int(brief[key]))
    whole |= {
        v for v in (brief.get("retired_saving_whole_usd") or {}).values() if isinstance(v, int)
    }
    return whole


def _sentences(text: str) -> list[str]:
    """Sentences, split at closing punctuation followed by a space and at every line
    break (a heading-style "Overall risk: medium" line is a sentence of its own)."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _level_with_risk(level: str) -> re.Pattern[str]:
    """The level within two words of "risk": "overall risk is medium", "Overall risk:
    medium", "the risk level is medium", "medium overall risk", "medium-risk"; not "risk
    is low, with medium effort"."""
    lv = re.escape(level)
    return re.compile(
        rf"\brisk\w*\W+(?:\w+\W+){{0,2}}{lv}\b|\b{lv}\W+(?:\w+\W+){{0,2}}risk",
        re.IGNORECASE,
    )


def _claims_saving(sentence: str) -> bool:
    if _NEGATION.search(sentence):
        return False
    return bool(
        _SAVING_NOUN.search(sentence)
        or (_SAVE_VERB.search(sentence) and _MONEY_CUE.search(sentence))
        or (_LOWER.search(sentence) and _LOWER_WHAT.search(sentence))
        or _CHEAPER.search(sentence)
    )


def _finding(message: str) -> dict[str, Any]:
    return {"high_confidence": True, "message": f"[high confidence] {message}"}


def check_summary_brief(summary: str, facts: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Findings when a model's summary breaks the executive structure (#1403).

    Each finding is high confidence: the summary falls back to the deterministic one.

    - no dollar amount in the text (``$505``, ``$ 505``, ``US$505``, ``USD 505``,
      ``about $505 a month``, ``$505.02``) rounds to the facts' whole-dollar projected
      cost (``executive_brief.projected_monthly_cost_whole_usd``);
    - a dollar amount rounds to no cost the facts state (``$510`` for ``$505.02``);
    - the sentence that states that cost does not say "full modernization" (#1474);
    - no sentence names the overall risk level within two words of "risk";
    - (#1992) no decision is checked here: a model's summary never states one. Its
      decision wording is stripped and the Decision is rendered from the facts
      (:func:`compose_summary`), so the summary is checked without it;
    - a saving is claimed (a "saving", "saves" with a money word, a lower or reduced
      bill or cost, "cheaper"; never in a negated sentence) when no deliverable states
      one (``executive_brief.saving_counted`` false);
    - the summary is over :data:`SUMMARY_WORD_BUDGET` words.

    Known limits: a sentence is negated as a whole, so "no downtime, and it saves money"
    passes; a decision for the right wave in a sentence that also names another wave
    passes.

    No finding when ``facts`` has no ``executive_brief`` (nothing to check against).
    """
    if not isinstance(facts, Mapping) or not isinstance(facts.get("executive_brief"), Mapping):
        return []
    brief = facts["executive_brief"]
    text = summary or ""
    sentences = _sentences(text)
    findings = []
    amounts = _amounts(text)
    whole = brief.get("projected_monthly_cost_whole_usd")
    cost = facts.get("projected_monthly_cost_usd")
    if isinstance(whole, int) and isinstance(cost, (int, float)) and cost > 0:
        if not any(rounded == whole for _, rounded in amounts):
            findings.append(
                _finding(
                    "executive structure: the summary leaves out the projected monthly cost "
                    f"{whole_usd(whole)}"
                )
            )
        elif not any(
            _FULL_MODERNIZATION.search(s) and any(r == whole for _, r in _amounts(s))
            for s in sentences
        ):
            findings.append(
                _finding(
                    "executive structure: the sentence that states the projected monthly cost "
                    "does not call it the full modernization's"
                )
            )
    known = _fact_dollars(facts)
    wrong = [written for written, rounded in amounts if rounded not in known]
    if wrong:
        findings.append(
            _finding(f"executive structure: the summary states a cost the facts do not: {wrong[0]}")
        )
    level = str(brief.get("overall_risk_level") or "").lower()
    if level and not any(_level_with_risk(level).search(s) for s in sentences):
        findings.append(
            _finding(
                f"executive structure: the summary leaves out the overall risk level ({level})"
            )
        )
    if not brief.get("saving_counted"):
        claims = [s for s in sentences if _claims_saving(s)]
        if claims:
            findings.append(
                _finding(
                    "executive structure: the summary claims a saving, but no source cost "
                    f"was collected: {claims[0]}"
                )
            )
    words = word_count(text)
    if words > SUMMARY_WORD_BUDGET:
        findings.append(
            _finding(
                f"executive structure: the summary is {words} words, over the word budget "
                f"of {SUMMARY_WORD_BUDGET}"
            )
        )
    return findings
