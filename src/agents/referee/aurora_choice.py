"""Pick one Aurora engine when both Aurora engines are candidates (#288, #381).

Shared by assignment resolution and the Reality Check correction passes so they
agree. The engine matching the source database's dialect wins (a MySQL source
goes to Aurora MySQL). For a source with no Aurora dialect of its own (SQL
Server, Oracle, DB2 -- ``triage.HETEROGENEOUS_SOURCE_ENGINES``), both Aurora
engines compete on the total adjusted score the assignment resolver computed
for every query (:func:`choose_heterogeneous_engine`), tipped by source
features within a close margin, Aurora PostgreSQL on an exact tie (#381).
Everywhere else -- a caller with no score totals, resolving a tie after the
#381 choice already dropped the losing engine from scoring -- the engine with
more queries wins, Aurora PostgreSQL the tie-break, as Reality Check's
absorption pass does. Never the first element of a set: set order depends on
PYTHONHASHSEED.

Within the margin, every signal this module tracks today (``PRIMARY_FEATURE_
SIGNALS`` and the Babelfish last resort) favours Aurora PostgreSQL -- there is
no SQL Server (or Oracle, or DB2) feature in the pool that would ever tip a
close call toward Aurora MySQL. This is one-sided by design, not an oversight:
no dialect-aware scoring exists yet to weigh a feature the *other* way for a
source whose own dialect leans MySQL-compatible. #445 tracks adding it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

AURORA_ENGINES = frozenset({"aurora_postgresql", "aurora_mysql"})

# Calibration (#381): on the wordpress (MySQL) and discourse (PostgreSQL) deterministic
# samples, aurora_mysql and aurora_postgresql's table_recommendations confidence scores
# -- base and anti-pattern-adjusted -- came out byte-identical for every query (both
# engines share the same relational-need/complexity/performance/cost formula; the two
# samples' plain CRUD SQL never hits either engine's dialect-specific catalog pattern).
# No systematic bias to normalize away, and real heterogeneous workloads (SQL Server,
# Oracle) can be expected to land close to parity whenever they stay close to ANSI SQL
# too -- so the margin is generous: within 5% of the larger total counts as "close
# enough" for source features to decide, and an exact tie (including 0 vs 0, no score
# at all) goes to Aurora PostgreSQL, same as today's homogeneous tie-break default.
MARGIN_RATIO = 0.05

# Source features that tip a close Aurora MySQL vs Aurora PostgreSQL call (#381 design):
# recursive CTEs, sequences, MERGE and JSON all have more direct PostgreSQL equivalents
# than MySQL ones, and multiple schemas per database map onto PostgreSQL schemas (MySQL
# has none). These are workload-based: they come from the collected schema/queries
# themselves, so a close call for *any* heterogeneous source (SQL Server, Oracle, DB2)
# can be tipped by them. Every one of them favours aurora_postgresql today; a future
# engine added to the pool would need its own column in ``choose_heterogeneous_engine``,
# not just a new entry here.
PRIMARY_FEATURE_SIGNALS: tuple[str, ...] = (
    "sequences",
    "recursive_ctes",
    "merge_statements",
    "json_usage",
    "multiple_schemas",
)

# Babelfish (SQL Server wire-protocol compatibility) only exists for Aurora PostgreSQL,
# but unlike the signals above it is a property of the *source dialect*, not of the
# collected workload: every SQL Server source reports it as True regardless of what the
# workload actually does. Letting it sit in the same list as the workload-based signals
# would mean every close call for a SQL Server source resolves on dialect alone, never on
# the workload (#381 review) -- so it is kept out of ``PRIMARY_FEATURE_SIGNALS`` and only
# consulted by ``choose_heterogeneous_engine`` as the last-resort tie-break, when no
# workload-based feature decided it either way (see ``_tie_break_features`` below).
FEATURE_SIGNALS: tuple[str, ...] = (*PRIMARY_FEATURE_SIGNALS, "babelfish_compatible")

# Human-readable labels for FEATURE_SIGNALS (#381 review): any user-visible text that
# lists which feature(s) tipped a choice -- wave 1's rationale
# (``migration_waves._aurora_wave``), this module's own ``reason`` trace -- names the
# feature this way, never the bare internal identifier.
FEATURE_LABELS: dict[str, str] = {
    "sequences": "sequences",
    "recursive_ctes": "recursive CTEs",
    "merge_statements": "MERGE statements",
    "json_usage": "JSON usage",
    "multiple_schemas": "multiple schemas",
    "babelfish_compatible": "Babelfish compatibility",
}


def describe_features(features: Iterable[str]) -> str:
    """Comma-joined, human-readable labels for a list of ``FEATURE_SIGNALS`` keys."""
    return ", ".join(FEATURE_LABELS.get(f, f) for f in features)


# case_insensitive_collation (SQL Server's default collation) is in the agreed design
# but is not implemented: neither the SQL Server nor the Oracle collector script reports
# collation today, and no query-text signal substitutes for it. Left out rather than
# inferred from nothing (#381 follow-up: extend the SQL Server collector's metadata).

_SEQUENCE_RE = re.compile(r"\bnext\s+value\s+for\b|\.\s*nextval\b|\.\s*currval\b", re.IGNORECASE)
# Requires MERGE to be followed by an optional TOP(n) clause, then (optionally "INTO"
# then) the start of a target identifier -- the shape an actual MERGE statement takes
# -- not just the bare keyword "merge" (e.g. a column or variable literally named
# ``merge``) coincidentally followed by "using" somewhere within the next 80
# characters. The negative lookahead excludes a handful of SQL keywords that can
# legitimately follow a bare "merge" column reference (``SELECT merge FROM ...
# USING (...)``) but never follow the MERGE statement itself. ``[#\w]`` (#381 review
# round 2) only requires the *first* character of the target identifier to look like
# one -- a word character, or ``#`` for a local temp table (``MERGE #target ...``) --
# rather than the whole identifier, since by the time this runs ``_scan_sql`` has
# already turned a bracketed or double-quoted identifier into its bare name (``[dbo].
# [Target]`` -> ``dbo.Target``), and the ``.{0,80}\busing\b`` that follows is what
# actually confirms this is a MERGE statement, not the identifier shape.
_MERGE_RE = re.compile(
    r"\bmerge\s+(?:top\s*\([^)]*\)\s*)?(?:into\s+)?"
    r"(?!(?:from|where|select|group|order|having|join)\b)[#\w]"
    r".{0,80}\busing\b",
    re.IGNORECASE | re.DOTALL,
)
# Every function-shaped signal requires an opening paren, so a column literally named
# e.g. ``json_value`` (not a call to the ``JSON_VALUE(...)`` function) does not match.
# ``FOR JSON`` and ``IS JSON`` are not function calls -- SQL Server's ``FOR JSON PATH``
# clause and the ``IS JSON`` predicate take no parens -- so those two keep the plain
# keyword-boundary match.
_JSON_FUNCTION_RE = re.compile(
    r"\bjson_value\s*\(|\bjson_query\s*\(|\bopenjson\s*\(|\bfor\s+json\b|\bjson_table\s*\(|"
    r"\bis\s+json\b|\bjson_exists\s*\(",
    re.IGNORECASE,
)
# Matches a CTE's own name definition, whether it is the first one after ``WITH`` or a
# later one introduced by a comma (``WITH a AS (...), b AS (...)``) -- #381 review: the
# pre-review version only matched the first, so a recursive CTE named anywhere but first
# in the list went undetected. ``[#\w]+`` (#381 review round 2) rather than
# ``[a-zA-Z_]\w*``: by the time this runs, ``_scan_sql`` has already turned a bracketed
# or double-quoted CTE name (``[Tree]``, ``"Tree"``) into its bare identifier, so the
# name itself is always a plain word here regardless of how the query originally quoted
# it.
_CTE_NAME_RE = re.compile(r"(?:\bwith\b|,)\s*([#\w]+)\s*(?:\([^)]*\))?\s+as\s*\(", re.IGNORECASE)


def _scan_sql(text: str) -> str:
    """One left-to-right pass over ``text`` neutralizing comments, strings and
    quoted identifiers, so every feature regex below runs against SQL as the engine
    would actually parse it, never raw source text (#381 review round 2).

    Two independent regex passes (strip strings, then strip comments) can each
    mis-handle what the other produces: a comment containing an apostrophe
    (``-- don't touch``) made the string-literal pass, if it ran first, swallow
    everything up to the *next* quote -- which could be an entire real statement
    later in the query. Scanning once, left to right, in the order these characters
    are actually encountered removes that ordering bug entirely.

    Each construct below is replaced the way the engine would resolve it, not simply
    deleted (so a real token on either side is never glued to its neighbour):

    - ``--`` to end of line: a space (the newline itself is kept, so later-line
      offsets are not shifted and a line comment never merges two lines).
    - ``/* ... */``: a space, with nesting support (SQL Server supports nested
      block comments) -- the whole span up to the matching outer ``*/`` is one
      comment, so text between an inner ``*/`` and the outer one is never
      mistaken for live code.
    - ``'...'`` string literals, with ``''``-doubled escapes and an optional
      ``N``/``n`` (Unicode) prefix: a space. Content is never a real keyword
      match, only text a customer or generator happened to write.
    - ``[ident]`` (T-SQL brackets, ``]]``-escaped) and ``"ident"`` (ANSI/Oracle/
      PostgreSQL double quotes, ``""``-escaped): the bare identifier, with the
      quoting removed. These name a column, table or CTE -- not a string -- so a
      feature regex must still be able to see and match the name inside
      (``MERGE [dbo].[Target]``, ``WITH "Tree" AS (...)``).

    Every other character passes through unchanged.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            out.append(" ")
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if text[i : i + 2] == "/*":
                    depth += 1
                    i += 2
                elif text[i : i + 2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        if c == "'" or (c in "Nn" and i + 1 < n and text[i + 1] == "'"):
            if c in "Nn":
                i += 1
            i += 1  # opening quote
            while i < n:
                if text[i] == "'" and i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                if text[i] == "'":
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if c == "[":
            j = i + 1
            body: list[str] = []
            while j < n:
                if text[j] == "]" and j + 1 < n and text[j + 1] == "]":
                    body.append("]")
                    j += 2
                    continue
                if text[j] == "]":
                    j += 1
                    break
                body.append(text[j])
                j += 1
            out.append("".join(body))
            i = j
            continue
        if c == '"':
            j = i + 1
            body = []
            while j < n:
                if text[j] == '"' and j + 1 < n and text[j + 1] == '"':
                    body.append('"')
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                body.append(text[j])
                j += 1
            out.append("".join(body))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _has_recursive_cte(text: str) -> bool:
    """A CTE whose own body both ``UNION``s and refers back to its own name.

    SQL Server and Oracle have no ``WITH RECURSIVE`` keyword (unlike PostgreSQL and
    MySQL 8): any ``name AS (...)`` CTE can be recursive if its body unions the anchor
    member with one that references ``name`` again -- checked for every CTE in the
    ``WITH`` list, not only the first (#381 review). Requiring both a self-reference and
    a ``UNION`` inside the CTE's own (paren-balanced) body avoids counting a CTE that is
    merely referenced again later in the main query -- normal, non-recursive CTE use.
    """
    if re.search(r"\bwith\s+recursive\b", text, re.IGNORECASE):
        return True
    for m in _CTE_NAME_RE.finditer(text):
        name = m.group(1)
        depth = 1
        i = m.end()
        start = i
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        body = text[start:i]
        if re.search(r"\bunion\b", body, re.IGNORECASE) and re.search(
            rf"\b{re.escape(name)}\b", body, re.IGNORECASE
        ):
            return True
    return False


def detect_heterogeneous_features(
    collector_output: Mapping, source_engine: str | None
) -> dict[str, bool]:
    """Source features the collector actually reports, for the #381 feature tie-break.

    Every key is a reported signal, never an inference from missing data: query-text
    patterns (sequences, recursive CTEs, ``MERGE``, JSON functions -- each query's text
    is scanned by :func:`_scan_sql` first, #381 review, so a keyword mentioned only in
    a comment or string literal is never counted, and a bracketed or double-quoted
    identifier is still visible under its bare name), the collected schema
    (JSON/JSONB columns, 2+ distinct ``schema_name`` values -- real per-table schemas:
    the offline collector path only started reporting the actual schema, rather than
    the database name for every table, once #381 fixed it in ``mysql_collector.
    _build_tables``), and the source engine itself (Babelfish only applies to a SQL
    Server source).
    """
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    texts = [_scan_sql(str(q.get("query_text") or "")) for q in queries]
    schema_names = {t.get("schema_name") for t in tables if t.get("schema_name")}
    json_columns = any(
        (c.get("data_type") or "").lower() in ("json", "jsonb")
        for t in tables
        for c in (t.get("columns") or [])
    )
    return {
        "sequences": any(_SEQUENCE_RE.search(t) for t in texts),
        "recursive_ctes": any(_has_recursive_cte(t) for t in texts),
        "merge_statements": any(_MERGE_RE.search(t) for t in texts),
        "json_usage": json_columns or any(_JSON_FUNCTION_RE.search(t) for t in texts),
        "multiple_schemas": len(schema_names) > 1,
        "babelfish_compatible": (source_engine or "").lower() == "sqlserver",
    }


def source_database_engine(collector_output: Mapping) -> str:
    """The collector's source database engine (e.g. ``"mysql"``), lower-cased."""
    metadata = collector_output.get("metadata") or {}
    return str((metadata.get("source_database") or {}).get("engine") or "").lower()


def source_database_version(collector_output: Mapping) -> str | None:
    """The collector's source database version (e.g. ``"8.0.45"``), or ``None`` (#321).

    Used only to state what the migration-waves builder checked for the
    Aurora wave's homogeneity note -- never to infer a feature or extension
    gap the collector itself did not report.
    """
    metadata = collector_output.get("metadata") or {}
    version = (metadata.get("source_database") or {}).get("version")
    return str(version) if version else None


def _tie_break_features(features: Mapping[str, bool] | None) -> list[str]:
    """Features that tip (or reinforce) a close/tied call toward Aurora PostgreSQL.

    A workload-based feature (``PRIMARY_FEATURE_SIGNALS``) decides first: it comes
    directly from the collected schema/queries, not from the source engine alone.
    Babelfish compatibility (SQL Server only) is consulted only as a last resort, when
    no workload-based feature is present -- it is a property of the source *dialect*,
    reported ``True`` for every SQL Server source regardless of what the workload
    actually does, so letting it decide on equal footing with the workload-based
    signals would mean every close call for a SQL Server source resolves on dialect
    alone, never on the workload (#381 review).
    """
    active = features or {}
    primary = sorted(k for k, v in active.items() if v and k in PRIMARY_FEATURE_SIGNALS)
    if primary:
        return primary
    if active.get("babelfish_compatible"):
        return ["babelfish_compatible"]
    return []


def choose_heterogeneous_engine(
    pool: Sequence[str],
    totals: Mapping[str, float],
    features: Mapping[str, bool] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Decide between the two Aurora engines for a heterogeneous source (#381).

    ``totals`` is each engine's total adjusted score across every query (the resolver's
    ``scores``, summed). The higher total wins; within ``MARGIN_RATIO`` of the larger
    total -- including an exact tie -- ``_tie_break_features`` names any source feature
    favouring Aurora PostgreSQL. On an exact tie Aurora PostgreSQL already wins by
    default, so a feature there only reinforces the tie-break (it is still recorded as
    ``deciding_features``, #381 review, so the trace is never silent about a real signal
    just because the default already agreed with it); on a close-but-not-exact call it
    is what actually tips the winner. Returns ``(winner, trace)``, where ``trace`` has
    the rounded totals, the margin (PostgreSQL's total minus MySQL's), the deciding
    features and a one-line human-readable reason -- everything
    :class:`src.contracts.assignment_models.AuroraEngineChoice` records. The reason never
    claims a feature-driven win "earned" the move when it was really a tie-break or an
    unneeded default (#381 review) -- callers (``migration_waves._aurora_wave``) should
    phrase it the same way.

    ``pool`` with only one Aurora engine (every other caller downstream of the #381
    choice, once the losing engine has been dropped from scoring) returns that engine
    with an empty trace -- there is no competition left to describe.
    """
    engines = sorted(AURORA_ENGINES.intersection(pool))
    if not engines:
        raise ValueError("choose_heterogeneous_engine needs at least one Aurora engine in pool")
    if len(engines) == 1:
        engine = engines[0]
        return engine, {
            "totals": {engine: round(float(totals.get(engine, 0.0)), 2)},
            "margin": None,
            "deciding_features": [],
            "reason": "only one Aurora engine competed",
        }

    mysql_total = float(totals.get("aurora_mysql", 0.0))
    pg_total = float(totals.get("aurora_postgresql", 0.0))
    margin = pg_total - mysql_total
    larger = max(mysql_total, pg_total)
    is_close = larger <= 0 or abs(margin) <= MARGIN_RATIO * larger

    if mysql_total == pg_total:
        active = _tie_break_features(features)
        winner = "aurora_postgresql"
        deciding_features = active
        reason = (
            f"exact tie on total adjusted score ({pg_total:.1f} each); "
            "Aurora PostgreSQL tie-break"
        )
        if active:
            reason += f", also favoured by: {describe_features(active)}"
    elif is_close:
        active = _tie_break_features(features)
        if active:
            winner = "aurora_postgresql"
            deciding_features = active
            reason = (
                f"close on total adjusted score ({mysql_total:.1f} vs {pg_total:.1f}); "
                f"tipped by: {describe_features(active)}"
            )
        else:
            deciding_features = []
            winner = "aurora_mysql" if mysql_total > pg_total else "aurora_postgresql"
            reason = (
                "close on total adjusted score with no deciding feature -- higher total "
                f"wins ({mysql_total:.1f} vs {pg_total:.1f})"
            )
    else:
        deciding_features = []
        winner = "aurora_mysql" if mysql_total > pg_total else "aurora_postgresql"
        reason = f"higher total adjusted score wins ({mysql_total:.1f} vs {pg_total:.1f})"

    return winner, {
        "totals": {
            "aurora_mysql": round(mysql_total, 2),
            "aurora_postgresql": round(pg_total, 2),
        },
        "margin": round(margin, 2),
        "deciding_features": deciding_features,
        "reason": reason,
    }


def pick_aurora_engine(
    candidates: Iterable[str],
    source_engine: str | None = None,
    query_counts: Mapping[str, int] | None = None,
    *,
    score_totals: Mapping[str, float] | None = None,
    features: Mapping[str, bool] | None = None,
    prior_choice: str | None = None,
) -> str | None:
    """The Aurora engine to use among ``candidates``, or ``None`` if there is none.

    ``source_engine`` is the source database engine (``"mysql"``, ``"postgresql"``,
    ...); ``query_counts`` maps engine to the queries it already serves.

    ``score_totals`` (each engine's total adjusted score, #381) and ``features``
    (:func:`detect_heterogeneous_features`) make the same heterogeneous-source
    decision :func:`choose_heterogeneous_engine` makes, so a caller that already has
    them (the assignment resolver) gets the authoritative answer here too. Every other
    caller omits them: by the time Reality Check or a tie-break runs, the #381 choice
    already dropped the losing engine from scoring, so ``query_counts`` alone (0 for
    the loser, since it never served a query) agrees without needing to repeat the
    comparison.

    ``prior_choice`` (#381 review) is the engine already recorded on
    ``Assignment.aurora_engine_choice.engine``, when the caller has one. It is
    consulted only when every other signal is silent -- no ``score_totals``, and
    neither engine in ``pool`` has a nonzero ``query_counts`` entry (e.g. the
    utility-statement pin running before any query has been assigned yet) -- so the
    tie-break default (Aurora PostgreSQL) never silently overrides a choice the
    resolver already made and recorded.
    """
    pool = sorted(AURORA_ENGINES.intersection(candidates))
    if not pool:
        return None
    matched = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())
    if matched in pool:
        return matched
    if len(pool) == 1:
        return pool[0]
    if score_totals:
        winner, _ = choose_heterogeneous_engine(pool, score_totals, features)
        return winner
    counts = query_counts or {}
    if prior_choice in pool and not any(counts.get(e) for e in pool):
        return prior_choice
    return min(pool, key=lambda e: (-counts.get(e, 0), e != "aurora_postgresql"))
