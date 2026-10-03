"""Ground synthesis output in the effective (post-reality-check) architecture.

The reality check can eliminate an engine triage selected and fold its workload into
another engine (for example OpenSearch absorbed into Aurora MySQL). The per-engine
artifacts synthesis reads (analysis anti-patterns, schema-design unsupported patterns,
reality-check pattern suggestions) were written while that engine was still a
candidate, so their free text can still recommend it. This module makes sure the
customer-facing report only recommends engines that are part of the target.

Rule for risks (never dropped, severity never changed): in a risk description or
mitigation, a sentence is dropped only when it *recommends* an eliminated engine
("stream to OpenSearch", "index in OpenSearch", "OpenSearch can handle ..."). Sentences
that describe a trade-off or history ("consolidated from OpenSearch", "without
OpenSearch", "instead of OpenSearch") always survive. A description left empty keeps
its original text plus a note that the engine is not part of the target; an empty
mitigation is replaced by a re-plan hint that names the absorbing engine only when the
risk's queries are actually assigned to it. Every rewritten risk carries a
``grounding_note``.
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

# Sentence boundary candidates: terminal punctuation followed by whitespace.
_BOUNDARY = re.compile(r"[.!?]+\s+")
# Tokens whose trailing period never ends a sentence ("e.g. by status").
_ABBREVIATIONS = frozenset("e.g i.e vs etc approx incl cf fig no mr ms dr st al eg ie resp".split())
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
    """Split prose into sentences without breaking on abbreviations or decimals.

    A boundary is terminal punctuation plus whitespace, followed by something that
    can start a sentence (not a lowercase letter), and not preceded by an
    abbreviation such as "e.g.", "i.e.", "vs." or "etc." ("etc." still ends a
    sentence when a capitalised word follows).
    """
    text = text.strip()
    out: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        nxt = text[m.end() : m.end() + 1]
        if not nxt or nxt.islower():
            continue
        words = text[start : m.start()].split()
        last = words[-1].lower().lstrip("([").rstrip(".") if words else ""
        if last in _ABBREVIATIONS and not (last == "etc" and nxt.isupper()):
            continue
        out.append(text[start : m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


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


# A sentence that describes a trade-off or the consolidation history. It explains
# why the engine is gone rather than recommending it, so it always survives.
_TRADE_OFF = re.compile(
    r"\b(?:consolidat\w*|absorb\w*|eliminat\w*|remov\w*|replac\w*|without|instead\s+of|"
    r"rather\s+than|los[et]\w*|no\s+longer|previously|formerly)\b",
    re.IGNORECASE,
)
# Verbs/prepositions that, right before an engine, make the sentence recommend it.
_RECOMMEND_BEFORE = re.compile(
    r"\b(?:use|using|move|moving|route|routing|stream|streaming|index|indexing|sync|"
    r"syncing|replicate|export|push|send|offload|offloading|adopt|leverage|consider|"
    r"requires?|requiring|via|to|in|into|on|through|with|add|deploy|introduce)\b"
    r"(?:\W+\w+){0,3}\W*$",
    re.IGNORECASE,
)
# "OpenSearch would/should/can handle ..." -- the engine as the proposed doer.
_RECOMMEND_AFTER = re.compile(
    r"^\W*(?:\w+\W+){0,1}?(?:would|should|can|could|will|must)\b"
    r"|^\W*(?:handles?|serves?|provides?|supports?|indexes|offers?)\b"
    r"|^\s+(?:ingestion|index(?:es)?|cluster|domain|pipeline)\b",
    re.IGNORECASE,
)


def _recommends(sentence: str, engines: Iterable[str]) -> bool:
    """True when ``sentence`` recommends one of ``engines`` (not a trade-off/history)."""
    targets = set(engines)
    mentions = [(e, s, t) for e, s, t in engine_mentions(sentence) if e in targets]
    if not mentions or _TRADE_OFF.search(sentence):
        return False
    for _, start, end in mentions:
        before = sentence[:start]
        if re.search(r"\bfrom\W*$", before, re.IGNORECASE):
            continue
        if _RECOMMEND_BEFORE.search(before) or _RECOMMEND_AFTER.match(sentence[end:]):
            return True
    return False


def _drop_recommendations(text: str, eliminated: dict[str, str | None]) -> tuple[str, str, bool]:
    """Return ``(tag, kept_body, changed)`` with recommending sentences removed.

    A leading ``[engine] `` tag and optional ``label: `` are kept apart from the body.
    """
    tag_match = _ENGINE_TAG.match(text)
    tag = tag_match.group(1) if tag_match else ""
    body = text[len(tag) :]
    sentences = split_sentences(body)
    kept = [s for s in sentences if not _recommends(s, eliminated)]
    return tag, " ".join(kept), len(kept) != len(sentences)


def _risk_engine(risk: dict) -> str | None:
    m = re.match(r"^\[([^\]]+)\]", risk.get("description") or "")
    return m.group(1) if m else None


def _named_eliminated(text: str, eliminated: dict[str, str | None]) -> list[str]:
    seen: list[str] = []
    for e, _, _ in engine_mentions(text):
        if e in eliminated and e not in seen:
            seen.append(e)
    return seen


def _not_in_target(engine: str, absorber: str | None) -> str:
    if absorber:
        return (
            f"({display_name(engine)} is not part of the target architecture; its queries "
            f"run on {display_name(absorber)}.)"
        )
    return f"({display_name(engine)} has no in-scope queries in the target architecture.)"


def _replan(
    risk_engine: str | None,
    absorber: str | None,
    query_ids: list[str],
    query_engine: dict[str, str],
) -> str:
    """Mitigation for a risk whose only recommendation named a removed engine.

    Points at the absorber only when the risk's queries are actually assigned to it
    (and it is not the risk's own engine, which would be a no-op instruction).
    """
    if (
        absorber
        and absorber != risk_engine
        and query_ids
        and all(query_engine.get(q) == absorber for q in query_ids)
    ):
        return (
            f"Handle this on {display_name(absorber)}, where the assignment now routes "
            "these queries."
        )
    targets = [e for e in (risk_engine, absorber) if e]
    targets = list(dict.fromkeys(targets))
    where = " or ".join(display_name(e) for e in targets) or "an engine in the target architecture"
    return f"Re-plan this on {where}; the original recommendation named an engine that was removed."


def ground_risks(
    risks: list[dict],
    eliminated: dict[str, str | None],
    query_engine: dict[str, str] | None = None,
) -> list[dict]:
    """Remove recommendations of eliminated engines from risk text (#202).

    Never drops a risk and never changes its severity: ``len(result) == len(risks)``.
    ``query_engine`` maps query ids to their effective engine; it decides whether a
    replacement mitigation may name the absorbing engine.
    """
    if not eliminated:
        return risks
    query_engine = query_engine or {}
    grounded = []
    for risk in risks:
        description = risk.get("description") or ""
        mitigation = risk.get("mitigation")
        named = _named_eliminated(f"{description} {mitigation or ''}", eliminated)
        if not named:
            grounded.append(risk)
            continue
        engine = named[0]
        absorber = eliminated[engine]
        own = _risk_engine(risk)
        notes = []

        tag, body, changed = _drop_recommendations(description, eliminated)
        if changed:
            if body:
                description = tag + body
                notes.append(f"Dropped a description sentence recommending {display_name(engine)}.")
            else:
                description = f"{description.rstrip()} {_not_in_target(engine, absorber)}"
                notes.append(
                    f"Description kept; it named {display_name(engine)}, which is not in the "
                    "target architecture."
                )

        if mitigation:
            tag, body, changed = _drop_recommendations(mitigation, eliminated)
            if changed:
                if body:
                    mitigation = tag + body
                    notes.append(
                        f"Dropped a mitigation sentence recommending {display_name(engine)}."
                    )
                else:
                    mitigation = _replan(
                        own, absorber, list(risk.get("query_ids") or []), query_engine
                    )
                    notes.append(
                        f"Mitigation replaced; it only recommended {display_name(engine)}."
                    )

        if notes:
            grounded.append(
                {
                    **risk,
                    "description": description,
                    "mitigation": mitigation,
                    "grounding_note": " ".join(notes),
                }
            )
        else:
            grounded.append(risk)
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


# ---------------------------------------------------------------------------
# Executive summary grounding (#205)
# ---------------------------------------------------------------------------

SUMMARY_GROUNDING_RULE = (
    "Every engine and table claim in the summary must match effective_architecture: "
    "name a table under an engine only if that table is in the engine's tables list "
    "(the tables its assigned in-scope queries touch), never present an eliminated "
    "engine as part of the target, and do not generalise a table to an engine that "
    "merely shares a query group with it. A summary that attributes a table to an "
    "engine none of whose queries touch it is rejected and the deterministic summary "
    "is shown instead."
)

_TOP_GROUPS_PER_ENGINE = 5


def _strip_db(table: str, database_name: str) -> str:
    prefix = f"{database_name}."
    return table[len(prefix) :] if database_name and table.startswith(prefix) else table


def engine_table_scope(
    assignment: dict | None,
    source_queries: list[dict],
    table_mappings: list[dict],
    known_tables: set[str],
) -> dict[str, list[str]]:
    """Tables each engine serves in the effective assignment.

    Same scope rule as schema-design input filtering (``filter_collector_for_assignment``):
    an engine's tables are the tables accessed by the in-scope queries assigned to it
    (``query_assignments[].source_tables``, falling back to the collector query's
    ``tables_accessed``). A table can belong to several engines. Names that are not
    known source tables (``unknown``, ``DUAL``) are ignored.

    Without an assignment (unversioned run) the schema designs stand in: every engine
    whose design covers a table (``table_mappings`` primary and alternatives).
    """
    scope: dict[str, set[str]] = {}
    qas = (assignment or {}).get("query_assignments") or []
    if qas:
        accessed = {q.get("query_id"): q.get("tables_accessed") or [] for q in source_queries}
        for qa in qas:
            engine = qa.get("assigned_engine")
            if not engine or not qa.get("in_scope", True):
                continue
            tables = qa.get("source_tables") or accessed.get(qa.get("query_id"), [])
            scope.setdefault(engine, set()).update(
                t for t in tables if not known_tables or t in known_tables
            )
    else:
        for m in table_mappings:
            scope.setdefault(m["recommended_database"], set()).add(m["source_table"])
            for alt in m.get("alternatives") or []:
                scope.setdefault(alt["database"], set()).add(m["source_table"])
    return {engine: sorted(tables) for engine, tables in scope.items()}


def build_effective_architecture(
    engine_tables: dict[str, list[str]],
    table_mappings: list[dict],
    ranking: list[dict],
    query_groups: list[dict],
    database_name: str,
    eliminated: dict[str, str | None],
) -> dict:
    """Compact per-engine view of the effective assignment for the summary LLM.

    ``tables`` per engine is ``engine_tables`` (see ``engine_table_scope``) without the
    ``<db>.`` prefix. ``recommended_engine_by_table`` is secondary information: the single
    engine the table mapping shows for each table. Query groups are the engine's busiest
    by design RPS; capabilities are the assignment's top reasons for routing to it.
    """
    group_rps: dict[str, dict[str, float]] = {}
    for g in query_groups:
        for ap in g.get("access_patterns", []):
            per_engine = group_rps.setdefault(ap.get("engine", ""), {})
            per_engine[g["group_name"]] = per_engine.get(g["group_name"], 0) + (
                ap.get("design_rps") or 0
            )

    order = [r["target"] for r in ranking]
    order += sorted(e for e in engine_tables if e not in order)
    engines = []
    for engine in order:
        if engine in eliminated:
            continue
        r = next((r for r in ranking if r["target"] == engine), {})
        groups = group_rps.get(engine, {})
        entry: dict = {
            "engine": engine,
            "display_name": display_name(engine),
            "tables": [_strip_db(t, database_name) for t in engine_tables.get(engine, [])],
            "top_query_groups": sorted(groups, key=lambda n: groups[n], reverse=True)[
                :_TOP_GROUPS_PER_ENGINE
            ],
        }
        if "assigned_queries" in r:
            entry["assigned_queries"] = r["assigned_queries"]
            entry["workload_percent"] = r.get("workload_percent", 0)
        if r.get("assignment_reason_summary"):
            entry["capabilities"] = r["assignment_reason_summary"]
        engines.append(entry)

    return {
        "rule": SUMMARY_GROUNDING_RULE,
        "source_database": database_name,
        "engines": engines,
        "recommended_engine_by_table": {
            _strip_db(m["source_table"], database_name): m["recommended_database"]
            for m in table_mappings
        },
        "eliminated_engines": [
            {"engine": e, "absorbed_by": a} for e, a in sorted(eliminated.items())
        ],
    }


_WORD = re.compile(r"[A-Za-z0-9]+")
_JOINER = re.compile(r"^[\s_.\-]*$")
_MAX_RUN = 8
# A bare one-word humanised stem ("post", "users") is ordinary English, so it only
# counts as a table reference when an access noun follows within a few words.
_ACCESS_NOUNS = frozenset(
    "lookup lookups read reads write writes query queries data record records row rows "
    "table tables item items entry entries traffic access".split()
)
_ACCESS_WINDOW = 4
_NEG_BEFORE = re.compile(
    r"\b(?:without|instead\s+of|rather\s+than|need\s+for|replac\w*|eliminat\w*|remov\w*|"
    r"than|no|not)\b(?:\W+\w+){0,3}\W*$",
    re.IGNORECASE,
)
_NEG_AFTER = re.compile(
    r"^(?:\W+\w+){0,3}?\W+(?:cannot|can't|can\s+not|could\s+not|couldn't|does\s+not|"
    r"doesn't|do\s+not|don't|won't|will\s+not|is\s+not|isn't|no\s+longer)\b",
    re.IGNORECASE,
)


def _canon(key: str) -> str:
    return key[:-1] if len(key) > 3 and key.endswith("s") else key


def _common_prefix(names: list[str]) -> str:
    """Shared ``xx_`` naming prefix (``wp_``) when every table carries it."""
    if len(names) < 2:
        return ""
    first = names[0].split("_", 1)[0] + "_"
    return first if all(n.startswith(first) and len(n) > len(first) for n in names) else ""


def _table_keys(tables: set[str], database_name: str) -> dict[str, set[str]]:
    """Map canonical compact keys to the source tables they name.

    Each table is matched as ``<db>.<name>``, ``<name>`` and the humanised stem with the
    shared naming prefix removed (``wp_postmeta`` -> ``post meta`` / ``postmeta``).
    """
    names = {t: _strip_db(t, database_name) for t in sorted(tables)}
    prefix = _common_prefix(list(names.values()))
    keys: dict[str, set[str]] = {}
    for table, name in names.items():
        variants = {name, f"{database_name}.{name}" if database_name else name}
        if prefix:
            variants.add(name[len(prefix) :])
        for v in variants:
            compact = "".join(_WORD.findall(v.lower()))
            if compact:
                keys.setdefault(_canon(compact), set()).add(table)
    return keys


def _table_mentions(
    sentence: str, keys: dict[str, set[str]], skip: list[tuple[int, int]]
) -> list[tuple[set[str], int, str]]:
    """Return ``(tables, position, matched_text)`` for table references in ``sentence``."""
    words = [m for m in _WORD.finditer(sentence) if not any(s <= m.start() < e for s, e in skip)]
    found = []
    i = 0
    while i < len(words):
        match = None
        for n in range(min(_MAX_RUN, len(words) - i), 0, -1):
            run = words[i : i + n]
            if any(
                not _JOINER.match(sentence[a.end() : b.start()])
                for a, b in zip(run, run[1:], strict=False)
            ):
                continue
            tables = keys.get(_canon("".join(w.group().lower() for w in run)))
            if not tables:
                continue
            span = sentence[run[0].start() : run[-1].end()]
            identifier = "_" in span or "." in span
            if n == 1 and not identifier:
                following = {w.group().lower() for w in words[i + 1 : i + 1 + _ACCESS_WINDOW]}
                if not following & _ACCESS_NOUNS:
                    continue
            match = (tables, run[0].start(), span, n)
            break
        if match:
            found.append(match[:3])
            i += match[3]
        else:
            i += 1
    return found


def _engine_refs(sentence: str, engines_in_play: set[str]) -> list[tuple[str, int, int, bool]]:
    """Engine mentions as ``(engine, start, end, negated)``.

    Bare "Aurora" resolves to the single Aurora engine in play, if there is exactly one.
    """
    refs = [(e, s, t) for e, s, t in engine_mentions(sentence)]
    aurora = sorted(engines_in_play & {"aurora_mysql", "aurora_postgresql"})
    if len(aurora) == 1:
        for m in re.finditer(r"\baurora\b(?![\s_-]?(?:mysql|postgre))", sentence, re.IGNORECASE):
            refs.append((aurora[0], m.start(), m.end()))
    refs.sort(key=lambda r: r[1])
    return [
        (e, s, t, bool(_NEG_BEFORE.search(sentence[:s]) or _NEG_AFTER.match(sentence[t:])))
        for e, s, t in refs
    ]


def check_summary_grounding(
    summary: str,
    engine_tables: dict[str, list[str]],
    database_name: str,
    known_tables: set[str] | None = None,
) -> list[str]:
    """Flag summary sentences that attribute a table to an engine that does not serve it.

    Rule (deterministic, documented in /synthesize):

    - An engine serves a table when at least one in-scope query assigned to it touches
      the table (``engine_tables``, from ``engine_table_scope``). A table can be served
      by several engines; naming it under any of them is correct. The table mapping's
      single ``recommended_database`` is not the test.
    - Tables are recognised as ``<db>.<table>``, ``<table>`` or the humanised stem
      without the shared naming prefix (``post meta`` for ``wp_postmeta``). A one-word
      stem without an underscore only counts when an access noun ("lookups",
      "queries", "table", ...) follows within four words.
    - Within a sentence, each table is attributed to the nearest engine named before
      it, or else the first engine named after it. Engines in a negated context
      ("cannot serve", "instead of", "removes the need for") are not attributions.
    - Sentences naming no engine, and names that are not known source tables, are not
      checked. Naming an engine outside the effective architecture with a table (an
      eliminated engine) is always a mis-attribution.

    Returns one human-readable warning per mis-attribution (empty list = grounded).
    """
    tables = set(known_tables or ()) | {t for ts in engine_tables.values() for t in ts}
    if not summary or not tables:
        return []
    served = {engine: set(ts) for engine, ts in engine_tables.items()}
    keys = _table_keys(tables, database_name)
    warnings: list[str] = []
    for sentence in split_sentences(summary):
        refs = _engine_refs(sentence, set(served))
        live = [r for r in refs if not r[3]]
        if not live:
            continue
        spans = [(s, t) for _, s, t, _ in refs]
        for named, pos, text in _table_mentions(sentence, keys, spans):
            before = [r for r in live if r[2] <= pos]
            engine = before[-1][0] if before else next(r[0] for r in live if r[1] > pos)
            if named & served.get(engine, set()):
                continue
            table = sorted(named)[0]
            owners = sorted(e for e, ts in served.items() if table in ts)
            where = ", ".join(display_name(e) for e in owners) or "no engine"
            warnings.append(
                f'Summary attributes {table} ("{text}") to {display_name(engine)}, but no '
                f"in-scope {display_name(engine)} query touches it (served by {where}): "
                f"{sentence}"
            )
    return warnings
