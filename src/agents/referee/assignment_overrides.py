"""Shared write path for customer assignment overrides (ADR-028).

One place builds the ``customer_modified`` next version from a set of per-query
overrides, so the web REST route and the ATX review gate produce byte-identical
artifacts (same version bump, status, source, provenance) instead of each
re-implementing the write. This is transport-agnostic: it raises domain errors
that callers translate to their own protocol (HTTP status, chat message, ...).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from src.agents.referee.query_scoring import ScoringContext

from src.agents.referee.assignment_resolver import (
    build_co_dependency_groups,
    derive_table_assignments,
    retained_engine_for,
)
from src.agents.referee.assignment_validator import AssignmentValidator
from src.agents.referee.aurora_choice import AURORA_ENGINES, source_database_engine
from src.agents.referee.cache_overlay import (
    CACHE_OVERLAY_ENGINES,
    CUSTOMER_UNCACHE_REASON,
    available_cache_engine,
    can_own,
    is_locking_read,
    is_write_query,
    normalize_cache_owners,
    overlay_summary,
    owner_decision,
    owner_move_fields,
    pin_customer_cache,
    refresh_cache_overlay,
    write_heavy_tables,
)
from src.agents.referee.capability_registry import missing_capabilities
from src.agents.referee.engine_exclusions import check_exclusions
from src.agents.referee.move_ledger import (
    MoveLedger,
    apply_to_assignment,
    check_replay,
    group_key,
    ledger_for,
    state_of,
)
from src.agents.referee.query_scoring import GATE_KIND_WRITE
from src.agents.referee.table_resolution import TableNameResolver
from src.agents.referee.tie_break import settled_counts
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    CacheOverlaySummary,
    QueryAssignment,
    ValidationResult,
)
from src.contracts.move_ledger import MoveActor, MovePass, ReasonCode
from src.shared.capability_labels import capability_gap_phrase
from src.shared.engine_names import SOURCE_ENGINE_DISPLAY_NAMES, display_engine
from src.storage.assignment_versioning import (
    assignment_artifact_path,
    resolve_effective_assignment_version,
)


class _Store(Protocol):
    """The store surface this module needs (satisfied by every ArtifactStore)."""

    def read_json(self, path: str) -> dict: ...
    def write_json(self, path: str, data: dict) -> None: ...
    def exists(self, path: str) -> bool: ...
    def list_prefix(self, prefix: str) -> Iterable[str]: ...


@dataclass
class QueryOverrideInput:
    """One customer edit to a query's routing. ``None`` fields are left unchanged.

    ``cached`` pins (True) or removes (False) the cache overlay (#296). Naming a
    cache engine as ``assigned_engine`` is the same as ``cached=True``: the cache
    never owns a query, so the owner stays.
    """

    query_id: str
    assigned_engine: str | None = None
    in_scope: bool | None = None
    cached: bool | None = None


@dataclass
class AssignmentOverrideResult:
    """Outcome of applying overrides and writing the new assignment version."""

    assignment: Assignment
    validation: ValidationResult
    skipped_engines: list[str] = field(default_factory=list)
    written_path: str = ""
    # Query IDs moved automatically to stay co-located with a co-dependent query
    # the customer re-routed (ADR-029 Amendment 3). Empty when nothing propagated.
    propagated_query_ids: list[str] = field(default_factory=list)


class AssignmentOverrideError(Exception):
    """Base class for override-application failures (caller maps to protocol)."""


class NoAssignmentFound(AssignmentOverrideError):
    """No assignment artifact exists to override."""


class UnknownQuery(AssignmentOverrideError):
    """An override referenced a query_id absent from the current assignment."""

    def __init__(self, query_id: str) -> None:
        self.query_id = query_id
        super().__init__(f"Query {query_id} not found in current assignment")


class AssignmentValidationFailed(AssignmentOverrideError):
    """The overridden assignment failed validation with hard errors."""

    def __init__(self, errors: list[str], warnings: list[str]) -> None:
        self.errors = errors
        self.warnings = warnings
        super().__init__("Assignment validation failed with hard errors")


class LosingAuroraEngineOverride(AssignmentOverrideError):
    """A customer override tried to move a query onto the #381 losing Aurora engine.

    Rejected up front, before validation runs, with a message naming both engines'
    display names and the source database -- clearer than letting it fall through
    to the generic validator error ("... which did not analyze it"), which is
    technically true (``load_assignment_for_edit`` drops the loser's analysis
    output, #381 review) but does not explain *why* that engine is not a candidate
    at all for this source (#381 review round 2).
    """

    def __init__(self, winner: str, loser: str, source_engine: str) -> None:
        self.winner = winner
        self.loser = loser
        self.source_engine = source_engine
        source_name = SOURCE_ENGINE_DISPLAY_NAMES.get(source_engine, display_engine(source_engine))
        super().__init__(
            f"This assessment chose {display_engine(winner)} for this {source_name} "
            f"source; {display_engine(loser)} is not a target."
        )


@dataclass
class ExcludedOverride:
    """One override in a batch that lands a query on an engine that cannot
    run it -- a hard exclusion (#548), a missing required capability
    (#801) or an engine that may not own it (#812) -- with the fully rendered, customer-facing sentence for it.

    ``query_id`` is the query actually blocked -- for a direct override that
    is the query the customer named; for a co-dependency group move it is the
    group-mate whose own gate blocks the whole group's move, which may
    differ from the query the customer explicitly overrode. ``gate_kind`` is
    the scorer's ``EngineScore.gate_kind`` for it (``"hard_exclusion"`` or
    ``"capability"``), or ``GATE_KIND_WRITE`` (``"write_gate"``) for the
    ownership gate (#812).
    """

    query_id: str
    engine: str
    message: str
    gate_kind: str = "hard_exclusion"


def _gate_failure(
    query_id: str, query: Mapping | None, engine: str, ctx: ScoringContext | None
) -> tuple[str, str] | None:
    """``(gate_kind, reason)`` when ``engine`` cannot run the query at all, else
    ``None``. ``reason`` is a plain verb phrase ("uses a leading-wildcard
    search ...", "needs aggregation") that reads after "it" or "query q2 in
    that group".

    The decision is the shared scorer's (``score_query_for_engine``, the same
    hard exclusion then capability gate every routing stage uses, ADR-031
    Decision 2 steps 2-3), never a private copy (#801). A hard exclusion
    quotes the exclusion's own ``customer_message`` (#548); a capability gap
    names only the capabilities ``engine`` lacks, phrased by
    ``capability_gap_phrase`` ("needs aggregation", "is a utility or DDL
    statement"). Without a scoring context
    only the hard exclusions can be checked. Past both, the ownership gate
    (Decision 2 step 3, :func:`_ownership_failure`, #812) is checked with or
    without one.
    """
    from src.agents.referee.query_scoring import (
        GATE_KIND_CAPABILITY,
        GATE_KIND_HARD_EXCLUSION,
        score_query_for_engine,
    )

    query_text = (query or {}).get("query_text", "")
    if ctx is None:
        exclusion = check_exclusions(query_id, query_text, engine)
        if exclusion:
            return GATE_KIND_HARD_EXCLUSION, exclusion.customer_message
        return _ownership_failure(query, engine)
    result = score_query_for_engine({**(query or {}), "query_id": query_id}, engine, ctx)
    if result.gate_kind == GATE_KIND_HARD_EXCLUSION:
        exclusion = check_exclusions(query_id, query_text, engine)
        if exclusion:
            return GATE_KIND_HARD_EXCLUSION, exclusion.customer_message
    if result.gate_kind == GATE_KIND_CAPABILITY:
        missing = missing_capabilities(engine, ctx.query_capabilities.get(query_id, []))
        return GATE_KIND_CAPABILITY, capability_gap_phrase(missing)
    # The model-flag gate is skipped on purpose: a customer may override a
    # model's flag (ADR-031 Decision 11, Amendment 2).
    return _ownership_failure(query, engine)


def _ownership_failure(query: Mapping | None, engine: str) -> tuple[str, str] | None:
    """``(GATE_KIND_WRITE, reason)`` when ``engine`` may not own the query, else ``None``.

    ADR-031 Decision 2 step 3, the same ``can_own`` every routing stage and
    the assignment validator apply: an engine that is not a system of record
    owns no write and no locking read (#812). The gate kind is the one the
    move ledger's candidate ladders record for it (``write_gate``).
    """
    if query is None or can_own(engine, query):
        return None
    kind = "a locking read" if is_locking_read(query) and not is_write_query(query) else "a write"
    return GATE_KIND_WRITE, f"is {kind}, and {display_engine(engine)} is not a system of record"


def _direct_exclusion_message(
    query_id: str, engine: str, reason: str, gate_kind: str = "hard_exclusion"
) -> str:
    """ "DynamoDB can't run query q1: it ..." (round-2 review: the query id
    must be in the message so a batch failure names its row; round-3 review:
    one "query" and one colon, not "Query q1: DynamoDB can't run this
    query: ..."). For the ownership gate it is "can't own" (#827 review): the
    engine could serve the statement, it just may not own it."""
    verb = "own" if gate_kind == GATE_KIND_WRITE else "run"
    return f"{display_engine(engine)} can't {verb} query {query_id}: it {reason}."


def _group_exclusion_message(
    anchor_query_id: str, blocking_query_id: str, engine: str, reason: str
) -> str:
    """ "Can't move q1's group to DynamoDB: query q2 in that group ..." --
    names both the query the customer actually moved (``anchor_query_id``,
    round-3 review) and the group-mate the move would land on an engine that
    cannot run it (``blocking_query_id``), since that may not be the same
    query."""
    return (
        f"Can't move {anchor_query_id}'s group to {display_engine(engine)}: "
        f"query {blocking_query_id} in that group {reason}."
    )


class ExcludedEngineOverride(AssignmentOverrideError):
    """One or more overrides in a batch tried to move a query -- directly, or
    as a co-dependency group-mate carried along with one -- onto an engine a
    hard exclusion rules out for it (#548) or that lacks a capability the
    query requires (#801, ADR-031 Decision 2 step 2: the same gate the scorer
    applies), or that may not own it (#812, Decision 2 step 3: a write or a
    locking read on an engine that is not a system of record). A pick among a query's tied engines never trips this: every
    tied engine scored, so it passed the gate (Decision 12).

    Rejected up front, before validation runs, the same way the #381
    losing-Aurora-engine override is: the customer's own pick is a real
    decision (#503), so a choice that cannot run at all is rejected with a
    plain reason and left for the customer to pick again, instead of being
    silently moved to some other engine on their behalf. Groups move as one
    unit (ADR-029 Amendment 3): one member failing this check blocks the
    whole group's move, not just that member.

    Every excluded override in the batch is reported (round 2 review), not
    just the first, so the customer can fix every bad row in one round trip --
    one per line (round 3 review), so a batch of three or more stays easy to
    scan instead of running together as one paragraph. Each message names the
    engine with ``display_engine`` and quotes the exclusion's own
    ``customer_message`` -- a plain-language fragment -- never
    ``description``, which ``engine_exclusions.py`` documents as the
    technical, log-only reason.
    """

    def __init__(self, excluded: list[ExcludedOverride]) -> None:
        self.excluded = excluded
        super().__init__("\n".join(e.message for e in excluded))


def _read_analysis_outputs(store: _Store, database_name: str, job_id: str) -> dict[str, dict]:
    """Read every ``analysis-<engine>/analysis.json`` for the job."""
    prefix = f"{database_name}/{job_id}/"
    engines: set[str] = set()
    for key in store.list_prefix(prefix):
        relative = key.replace(prefix, "")
        if relative.startswith("analysis-"):
            engines.add(relative.split("/")[0].replace("analysis-", ""))
    outputs: dict[str, dict] = {}
    for engine in engines:
        path = f"{database_name}/{job_id}/analysis-{engine}/analysis.json"
        if store.exists(path):
            outputs[engine] = store.read_json(path)
    return outputs


def _drop_losing_aurora_analysis(analysis_outputs: dict[str, dict], raw: dict) -> dict[str, dict]:
    """Drop the #381 losing Aurora engine's analysis from ``analysis_outputs``.

    Triage selects both ``aurora_mysql`` and ``aurora_postgresql`` for a
    heterogeneous source (SQL Server, Oracle, DB2), so ``_read_analysis_outputs``
    (which lists every ``analysis-<engine>`` artifact the job actually wrote,
    not just the engines an assignment currently uses) reads both back in --
    but the assignment never uses the loser once ``Assignment.
    aurora_engine_choice`` is recorded. Returns a new dict; never mutates the
    caller's. A no-op when ``raw`` has no recorded choice (homogeneous source,
    or an assignment written before #381).
    """
    choice = raw.get("aurora_engine_choice")
    engine = choice.get("engine") if isinstance(choice, Mapping) else None
    if engine not in AURORA_ENGINES:
        return analysis_outputs
    loser = next(e for e in AURORA_ENGINES if e != engine)
    return {e: a for e, a in analysis_outputs.items() if e != loser}


def load_assignment_for_edit(
    store: _Store, database_name: str, job_id: str, version: int
) -> tuple[dict, dict, dict[str, dict], dict]:
    """``(assignment, collector_output, analysis_outputs, triage)`` with legacy
    cache owners moved.

    An assignment written before the cache overlay (#296) can have ElastiCache
    owners, which the validator rejects; they are moved to their system-of-record
    engine here, in memory, with a note in ``cache_notes``, so the assignment
    stays viewable and editable. The stored artifact is not rewritten. Each
    such move is a Move (#616, ``cache_owner_normalize``) appended to the
    in-memory version's own ``moves``, so what is shown still replays from the
    stored version.

    ``triage`` is read the same way ``collector_output`` is (missing reads as
    ``{}``) and passed to :func:`normalize_cache_owners` so its
    ``query_capabilities`` -- the collector-signal-aware capability map
    triage.json itself carries -- feeds the fallback owner's capability gate
    (#549 round-2 review, Critical 1), the same way
    ``AssignmentResolver.resolve()`` reads it. Returned too, so a caller that
    also needs it (``apply_assignment_overrides``'s own ``fallback_owner``
    calls) does not read triage.json a second time.
    """
    loaded = _load_for_edit(store, database_name, job_id, version, new_version=False)
    return loaded.raw, loaded.collector_output, loaded.analysis_outputs, loaded.triage


@dataclass
class _EditInputs:
    """What the customer gate reads to derive a new version (#616)."""

    raw: dict
    collector_output: dict
    analysis_outputs: dict[str, dict]
    triage: dict
    # Each query's (engine, tied_engines) in the stored version, before the
    # cache owner normalisation: the state the new version's Moves replay from.
    stored_state: dict[str, tuple[str, list[str]]]
    # The new version's ledger, holding the normalisation's Moves (None when
    # the version is only read, not derived).
    ledger: MoveLedger | None


def _load_for_edit(
    store: _Store,
    database_name: str,
    job_id: str,
    version: int,
    *,
    new_version: bool,
) -> _EditInputs:
    """:func:`load_assignment_for_edit`'s reads. For a ``new_version`` the cache
    owner normalisation is recorded on that version's ledger (returned);
    otherwise on the in-memory version's own ``moves``."""
    raw = store.read_json(assignment_artifact_path(database_name, job_id, version))
    stored_state = state_of(raw.get("query_assignments") or [])
    ledger = ledger_for(raw.get("query_assignments") or []) if new_version else None
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key) if store.exists(collector_key) else {}
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    triage = store.read_json(triage_key) if store.exists(triage_key) else {}
    analysis_outputs = _read_analysis_outputs(store, database_name, job_id)
    # #381 review: triage selected both Aurora engines for a heterogeneous source, so
    # both have an analysis artifact on disk -- drop the one the resolver's recorded
    # choice says lost, so the editing surface (and the validator run against it) never
    # treats it as a live candidate again.
    analysis_outputs = _drop_losing_aurora_analysis(analysis_outputs, raw)
    if normalize_cache_owners(
        raw,
        collector_output.get("queries", {}).get("query_patterns", []),
        analysis_outputs,
        source_database_engine(collector_output),
        triage=triage,
        collector=collector_output,
        ledger=ledger,
    ):
        qas = [QueryAssignment.model_validate(qa) for qa in raw["query_assignments"]]
        table_assignments, unresolved_table_names = derive_table_assignments(
            qas,
            known_tables=TableNameResolver.from_collector(collector_output),
            retained_engine=retained_engine_for(
                collector_output, qas, raw.get("aurora_engine_choice")
            ),
        )
        raw["table_assignments"] = [ta.model_dump(mode="json") for ta in table_assignments]
        raw["unresolved_table_names"] = unresolved_table_names.model_dump(mode="json")
    return _EditInputs(raw, collector_output, analysis_outputs, triage, stored_state, ledger)


def _apply_cache_override(
    qa: QueryAssignment,
    override: QueryOverrideInput,
    cache: str,
    query: dict | None,
    heavy: set[str],
    analysis_outputs: dict[str, dict],
    source_engine: str,
    owner_counts: dict[str, int],
    triage: dict | None,
    collector: dict | None,
    ctx: ScoringContext | None = None,
    *,
    ledger: MoveLedger,
) -> str | None:
    """Apply a cache request to ``qa`` in place; return the note to record, if any.

    A cache engine cannot own ``qa``: when it does (a legacy owner the edit
    surface did not already move), the owner :func:`owner_decision` picks is a
    Move on ``ledger`` (#616: ``cache_owner_normalize``, actor ``customer``,
    since the customer's cache request is what moved it).
    """
    wants_cache = override.cached is True or override.assigned_engine in CACHE_OVERLAY_ENGINES
    if wants_cache:
        note = None
        if qa.assigned_engine in CACHE_OVERLAY_ENGINES:
            decision = owner_decision(
                query,
                analysis_outputs,
                source_engine,
                owner_counts,
                triage=triage,
                collector=collector,
                ctx=ctx,
            )
            owner, fallback_reason = decision.owner, decision.fallback_reason
            if fallback_reason:
                note = (
                    f"Query {qa.query_id}: {qa.assigned_engine} is a cache layer and cannot "
                    f"own it; {fallback_reason} It is cached by {cache} as requested."
                )
            else:
                note = (
                    f"Query {qa.query_id}: {qa.assigned_engine} is a cache layer and cannot "
                    f"own it; owned by {owner}, cached by {cache} as requested."
                )
            apply_to_assignment(
                ledger,
                qa,
                owner,
                pass_=MovePass.CACHE_OWNER_NORMALIZE,
                actor=MoveActor.CUSTOMER,
                **owner_move_fields(decision),
            )
        elif override.assigned_engine in CACHE_OVERLAY_ENGINES:
            note = (
                f"Query {qa.query_id}: requested engine {override.assigned_engine} is a "
                f"cache layer, not a system of record; it stays owned by {qa.assigned_engine} "
                f"and is cached by {cache}."
            )
        d = qa.model_dump()
        pin_customer_cache(d, cache, query, heavy)
        for k in ("cache_engine", "cache_pattern", "cache_reason", "warnings"):
            setattr(qa, k, d[k])
        qa.cache_customer_override = True
        qa.cache_dropped = False
        if note:
            qa.warnings = [*qa.warnings, f"NOTE: {note}"]
        return note
    if override.cached is False:
        # Recorded even when the query is not cached now, so a later re-evaluation
        # (Reality Check refresh) never caches it against the customer's choice
        qa.cache_engine = None
        qa.cache_pattern = None
        qa.cache_reason = CUSTOMER_UNCACHE_REASON
        qa.cache_customer_override = True
        qa.cache_dropped = False
        return None
    return None


def apply_assignment_overrides(
    store: _Store,
    database_name: str,
    job_id: str,
    overrides: list[QueryOverrideInput],
    exclude_tables: list[str] | None = None,
    *,
    source: AssignmentSource = AssignmentSource.CUSTOMER_GATE,
) -> AssignmentOverrideResult:
    """Apply customer overrides to the effective assignment and write the next version.

    Reads the effective assignment, applies per-query engine/scope overrides and
    optional table-level scope narrowing, builds a new ``customer_modified``
    version (``version+1``, ``previous_version`` set, ``source`` stamped, fresh
    timestamp), validates it, and writes it. Returns the new assignment plus the
    validation result and the engines left with zero in-scope queries.

    Raises:
        NoAssignmentFound: no assignment artifact exists yet.
        UnknownQuery: an override names a query not in the assignment.
        LosingAuroraEngineOverride: an override names the #381 losing Aurora
            engine for a heterogeneous source.
        ExcludedEngineOverride: a direct override, or a co-dependency
            group-mate it would carry along, would land on an engine a hard
            exclusion rules out for it (#548) or that lacks a capability it
            requires (#801), or that may not own it (a write or a locking
            read on an engine that is not a system of record, #812); every
            such violation in the batch is reported
            together.
        AssignmentValidationFailed: the result has hard validation errors (the
            new version is NOT written in that case).
    """
    current_version = resolve_effective_assignment_version(store, database_name, job_id)
    if current_version == 0:
        raise NoAssignmentFound

    loaded = _load_for_edit(store, database_name, job_id, current_version, new_version=True)
    raw, collector_output = loaded.raw, loaded.collector_output
    analysis_outputs, triage = loaded.analysis_outputs, loaded.triage
    # The move ledger of the new version (#616): it already holds the cache
    # owner normalisation's Moves; every engine change below is a Move on it,
    # and the version holds exactly these Moves.
    ledger = loaded.ledger
    if ledger is None:  # pragma: no cover - new_version=True always builds one
        raise RuntimeError("customer gate: no move ledger for the new version")
    current = Assignment.model_validate(raw)
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_by_id = {q.get("query_id"): q for q in queries}
    cache = available_cache_engine(analysis_outputs) or "elasticache"
    heavy = write_heavy_tables(queries)
    source_engine = source_database_engine(collector_output)
    # The settled-workload count ``fallback_owner`` passes on to an unresolved
    # tie's placeholder (ADR-031 Decision 12): a query already carrying its
    # own ``tied_engines`` must not feed another tie's count (#549 review,
    # Important 3), so this is ``tie_break.settled_counts``, never a raw
    # count of every ``assigned_engine``.
    owner_counts = settled_counts(current.query_assignments)
    # Built once, before the per-override loop below, and handed to every
    # ``_apply_cache_override``/``fallback_owner`` call as ``ctx=`` (#549
    # round-3 review, Suggestion 3): a customer edit can carry several
    # overrides in one request, and rebuilding the anti-pattern map (and the
    # capability map, when triage needs re-detecting it) per override rather
    # than once is wasted, measurable work.
    ctx = None
    if isinstance(analysis_outputs, Mapping):
        from src.agents.referee.query_scoring import ScoringContext

        ctx = ScoringContext.from_pipeline(analysis_outputs, triage, collector_output)

    qa_map: dict[str, QueryAssignment] = {qa.query_id: qa for qa in current.query_assignments}

    def scores(query_id: str, from_engine: str, to_engine: str) -> dict:
        """A customer Move's scores: the shared scorer's on both engines, and the
        runner-up (the best-scoring other analyzed engine that may own the query)."""
        from src.agents.referee.move_ledger import best_other
        from src.agents.referee.query_scoring import score_query_for_engine

        if ctx is None:
            return {}
        query = query_by_id.get(query_id) or {"query_id": query_id}
        serviceable = {
            engine: score
            for engine in sorted(analysis_outputs)
            if can_own(engine, query)
            and (score := score_query_for_engine(query, engine, ctx).score) is not None
        }
        return {
            "score_before": score_query_for_engine(query, from_engine, ctx).score,
            "score_after": score_query_for_engine(query, to_engine, ctx).score,
            "runner_up": best_other(serviceable, to_engine),
        }

    cache_notes: list[str] = []
    # #548 round 2: every excluded override in the batch is collected and
    # reported together (never just the first), and a group move is checked
    # for every member before anything is written -- see
    # ``_propagate_codependent_overrides`` below for the group half of this.
    excluded: list[ExcludedOverride] = []

    # Per-query overrides. A changed engine marks the query customer-overridden;
    # an in_scope toggle is a scoping change, not an engine reassignment. A cache
    # engine is never an owner (#296): naming it, or ``cached``, pins the cache
    # overlay and keeps the owner.
    for override in overrides:
        qa = qa_map.get(override.query_id)
        if qa is None:
            raise UnknownQuery(override.query_id)
        # #381 review round 2: reject a re-route onto the losing Aurora engine up
        # front, with a clear reason, instead of letting it fall through to the
        # generic "did not analyze it" validator error.
        choice = current.aurora_engine_choice
        if (
            choice is not None
            and override.assigned_engine in AURORA_ENGINES
            and override.assigned_engine != choice.engine
        ):
            raise LosingAuroraEngineOverride(
                choice.engine, override.assigned_engine, choice.source_engine
            )
        note = _apply_cache_override(
            qa,
            override,
            cache,
            query_by_id.get(qa.query_id),
            heavy,
            analysis_outputs,
            source_engine,
            owner_counts,
            triage,
            collector_output,
            ctx,
            ledger=ledger,
        )
        if note:
            cache_notes.append(note)
        if (
            override.assigned_engine is not None
            and override.assigned_engine not in CACHE_OVERLAY_ENGINES
        ):
            # #548/#801/#812: reject a re-route onto an engine that cannot
            # run or own this exact query (a hard exclusion, a missing
            # capability, or a write or locking read on an engine that is not
            # a system of record), up front, the same way the #381 losing-Aurora-engine check above
            # does -- never silently move it to a different engine on the
            # customer's behalf. Collected, not raised immediately, so the
            # rest of the batch is still checked (round 2 review) -- nothing
            # is written while any violation is outstanding (checked once,
            # below, after the whole batch runs).
            gated = _gate_failure(
                override.query_id,
                query_by_id.get(override.query_id),
                override.assigned_engine,
                ctx,
            )
            if gated:
                excluded.append(
                    ExcludedOverride(
                        query_id=override.query_id,
                        engine=override.assigned_engine,
                        message=_direct_exclusion_message(
                            override.query_id, override.assigned_engine, gated[1], gated[0]
                        ),
                        gate_kind=gated[0],
                    )
                )
            else:
                # #503: the customer's own pick is a real decision, so any open
                # tie this query carried is resolved by it, not left to still
                # read "pending your choice" after the customer chose -- and the
                # reason itself is rewritten to say so, rather than leave the old
                # tie sentence standing after the field it was about has already
                # been cleared.
                was_tied = sorted(qa.tied_engines)
                if qa.assigned_engine != override.assigned_engine or was_tied:
                    # #616: the pick is a Move (pass and rule customer_pick,
                    # actor customer) with the shared scorer's scores.
                    apply_to_assignment(
                        ledger,
                        qa,
                        override.assigned_engine,
                        pass_=MovePass.CUSTOMER_PICK,
                        reason_code=ReasonCode.CUSTOMER_PICK,
                        tied_engines=[],
                        actor=MoveActor.CUSTOMER,
                        **scores(qa.query_id, qa.assigned_engine, override.assigned_engine),
                    )
                qa.customer_override = True
                if was_tied:
                    others = ", ".join(
                        display_engine(e) for e in was_tied if e != qa.assigned_engine
                    )
                    qa.assignment_reason = (
                        f"Placed on {display_engine(qa.assigned_engine)} by your choice; "
                        f"scores tied with {others}."
                    )
        if override.in_scope is not None:
            qa.in_scope = override.in_scope

    # Co-dependency-aware propagation (ADR-029 Amendment 3): if the customer
    # re-routed a query that shares a significant JOIN group with others, move the
    # group's other in-scope members with it so the JOIN stays co-located instead
    # of silently splitting. A cache request is not a re-route.
    owner_overrides = [o for o in overrides if o.assigned_engine not in CACHE_OVERLAY_ENGINES]
    propagated_query_ids, group_excluded = _propagate_codependent_overrides(
        qa_map,
        owner_overrides,
        query_by_id,
        collector_output,
        ledger=ledger,
        scores=scores,
        ctx=ctx,
    )
    excluded.extend(group_excluded)

    # #548 round 2, #801, #812: reject the whole batch -- nothing written --
    # when any override, direct or propagated, lands on an engine that cannot
    # run or own the query (a hard exclusion, a missing capability, or the
    # ownership gate). Checked once
    # here, after both the direct loop and group propagation above have had
    # a chance to find every violation, so the customer sees all of them in
    # one round trip rather than one at a time.
    if excluded:
        raise ExcludedEngineOverride(excluded)

    # Table-level scope narrowing: a query whose tables are all excluded drops
    # out of scope; a partial overlap stays in scope with a low-severity warning.
    scope_warnings: list[str] = []
    if exclude_tables:
        excluded_tables = set(exclude_tables)
        for qa in qa_map.values():
            tables = set(qa.source_tables)
            if tables and tables.issubset(excluded_tables):
                qa.in_scope = False
            elif tables & excluded_tables:
                scope_warnings.append(
                    f"WARNING [LOW]: Query {qa.query_id} accesses both in-scope and "
                    f"excluded tables ({sorted(tables & excluded_tables)}). Keeping query in scope."
                )

    new_version = current_version + 1
    new_assignment = current.model_copy(
        update={
            "version": new_version,
            "status": AssignmentStatus.CUSTOMER_MODIFIED,
            "source": source,
            "timestamp": datetime.now(UTC),
            "query_assignments": list(qa_map.values()),
            # #713 review round 3: a post-schema reroute stays on record only
            # while it still holds; one the customer undid (or re-routed
            # elsewhere) is not carried into their edit.
            "post_schema_reroutes": _reroutes_still_holding(
                current.post_schema_reroutes, qa_map.values()
            ),
            "previous_version": current_version,
            "rolled_back_version": None,
            "cache_notes": [*current.cache_notes, *cache_notes],
            # #459 round 2: cache_safety_net_notes is this version's own
            # customer-facing explanation, not a cumulative log like
            # cache_notes -- a previous version's safety-net note (e.g. "10
            # cached reads remain") would otherwise read as current fact
            # about THIS version's (different) numbers once synthesis runs
            # again. The next synthesis run, if it drops anything from this
            # version's overlay, writes its own note here.
            "cache_safety_net_notes": [],
            # #616: moves are per-version: exactly the Moves that produced this
            # version (the cache owner normalisation's, then the customer's).
            "moves": list(ledger.moves),
        }
    )

    # Recompute derived views against the NEW routing instead of carrying the
    # previous version's forward (ADR-029 Layer B). model_copy only replaced
    # query_assignments, so table_assignments and co_dependency_groups would
    # otherwise go stale relative to the customer's edits.
    _recompute_derived_views(new_assignment, collector_output)
    # The cache overlay summary counts in-scope queries only, so a scope edit
    # changes it; a customer re-route never touches a query's cache overlay.
    summary = overlay_summary(
        [qa.model_dump() for qa in new_assignment.query_assignments],
        collector_output.get("queries", {}).get("query_patterns", []),
    )
    new_assignment.cache_overlay = CacheOverlaySummary(**summary) if summary else None

    validation = AssignmentValidator().validate(new_assignment, collector_output, analysis_outputs)
    if not validation.valid:
        raise AssignmentValidationFailed(validation.errors, validation.warnings)

    new_assignment.validation_warnings = scope_warnings + validation.warnings

    in_scope_counts: dict[str, int] = {}
    for qa in new_assignment.query_assignments:
        if qa.in_scope:
            in_scope_counts[qa.assigned_engine] = in_scope_counts.get(qa.assigned_engine, 0) + 1
    skipped_engines = sorted(
        engine
        for engine in {qa.assigned_engine for qa in new_assignment.query_assignments}
        if in_scope_counts.get(engine, 0) == 0
    )

    # #616: refuse a version whose engines do not follow from the stored one
    # plus its own Moves (an engine set without move_ledger).
    check_replay(
        new_assignment.moves,
        loaded.stored_state,
        state_of(new_assignment.query_assignments),
        "customer gate",
    )
    new_path = assignment_artifact_path(database_name, job_id, new_version)
    store.write_json(new_path, new_assignment.model_dump(mode="json"))

    return AssignmentOverrideResult(
        assignment=new_assignment,
        validation=validation,
        skipped_engines=skipped_engines,
        written_path=new_path,
        propagated_query_ids=propagated_query_ids,
    )


def _reroutes_still_holding(reroutes: list, query_assignments) -> list:
    """The post-schema reroutes whose query still sits on the engine they moved it to."""
    engine_of = {qa.query_id: qa.assigned_engine for qa in query_assignments}
    return [r for r in reroutes if engine_of.get(r.query_id) == r.to_engine]


def mark_assignment_customer_approved(
    store: _Store, database_name: str, job_id: str
) -> Assignment | None:
    """Stamp the effective assignment's status as ``CUSTOMER_APPROVED`` in place.

    Used when the customer approves the routing **as-is** (no edits) at the review
    gate. The gate's approval signal remains the ``.meta`` ASSIGNMENT_REVIEW phase;
    this additionally records approval on the artifact itself so it is
    self-describing (an auditor reading ``assignment/v<N>/assignment.json`` sees
    ``customer_approved``). It does NOT write a new version — the content is
    unchanged, so append-only lineage is preserved and downstream staleness
    detection (which keys on the version number) is unaffected.

    Idempotent and narrow: a ``CUSTOMER_MODIFIED`` artifact is left untouched (a
    modification already implies approval-with-changes), and re-stamping an
    already-approved artifact is a no-op. Returns the (possibly unchanged)
    effective assignment, or ``None`` when no assignment exists.
    """
    version = resolve_effective_assignment_version(store, database_name, job_id)
    if version == 0:
        return None
    path = assignment_artifact_path(database_name, job_id, version)
    current = Assignment.model_validate(store.read_json(path))
    if current.status in (AssignmentStatus.CUSTOMER_MODIFIED, AssignmentStatus.CUSTOMER_APPROVED):
        return current
    updated = current.model_copy(update={"status": AssignmentStatus.CUSTOMER_APPROVED})
    store.write_json(path, updated.model_dump(mode="json"))
    return updated


def _propagate_codependent_overrides(
    qa_map: dict[str, QueryAssignment],
    overrides: list[QueryOverrideInput],
    query_by_id: dict[str, dict],
    collector_output: dict,
    *,
    ledger: MoveLedger,
    scores: Callable[[str, str, str], dict] | None = None,
    ctx: ScoringContext | None = None,
) -> tuple[list[str], list[ExcludedOverride]]:
    """Move a customer-re-routed query's co-dependent group-mates with it.

    When the customer changes a query's engine and that query shares a significant
    JOIN group with others (``co_dependency_groups``), the group's other IN-SCOPE
    members that the customer did NOT explicitly set are moved to the same engine,
    so the JOIN group stays co-located instead of silently splitting (ADR-029
    Amendment 3). Mutates ``qa_map`` in place; returns ``(propagated_query_ids,
    excluded)``.

    Respects explicit customer picks: a group the customer explicitly split (two
    members set to different engines in the same edit) is left as chosen — the
    resulting split is surfaced by the feasibility reviewer, not overridden here.
    A propagated query is tagged ``co_dependency_propagated`` (not
    ``customer_override``) so it is distinguishable from the customer's own picks.

    A group moves as one unit (#548 round 2): before a group-mate is actually
    moved, it is checked against the same gate a direct override is
    (:func:`_gate_failure`: the hard exclusions, given ``ctx`` the
    capability gate (#801), and the ownership gate (#812)). A group-mate the
    target engine cannot run or own is never
    silently left behind on its old engine either -- the group is moving as a
    unit, so a member that cannot make the move blocks the whole move, which
    the caller enforces by rejecting the batch when ``excluded`` is non-empty.
    This function itself only reports the violations; it does not raise.

    Best-effort: any error building the groups leaves the explicit overrides intact
    and propagates nothing — propagation is an enhancement, never a blocker.

    Each move is a Move on ``ledger`` (#616: pass
    ``customer_group_propagation``, rule ``group.customer_propagation``, actor ``customer``,
    with the group's key), scored by ``scores(query_id, from, to)`` when given.
    """
    explicit: dict[str, str] = {
        ov.query_id: ov.assigned_engine for ov in overrides if ov.assigned_engine is not None
    }
    if not explicit:
        return [], []
    try:
        groups = build_co_dependency_groups(
            collector_output.get("queries", {}).get("query_patterns", []),
            collector_output.get("database_schema", {}).get("tables", []),
        )
    except Exception:  # noqa: BLE001 - never block a customer edit on a grouping error
        return [], []

    propagated: list[str] = []
    excluded: list[ExcludedOverride] = []
    for group in groups:
        touched = set(group) & set(explicit)
        if not touched:
            continue  # customer did not touch this group
        chosen = {explicit[q] for q in touched}
        if len(chosen) != 1:
            continue  # explicitly split across engines -> respect; feasibility flags it
        target = next(iter(chosen))
        anchor = sorted(touched)[0]
        for qid in group:
            if qid in explicit:
                continue  # the customer's own pick is authoritative
            qa = qa_map.get(qid)
            if qa is None or not qa.in_scope:
                continue
            gated = _gate_failure(qid, query_by_id.get(qid), target, ctx)
            if gated:
                excluded.append(
                    ExcludedOverride(
                        query_id=qid,
                        engine=target,
                        message=_group_exclusion_message(anchor, qid, target, gated[1]),
                        gate_kind=gated[0],
                    )
                )
                continue
            if qa.assigned_engine != target:
                # #503: a propagated move is a real decision too, so it clears a tie.
                apply_to_assignment(
                    ledger,
                    qa,
                    target,
                    pass_=MovePass.CUSTOMER_GROUP_PROPAGATION,
                    reason_code=ReasonCode.GROUP_CUSTOMER_PROPAGATION,
                    group_key=group_key(group),
                    tied_engines=[],
                    actor=MoveActor.CUSTOMER,
                    **(scores(qid, qa.assigned_engine, target) if scores else {}),
                )
                qa.co_dependency_propagated = True
                qa.assignment_reason = (
                    f"Co-located with co-dependent query group of {anchor} (shared JOIN) "
                    f"after a customer routing change."
                )
                propagated.append(qid)
    return sorted(propagated), excluded


def _recompute_derived_views(assignment: Assignment, collector_output: dict) -> None:
    """Recompute ``table_assignments`` and ``co_dependency_groups`` against the
    current query assignments and collector output, in place.

    Both are derived views that must reflect the routing rather than be carried
    forward from a previous version (ADR-029 Layer B).
    """
    table_assignments, unresolved_table_names = derive_table_assignments(
        assignment.query_assignments,
        known_tables=TableNameResolver.from_collector(collector_output),
        retained_engine=retained_engine_for(
            collector_output, assignment.query_assignments, assignment.aurora_engine_choice
        ),
    )
    assignment.table_assignments = table_assignments
    assignment.unresolved_table_names = unresolved_table_names
    assignment.co_dependency_groups = build_co_dependency_groups(
        collector_output.get("queries", {}).get("query_patterns", []),
        collector_output.get("database_schema", {}).get("tables", []),
    )


def refresh_consolidated_assignment(
    raw: dict,
    collector_output: dict,
    analysis_outputs: dict[str, dict],
    *,
    dead_engines: set[str] | None = None,
) -> dict:
    """Refresh a Reality-Check consolidated assignment dict (ADR-029 Layers B+E).

    Recomputes the derived views against the consolidated routing, refreshes
    ``validation_warnings`` via the validator (so a split warning naming an engine
    that was consolidated away is not re-emitted), and drops per-query warnings
    that name an engine ``dead_engines`` says consolidation eliminated, so
    dead-engine noise does not persist into the revised version.

    Operates on ``raw`` as a dict and patches only the recomputed fields, so all
    other keys (including the ``reality_check_applied`` marker) are preserved and
    partial/legacy assignment dicts are tolerated — a probe model is built only to
    drive the pure computations.
    """
    qa_dicts = raw.get("query_assignments", [])
    qas = [
        QueryAssignment(
            query_id=qa.get("query_id", ""),
            assigned_engine=qa.get("assigned_engine", ""),
            confidence=int(qa.get("confidence", 0) or 0),
            source_tables=qa.get("source_tables", []) or [],
            assignment_reason=qa.get("assignment_reason", ""),
            in_scope=qa.get("in_scope", True),
            customer_override=qa.get("customer_override", False),
            warnings=qa.get("warnings", []) or [],
        )
        for qa in qa_dicts
    ]

    table_assignments, unresolved_table_names = derive_table_assignments(
        qas,
        known_tables=TableNameResolver.from_collector(collector_output),
        retained_engine=retained_engine_for(collector_output, qas, raw.get("aurora_engine_choice")),
    )
    co_dependency_groups = build_co_dependency_groups(
        collector_output.get("queries", {}).get("query_patterns", []),
        collector_output.get("database_schema", {}).get("tables", []),
    )
    probe = Assignment(
        job_id=str(raw.get("job_id", "")),
        version=int(raw.get("version", 1)),
        status=AssignmentStatus.CUSTOMER_MODIFIED,
        timestamp=datetime.now(UTC),
        query_assignments=qas,
        table_assignments=table_assignments,
        co_dependency_groups=co_dependency_groups,
        validation_warnings=[],
        unresolved_table_names=unresolved_table_names,
    )
    validation = AssignmentValidator().validate(probe, collector_output, analysis_outputs)

    out = dict(raw)
    out["table_assignments"] = [ta.model_dump(mode="json") for ta in table_assignments]
    out["co_dependency_groups"] = co_dependency_groups
    out["validation_warnings"] = validation.warnings
    out["unresolved_table_names"] = unresolved_table_names.model_dump(mode="json")
    # Re-evaluate the cache overlay against the consolidated routing (#296): an
    # overlay survives its owner's consolidation, and the summary follows scope.
    refresh_cache_overlay(
        out, collector_output.get("queries", {}).get("query_patterns", []), analysis_outputs
    )
    if dead_engines:
        pruned: list[dict] = []
        for qa in qa_dicts:
            qa = dict(qa)
            qa["warnings"] = [
                w for w in qa.get("warnings", []) if not any(e in w for e in dead_engines)
            ]
            pruned.append(qa)
        out["query_assignments"] = pruned
    return out
