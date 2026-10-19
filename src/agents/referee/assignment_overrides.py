"""Shared write path for customer assignment overrides (ADR-028).

One place builds the ``customer_modified`` next version from a set of per-query
overrides, so the web REST route and the ATX review gate produce byte-identical
artifacts (same version bump, status, source, provenance) instead of each
re-implementing the write. This is transport-agnostic: it raises domain errors
that callers translate to their own protocol (HTTP status, chat message, ...).
"""

from __future__ import annotations

import logging
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
    best_other,
    check_replay,
    group_key,
    ledger_for,
    state_of,
)
from src.agents.referee.pending_choices import AutoPick, auto_decisions_still_holding
from src.agents.referee.query_pattern import write_access_patterns
from src.agents.referee.query_scoring import GATE_KIND_WRITE
from src.agents.referee.table_resolution import TableNameResolver
from src.agents.referee.tie_break import settled_counts
from src.agents.referee.write_groups import (
    assignment_sources_of_truth,
    collector_write_groups,
    placement_groups,
    write_group_tables,
)
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    CacheOverlaySummary,
    PendingEngineChoice,
    PostSchemaReroute,
    QueryAssignment,
    ValidationResult,
)
from src.contracts.move_ledger import EngineScore, MoveActor, MoveEvidence, MovePass, ReasonCode
from src.shared.cache_engines import DEFAULT_CACHE_ENGINE
from src.shared.capability_labels import capability_gap_phrase
from src.shared.contract_versioning import load_contract, stamp_artifact_version
from src.shared.engine_names import SOURCE_ENGINE_DISPLAY_NAMES, display_engine
from src.storage.assignment_versioning import (
    assignment_artifact_path,
    resolve_effective_assignment_version,
)
from src.tools.sql.query_facts import remember_collected_facts

logger = logging.getLogger(__name__)


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
    # #1003: set when ``--auto`` ("decide for me") picked ``assigned_engine`` for a
    # pending engine choice; the Move is then the rule's (pass ``auto_pick``), not
    # the customer's, and the version records the decision.
    auto_pick: AutoPick | None = None


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
        # No scoring context here, so no collector to name the dialect.
        return _ownership_failure(query, engine, "")  # engine unknown: no collector
    result = score_query_for_engine({**(query or {}), "query_id": query_id}, engine, ctx)
    if result.gate_kind == GATE_KIND_HARD_EXCLUSION:
        exclusion = check_exclusions(query_id, query_text, engine, ctx.source_engine)
        if exclusion:
            return GATE_KIND_HARD_EXCLUSION, exclusion.customer_message
    if result.gate_kind == GATE_KIND_CAPABILITY:
        missing = missing_capabilities(engine, ctx.query_capabilities.get(query_id, []))
        return GATE_KIND_CAPABILITY, capability_gap_phrase(missing)
    # The model-flag gate is skipped on purpose: a customer may override a
    # model's flag (ADR-031 Decision 11, Amendment 2).
    return _ownership_failure(query, engine, ctx.source_engine)


def _ownership_failure(
    query: Mapping | None, engine: str, source_engine: str | None
) -> tuple[str, str] | None:
    """``(GATE_KIND_WRITE, reason)`` when ``engine`` may not own the query, else ``None``.

    ADR-031 Decision 2 step 3, the same ``can_own`` every routing stage and
    the assignment validator apply: an engine that is not a system of record
    owns no write and no locking read (#812). The gate kind is the one the
    move ledger's candidate ladders record for it (``write_gate``).
    """
    if query is None or can_own(engine, query, source_engine):
        return None
    kind = (
        "a locking read"
        if is_locking_read(query, source_engine) and not is_write_query(query, source_engine)
        else "a write"
    )
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
    anchor_query_id: str,
    blocking_query_id: str,
    engine: str,
    reason: str,
    table: str | None = None,
) -> str:
    """ "Can't move q1's group to DynamoDB: query q2 in that group ..." --
    names both the query the customer actually moved (``anchor_query_id``,
    round-3 review) and the group-mate the move would land on an engine that
    cannot run it (``blocking_query_id``), since that may not be the same
    query. With ``table``, the group-mate is another write of that table
    (#992): a table's writes stay on one engine."""
    if table:
        return (
            f"Can't move {anchor_query_id} to {display_engine(engine)}: query "
            f"{blocking_query_id} also writes table {table}, and a table's writes stay on "
            f"one engine, its source of truth; query {blocking_query_id} {reason}."
        )
    return (
        f"Can't move {anchor_query_id}'s group to {display_engine(engine)}: "
        f"query {blocking_query_id} in that group {reason}."
    )


# ``ExcludedOverride.gate_kind`` for two writes of one table picked onto
# different engines in one edit (#992).
GATE_KIND_WRITE_GROUP_SPLIT = "write_group_split"


def _write_split_message(
    table: str, first_qid: str, first_engine: str, qid: str, engine: str
) -> str:
    """Two writes of ``table`` picked onto different engines (#992)."""
    return (
        f"Can't put query {qid} on {display_engine(engine)} and query {first_qid} on "
        f"{display_engine(first_engine)}: both write table {table}, and a table's writes "
        "stay on one engine, its source of truth. Pick one engine for both."
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
    remember_collected_facts(collector_output)
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
            write_groups=collector_write_groups(collector_output),
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
        pin_customer_cache(d, cache, query, heavy, source_engine)
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
    version (``auto_decided``, status and source, when every override is an
    ``--auto`` pick and nothing else changes, #1003) (``version+1``, ``previous_version`` set, ``source`` stamped, fresh
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
    current = load_contract(
        Assignment, raw, artifact=assignment_artifact_path(database_name, job_id, current_version)
    )
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_by_id = {q.get("query_id"): q for q in queries}
    cache = available_cache_engine(analysis_outputs) or DEFAULT_CACHE_ENGINE
    source_engine = source_database_engine(collector_output)
    heavy = write_heavy_tables(queries, source_engine)
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

        # The capability waivers (#377) read the edited version's sources of truth.
        ctx = ScoringContext.from_pipeline(
            analysis_outputs, triage, collector_output
        ).with_sources_of_truth(assignment_sources_of_truth(current.model_dump(mode="json")))

    qa_map: dict[str, QueryAssignment] = {qa.query_id: qa for qa in current.query_assignments}

    def scores(query_id: str, from_engine: str, to_engine: str) -> dict:
        """A customer Move's scores: the shared scorer's on both engines, and the
        runner-up (the best-scoring other analyzed engine that may own the query)."""
        from src.agents.referee.query_scoring import score_query_for_engine

        if ctx is None:
            return {}
        query = query_by_id.get(query_id) or {"query_id": query_id}
        serviceable = {
            engine: score
            for engine in sorted(analysis_outputs)
            if can_own(engine, query, source_engine)
            and (score := score_query_for_engine(query, engine, ctx).score) is not None
        }
        return {
            "score_before": score_query_for_engine(query, from_engine, ctx).score,
            "score_after": score_query_for_engine(query, to_engine, ctx).score,
            "runner_up": best_other(serviceable, to_engine),
        }

    cache_notes: list[str] = []
    # #1003: the --auto picks this batch applies (by choice id), and the queries the
    # customer picked themselves (a pick drops an earlier --auto decision).
    auto_picks: dict[str, AutoPick] = {}
    customer_picked: set[str] = set()
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
                if override.auto_pick is not None:
                    if qa.assigned_engine != override.assigned_engine or was_tied:
                        pick = override.auto_pick
                        alternative = pick.alternatives[0] if pick.alternatives else None
                        _apply_auto_pick(
                            ledger,
                            qa,
                            pick,
                            scores(qa.query_id, qa.assigned_engine, pick.engine),
                            (
                                scores(qa.query_id, alternative, pick.engine).get("score_before")
                                if alternative
                                else None
                            ),
                        )
                    auto_picks[override.auto_pick.choice_id] = override.auto_pick
                elif qa.assigned_engine != override.assigned_engine or was_tied:
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
                if override.auto_pick is None:
                    qa.customer_override = True
                    customer_picked.add(qa.query_id)
                if was_tied and override.auto_pick is None:
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
        decided_ids={o.query_id for o in owner_overrides if o.auto_pick is not None},
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

    decided_only = (
        bool(overrides)
        and all(
            o.auto_pick is not None and o.in_scope is None and o.cached is None for o in overrides
        )
        and not exclude_tables
    )
    new_version = current_version + 1
    new_assignment = current.model_copy(
        update={
            # #972: a new file, so today's version, not the loaded one's.
            "contract_version": Assignment.model_fields["contract_version"].default,
            "version": new_version,
            # #1003: a version only --auto's decisions produced is the rule's, not a
            # customer edit; any customer pick or scope edit keeps it a customer edit.
            "status": (
                AssignmentStatus.AUTO_DECIDED
                if decided_only
                else AssignmentStatus.CUSTOMER_MODIFIED
            ),
            "source": AssignmentSource.AUTO_DECIDED if decided_only else source,
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
            # #1003: --auto's decisions that still hold (a customer pick on one of
            # their queries drops it), then this batch's own.
            "auto_decided_choices": _auto_decisions(
                current.auto_decided_choices,
                auto_picks,
                customer_picked,
                qa_map.values(),
                new_version_number=current_version + 1,
            ),
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
    write_access_patterns(store, database_name, job_id, new_version, collector_output)

    return AssignmentOverrideResult(
        assignment=new_assignment,
        validation=validation,
        skipped_engines=skipped_engines,
        written_path=new_path,
        propagated_query_ids=propagated_query_ids,
    )


def _apply_auto_pick(
    ledger: MoveLedger,
    qa: QueryAssignment,
    pick: AutoPick,
    scored: dict,
    alternative_score: float | None,
) -> None:
    """``--auto``'s pick for one query of a pending engine choice (#1003): a Move
    with pass ``auto_pick``, rule ``tie.decided_for_you``, actor ``rule``; the
    alternative (with the query's own score there) is the runner-up, and the
    choice's whole-point and unrounded scores are in the evidence."""
    alternative = pick.alternatives[0] if pick.alternatives else None
    runner_up = scored.get("runner_up")
    if alternative is not None:
        runner_up = EngineScore(engine=alternative, score=alternative_score)
    apply_to_assignment(
        ledger,
        qa,
        pick.engine,
        pass_=MovePass.AUTO_PICK,
        reason_code=ReasonCode.TIE_DECIDED_FOR_YOU,
        tied_engines=[],
        actor=MoveActor.RULE,
        score_before=scored.get("score_before"),
        score_after=scored.get("score_after"),
        runner_up=runner_up,
        evidence=MoveEvidence(
            choice_id=pick.choice_id,
            choice_scores=dict(pick.scores),
            choice_unrounded_scores=dict(pick.unrounded_scores),
            choice_rule=pick.rule,
            previous_tied_engines=sorted(qa.tied_engines),
        ),
    )
    others = _join_names(pick.alternatives)
    qa.assignment_reason = (
        f"Placed on {display_engine(pick.engine)}: it tied with {others} on table "
        f"{pick.table.rsplit('.', 1)[-1]}. {pick.reason} Decided automatically, review if needed."
    )


def _join_names(engines: Iterable[str]) -> str:
    names = [display_engine(e) for e in engines]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _auto_decisions(
    previous: Iterable,
    picks: Mapping[str, AutoPick],
    customer_picked: set[str],
    query_assignments: Iterable[QueryAssignment],
    *,
    new_version_number: int,
) -> list:
    """The version's ``auto_decided_choices`` (#1003): the earlier ones that still
    hold and that no customer pick in this batch touched, then this batch's own."""
    qas = list(query_assignments)
    kept = [
        d
        for d in auto_decisions_still_holding(previous, qas)
        if d.choice_id not in picks and not set(d.query_ids) & customer_picked
    ]
    new = [pick.record(new_version_number) for _, pick in sorted(picks.items())]
    return sorted([*kept, *new], key=lambda d: d.choice_id)


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
    stored = store.read_json(path)
    current = load_contract(Assignment, stored, artifact=path)
    if current.status in (AssignmentStatus.CUSTOMER_MODIFIED, AssignmentStatus.CUSTOMER_APPROVED):
        return current
    # #1056: patch the stored dict rather than dumping the model, so a field a newer
    # MINOR added (top level or on a query assignment) is kept as it was stored.
    # The model above still validates the file. #972: rewritten by today's code, so
    # the file says today's version.
    patched = stamp_artifact_version(
        path, {**stored, "status": AssignmentStatus.CUSTOMER_APPROVED.value}
    )
    load_contract(Assignment, patched, artifact=path)  # the patched file must still load
    store.write_json(path, patched)
    updated = current.model_copy(update={"status": AssignmentStatus.CUSTOMER_APPROVED})
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
    decided_ids: Iterable[str] = (),
) -> tuple[list[str], list[ExcludedOverride]]:
    """Move a customer-re-routed query's co-dependent group-mates, and the other
    writes of the tables it writes, with it.

    When the customer changes a query's engine and that query shares a significant
    JOIN group with others (``co_dependency_groups``), the group's other IN-SCOPE
    members that the customer did NOT explicitly set are moved to the same engine,
    so the JOIN group stays co-located instead of silently splitting (ADR-029
    Amendment 3). Mutates ``qa_map`` in place; returns ``(propagated_query_ids,
    excluded)``.

    A table's writes share one engine, its source of truth (ADR-031 Amendment 9,
    #992): a pick on one write or locking read of a table pulls the table's
    other in-scope writes with it and, when every pick in its placement unit
    (``write_groups.placement_groups``) names one engine, the co-dependency groups
    they belong to; in a unit the customer split, a pick pulls its tables' writes
    only. The pick is
    rejected instead, with the reason, when it would split a table's writes: two
    writes of one table set to different engines in the same edit, or a write
    that cannot make the move (the gate below).

    Respects explicit customer picks otherwise: a JOIN group the customer
    explicitly split (two members set to different engines in the same edit) is
    left as chosen -- the resulting split is surfaced by the feasibility reviewer,
    not overridden here -- and only the writes of a picked table follow the pick.
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

    ``decided_ids`` (#1003) are the overrides the decide-for-me rule made, not the
    customer: a query pulled along only by them is the rule's Move (actor
    ``rule``) and its reason says it was decided automatically; one pulled by any
    customer pick in its unit is the customer's.

    Each move is a Move on ``ledger`` (#616: pass ``customer_group_propagation``,
    actor ``customer`` (``rule`` for a decide-for-me pull), with the group's key): rule ``group.write_customer_propagation``
    for a write of a picked table, ``group.customer_propagation`` for a JOIN
    group-mate, scored by ``scores(query_id, from, to)`` when given.
    """
    explicit: dict[str, str] = {
        ov.query_id: ov.assigned_engine for ov in overrides if ov.assigned_engine is not None
    }
    if not explicit:
        return [], []
    try:
        queries = collector_output.get("queries", {}).get("query_patterns", [])
        co_groups = build_co_dependency_groups(
            queries, collector_output.get("database_schema", {}).get("tables", [])
        )
        write_groups = collector_write_groups(collector_output)
        groups = placement_groups(co_groups, write_groups)
    except Exception:  # noqa: BLE001 - never block a customer edit on a grouping error
        return [], []
    tables_written_by = write_group_tables(write_groups)

    propagated: list[str] = []
    excluded: list[ExcludedOverride] = []

    # A table's writes have one engine: two of them picked onto different
    # engines in one edit is rejected, whatever else the edit does.
    for table, members in write_groups.items():
        picked = {qid: explicit[qid] for qid in members if qid in explicit}
        if len(set(picked.values())) > 1:
            first = sorted(picked)[0]
            for qid in sorted(picked)[1:]:
                if picked[qid] != picked[first]:
                    excluded.append(
                        ExcludedOverride(
                            query_id=qid,
                            engine=picked[qid],
                            message=_write_split_message(
                                table, first, picked[first], qid, picked[qid]
                            ),
                            gate_kind=GATE_KIND_WRITE_GROUP_SPLIT,
                        )
                    )

    decided = set(decided_ids)

    def _move(
        qid: str,
        target: str,
        anchor: str,
        group: list[str],
        table: str | None,
        by_rule: bool = False,
    ) -> None:
        qa = qa_map.get(qid)
        if qa is None or not qa.in_scope or qid in explicit:
            return
        gated = _gate_failure(qid, query_by_id.get(qid), target, ctx)
        if gated:
            excluded.append(
                ExcludedOverride(
                    query_id=qid,
                    engine=target,
                    message=_group_exclusion_message(anchor, qid, target, gated[1], table),
                    gate_kind=gated[0],
                )
            )
            return
        if qa.assigned_engine == target or qid in propagated:
            return
        # #503: a propagated move is a real decision too, so it clears a tie.
        apply_to_assignment(
            ledger,
            qa,
            target,
            pass_=MovePass.CUSTOMER_GROUP_PROPAGATION,
            reason_code=(
                ReasonCode.WRITE_GROUP_CUSTOMER_PROPAGATION
                if table
                else ReasonCode.GROUP_CUSTOMER_PROPAGATION
            ),
            group_key=group_key(group),
            tied_engines=[],
            actor=MoveActor.RULE if by_rule else MoveActor.CUSTOMER,
            **(scores(qid, qa.assigned_engine, target) if scores else {}),
        )
        qa.co_dependency_propagated = True
        cause = (
            f"{anchor} was decided automatically" if by_rule else f"the customer re-routed {anchor}"
        )
        change = "an automatic decision" if by_rule else "a customer routing change"
        qa.assignment_reason = (
            f"Moved with the other writes of table {table} when {cause}: "
            "a table's writes stay on one engine, its source of truth."
            if table
            else f"Co-located with co-dependent query group of {anchor} (shared JOIN) "
            f"after {change}."
        )
        propagated.append(qid)

    for group in groups:
        touched = set(group) & set(explicit)
        if not touched:
            continue  # customer did not touch this group
        chosen = {explicit[q] for q in touched}
        if len(chosen) == 1:
            # The whole unit follows the one engine picked for it.
            target = next(iter(chosen))
            # The customer's pick leads when there is one; a unit only the rule
            # picked moves by the rule.
            by_customer = sorted(touched - decided)
            by_rule = not by_customer
            anchor = (by_customer or sorted(touched))[0]
            anchor_tables = set(tables_written_by.get(anchor, []))
            for qid in group:
                # Each member says why it moves: a write of a table moves with the
                # table's other writes (whoever the anchor is), any other member with
                # its JOIN group.
                own = tables_written_by.get(qid, [])
                shared = sorted(anchor_tables & set(own)) or sorted(own)
                _move(qid, target, anchor, group, shared[0] if shared else None, by_rule)
            continue
        # Explicitly split across engines: the JOIN split is respected (the
        # feasibility reviewer flags it), but each picked write still pulls
        # the other writes of its tables.
        for anchor in sorted(touched):
            for table in tables_written_by.get(anchor, []):
                members = write_groups.get(table, [])
                for qid in members:
                    _move(qid, explicit[anchor], anchor, list(members), table, anchor in decided)
    return sorted(propagated), excluded


def _recompute_derived_views(assignment: Assignment, collector_output: dict) -> None:
    """Recompute ``table_assignments`` and ``co_dependency_groups`` against the
    current query assignments and collector output, in place.

    Both are derived views that must reflect the routing rather than be carried
    forward from a previous version (ADR-029 Layer B).
    """
    write_groups = collector_write_groups(collector_output)
    table_assignments, unresolved_table_names = derive_table_assignments(
        assignment.query_assignments,
        known_tables=TableNameResolver.from_collector(collector_output),
        retained_engine=retained_engine_for(
            collector_output, assignment.query_assignments, assignment.aurora_engine_choice
        ),
        write_groups=write_groups,
    )
    assignment.write_groups = write_groups
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

    write_groups = collector_write_groups(collector_output)
    table_assignments, unresolved_table_names = derive_table_assignments(
        qas,
        known_tables=TableNameResolver.from_collector(collector_output),
        retained_engine=retained_engine_for(collector_output, qas, raw.get("aurora_engine_choice")),
        write_groups=write_groups,
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
    out["write_groups"] = write_groups
    out["validation_warnings"] = validation.warnings
    out["unresolved_table_names"] = unresolved_table_names.model_dump(mode="json")
    # Re-evaluate the cache overlay against the consolidated routing (#296): an
    # overlay survives its owner's consolidation, and the summary follows scope.
    refresh_cache_overlay(
        out,
        collector_output.get("queries", {}).get("query_patterns", []),
        analysis_outputs,
        source_database_engine(collector_output),
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


# ---------------------------------------------------------------------------
# Saved assignments that split a table's writes (#992, ADR-031 Amendment 9)
# ---------------------------------------------------------------------------


@dataclass
class SourceOfTruthReroute:
    """A saved assignment re-routed so each table's writes land on one engine."""

    assignment: Assignment
    written_path: str
    # The writes (and their placement units' other members) that moved.
    moved_query_ids: list[str]
    # The tables whose writes were split on the saved version.
    split_tables: list[str]
    # Every engine a moved query left or joined: their schema designs and the
    # synthesis are stale against the new version and run again, as after a
    # customer edit (``storage.assignment_versioning.stale_schema_versions``).
    affected_engines: list[str]
    # Customer pins the re-route overrode, each with the conflict in words.
    pin_conflicts: list[str] = field(default_factory=list)


# ``PostSchemaReroute.reason_category`` of a write re-routed to one source of truth
# after approval (#992); the deliverables word it (``shared.flag_reasons``).
SOURCE_OF_TRUTH_REROUTE_CATEGORY = "source_of_truth"


def _split_write_tables(
    write_groups: Mapping[str, list[str]], engine_of: Mapping[str, str]
) -> list[str]:
    return sorted(
        table
        for table, members in write_groups.items()
        if len({engine_of[q] for q in members if q in engine_of}) > 1
    )


def reroute_split_writes(
    store: _Store, database_name: str, job_id: str, *, after_approval: bool = False
) -> SourceOfTruthReroute | None:
    """Re-route a saved assignment whose writes split a table across engines.

    An assignment saved before a table had one writer (ADR-031 Amendment 9) can
    hold a table's writes on two engines. When such a version is loaded for
    approval or resumed, each placement unit holding a split table's writes
    (``write_groups.placement_groups``) goes to one engine: the engine the
    approved version keeps with the best call-weighted mean over the unit's
    in-scope members (``co_dependency.group_engine_scores`` with
    ``write_groups.unit_call_weights``, the shared scorer, every member servable
    and ownable), or the engine a customer pinned one of them to. An exact tie
    goes through ``tie_break.break_tie``, stability first. Each moved query is a
    Move (pass ``co_dependency``, rule ``group.write_reroute``) whose reason says
    it was re-routed to one source of truth, and the result is a NEW version
    (``source = source_of_truth_reroute``), never an overwrite. The changed
    engines' designs and the synthesis are then stale against it and run
    again, as after a customer edit.

    A unit whose writes carry customer pins on two engines has no one engine the
    customer chose: it is re-routed by score like an unpinned one, each pin the
    move overrides is cleared (``customer_override`` off) and the conflict is
    stated in the version's ``validation_warnings`` and ``pin_conflicts``.

    ``after_approval`` (a resumed schema design, after the customer approved): the
    version is marked ``rerouted_after_approval`` and each move is recorded in
    ``post_schema_reroutes`` (reason category ``source_of_truth``), as the
    post-schema router does, so every deliverable states what moved after
    approval.

    A run that finds the new version already written (another resumed run re-routed
    the same saved version first) returns it and writes nothing. Two runs racing
    past that check write the same moves and engines; only the timestamp differs.

    Returns None when the effective version splits no table's writes (nothing
    is written then), or no version exists.
    """
    from src.agents.referee.co_dependency import build_co_dependency_groups, group_engine_scores
    from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
    from src.agents.referee.tie_break import break_tie
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
    from src.agents.referee.write_groups import unit_call_weights

    version = resolve_effective_assignment_version(store, database_name, job_id)
    if version == 0:
        return None
    raw = store.read_json(assignment_artifact_path(database_name, job_id, version))
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key) if store.exists(collector_key) else {}
    write_groups = collector_write_groups(collector_output)
    in_scope = {
        qa["query_id"]: qa["assigned_engine"]
        for qa in raw.get("query_assignments") or []
        if qa.get("in_scope", True) and qa.get("assigned_engine")
    }
    split = _split_write_tables(write_groups, in_scope)
    if not split:
        return None

    loaded = _load_for_edit(store, database_name, job_id, version, new_version=True)
    ledger = loaded.ledger
    if ledger is None:  # pragma: no cover - new_version=True always builds one
        raise RuntimeError("source-of-truth reroute: no move ledger for the new version")
    current = load_contract(
        Assignment,
        loaded.raw,
        artifact=assignment_artifact_path(database_name, job_id, version),
    )
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_by_id = {q.get("query_id"): q for q in queries}
    qa_map = {qa.query_id: qa for qa in current.query_assignments}
    ctx = ScoringContext.from_pipeline(loaded.analysis_outputs, loaded.triage, collector_output)
    source_engine = ctx.source_engine
    units = placement_groups(
        build_co_dependency_groups(
            queries, collector_output.get("database_schema", {}).get("tables", [])
        ),
        write_groups,
    )
    weights = unit_call_weights(units, write_groups, query_by_id)
    kept = sorted({qa.assigned_engine for qa in qa_map.values() if qa.in_scope})
    source_compatible = SOURCE_ENGINE_TO_AURORA.get(source_database_engine(collector_output))
    settled = settled_counts(current.query_assignments)
    split_members = {q for t in split for q in write_groups[t]}

    moved: list[str] = []
    affected: set[str] = set()
    pin_conflicts: list[str] = []
    reroutes: list[dict] = []
    for unit in units:
        members = [q for q in unit if q in qa_map and qa_map[q].in_scope]
        if not set(members) & split_members:
            continue

        def member_score(qid: str, engine: str) -> float | None:
            query = query_by_id.get(qid) or {"query_id": qid}
            if not can_own(engine, query, source_engine):
                return None
            return score_query_for_engine(query, engine, ctx).score

        pinned = {qa_map[q].assigned_engine for q in members if qa_map[q].customer_override}
        candidates = sorted(pinned) if len(pinned) == 1 else kept
        conflicting_pins = len(pinned) > 1
        unit_weights = {q: weights[q] for q in members if q in weights}
        scores = group_engine_scores(members, candidates, member_score, unit_weights)
        if not scores:
            logger.warning(
                "source-of-truth reroute: no kept engine can take every query of a unit "
                "writing %s; left as saved",
                ", ".join(t for t in split if set(write_groups[t]) & set(members)),
            )
            continue
        top = max(scores.values())
        tied = sorted(e for e, v in scores.items() if v == top)
        if len(tied) == 1:
            (winner,) = tied
        else:
            winner, _ = break_tie(
                tied,
                {qa_map[q].assigned_engine for q in members},
                source_compatible,
                settled,
                ",".join(sorted(members)),
            )
        tables = sorted(t for t in split if set(write_groups[t]) & set(members))
        for qid in members:
            qa = qa_map[qid]
            if qa.assigned_engine == winner:
                continue
            previous = qa.assigned_engine
            affected.update({previous, winner})
            apply_to_assignment(
                ledger,
                qa,
                winner,
                pass_=MovePass.CO_DEPENDENCY,
                reason_code=ReasonCode.WRITE_GROUP_REROUTE,
                score_before=member_score(qid, qa.assigned_engine),
                score_after=member_score(qid, winner),
                evidence=MoveEvidence(group_scores=dict(scores)),
                group_key=group_key(unit),
                tied_engines=[],
                actor=MoveActor.RULE,
            )
            qa.assignment_reason = (
                f"Re-routed to one source of truth on {display_engine(winner)}: the saved "
                f"routing split the writes of {', '.join(tables)} across engines, and a "
                "table's writes stay on one engine (ADR-031 Amendment 9)."
            )
            if conflicting_pins and qa.customer_override:
                # The customer pinned this table's writes to two engines; the one
                # this move overrides is no longer their pin, and the conflict is said.
                qa.customer_override = False
                pin_conflicts.append(
                    f"Query {qid} was pinned to {display_engine(previous)}, but other "
                    f"writes of {', '.join(tables)} were pinned to "
                    f"{', '.join(display_engine(e) for e in sorted(pinned - {previous}))}; "
                    f"a table's writes stay on one engine, so it moved to "
                    f"{display_engine(winner)} and is no longer pinned. Pin one engine "
                    "for the table's writes in the assignment review to change it."
                )
            reroutes.append(
                {
                    "query_id": qid,
                    "from_engine": previous,
                    "to_engine": winner,
                    "reason": qa.assignment_reason,
                    "reason_category": SOURCE_OF_TRUTH_REROUTE_CATEGORY,
                    "score_before": member_score(qid, previous),
                    "score_after": member_score(qid, winner),
                    "approved_version": version,
                }
            )
            moved.append(qid)
    if not moved:
        return None

    new_assignment = current.model_copy(
        update={
            "contract_version": Assignment.model_fields["contract_version"].default,
            "version": version + 1,
            "previous_version": version,
            "status": AssignmentStatus.AUTO_GENERATED,
            "source": AssignmentSource.SOURCE_OF_TRUTH_REROUTE,
            "timestamp": datetime.now(UTC),
            "query_assignments": list(qa_map.values()),
            "moves": list(ledger.moves),
        }
    )
    if after_approval:
        # Like the post-schema router's reroute: the deliverables say what moved
        # after the customer approved (``post_schema_reroutes``).
        new_assignment = new_assignment.model_copy(
            update={
                "status": AssignmentStatus.REROUTED_AFTER_APPROVAL,
                "post_schema_reroutes": [
                    *current.post_schema_reroutes,
                    *(PostSchemaReroute.model_validate(r) for r in reroutes),
                ],
            }
        )
    _recompute_derived_views(new_assignment, collector_output)
    validation = AssignmentValidator().validate(
        new_assignment, collector_output, loaded.analysis_outputs
    )
    if not validation.valid:
        raise AssignmentValidationFailed(validation.errors, validation.warnings)
    new_assignment.validation_warnings = [*validation.warnings, *pin_conflicts]
    check_replay(
        new_assignment.moves,
        loaded.stored_state,
        state_of(new_assignment.query_assignments),
        "source-of-truth reroute",
    )
    path = assignment_artifact_path(database_name, job_id, version + 1)
    if store.exists(path):
        # Another run re-routed the same saved version first (two schema-design runs
        # resumed at once): its version stands, and this call writes nothing.
        existing = load_contract(Assignment, store.read_json(path), artifact=path)
        return SourceOfTruthReroute(
            assignment=existing,
            written_path=path,
            moved_query_ids=sorted(
                {
                    m.query_id
                    for m in existing.moves
                    if m.reason_code == ReasonCode.WRITE_GROUP_REROUTE
                }
            ),
            split_tables=split,
            affected_engines=sorted(affected),
            pin_conflicts=[],
        )
    store.write_json(path, new_assignment.model_dump(mode="json"))
    write_access_patterns(store, database_name, job_id, version + 1, collector_output)
    return SourceOfTruthReroute(
        assignment=new_assignment,
        written_path=path,
        moved_query_ids=sorted(moved),
        split_tables=split,
        affected_engines=sorted(affected),
        pin_conflicts=pin_conflicts,
    )


# ---------------------------------------------------------------------------
# Pending engine choices, one per table (#1003, ADR-031 Decision 12 Amendment 10)
# ---------------------------------------------------------------------------


def pending_choices_for_job(
    store: _Store, database_name: str, job_id: str, version: int | None = None
) -> tuple[list[PendingEngineChoice], int]:
    """``(choices, version)``: the effective assignment's open ties grouped per table
    (``pending_choices.build_pending_choices``), scored with the pipeline's own
    ``ScoringContext``. ``([], 0)`` when no assignment exists."""
    version = version or resolve_effective_assignment_version(store, database_name, job_id)
    if version == 0:
        return [], 0
    loaded = _load_for_edit(store, database_name, job_id, version, new_version=False)
    choices = pending_choices_from(
        loaded.raw, loaded.collector_output, loaded.analysis_outputs, loaded.triage
    )
    return choices, version


def pending_choices_from(
    assignment: Mapping,
    collector_output: Mapping | None,
    analysis_outputs: Mapping[str, Mapping] | None,
    triage: Mapping | None,
) -> list[PendingEngineChoice]:
    """An assignment's pending engine choices (#1003) from the pipeline's own
    artifacts, scored the way the customer gate scores (the losing Aurora
    engine's analysis dropped, ``ScoringContext.from_pipeline``). The gate and
    synthesis both read them through here, so every surface shows the same ones."""
    from src.agents.referee.pending_choices import build_pending_choices
    from src.agents.referee.query_scoring import ScoringContext
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    collector_output = dict(collector_output or {})
    outputs = _drop_losing_aurora_analysis(
        {e: dict(a) for e, a in (analysis_outputs or {}).items() if a}, dict(assignment)
    )
    ctx = (
        ScoringContext.from_pipeline(outputs, triage or {}, collector_output).with_sources_of_truth(
            assignment_sources_of_truth(assignment)
        )
        if outputs
        else None
    )
    return build_pending_choices(
        assignment.get("query_assignments") or [],
        collector_output,
        ctx,
        SOURCE_ENGINE_TO_AURORA.get(source_database_engine(collector_output)),
    )


@dataclass
class EngineChoicesResult:
    """What applying per-table picks (and ``--auto``'s decisions) wrote (#1003)."""

    override: AssignmentOverrideResult
    picked: dict[str, str]
    decided: list[AutoPick]


def apply_engine_choices(
    store: _Store,
    database_name: str,
    job_id: str,
    picks: Mapping[str, str] | None = None,
    *,
    decide_for_me: bool = False,
    source: AssignmentSource = AssignmentSource.CUSTOMER_GATE,
    overrides: Iterable[QueryOverrideInput] = (),
    exclude_tables: list[str] | None = None,
) -> EngineChoicesResult | None:
    """Apply per-table engine picks, and with ``decide_for_me`` ``--auto``'s pick for
    every other pending choice, as ONE new version through the customer gate
    (:func:`apply_assignment_overrides`).

    ``picks`` maps a choice id (or a table's bare name) to one of its tied engines;
    a customer pick is a ``customer_pick`` Move, an ``--auto`` one an ``auto_pick``
    Move recorded in ``auto_decided_choices``. ``overrides`` and ``exclude_tables``
    (the gate's per-query edits) go into the same version; a per-query override
    wins over a table pick for its own query. Returns None when there is nothing
    to apply (no pick, no pending choice and no override). Raises
    ``pending_choices.UnknownChoice`` / ``EngineNotTied`` (nothing written) or the
    gate's own errors.
    """
    from src.agents.referee.pending_choices import choice_overrides

    choices, version = pending_choices_for_job(store, database_name, job_id)
    if version == 0:
        raise NoAssignmentFound
    engine_of, auto, picked = choice_overrides(choices, picks, decide=decide_for_me)
    explicit = list(overrides)
    if not engine_of and not explicit and not exclude_tables:
        return None
    own = {o.query_id for o in explicit if o.assigned_engine is not None}
    batch = explicit + [
        QueryOverrideInput(query_id=qid, assigned_engine=engine, auto_pick=auto.get(qid))
        for qid, engine in sorted(engine_of.items())
        if qid not in own
    ]
    result = apply_assignment_overrides(
        store, database_name, job_id, batch, exclude_tables, source=source
    )
    decided = sorted({p.choice_id: p for p in auto.values()}.values(), key=lambda p: p.choice_id)
    return EngineChoicesResult(override=result, picked=picked, decided=decided)
