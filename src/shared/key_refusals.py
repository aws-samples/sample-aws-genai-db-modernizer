"""Why the redesign accepted or refused a candidate key, and the one sentence for it (#1361).

ADR-031 Decision 2 Amendment 13 reads each query after redesign: a column a filter tests
may get the key the access needs unless a guard refuses it. The access-pattern record
(``assignment/v<N>/access_patterns.json``) stores each candidate's codes only (its filter,
``via``, and the guards that refused it, ``reasons``); the sentence a reader sees is built
here, when the trace is read, and the anti-pattern evidence builds its part from the same
function, so the two never word one refusal two ways.
"""

from __future__ import annotations

from collections.abc import Iterable

# How a redesign key serves the filter it is assumed for.
VIA_EQUALITY = "equality"
VIA_NOT_NULL_TEST = "not_null_test"
VIA_OR_BRANCH = "or_branch"

# What the redesign decided for one candidate (``KeyCandidate.outcome``).
ACCEPTED = "accepted"
REFUSED = "refused"
OUTCOMES: tuple[str, ...] = (ACCEPTED, REFUSED)

# Why a candidate was refused, in the order the guards are read, each with its sentence.
# A refused candidate carries every guard that refuses it, not only the first.
ALREADY_A_KEY = "already_a_key"
NOT_IN_SCHEMA = "not_in_schema"
CORRELATED_PIN = "correlated_pin"
KEYSET_WALK = "keyset_walk"
REWRITTEN_BY_WRITE = "rewritten_by_write"
COMPOSITE_TAIL = "composite_tail"
NO_SELECTIVITY_SIGNAL = "no_selectivity_signal"
NOT_KEY_ELIGIBLE = "not_key_eligible"
REFUSAL_REASONS: dict[str, str] = {
    ALREADY_A_KEY: (
        "already indexed in the source, which an IS NOT NULL test does not anchor on as "
        "written, and the redesign adds no key a column already has"
    ),
    NOT_IN_SCHEMA: "not in the collected schema",
    CORRELATED_PIN: (
        "a pin whose value is another scope's column (a correlated join edge), not a value "
        "a key looks up"
    ),
    KEYSET_WALK: (
        "an IS NOT NULL test in a keyset batch walk, which reads every row whatever is indexed"
    ),
    REWRITTEN_BY_WRITE: "a column the statement's own write rewrites",
    COMPOSITE_TAIL: "a later column of a composite key, not a key alone",
    NO_SELECTIVITY_SIGNAL: (
        "an IS NOT NULL test with no selectivity signal (no partial index or statistics), "
        "which may read most rows"
    ),
    NOT_KEY_ELIGIBLE: (
        "a date, time, true/false, enum or category column, or one the statistics show "
        "low-cardinality"
    ),
}
VIA_TEXT: dict[str, str] = {
    VIA_EQUALITY: "an equality filter",
    VIA_OR_BRANCH: "a branch of an OR of equalities",
    VIA_NOT_NULL_TEST: "an IS NOT NULL test (a sparse index)",
}


def in_guard_order(reasons: Iterable[str]) -> tuple[str, ...]:
    """``reasons`` once each, in the order the guards are read."""
    order = list(REFUSAL_REASONS)
    return tuple(sorted(set(reasons), key=order.index))


def refusal_sentence(reasons: Iterable[str]) -> str:
    """Every guard that refused a candidate, in one phrase: the guard's sentence alone,
    or ``N reasons: (1) ...; (2) ...``."""
    whys = [REFUSAL_REASONS[r] for r in in_guard_order(reasons)]
    if len(whys) == 1:
        return whys[0]
    return f"{len(whys)} reasons: " + "; ".join(f"({i}) {w}" for i, w in enumerate(whys, 1))


def candidate_explanation(via: str, reasons: Iterable[str]) -> str:
    """The decision-trace sentence for one candidate key and the filter it serves."""
    filter_text = VIA_TEXT.get(via, via)
    reasons = tuple(reasons)
    if not reasons:
        return f"accepted: a key the redesign may assume for {filter_text}"
    return f"refused for {filter_text}: {refusal_sentence(reasons)}"
