"""Pick one Aurora engine when both Aurora engines are candidates (#288, #381).

Shared by assignment resolution and the Reality Check correction passes so they
agree. The engine matching the source database's dialect wins (a MySQL source
goes to Aurora MySQL). For a source with no Aurora dialect of its own (SQL
Server, Oracle, DB2 -- ``triage.HETEROGENEOUS_SOURCE_ENGINES``), both Aurora
engines compete on the total adjusted score the assignment resolver computed
for every query (:func:`choose_heterogeneous_engine`), tipped by source
features within a close margin, Aurora PostgreSQL on an exact tie (#381; a
maintainer rule since 2026-10-14, ADR-031 Decision 6, #656).
Everywhere else -- a caller with no score totals, resolving a tie after the
#381 choice already dropped the losing engine from scoring -- the engine
already carrying more of the recorded SETTLED workload wins; a genuine tie
(including 0 vs 0, no settled workload recorded at all) goes through the
shared ADR-031 tie-break (:func:`src.agents.referee.tie_break.break_tie`),
the same rule set Reality Check's placement uses, hashed together with
a caller-supplied ``tie_key`` -- never a constant seed, and never a bare name
preference (#550). :func:`pick_aurora_engine_with_tie` is the same decision,
for a caller that also needs the tie kind, to record on the query. Never the
first element of a set: set order depends on PYTHONHASHSEED.

Within the margin, every signal this module tracks today (``PRIMARY_FEATURE_
SIGNALS`` and the Babelfish last resort) favours Aurora PostgreSQL -- there is
no SQL Server (or Oracle, or DB2) feature in the pool that would ever tip a
close call toward Aurora MySQL. This is one-sided by design, not an oversight:
no dialect-aware scoring exists yet to weigh a feature the *other* way for a
source whose own dialect leans MySQL-compatible. #445 tracks adding it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.agents.referee.tie_break import break_tie
from src.agents.referee.triage import BASELINE, SOURCE_ENGINE_TO_AURORA
from src.agents.referee.utility_statements import has_sequence_reference
from src.shared.engine_names import display_engine
from src.tools.sql.query_facts import query_facts, scan_sql

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
# at all) goes to Aurora PostgreSQL (by feature, else the maintainer rule below).
# This is ``choose_heterogeneous_engine``'s own, unchanged preference (out of scope for #550, which only fixed the OTHER, no-score
# fallback below, in ``pick_aurora_engine``, to go through the shared ADR-031
# tie-break instead of a bare name preference) -- it is not "the same as" that
# fallback's default any more, now that the fallback has none of its own.
MARGIN_RATIO = 0.05

# ADR-031 Decision 6 amendment (maintainer decision, 2026-10-14, #656): when the two
# Aurora engines tie EXACTLY on total adjusted score for a heterogeneous source,
# Aurora PostgreSQL wins. This is a stated maintainer rule, not a score or a feature,
# and the trace says so. It covers only exact equality (including 0 vs 0) with no
# deciding feature; a close call inside ``MARGIN_RATIO`` is still decided by features,
# then the higher total.
# It is the LAST check: per-query scores (which already include per-operation
# support, through the scorer's hard capability gate), the margin band and the
# workload/source features all run first, so it fires only when they all leave an
# exact tie -- expected to be rare. The chosen engine is NOT source-compatible for
# a heterogeneous source (ADR-031 Decisions 2 and 13 stay inapplicable): whether it
# can serve a query depends on that query's own operations, not on this choice.
EXACT_TIE_RULE_REASON = (
    f"tie between {display_engine('aurora_mysql')} and {display_engine('aurora_postgresql')}; "
    "PostgreSQL preferred on a tie by maintainer rule, ADR-031 Decision 6"
)

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
# (``migration_wave_builders._aurora_wave``), this module's own ``reason`` trace -- names the
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


def detect_heterogeneous_features(
    collector_output: Mapping, source_engine: str | None
) -> dict[str, bool]:
    """Source features the collector actually reports, for the #381 feature tie-break.

    Every key is a reported signal, never an inference from missing data: query-text
    patterns (sequences, recursive CTEs, ``MERGE``, JSON functions -- each query's text
    is scanned by :func:`~src.tools.sql.query_facts.scan_sql` first, #381 review, and
    each feature is an engine-neutral query fact, #491, so a keyword mentioned only in
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
    query_texts = [str(q.get("query_text") or "") for q in queries]
    facts = [query_facts(t) for t in query_texts]
    schema_names = {t.get("schema_name") for t in tables if t.get("schema_name")}
    json_columns = any(
        (c.get("data_type") or "").lower() in ("json", "jsonb")
        for t in tables
        for c in (t.get("columns") or [])
    )
    return {
        # shared, #654
        "sequences": any(has_sequence_reference(scan_sql(t)) for t in query_texts),
        "recursive_ctes": any(f.self_referencing_recursive_cte for f in facts),
        "merge_statements": any(f.merge_statement for f in facts),
        "json_usage": json_columns or any(f.json_function_call for f in facts),
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
    favouring Aurora PostgreSQL, and that feature decides. Per-operation support is
    already inside ``totals``: the scorer's hard capability gate scores an engine 0 on
    a query it cannot serve. Only when the totals tie exactly AND no feature decides
    does Aurora PostgreSQL win by the maintainer rule (``EXACT_TIE_RULE_REASON``,
    ADR-031 Decision 6 amendment, 2026-10-14) -- the final tie-break, after every other
    check. A close-but-not-exact call with no feature still goes to the higher total.
    Returns ``(winner, trace)``, where ``trace`` has
    the rounded totals, the margin (PostgreSQL's total minus MySQL's), the deciding
    features and a one-line human-readable reason -- everything
    :class:`src.contracts.assignment_models.AuroraEngineChoice` records. The reason never
    claims a feature-driven win "earned" the move when it was really a tie-break or an
    unneeded default (#381 review) -- callers (``migration_wave_builders._aurora_wave``) should
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
        # Every feature in the pool favours Aurora PostgreSQL today (#445), so the
        # winner is the same either way; what differs is WHAT decided it. A feature
        # is checked first (ADR-031 Decision 6 amendment ordering), and only an
        # exact tie that no feature breaks falls to the maintainer rule.
        winner = "aurora_postgresql"
        deciding_features = active
        if active:
            reason = (
                f"exact tie on total adjusted score ({pg_total:.1f} each); "
                f"tipped by: {describe_features(active)}"
            )
        else:
            reason = (
                f"{EXACT_TIE_RULE_REASON} (exact tie on total adjusted score, "
                f"{pg_total:.1f} each, and no deciding feature)"
            )
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


def fallback_aurora_engine(source_engine: str | None) -> str:
    """The engine a query falls back to when no engine in play can serve it (#786).

    The one rule both "no serviceable engine" fallbacks use: the first
    assignment's Aurora fallback once no Aurora engine is in its pool
    (``assignment_resolver._resolve_aurora_fallback``) and the cache owner
    fallback (``cache_overlay.owner_decision``). It is the source-compatible
    Aurora engine when the source has one (ADR-031 Decision 2 step 1: a
    homogeneous source always has its matching relational engine as a
    candidate), else the generic ``aurora`` placeholder: a heterogeneous
    source has no source-compatible Aurora engine (Decision 6), and an
    unknown source names none.
    """
    return SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower()) or BASELINE


def pick_aurora_engine_with_tie(
    candidates: Iterable[str],
    source_engine: str | None = None,
    query_counts: Mapping[str, int] | None = None,
    *,
    score_totals: Mapping[str, float] | None = None,
    features: Mapping[str, bool] | None = None,
    prior_choice: str | None = None,
    tie_key: str = "",
) -> tuple[str | None, str | None]:
    """:func:`pick_aurora_engine`'s decision, plus the ``tie_kind`` a caller
    needs to record ``QueryAssignment.tied_engines``/``routing_trace.record_tie``
    when the pick stayed unresolved (#550 review, Important 3).

    ``tie_kind`` is ``None`` whenever nothing here needed a tie-break at all
    -- a single candidate, the source-compatible dialect match, the #381
    scored decision, or a live ``prior_choice`` all decide the engine
    outright, with no ADR-031 Decision 12 fact to report. It is whatever
    :func:`src.agents.referee.tie_break.break_tie` itself returns only on
    the final, no-score fallback below -- in practice always
    ``"tie_unresolved"`` here, since this function's own source-compatible
    check already returned before ``break_tie`` ever runs (so its
    ``"source_compatible"`` tie kind can never come from here), and there is
    no "current engine for this decision" concept at this generic call site
    for ``break_tie``'s stability rule (``"stability"``) to ever fire either.

    ``tie_key`` (#550 review) is the stable identifier the no-score
    fallback's tie-break hashes together with each candidate engine's name --
    the sorted, comma-joined ids of the queries THIS call is deciding for,
    the same way ``assignment_resolver``'s Step 6b builds its own
    ``hash_seed``. The default ``""`` reproduces the exact bug #550 fixed: a
    constant seed makes the hash a comparison BY the engine's name (every
    caller that leaves it unset gets the same winner, on every workload),
    not of it, together with something that actually varies -- so every
    caller that reaches the fallback (not the single-candidate, dialect-match
    or scored branches, which never consult it) must pass one of its own.
    """
    pool = sorted(AURORA_ENGINES.intersection(candidates))
    if not pool:
        return None, None
    matched = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())
    if matched in pool:
        return matched, None
    if len(pool) == 1:
        return pool[0], None
    if score_totals:
        winner, _ = choose_heterogeneous_engine(pool, score_totals, features)
        return winner, None
    counts = query_counts or {}
    if prior_choice in pool and not any(counts.get(e) for e in pool):
        return prior_choice, None
    # #550: no score and no live prior choice -- this is exactly the kind of
    # tie ADR-031 Decision 2 governs, so it goes through the one shared
    # tie-break (``tie_break.break_tie``), not a module-local comparison.
    # ``matched`` is already ``None`` or not in ``pool`` by this point (the
    # source-compatible branch above already returned), and there is no
    # "current engine for this decision" concept at this generic call site,
    # so rule 3 -- the settled-workload-share placeholder ADR-031 Decision 12
    # approved -- is what actually decides: the engine already carrying more
    # of the recorded workload (``counts``, expected to be a genuine settled
    # count -- see ``tie_break.settled_counts`` -- never a raw recount that
    # includes placeholder picks) wins; a genuine tie (including 0 vs 0)
    # falls through to the placeholder's deterministic hash of ``tie_key``
    # and the engine's name, never a bare name preference.
    return break_tie(pool, set(), matched, settled_counts=counts, query_id=tie_key)


def pick_aurora_engine(
    candidates: Iterable[str],
    source_engine: str | None = None,
    query_counts: Mapping[str, int] | None = None,
    *,
    score_totals: Mapping[str, float] | None = None,
    features: Mapping[str, bool] | None = None,
    prior_choice: str | None = None,
    tie_key: str = "",
) -> str | None:
    """The Aurora engine to use among ``candidates``, or ``None`` if there is none.

    ``source_engine`` is the source database engine (``"mysql"``, ``"postgresql"``,
    ...); ``query_counts`` maps engine to the queries it already serves (a genuine
    settled count, per :func:`src.agents.referee.tie_break.settled_counts` --
    never a raw recount of every current assignment, which would feed
    placeholder ties and reality-check moves back into this decision).

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
    utility-statement pin running before any query has been assigned yet) -- so a
    live ``prior_choice`` never gets silently overridden by a 0-vs-0 non-decision.

    With no ``score_totals`` and no live ``prior_choice``, the remaining tie
    between the two Aurora engines goes through the shared ADR-031 tie-break
    (:func:`src.agents.referee.tie_break.break_tie`, #550): ``counts`` stands in
    for its settled-workload-share placeholder, so the engine already carrying
    more of the recorded workload wins, and a genuine tie (including 0 vs 0)
    falls through to that placeholder's deterministic hash of ``tie_key`` and
    the engine's name -- never a bare engine-name preference. See ``tie_key``
    on :func:`pick_aurora_engine_with_tie` (this function's own default is the
    same, and the same caveat applies): a caller that can reach the fallback
    must pass a real one. Call :func:`pick_aurora_engine_with_tie` directly
    when the caller also needs to know whether the pick was an unresolved tie.
    """
    winner, _ = pick_aurora_engine_with_tie(
        candidates,
        source_engine,
        query_counts,
        score_totals=score_totals,
        features=features,
        prior_choice=prior_choice,
        tie_key=tie_key,
    )
    return winner
