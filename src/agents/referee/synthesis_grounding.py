"""Ground synthesis output in the effective (post-reality-check) architecture.

The reality check can eliminate an engine triage selected and fold its workload into
another engine (for example OpenSearch absorbed into Aurora MySQL). The per-engine
artifacts synthesis reads (analysis anti-patterns, schema-design unsupported patterns,
reality-check pattern suggestions) were written while that engine was still a
candidate, so their free text can still recommend it. This module makes sure the
customer-facing report only recommends engines that are part of the target.

Rule for target recommendations (risks, mitigations, mitigation strategies,
architectural patterns): a sentence that names an eliminated engine is dropped. If
nothing is left, the text is rewritten to point at the absorbing engine, or the item
is dropped when no engine absorbed the workload. Consolidation history ("moved from
OpenSearch to Aurora MySQL") is not a recommendation and is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

ENGINE_DISPLAY_NAMES: dict[str, str] = {
    "dynamodb": "DynamoDB",
    "opensearch": "OpenSearch Service",
    "elasticache": "ElastiCache",
    "documentdb": "DocumentDB",
    "aurora_mysql": "Aurora MySQL",
    "aurora_postgresql": "Aurora PostgreSQL",
}

# How each engine is written in prose. Ids (``aurora_mysql``) and product names
# (``Aurora MySQL``, ``OpenSearch Service``) both count. Bare "MySQL"/"PostgreSQL"
# name the *source* database and deliberately match nothing.
_ENGINE_PATTERNS: dict[str, re.Pattern[str]] = {
    "dynamodb": re.compile(r"\bdynamo[\s_-]?db\b", re.IGNORECASE),
    "opensearch": re.compile(
        r"\bopen[\s_-]?search(?:\s+service)?\b|\belasticsearch\b", re.IGNORECASE
    ),
    "elasticache": re.compile(r"\belasti[\s_-]?cache\b|\bredis\b|\bvalkey\b", re.IGNORECASE),
    "documentdb": re.compile(r"\bdocument[\s_-]?db\b|\bmongo[\s_-]?db\b", re.IGNORECASE),
    "aurora_mysql": re.compile(r"\baurora[\s_-]?mysql\b", re.IGNORECASE),
    "aurora_postgresql": re.compile(r"\baurora[\s_-]?postgre(?:sql|s)\b", re.IGNORECASE),
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# "[engine] " tag and an optional "pattern_type: " label that prefix risk descriptions.
_ENGINE_TAG = re.compile(r"^(\[[^\]]+\]\s*(?:[\w-]+:\s+)?)")


def display_name(engine: str) -> str:
    return ENGINE_DISPLAY_NAMES.get(engine, engine)


def engine_mentions(text: str) -> list[tuple[str, int, int]]:
    """Return ``(engine, start, end)`` for every engine named in ``text``, in order."""
    found: list[tuple[str, int, int]] = []
    for engine, pattern in _ENGINE_PATTERNS.items():
        found.extend((engine, m.start(), m.end()) for m in pattern.finditer(text))
    found.sort(key=lambda f: f[1])
    return found


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text.strip()) if s]


# ---------------------------------------------------------------------------
# Effective vs eliminated engines
# ---------------------------------------------------------------------------


def eliminated_engines(
    effective: Iterable[str],
    reality_check: dict | None,
) -> dict[str, str | None]:
    """Map each engine the reality check eliminated to the engine that absorbed it.

    An engine is eliminated only when the reality check removed it from the effective
    assignment: it carried queries before the reality check (``before_distribution``)
    or was a consolidation ``from_engine``, and has no in-scope query in ``effective``.
    Engines triage selected but the assignment never routed anything to are not
    "eliminated" here. The absorber is the effective ``to_engine`` that took the most
    queries from it (None when nothing absorbed it). No reality check, nothing
    eliminated.
    """
    if not reality_check:
        return {}
    effective_set = set(effective)
    consolidations = reality_check.get("consolidations") or []
    candidates = set(reality_check.get("before_distribution") or {}) | {
        c.get("from_engine") for c in consolidations
    }
    out: dict[str, str | None] = {}
    for engine in sorted(e for e in candidates if e and e not in effective_set):
        moves = [
            c
            for c in consolidations
            if c.get("from_engine") == engine and c.get("to_engine") in effective_set
        ]
        moves.sort(key=lambda c: c.get("query_count", 0), reverse=True)
        out[engine] = moves[0]["to_engine"] if moves else None
    return out


def _absorber_note(absorber: str) -> str:
    return (
        f"Handle this on {display_name(absorber)}, which the reality check made "
        "responsible for this workload."
    )


def scrub_eliminated(text: str | None, eliminated: dict[str, str | None]) -> str | None:
    """Drop sentences of ``text`` that name an eliminated engine.

    Returns the cleaned text, the absorbing-engine rewrite when every sentence was
    dropped, or None when nothing is left and no engine absorbed the workload. A leading
    ``[engine]`` tag (and ``label:``) is preserved. Text naming no eliminated engine is returned unchanged.
    """
    if not text or not eliminated:
        return text
    tag_match = _ENGINE_TAG.match(text)
    tag = tag_match.group(1) if tag_match else ""
    body = text[len(tag) :]
    hits = [e for e, _, _ in engine_mentions(body) if e in eliminated]
    if not hits:
        return text
    kept = [
        s
        for s in split_sentences(body)
        if not any(e in eliminated for e, _, _ in engine_mentions(s))
    ]
    if kept:
        return tag + " ".join(kept)
    absorber = next((eliminated[e] for e in hits if eliminated[e]), None)
    if absorber:
        return tag + _absorber_note(absorber)
    return None


def ground_risks(risks: list[dict], eliminated: dict[str, str | None]) -> list[dict]:
    """Rewrite or drop risks whose text recommends an eliminated engine."""
    if not eliminated:
        return risks
    grounded = []
    for risk in risks:
        description = scrub_eliminated(risk.get("description"), eliminated)
        if not description:
            continue
        grounded.append(
            {
                **risk,
                "description": description,
                "mitigation": scrub_eliminated(risk.get("mitigation"), eliminated),
            }
        )
    return grounded


# ---------------------------------------------------------------------------
# Reality-check architectural patterns
# ---------------------------------------------------------------------------

# applies_to keys holding a single engine: the pattern is meaningless without it.
_SCALAR_ENGINE_KEYS = ("write_engine", "source_engine", "view_engine")
# applies_to keys listing every engine in the architecture: an eliminated engine is
# replaced by its absorber, since the absorber now carries that workload.
_ABSORBING_LIST_KEYS = ("engines", "target_engines")
_MIN_ENGINES = {"Polyglot Persistence": 3}


def ground_pattern(pattern: dict, eliminated: dict[str, str | None]) -> dict | None:
    """Return ``pattern`` with eliminated engines removed, or None if it no longer applies."""
    applies = dict(pattern.get("applies_to") or {})
    for key in _SCALAR_ENGINE_KEYS:
        if applies.get(key) in eliminated:
            return None
    for key, value in list(applies.items()):
        if not isinstance(value, list):
            continue
        engines: list[str] = []
        for engine in value:
            replacement = eliminated.get(engine) if key in _ABSORBING_LIST_KEYS else None
            engine = replacement or engine
            if engine not in eliminated and engine not in engines:
                engines.append(engine)
        if not engines:
            return None
        applies[key] = engines
    minimum = _MIN_ENGINES.get(pattern.get("name", ""), 0)
    if minimum and len(applies.get("engines", [])) < minimum:
        return None
    return {**pattern, "applies_to": applies}


def ground_reality_check_summary(summary: dict, eliminated: dict[str, str | None]) -> dict:
    """Re-derive pattern recommendations against the effective engines.

    Consolidation lines are history and are kept verbatim; ``Recommended pattern``
    lines are regenerated from the grounded patterns.
    """
    if not eliminated:
        return summary
    from src.agents.referee.reality_check import format_pattern_recommendation

    patterns = [
        p
        for p in (
            ground_pattern(p, eliminated) if isinstance(p, dict) else p
            for p in summary.get("architectural_patterns", [])
        )
        if p is not None
    ]
    recommendations = [
        r
        for r in summary.get("recommendations", [])
        if not (isinstance(r, str) and r.startswith("Recommended pattern"))
    ]
    recommendations += [format_pattern_recommendation(p) for p in patterns if isinstance(p, dict)]
    return {**summary, "architectural_patterns": patterns, "recommendations": recommendations}
