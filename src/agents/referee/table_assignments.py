"""Table-level assignments derived from the per-query routing, and the retained Aurora engine."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.referee.aurora_choice import (
    AURORA_ENGINES,
    pick_aurora_engine,
    source_database_engine,
)
from src.agents.referee.table_resolution import PSEUDO_TABLES, TableNameResolver
from src.agents.referee.tie_break import break_tie
from src.agents.referee.tie_break import settled_counts as settled_workload_counts
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.contracts.assignment_models import (
    AuroraEngineChoice,
    QueryAssignment,
    TableAssignment,
    UnresolvedNames,
)
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import NON_OWNER_ENGINES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table assignment derivation (Task 4.3)
# ---------------------------------------------------------------------------


def retained_engine_for(
    collector_output: Mapping,
    query_assignments: Iterable[Mapping | QueryAssignment] | None = None,
    aurora_engine_choice: str | Mapping | AuroraEngineChoice | None = None,
) -> str | None:
    """The retained source-compatible Aurora engine for ``collector_output`` (#317).

    The Aurora engine matching the source database's dialect (``aurora_mysql``
    for a MySQL source, ``aurora_postgresql`` for Postgres) — never a member of
    ``NON_OWNER_ENGINES``. ``None`` when the source engine is missing or
    unrecognized and neither ``aurora_engine_choice`` nor ``query_assignments``
    is given. Every caller that recomputes ``table_assignments`` from a
    ``collector_output`` should pass this through so a table with no durable
    owner among its own engines never keeps a cache or search engine as
    ``primary_engine``.

    ``aurora_engine_choice`` (#381 review) is the authoritative
    ``Assignment.aurora_engine_choice`` -- as the engine string itself, the
    ``AuroraEngineChoice`` model, or its ``model_dump()``/raw-JSON dict -- when
    the caller has it, for a source with no Aurora dialect of its own (SQL
    Server, Oracle, DB2 — no entry in ``SOURCE_ENGINE_TO_AURORA``). Preferred
    over reconstructing the choice from ``query_assignments``: a customer
    override that moves one query onto the *losing* Aurora engine would make
    the reconstruction see both engines in use and resolve the ambiguity by
    alphabetical sort, not by the resolver's actual decision.

    ``query_assignments`` (#381), when given and ``aurora_engine_choice`` is
    not, recovers the same Aurora engine from the current assignment's own
    routing instead: by the time an assignment exists, the resolver's own
    choice (``choose_heterogeneous_engine``) already dropped the losing engine
    from scoring, so at most one of ``aurora_mysql``/``aurora_postgresql``
    ever appears as an ``assigned_engine`` -- unless a later customer override
    changed that, which is exactly why ``aurora_engine_choice`` is preferred
    when available.

    When ``query_assignments`` itself shows BOTH Aurora engines genuinely in
    use (a legacy assignment written before the #381 choice existed, or a
    later customer override) with no recorded choice to break it, that is a
    real, unresolved tie -- never a default -- resolved through the one
    shared ADR-031 Decision 2/12 placeholder (:func:`~src.agents.referee.
    tie_break.break_tie` with :func:`~src.agents.referee.tie_break.
    settled_counts`), not by sorting the two engine names (#583/#586 review).

    This is the single place "which Aurora engine is retained" is decided
    for a heterogeneous source with no Aurora dialect of its own --
    ``migration_waves.build_migration_waves`` and ``synthesis_report.
    _source_compatible_aurora_engine`` both call this instead of
    re-deriving their own answer, so wave 1, the architecture section and
    the cache-layer "Why" cell cannot name different engines for the same
    assignment.
    """
    native = SOURCE_ENGINE_TO_AURORA.get(source_database_engine(collector_output))
    if native:
        return native
    if aurora_engine_choice is not None:
        engine: str | None
        if isinstance(aurora_engine_choice, str):
            engine = aurora_engine_choice
        elif isinstance(aurora_engine_choice, Mapping):
            engine = aurora_engine_choice.get("engine")
        else:
            engine = getattr(aurora_engine_choice, "engine", None)
        if engine in AURORA_ENGINES:
            return engine
    if not query_assignments:
        return None
    query_assignments = list(query_assignments)
    used = {
        (qa.get("assigned_engine") if isinstance(qa, Mapping) else qa.assigned_engine)
        for qa in query_assignments
    }
    tied = sorted(AURORA_ENGINES.intersection(used))
    if len(tied) <= 1:
        return tied[0] if tied else None
    # A stable tie key: every query id that actually contributed to ``used``
    # (sorted, joined), the same "sorted member ids" pattern ADR-031
    # Decision 12 already uses for a co-dependency group -- deterministic
    # for this input, so the same ambiguous data always resolves the same
    # way, without forcing every different ambiguous run to the same answer.
    contributing_ids = sorted(
        str(qa.get("query_id") if isinstance(qa, Mapping) else qa.query_id)
        for qa in query_assignments
        if (qa.get("assigned_engine") if isinstance(qa, Mapping) else qa.assigned_engine) in tied
        and (qa.get("query_id") if isinstance(qa, Mapping) else qa.query_id)
    )
    query_key = ",".join(contributing_ids) or "retained_engine_for:no_query_ids"
    winner, _tie_kind = break_tie(
        tied,
        set(),
        None,
        settled_counts=settled_workload_counts(query_assignments),
        query_id=query_key,
    )
    return winner


def derive_table_assignments(
    query_assignments: list[QueryAssignment],
    *,
    known_tables: TableNameResolver | None = None,
    retained_engine: str | None = None,
    write_groups: Mapping[str, Iterable[str]] | None = None,
) -> tuple[list[TableAssignment], UnresolvedNames]:
    """Derive table-level assignments from query assignments.

    For each table:
    - Find all engines with assigned queries referencing it
    - Set primary_engine to the owner engine with most assigned queries for
      that table — never a cache or search/read-model engine (#317): among a
      table's engines, ElastiCache and OpenSearch (``NON_OWNER_ENGINES``) are
      skipped, and the durable owner engine (Aurora, DynamoDB or DocumentDB)
      with the most queries wins. An exact tie goes to ``retained_engine``
      (the least migration), then to the engine name, so the pick never
      depends on dict insertion order. If the table has no owner engine at
      all (every assigned query went to a cache or search engine),
      ``primary_engine`` falls back to ``retained_engine``, then to an Aurora
      engine used anywhere in ``query_assignments``, then to the busiest
      owner engine used anywhere in ``query_assignments`` — never a member of
      ``NON_OWNER_ENGINES``, even with no ``retained_engine`` and no owner
      engine on the table itself.
    - Set multi_engine_reason when engines list has 2+ entries

    ``known_tables`` (:class:`.table_resolution.TableNameResolver`, built via
    ``TableNameResolver.from_collector``), when given, resolves each
    ``source_tables`` entry to the collector's canonical ``table_id``/
    ``view_id``. An entry that does not resolve is not a table at all -- a
    CTE alias, a system catalog, a sequence, a keyword or a column the SQL
    parser mistook for a table (#316) -- and is dropped before building
    ``table_assignments`` rather than counted as one; the second return value
    records what was dropped (minus ``PSEUDO_TABLES``, a parser or dialect
    artifact, never a real table, so never worth reporting as unresolved).
    ``known_tables=None`` (no collector schema available) keeps every name,
    the behavior before #316.

    A name that resolves canonically but is spelled differently across
    queries (``wp_posts`` in one, ``wordpress.wp_posts`` in another) is
    merged into a single row under the canonical ``table_id`` — never split
    into two tables.

    ``write_groups`` (``Assignment.write_groups``, #992), when given, sets each
    written table's ``source_of_truth``, the one engine its writes land on
    (ADR-031 Amendment 9), and its ``replica_engines``, the table's other
    engines: they serve its reads as replicas fed by CDC from the source of
    truth. A table whose writes still sit on two engines (an artifact written
    before the rule) has no source of truth.

    Requirements: 2.6, 11.1, 11.2
    """
    # table_id → engine → count of queries
    table_engine_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # table_id → total query count
    table_query_counts: dict[str, int] = defaultdict(int)
    unresolved: set[str] = set()

    for qa in query_assignments:
        for table_id in qa.source_tables:
            if table_id in PSEUDO_TABLES:
                continue
            resolved = table_id if known_tables is None else known_tables.resolve(table_id)
            if resolved is None:
                unresolved.add(table_id)
                continue
            table_engine_counts[resolved][qa.assigned_engine] += 1
            table_query_counts[resolved] += 1

    # Global, assignment-wide fallback (#317 finding 3): used only when a table
    # has no owner engine among its own engines *and* no retained_engine was
    # given. Prefers an Aurora engine used anywhere in the assignment (the
    # retained relational core), then the busiest owner engine used anywhere,
    # so the result is never a cache or search engine even in that corner case.
    global_counts = Counter(qa.assigned_engine for qa in query_assignments if qa.assigned_engine)
    global_owner_counts = {e: c for e, c in global_counts.items() if e not in NON_OWNER_ENGINES}
    # #550 review (Critical 2): this function has no live resolver snapshot to
    # reuse (it runs on the finished ``query_assignments``, after the resolver's
    # own Step 6b already resolved every tie), so it computes the same settled
    # count ``tie_break.settled_counts`` defines -- a query's own ``tied_engines``
    # empty -- directly from ``query_assignments``, never a raw recount that
    # would fold placeholder picks back in. ``retained_engine`` (Important 4) is
    # passed as ``prior_choice``, not ``source_engine``: it is already resolved
    # to an Aurora engine name (``aurora_mysql``/``aurora_postgresql``), never
    # the raw source dialect ``pick_aurora_engine``'s own dialect lookup needs,
    # so it is ``prior_choice`` -- "the engine already recorded" -- that
    # actually makes a homogeneous source's own dialect win this fallback too,
    # not left to the tie-break by omission.
    settled_global_owner_counts = {
        e: c
        for e, c in settled_workload_counts(query_assignments).items()
        if e not in NON_OWNER_ENGINES
    }
    global_fallback = pick_aurora_engine(
        global_owner_counts,
        query_counts=settled_global_owner_counts,
        prior_choice=retained_engine,
        tie_key=",".join(sorted({qa.query_id for qa in query_assignments})),
    ) or (
        min(global_owner_counts, key=lambda e: (-global_owner_counts[e], e))
        if global_owner_counts
        else None
    )
    if global_fallback is None:
        logger.warning(
            "derive_table_assignments: no retained engine, and no owner engine anywhere in "
            "the assignment; falling back to the generic 'aurora' placeholder as primary_engine "
            "for a table with no owner engine of its own."
        )
        global_fallback = "aurora"

    engine_of = {qa.query_id: qa.assigned_engine for qa in query_assignments}
    write_engines: dict[str, set[str]] = defaultdict(set)
    for table, members in (write_groups or {}).items():
        resolved = table if known_tables is None else known_tables.resolve(table)
        if resolved is None:
            continue
        write_engines[resolved].update(engine_of[q] for q in members if engine_of.get(q))

    result: list[TableAssignment] = []
    for table_id in sorted(table_engine_counts.keys()):
        engine_counts = table_engine_counts[table_id]
        engines = sorted(engine_counts.keys())
        owner_counts = {e: c for e, c in engine_counts.items() if e not in NON_OWNER_ENGINES}
        if owner_counts:
            primary_engine = min(
                owner_counts, key=lambda e: (-owner_counts[e], e != retained_engine, e)
            )
        elif retained_engine:
            primary_engine = retained_engine
        else:
            primary_engine = global_fallback

        multi_engine_reason = None
        if len(engines) >= 2:
            engine_names = ", ".join(display_engine(e) for e in engines)
            multi_engine_reason = (
                f"Table {table_id} has queries assigned to multiple engines: {engine_names}"
            )

        result.append(
            TableAssignment(
                table_id=table_id,
                primary_engine=primary_engine,
                engines=engines,
                query_count=table_query_counts[table_id],
                multi_engine_reason=multi_engine_reason,
                **_source_of_truth(write_engines.get(table_id), engines),
            )
        )

    sorted_unresolved = sorted(unresolved)
    return result, UnresolvedNames(count=len(sorted_unresolved), names=sorted_unresolved)


def _source_of_truth(writers: set[str] | None, engines: list[str]) -> dict[str, Any]:
    """A table's ``write_engines``, ``source_of_truth`` and ``replica_engines``
    (#992): the engines its writes land on and, when that is one engine, it and
    the other engines its queries run on. Nothing when no query writes it."""
    if not writers:
        return {}
    if len(writers) != 1:
        return {"write_engines": sorted(writers)}
    (owner,) = writers
    return {
        "write_engines": [owner],
        "source_of_truth": owner,
        "replica_engines": [e for e in engines if e != owner],
    }
