"""Ground synthesis output in the effective (post-reality-check) architecture.

The reality check can eliminate an engine triage selected and fold its workload into
another engine (for example OpenSearch absorbed into Aurora MySQL). The per-engine
artifacts synthesis reads (analysis anti-patterns, schema-design unsupported patterns,
reality-check pattern suggestions) were written while that engine was still a
candidate, so their free text can still recommend it. This module makes sure the
customer-facing report only recommends engines that are part of the target.

Rule for risks (never dropped, severity never changed): a sentence *recommends* an
eliminated engine when the nearest cue in the three words before the engine, within its
clause, is a recommend cue ("with OpenSearch", "to OpenSearch", "use OpenSearch"), or,
with no cue there and no sentence-wide trade-off cue, when the engine is the proposed
doer ("OpenSearch can handle ..."). A nearest trade-off cue ("without", "instead of",
"consolidating", "from", "between") keeps it. An engine listed as an alternative right
after "or"/"and" takes the cue before the whole list ("in the relational database or
OpenSearch", #253); that list-derived cue yields to a sentence-wide trade-off cue and
to a clause verb right after the mention ("... and OpenSearch is no longer required").
Descriptions never lose a sentence: one that recommends an eliminated engine gets a note
that the engine is not part of the target. Mitigations lose only the eliminated
alternative when it ends a list, and otherwise drop recommending sentences; an emptied
mitigation is replaced by a re-plan hint that names the absorbing engine only when the
risk's queries are actually assigned to it. Every rewritten risk carries a
``grounding_note``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.agents.referee.table_resolution import TableNameResolver
from src.shared.engine_names import display_engine

# How each engine is written in prose, for *recognising* a mention -- deliberately
# a separate list from the display names in src.shared.engine_names (used below by
# ``display_name`` for the name customer-facing text actually prints), and not
# keyed off that map's values. Both ids (``aurora_mysql``) and every product-name
# alias prose uses for the same engine (``OpenSearch Service``, ``Amazon
# OpenSearch``, ``Elasticsearch``, ``ElastiCache for Redis``/``for Memcached``,
# bare ``Redis``/``Valkey``) count as a mention via substring match -- the pattern
# only needs to find the engine's own name inside the alias, so an "Amazon "/"for
# Redis" wrapper around it is already tolerated without listing it explicitly.
# Keeping this independent of the display-name map means narrowing a display name
# (``"OpenSearch Service"`` -> ``"OpenSearch"``, #221) can never narrow what #202's
# grounding or #205's summary post-check recognise as a mention of the engine.
# Bare "MySQL"/"PostgreSQL" name the *source* database and deliberately match
# nothing.
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
    """Customer-facing name for ``engine`` (#221): the one shared, AWS-branded source
    of truth in ``src.shared.engine_names``, not a second hand-kept copy."""
    return display_engine(engine)


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
    # Assumes reality_check belongs to the same lineage as the effective assignment;
    # the lineage check (reality-check output versioning) is tracked in #215.
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


# Cue words in the <= 3 words before an eliminated-engine mention. The nearest cue
# decides: a trade-off/history cue keeps the sentence ("Without OpenSearch",
# "consolidating OpenSearch into ..."), a recommend cue drops it ("with OpenSearch",
# "to OpenSearch", "use OpenSearch").
_CUE_WINDOW = 3
_TRADE_OFF_CUE = re.compile(
    r"^(?:consolidat\w*|absorb\w*|without|replacing|from|than|between|versus|vs)$",
    re.IGNORECASE,
)
# An engine listed as an alternative ("in the relational database or OpenSearch",
# "with Aurora MySQL, ElastiCache, or OpenSearch") is governed by the cue before the
# whole list (#253). The window extends past a conjunction only when the conjunction
# directly precedes the mention (optionally with an article), stays within the clause,
# and stops at a clause verb. The list-derived cue is weaker than an adjacent one: it
# yields to a sentence-wide trade-off cue, and a clause verb right after the mention
# ("... and OpenSearch is no longer required") means "and" joined two clauses.
_ALTERNATIVE = re.compile(r"^(?:or|and)$", re.IGNORECASE)
_ARTICLE = re.compile(r"^(?:the|a|an|amazon)$", re.IGNORECASE)
_ALTERNATIVE_WINDOW = 8
_CLAUSE_BREAK = re.compile(r"[;:!?]|\.\s")
_CLAUSE_VERB_WORDS = (
    r"is|are|was|were|be|been|being|has|have|had|does|do|did|can|will|would|should|"
    r"must|may|might|need|needs"
)
_CLAUSE_VERB = re.compile(rf"^(?:{_CLAUSE_VERB_WORDS})$", re.IGNORECASE)
# A clause verb in the first two words after a mention ("OpenSearch is ...",
# "OpenSearch domain is ...").
_VERB_AFTER = re.compile(rf"^[^\w;:]*(?:\w+\s+)?(?:{_CLAUSE_VERB_WORDS})\b", re.IGNORECASE)
_RECOMMEND_CUE = re.compile(
    r"^(?:with|to|use|using|uses|via|in|into|on|through|offload\w*|adopt\w*|leverag\w*|"
    r"consider\w*|move|moving|route|routing|stream|streaming|index|indexing|sync|syncing|"
    r"replicate|export|push|send|requires?|requiring|add|deploy|introduce)$",
    re.IGNORECASE,
)
# Sentence-wide trade-off cues; they only count when no recommend cue is adjacent to
# the engine ("Offload search to OpenSearch, which eliminates scans" still recommends).
_SENTENCE_TRADE_OFF = re.compile(
    r"\b(?:los[et]\w*|no\s+longer|previously|formerly|consolidat\w*|absorb\w*|"
    r"eliminat\w*|remov\w*|replac\w*)\b",
    re.IGNORECASE,
)
# "OpenSearch would/should/can handle ..." -- the engine as the proposed doer.
_RECOMMEND_AFTER = re.compile(
    r"^\W*(?:\w+\W+){0,1}?(?:would|should|can|could|will|must)\b"
    r"|^\W*(?:handles?|serves?|provides?|supports?|indexes|offers?)\b"
    r"|^\s+(?:ingestion|index(?:es)?|cluster|domain|pipeline)\b",
    re.IGNORECASE,
)


def _mention_cue(before: str) -> str | None:
    """The nearest cue in the words before a mention, within the mention's clause.

    ``"recommend"``/``"trade_off"`` for a cue in the three words before the mention;
    ``"list_recommend"`` for a recommend cue reached past a conjunction that directly
    precedes the mention ("in the relational database or OpenSearch", #253).
    """
    words = re.findall(r"[A-Za-z]+", _CLAUSE_BREAK.split(before)[-1])
    limit = _CUE_WINDOW
    in_list = False
    adjacent = True  # only articles so far between the conjunction and the mention
    for k, i in enumerate(range(len(words) - 1, -1, -1), start=1):
        if k > limit:
            break
        word = words[i]
        if word.lower() == "of" and i > 0 and words[i - 1].lower() == "instead":
            return "trade_off"  # "instead of OpenSearch"
        if _TRADE_OFF_CUE.match(word):
            return "trade_off"
        if _RECOMMEND_CUE.match(word):
            return "list_recommend" if in_list else "recommend"
        if in_list and _CLAUSE_VERB.match(word):
            return None
        if adjacent and _ALTERNATIVE.match(word) and k < len(words):
            in_list = True
            limit = max(limit, min(k + _ALTERNATIVE_WINDOW, len(words)))
        adjacent = adjacent and bool(_ARTICLE.match(word))
    return None


def _recommends(sentence: str, engines: Iterable[str]) -> bool:
    """True when ``sentence`` recommends one of ``engines``.

    Decided per mention by the nearest cue before it (see ``_mention_cue``). An adjacent
    recommend cue decides. A list-derived one yields to a sentence-wide trade-off cue
    and to a clause verb right after the mention. With no cue, a sentence-wide trade-off
    cue keeps it, else "X can/should handle ..." after the mention recommends.
    """
    targets = set(engines)
    for engine, start, end in engine_mentions(sentence):
        if engine not in targets:
            continue
        cue = _mention_cue(sentence[:start])
        if cue == "recommend":
            return True
        trade_off = bool(_SENTENCE_TRADE_OFF.search(sentence))
        if cue == "list_recommend":
            if not trade_off and not _VERB_AFTER.match(sentence[end:]):
                return True
            continue
        if cue == "trade_off" or trade_off:
            continue
        if _RECOMMEND_AFTER.match(sentence[end:]):
            return True
    return False


def recommends_engine(text: str, engine: str) -> bool:
    """True when any sentence of ``text`` recommends ``engine`` (#221).

    Used to tell an anti-pattern whose own advice was "move these queries to X" (and the
    assignment did) from one that merely describes a limitation.
    """
    return any(_recommends(s, [engine]) for s in split_sentences(text or ""))


# A trailing alternative ("..., or OpenSearch", "... or in Amazon OpenSearch") that
# ends its phrase: followed by punctuation, the end, or a "for" complement.
_ALT_LEAD = r"(?:(?:in|on|to|into|with|via|using|use)\s+)?(?:the\s+)?(?:amazon\s+)?"
_ALT_END = r"(?=\s*(?:[;,.:)]|$)|\s+for\b)"
_RECOMMEND_WORD = re.compile(rf"\b{_RECOMMEND_CUE.pattern[1:-1]}\b", re.IGNORECASE)


def _prune_alternative(sentence: str, engine: str) -> str | None:
    """``sentence`` without ``engine`` where it is the last of several alternatives (#253).

    "Keep in the relational database or OpenSearch; ..." -> "Keep in the relational
    database; ...", "with A, B(,) or OpenSearch" -> "with A or B", "in A, B and
    OpenSearch" -> "in A and B". Only the list after the governing cue is rewritten.
    None when the engine is not such an alternative.
    """
    m = re.search(
        rf"(,?)\s+(or|and)\s+{_ALT_LEAD}(?:{_ENGINE_PATTERNS[engine].pattern}){_ALT_END}",
        sentence,
        re.IGNORECASE,
    )
    if not m:
        return None
    head = sentence[: m.start()]
    clause_start = max(head.rfind(";"), head.rfind(":")) + 1
    cues = list(_RECOMMEND_WORD.finditer(head, clause_start))
    list_start = cues[-1].end() if cues else clause_start
    last_comma = head.rfind(",", list_start)
    if last_comma != -1:
        # "A, B, or C" / "A, B or C" minus C is "A or B".
        head = f"{head[:last_comma]} {m.group(2)}{head[last_comma + 1 :]}"
    return head + sentence[m.end() :]


def _drop_recommendations(
    text: str, eliminated: dict[str, str | None], prune: bool = False
) -> tuple[str, str, bool]:
    """Return ``(tag, kept_body, changed)`` with recommending sentences removed.

    A leading ``[engine] `` tag and optional ``label: `` are kept apart from the body.
    With ``prune``, a sentence that recommends an eliminated engine only as one of
    several alternatives keeps the other alternatives instead of being dropped (#253).
    """
    tag_match = _ENGINE_TAG.match(text)
    tag = tag_match.group(1) if tag_match else ""
    body = text[len(tag) :]
    sentences = split_sentences(body)
    kept = []
    for sentence in sentences:
        if prune:
            for engine in eliminated:
                if not _recommends(sentence, eliminated):
                    break
                pruned = _prune_alternative(sentence, engine)
                if pruned is not None:
                    sentence = pruned
        if not _recommends(sentence, eliminated):
            kept.append(sentence)
    return tag, " ".join(kept), kept != sentences


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

        # Descriptions explain the risk, so no sentence is ever dropped from them; a
        # recommendation of the eliminated engine only earns a not-in-target note.
        _, _, changed = _drop_recommendations(description, eliminated)
        if changed:
            description = f"{description.rstrip()} {_not_in_target(engine, absorber)}"
            notes.append(
                f"Description kept with a note; it recommended {display_name(engine)}, which "
                "is not in the target architecture."
            )

        if mitigation:
            tag, body, changed = _drop_recommendations(mitigation, eliminated, prune=True)
            if changed:
                if body:
                    mitigation = tag + body
                    notes.append(
                        f"Removed the mitigation text recommending {display_name(engine)}."
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


def recompute_reality_check_patterns(summary: dict, query_assignments: list[dict]) -> dict:
    """Recompute patterns and their recommendation lines from the effective assignment.

    The stored reality-check output can predate the final assignment (patterns were
    detected before corrections, the sweep or a customer edit), so synthesis detects
    them again from the effective in-scope ``query_assignments``. This also fixes jobs
    written before the reality check refreshed its own patterns. Consolidation lines are
    history and are kept verbatim. Without an assignment the summary is returned as is.
    """
    if not query_assignments:
        return summary
    from src.agents.referee.reality_check import (
        format_pattern_recommendation,
        patterns_for_assignment,
    )

    patterns = patterns_for_assignment(query_assignments)
    recommendations = [
        r
        for r in summary.get("recommendations", [])
        if not (isinstance(r, str) and r.startswith(("Recommended pattern", "No consolidation")))
    ]
    recommendations += [format_pattern_recommendation(p) for p in patterns]
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
    known_tables: Iterable[str],
) -> dict[str, list[str]]:
    """Tables each engine serves in the effective assignment.

    Same scope rule as schema-design input filtering (``filter_collector_for_assignment``):
    an engine's tables are the tables accessed by the in-scope queries assigned to it
    (``query_assignments[].source_tables``, falling back to the collector query's
    ``tables_accessed``), and the cache layer's tables are those of the queries it
    fronts (``cache_engine``, #296). A table can belong to several engines. Names that are not
    known source tables (``unknown``, ``DUAL``) are ignored.

    ``source_tables``/``tables_accessed`` can be qualified with the SQL schema
    the parser saw, while ``known_tables`` (collector ``table_id``s) is
    qualified with the customer-entered database label; an exact-string match
    against ``known`` drops every table when the two diverge (#116, 367-3).
    Resolved through :class:`TableNameResolver` the same way
    ``filter_collector_for_assignment`` is, built from the flat ``known_tables``
    set (no bare ``table_name``/``view_name`` side here, so resolution falls
    back to :meth:`TableNameResolver.from_known_ids`'s normalized-only match).

    Without an assignment (unversioned run) the schema designs stand in: every engine
    whose design covers a table (``table_mappings`` primary and alternatives).
    """
    known = set(known_tables)
    resolver = TableNameResolver.from_known_ids(known)
    scope: dict[str, set[str]] = {}
    qas = (assignment or {}).get("query_assignments") or []
    if qas:
        accessed = {q.get("query_id"): q.get("tables_accessed") or [] for q in source_queries}
        for qa in qas:
            if not qa.get("in_scope", True):
                continue
            tables = qa.get("source_tables") or accessed.get(qa.get("query_id"), [])
            # The cache layer serves the tables of the reads it fronts (#296)
            for engine in (qa.get("assigned_engine"), qa.get("cache_engine")):
                if not engine:
                    continue
                resolved: set[str] = set()
                for t in tables:
                    if not known:
                        resolved.add(t)
                        continue
                    match = resolver.resolve(t) if resolver is not None else None
                    if match:
                        resolved.add(match)
                    elif t in known:
                        resolved.add(t)
                scope.setdefault(engine, set()).update(resolved)
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
        if r.get("role") == "cache_layer":
            # Owns no query: described by the reads it fronts, never a workload share
            entry["role"] = "cache_layer"
            entry["cached_queries"] = r.get("cache_overlay_queries", 0)
            entry["cached_call_share_percent"] = r.get("cache_call_share_percent", 0)
        elif "assigned_queries" in r:
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
# Only "not"/"no" directly before the engine negates it ("not DynamoDB").
_NEG_DIRECT = re.compile(r"\b(?:not|no)\s+$", re.IGNORECASE)
# Clause boundaries for attribution: punctuation and clause-joining words.
_CLAUSE_SPLIT = re.compile(
    r"[,;:\u2014\u2013()]|\b(?:while|whereas|but|and|leaving|freeing)\b", re.IGNORECASE
)
# A clause narrating history ("moved from OpenSearch to Aurora MySQL") attributes nothing.
_HISTORY = re.compile(
    r"\bfrom\b.+\bto\b|\b(?:consolidat\w*|absorb\w*|moved|previously|formerly|"
    r"migrated\s+from)\b",
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
) -> list[tuple[set[str], int, str, int]]:
    """Return ``(tables, position, matched_text, word_count)`` for table references."""
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
            found.append(match)
            i += match[3]
        else:
            i += 1
    return found


def _engine_refs(clause: str, engines_in_play: set[str]) -> list[tuple[str, int, int, bool]]:
    """Engine mentions as ``(engine, start, end, negated)``.

    Bare "Aurora" resolves to the single Aurora engine in play, if there is exactly one.
    Only "not"/"no" directly before the engine negates it.
    """
    refs = [(e, s, t) for e, s, t in engine_mentions(clause)]
    aurora = sorted(engines_in_play & {"aurora_mysql", "aurora_postgresql"})
    if len(aurora) == 1:
        for m in re.finditer(r"\baurora\b(?![\s_-]?(?:mysql|postgre))", clause, re.IGNORECASE):
            refs.append((aurora[0], m.start(), m.end()))
    refs.sort(key=lambda r: r[1])
    return [(e, s, t, bool(_NEG_DIRECT.search(clause[:s]))) for e, s, t in refs]


def _clauses(sentence: str) -> list[str]:
    if re.search(r"\brespectively\b", sentence, re.IGNORECASE):
        return [sentence]  # "A and B serve X and Y, respectively": one unit
    return [c for c in _CLAUSE_SPLIT.split(sentence) if c.strip()]


def check_summary_grounding(
    summary: str,
    engine_tables: dict[str, list[str]],
    database_name: str,
    known_tables: Iterable[str] | None = None,
) -> list[dict]:
    """Find summary clauses that attribute a table to an engine that does not serve it.

    Rule (deterministic, documented in /synthesize):

    - An engine serves a table when at least one in-scope query assigned to it touches
      the table (``engine_tables``, from ``engine_table_scope``). A table can be served
      by several engines. The table mapping's single ``recommended_database`` is not
      the test.
    - Tables are recognised as ``<db>.<table>``, ``<table>`` or the humanised stem
      without the shared naming prefix (``post meta`` for ``wp_postmeta``). A one-word
      stem without an underscore only counts when an access noun ("lookups",
      "queries", "table", ...) follows within four words.
    - Sentences are split into clauses at , ; : dashes and parentheses and at
      while/whereas/but/and/leaving/freeing (a sentence using "respectively" stays one
      unit). A table is checked against the live engines named in its clause; a clause
      naming none inherits the engines of the nearest earlier clause in the sentence
      (list continuation), or else of the next one. It passes if ANY of those engines
      serves it. Engines directly preceded by "not"/"no" are not attributions, and
      history clauses (from ... to, consolidated, absorbed, moved, previously,
      formerly, migrated from) are skipped.
    - A mismatch is high confidence when the table is written as an identifier
      (``wp_x``, ``db.x``) or a multi-word stem AND its own clause names exactly one
      live engine, or when the clause names an engine outside the effective
      architecture (an eliminated engine) as the server. Only high-confidence
      mismatches reject a summary; the rest are recorded as warnings.

    Returns findings ``{"table", "text", "engines", "high_confidence", "sentence",
    "message"}``; an empty list means grounded.
    """
    tables = set(known_tables or ()) | {t for ts in engine_tables.values() for t in ts}
    if not summary or not tables:
        return []
    served = {engine: set(ts) for engine, ts in engine_tables.items()}
    keys = _table_keys(tables, database_name)
    findings: list[dict] = []

    def judge(named, text, n, engines, own, sentence) -> None:
        if any(named & served.get(e, set()) for e in engines):
            return
        table = sorted(named)[0]
        precise = n > 1 or "_" in text or "." in text
        outside = [e for e in engines if e not in served]
        high = bool(outside) or (own and len(engines) == 1 and precise)
        owners = sorted(e for e, ts in served.items() if table in ts)
        where = ", ".join(display_name(e) for e in owners) or "no engine"
        said = " or ".join(display_name(e) for e in engines)
        level = "high confidence" if high else "low confidence"
        findings.append(
            {
                "table": table,
                "text": text,
                "engines": list(engines),
                "high_confidence": high,
                "sentence": sentence,
                "message": (
                    f'[{level}] Summary attributes {table} ("{text}") to {said}, but no '
                    f"in-scope query of that engine touches it (served by {where}): {sentence}"
                ),
            }
        )

    for sentence in split_sentences(summary):
        inherited: list[str] | None = None
        pending: list[tuple] = []
        for clause in _clauses(sentence):
            if _HISTORY.search(clause):
                continue
            refs = _engine_refs(clause, set(served))
            live = list(dict.fromkeys(e for e, _, _, neg in refs if not neg))
            mentions = _table_mentions(clause, keys, [(s, t) for _, s, t, _ in refs])
            if live:
                for named, _, text, n in mentions:
                    judge(named, text, n, live, True, sentence)
                for named, _, text, n in pending:
                    judge(named, text, n, live, False, sentence)
                pending = []
                inherited = live
            elif inherited:
                for named, _, text, n in mentions:
                    judge(named, text, n, inherited, False, sentence)
            else:
                pending.extend(mentions)
    return findings


# #380: internal field/section names this codebase uses for its own JSON
# shape (``report.json``'s own keys, or a sibling object's), which a model
# sometimes points a customer at instead of just stating the figure (e.g.
# "see tco_analysis's cost_breakdown for documentdb's own figure before
# removal"). Matched whole-word, case-insensitively, so prose that happens to
# contain one of these words in a different sense is still caught -- a
# customer-facing summary has no legitimate reason to name any of them at
# all, so there is no false-positive case worth tolerating a narrower regex
# for.
_INTERNAL_FIELD_NAMES = (
    "tco_analysis",
    "cost_breakdown",
    "table_mappings",
    "risk_assessment",
    "query_groups",
    "schema_designs",
    "recommended_architecture",
    "migration_waves",
    "assignment_summary",
    "effective_architecture",
)
_INTERNAL_FIELD_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _INTERNAL_FIELD_NAMES) + r")\b",
    re.IGNORECASE,
)


def check_summary_internal_leaks(summary: str) -> list[dict]:
    """Find internal field/section names leaked into customer-facing ``summary``.

    Returns findings shaped like :func:`check_summary_grounding`'s own
    (``high_confidence`` always ``True`` -- there is no legitimate, low-stakes
    reason for a customer-facing summary to name one of this codebase's own
    JSON field names), so a caller can fold them into the same accept/reject
    decision without a second code path. An empty list means clean.
    """
    if not summary:
        return []
    findings: list[dict] = []
    for m in _INTERNAL_FIELD_RE.finditer(summary):
        findings.append(
            {
                "table": None,
                "text": m.group(0),
                "engines": [],
                "high_confidence": True,
                "sentence": summary,
                "message": (
                    f'[high confidence] Summary names an internal field, "{m.group(0)}", '
                    "instead of just stating the figure."
                ),
            }
        )
    return findings


# #393: the wave order (``migration_waves``, ``src.agents.referee.migration_waves``) is
# one deterministic rule -- a model may explain a wave, it never decides the sequence --
# so a summary that states an explicit "X first, then Y" order contradicting it is wrong
# the same way a table/engine mismatch is.
#
# Matched narrowly, binding each cue to the engine actually next to it (#393 review: a
# looser "first ... then" co-occurrence anywhere in the sentence, paired with every
# engine mention ordered by position, rejected unrelated prose such as "Validate the
# schema first, then roll out: Aurora MySQL serves relational joins, DynamoDB serves
# key-value lookups" -- no engine sits anywhere near either cue there):
# - "first" binds to the nearest engine mention immediately BEFORE it ("the cache
#   first"); "then" binds to the nearest engine mention immediately AFTER it ("then
#   Aurora MySQL"); each within a few words.
# - clauses are also split on ";"/":" (sentences read as a list of steps there, e.g.
#   "DynamoDB first; then Aurora takes over"), but a cue's binding never crosses that
#   split to reach an engine named only in the OTHER clause -- only the one adjacent
#   pair of clauses where one clause ENDS with a bound "first" (nothing but the cue
#   itself trails it) and the next STARTS with a "then" cue (within the usual word gap)
#   is paired across the boundary (#393 review round 2: splitting unconditionally let a
#   "DynamoDB moves first; then Aurora MySQL takes over." order escape entirely, since
#   each engine then sat in its own clause with no partner to compare against).
# - "first" immediately next to a hyphen ("first-party", "class-first") or followed by
#   "wave" ("the first wave", describing a wave, not an order) is not a cue at all.
# - every cue-bound engine (whether bound to "first" or to "then") is compared only
#   against another cue-bound engine later in the clause (or, for the one adjacent-pair
#   case above, in the next clause), so "X first, then Y, then Z" also catches Y stated
#   ahead of Z -- an engine mention that is not itself next to a cue never enters the
#   comparison at all.
# When no cue binds to an engine this way, the check makes no claim and rejects
# nothing -- in doubt, it stays silent rather than guessing from loose proximity.
_FIRST_CUE_RE = re.compile(r"(?<!-)\bfirst\b(?!-)(?!\s+wave\b)", re.IGNORECASE)
_THEN_CUE_RE = re.compile(r"\bthen\b", re.IGNORECASE)
_WAVE_CLAUSE_SPLIT = re.compile(r"[;:]")
# How many whole words may separate a cue from the engine mention it binds to.
_MAX_CUE_GAP_WORDS = 4
# Prose often calls the cache wave just "the cache", not "ElastiCache"/"Redis"/"Valkey"
# (the only aliases ``_ENGINE_PATTERNS`` recognises) -- recognised here, in addition to
# that map, only when a cache wave actually exists, so this never invents a mention of
# an engine that is not even part of the migration.
_BARE_CACHE_RE = re.compile(r"\bcache\b", re.IGNORECASE)


def _clause_engine_mentions(clause: str, has_cache_wave: bool) -> list[tuple[str, int, int]]:
    mentions = engine_mentions(clause)
    if has_cache_wave:
        mentions = mentions + [
            ("elasticache", m.start(), m.end()) for m in _BARE_CACHE_RE.finditer(clause)
        ]
    return sorted(mentions, key=lambda f: f[1])


def _nearest_bound_engine(
    clause: str,
    cue_start: int,
    cue_end: int,
    mentions: list[tuple[str, int, int]],
    *,
    before: bool,
) -> str | None:
    """The engine mention closest to the cue, strictly before/after it, within
    ``_MAX_CUE_GAP_WORDS`` whole words -- or ``None`` when none is that close."""
    best: tuple[str, int] | None = None  # (engine, distance in characters, for tie-break)
    for engine, m_start, m_end in mentions:
        if before:
            if m_end > cue_start:
                continue
            gap_text, distance = clause[m_end:cue_start], cue_start - m_end
        else:
            if m_start < cue_end:
                continue
            gap_text, distance = clause[cue_end:m_start], m_start - cue_end
        if len(gap_text.split()) > _MAX_CUE_GAP_WORDS:
            continue
        if best is None or distance < best[1]:
            best = (engine, distance)
    return best[0] if best else None


class _WaveOrderClause:
    """One ``;``/``:``-separated clause's cue-bound engines (#393 review round 2:
    split out so the cross-clause boundary pairing can reuse the same per-clause
    binding as the within-clause pass, instead of re-deriving it)."""

    def __init__(self, clause: str, has_cache_wave: bool) -> None:
        self.clause = clause
        self.firsts = list(_FIRST_CUE_RE.finditer(clause))
        self.thens = list(_THEN_CUE_RE.finditer(clause))
        mentions = (
            _clause_engine_mentions(clause, has_cache_wave) if (self.firsts or self.thens) else []
        )
        self.bound_firsts = [
            (m.start(), engine)
            for m in self.firsts
            if (engine := _nearest_bound_engine(clause, m.start(), m.end(), mentions, before=True))
        ]
        self.bound_thens = [
            (m.start(), engine)
            for m in self.thens
            if (engine := _nearest_bound_engine(clause, m.start(), m.end(), mentions, before=False))
        ]
        self._mentions = mentions

    def ends_with_bound_first(self) -> tuple[int, str] | None:
        """The last "first" cue's bound engine, when nothing but the cue itself
        trails it in this clause -- or ``None``."""
        if not self.firsts:
            return None
        last = self.firsts[-1]
        if self.clause[last.end() :].split():  # words remain after the cue
            return None
        engine = _nearest_bound_engine(
            self.clause, last.start(), last.end(), self._mentions, before=True
        )
        return (last.start(), engine) if engine else None

    def starts_with_bound_then(self) -> tuple[int, str] | None:
        """The first "then" cue's bound engine, when it is within the usual word gap
        of this clause's start -- or ``None``."""
        if not self.thens:
            return None
        first = self.thens[0]
        if len(self.clause[: first.start()].split()) > _MAX_CUE_GAP_WORDS:
            return None
        engine = _nearest_bound_engine(
            self.clause, first.start(), first.end(), self._mentions, before=False
        )
        return (first.start(), engine) if engine else None


def _wave_order_finding(earlier: str, later: str, wave_of: dict[str, int], sentence: str) -> dict:
    return {
        "table": None,
        "text": f"{display_name(earlier)} ... {display_name(later)}",
        "engines": [earlier, later],
        "high_confidence": True,
        "sentence": sentence,
        "message": (
            f"[high confidence] Summary states {display_name(earlier)} before "
            f"{display_name(later)}, but the migration waves move {display_name(later)} "
            f"first (wave {wave_of[later]}) and {display_name(earlier)} later "
            f"(wave {wave_of[earlier]}): {sentence}"
        ),
    }


def check_summary_wave_order(summary: str, migration_waves: list[dict] | None) -> list[dict]:
    """Find a summary clause whose stated migration order contradicts ``migration_waves``.

    ``migration_waves`` is synthesis's own deterministic roadmap (one wave per step, each
    carrying ``wave`` (its 1-based position) and ``engines`` (the engine(s) it moves)). A
    clause that binds "first" or "then" to an engine (see the module comment above for
    exactly how narrow that binding is) is checked against every other cue-bound engine
    later in the clause -- or, when one clause ends with a bound "first" and the very
    next one starts with a bound "then", in that next clause too -- and the earlier one
    must have an earlier (or equal) wave number than the later one. ``display_name``
    renders every engine in the message, never a raw id.

    Returns findings shaped like :func:`check_summary_grounding`'s own (``table`` is
    always ``None``; ``high_confidence`` is always ``True`` -- the wave order is a fact,
    not a judgement call, so there is no low-stakes reading of stating it backwards). An
    empty list means the summary states nothing this check can bind an order from, or the
    order it states agrees with the waves -- when in doubt, this check does not reject.
    """
    if not summary or not migration_waves:
        return []
    wave_of: dict[str, int] = {}
    for wave in migration_waves:
        n = wave.get("wave")
        if not isinstance(n, int):
            continue
        for engine in wave.get("engines") or []:
            wave_of.setdefault(engine, n)
    if len(wave_of) < 2:
        return []
    has_cache_wave = "elasticache" in wave_of

    def ordered_pair_is_wrong(earlier: str, later: str) -> bool:
        return (
            earlier in wave_of
            and later in wave_of
            and earlier != later
            and wave_of[earlier] > wave_of[later]
        )

    findings: list[dict] = []
    for sentence in split_sentences(summary):
        clauses = [
            _WaveOrderClause(clause, has_cache_wave)
            for clause in _WAVE_CLAUSE_SPLIT.split(sentence)
        ]
        for clause in clauses:
            # Every cue-bound engine, in the order its cue appears in the clause --
            # "X first, then Y, then Z" chains into X, Y, Z, so Y stated ahead of Z is
            # just as wrong as X stated ahead of Y, even though neither Y nor Z is
            # itself bound to "first". Unlike the engine mentions ``check_summary_
            # grounding`` reads, every entry here is already cue-bound (#393 review),
            # so adding "then"-to-"then" pairs does not reintroduce the original,
            # unbound "any two engines in the sentence" false positives.
            bound = sorted(clause.bound_firsts + clause.bound_thens, key=lambda b: b[0])
            for i, (first_pos, earlier) in enumerate(bound):
                for then_pos, later in bound[i + 1 :]:
                    if then_pos <= first_pos or not ordered_pair_is_wrong(earlier, later):
                        continue
                    findings.append(_wave_order_finding(earlier, later, wave_of, sentence))
        # The one cross-boundary case (#393 review round 2): a clause ending with a
        # bound "first" paired with the very next clause starting with a bound "then".
        for cur, nxt in zip(clauses, clauses[1:], strict=False):
            first = cur.ends_with_bound_first()
            then = nxt.starts_with_bound_then()
            if not first or not then:
                continue
            _, earlier = first
            _, later = then
            if not ordered_pair_is_wrong(earlier, later):
                continue
            findings.append(_wave_order_finding(earlier, later, wave_of, sentence))
    return findings


# ---------------------------------------------------------------------------
# Customer-facing fallback summary
# ---------------------------------------------------------------------------

_MAX_GROUPS_IN_SUMMARY = 3


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_fallback_summary(effective_architecture: dict | None) -> str | None:
    """Short templated narrative used when the LLM summary is rejected.

    Built only from ``effective_architecture``: display engine names, each engine's
    workload share and busiest query groups, and the engines the reality check
    consolidated away. No cost figures, no confidence scores, no table-mapping counts.
    Returns None when there is nothing to describe.
    """
    engines = [e for e in (effective_architecture or {}).get("engines", []) if e.get("engine")]
    if not engines:
        return None
    engines = sorted(engines, key=lambda e: e.get("assigned_queries", 0), reverse=True)
    names = [e.get("display_name") or display_name(e["engine"]) for e in engines]
    if len(names) == 1:
        parts = [f"The target architecture runs on {names[0]}."]
    else:
        parts = [
            f"The target architecture combines {_join(names)}, each serving the queries it fits best."
        ]
    for entry, name in zip(engines, names, strict=True):
        queries = entry.get("assigned_queries")
        if entry.get("role") == "cache_layer":
            cached = entry.get("cached_queries") or 0
            if cached:
                parts.append(
                    f"{name} caches {cached} hot {'read' if cached == 1 else 'reads'} "
                    f"({entry.get('cached_call_share_percent', 0)}% of calls) in front of "
                    "the engines that own them."
                )
            continue
        if queries:
            share = entry.get("workload_percent")
            load = f"{queries} queries" + (f" ({share}% of the workload)" if share else "")
        else:
            tables = len(entry.get("tables") or [])
            if not tables:
                continue
            load = f"{tables} source table{'s' if tables != 1 else ''}"
        groups = [g for g in entry.get("top_query_groups") or [] if g and g != "ungrouped"]
        led = f", led by {_join(groups[:_MAX_GROUPS_IN_SUMMARY])}" if groups else ""
        parts.append(f"{name} serves {load}{led}.")
    moved = [
        f"{display_name(e['engine'])} into {display_name(e['absorbed_by'])}"
        for e in (effective_architecture or {}).get("eliminated_engines", [])
        if e.get("absorbed_by")
    ]
    if moved:
        parts.append(
            f"The reality check consolidated {_join(moved)}, so "
            + ("it does" if len(moved) == 1 else "they do")
            + " not need a separate deployment."
        )
    return " ".join(parts)
