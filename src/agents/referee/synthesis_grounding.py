"""Ground synthesis output in the effective (post-reality-check) architecture.

The reality check can eliminate an engine triage selected and fold its workload into
another engine (for example OpenSearch absorbed into Aurora MySQL). The per-engine
artifacts synthesis reads (analysis anti-patterns, schema-design unsupported patterns,
reality-check pattern suggestions) were written while that engine was still a
candidate, so their free text can still recommend it. This module makes sure the
customer-facing report only recommends engines that are part of the target.

Rule for risks (never dropped, severity never changed): a sentence *recommends* an
eliminated engine when the nearest cue in the three words before the engine is a
recommend cue ("with OpenSearch", "to OpenSearch", "use OpenSearch"), or, with no cue
there and no sentence-wide trade-off cue, when the engine is the proposed doer
("OpenSearch can handle ..."). A nearest trade-off cue ("without", "instead of",
"consolidating", "from") keeps it. Descriptions never lose a sentence: one that
recommends an eliminated engine gets a note that the engine is not part of the target.
Mitigations drop recommending sentences; an emptied mitigation is replaced by a re-plan
hint that names the absorbing engine only when the risk's queries are actually assigned
to it. Every rewritten risk carries a ``grounding_note``.
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
    r"^(?:consolidat\w*|absorb\w*|without|replacing|from|than)$", re.IGNORECASE
)
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
    """``"trade_off"``/``"recommend"`` for the nearest cue in the words before a mention."""
    words = re.findall(r"[A-Za-z]+", before)[-_CUE_WINDOW:]
    for i in range(len(words) - 1, -1, -1):
        word = words[i]
        if word.lower() == "of" and i > 0 and words[i - 1].lower() == "instead":
            return "trade_off"  # "instead of OpenSearch"
        if _TRADE_OFF_CUE.match(word):
            return "trade_off"
        if _RECOMMEND_CUE.match(word):
            return "recommend"
    return None


def _recommends(sentence: str, engines: Iterable[str]) -> bool:
    """True when ``sentence`` recommends one of ``engines``.

    Decided per mention by the nearest cue in the three words before it; with no cue
    there, a sentence-wide trade-off cue keeps it, else "X can/should handle ..."
    after the mention makes it a recommendation.
    """
    targets = set(engines)
    for engine, start, end in engine_mentions(sentence):
        if engine not in targets:
            continue
        cue = _mention_cue(sentence[:start])
        if cue == "recommend":
            return True
        if cue == "trade_off" or _SENTENCE_TRADE_OFF.search(sentence):
            continue
        if _RECOMMEND_AFTER.match(sentence[end:]):
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
    ``tables_accessed``). A table can belong to several engines. Names that are not
    known source tables (``unknown``, ``DUAL``) are ignored.

    Without an assignment (unversioned run) the schema designs stand in: every engine
    whose design covers a table (``table_mappings`` primary and alternatives).
    """
    known = set(known_tables)
    scope: dict[str, set[str]] = {}
    qas = (assignment or {}).get("query_assignments") or []
    if qas:
        accessed = {q.get("query_id"): q.get("tables_accessed") or [] for q in source_queries}
        for qa in qas:
            engine = qa.get("assigned_engine")
            if not engine or not qa.get("in_scope", True):
                continue
            tables = qa.get("source_tables") or accessed.get(qa.get("query_id"), [])
            scope.setdefault(engine, set()).update(t for t in tables if not known or t in known)
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
