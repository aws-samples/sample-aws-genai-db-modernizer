"""A customer's engine scope (ADR-031 Decision 2 Amendment 14, #1327).

The scope is the set of engine catalog ids a customer will consider. The candidates
are Decision 2 step 1's, limited to the scope, with one change: when the scope leaves
out a homogeneous source's source-compatible Aurora engine and lists the other Aurora
engine, that one is the relational candidate (:func:`scoped_aurora_agents`). A scope
that lists both keeps the source-compatible engine as the one relational candidate,
so a scope of every engine routes as no scope does.

No engine outside the scope is ever assigned, tied or used as a query's cache: every
writer of an assignment version calls :func:`check_in_scope` next to its
``move_ledger.check_replay`` (the first assignment, the reality check, the customer
gate, the source-of-truth reroute, the post-schema reroute and its rollback), and
refuses a version outside the scope (until #1329 gives a query no in-scope engine can
serve its own unservable state). The customer gate refuses a pick or a cache pin onto
an engine the scope leaves out before anything is applied.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from src.contracts.assignment_models import AuroraEngineChoice, QueryAssignment
from src.shared.engine_names import display_engine

# The ``skipped_agents`` reason of every engine the scope leaves out.
SCOPE_SKIPPED_REASON = "Not in the customer's engine scope"

# ``AuroraEngineChoice.basis`` of a choice the scope made (the workload made none).
SCOPE_BASIS: Literal["engine_scope"] = "engine_scope"


class EngineScopeError(ValueError):
    """An engine scope that is empty, repeats an engine or names an unknown one."""


class OutOfScopeAssignment(RuntimeError):
    """A pass placed a query on an engine outside the customer's engine scope."""


def normalize_engine_scope(scope: Iterable[str] | None) -> tuple[str, ...] | None:
    """The scope as sorted engine ids, or ``None`` (every engine) when there is none.

    Every id must be an active engine of the catalog (the engines triage can
    analyse): an empty scope, a repeated id or any other id raises
    :class:`EngineScopeError` naming it.
    """
    if scope is None:
        return None
    from src.agents.referee.triage import ANALYSIS_AGENTS  # import cycle

    ids = [str(e) for e in scope]
    if not ids:
        raise EngineScopeError("the engine scope is empty: name at least one engine")
    repeated = sorted({e for e in ids if ids.count(e) > 1})
    if repeated:
        raise EngineScopeError(f"the engine scope names {', '.join(repeated)} more than once")
    unknown = sorted(set(ids) - ANALYSIS_AGENTS)
    if unknown:
        raise EngineScopeError(
            f"the engine scope names {', '.join(unknown)}, which is not a routing engine; "
            f"choose from {', '.join(sorted(ANALYSIS_AGENTS))}"
        )
    return tuple(sorted(ids))


def scope_of(triage: Mapping[str, Any] | None) -> frozenset[str] | None:
    """The scope ``triage.json`` recorded, or ``None`` (a triage written before #1327,
    or a run with no scope)."""
    scope = (triage or {}).get("engine_scope")
    return frozenset(scope) if scope is not None else None


def scoped_aurora_agents(
    source_engine: str, aurora_agents: list[str], scope: frozenset[str] | None
) -> tuple[list[str], str | None]:
    """The Aurora engines triage analyses under ``scope``, and the source-compatible
    engine the scope left out (``None`` when it left none out).

    ``aurora_agents`` are the ones the source allows (Decision 2 step 1). With no
    scope they are unchanged. A homogeneous source whose source-compatible engine is
    out of scope takes the other Aurora engine when the scope lists it; any other
    source keeps the allowed engines that are in scope.
    """
    if scope is None:
        return aurora_agents, None
    from src.agents.referee.aurora_choice import AURORA_ENGINES  # import cycle
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    native = SOURCE_ENGINE_TO_AURORA.get(source_engine)
    if native and native not in scope:
        return sorted(AURORA_ENGINES & scope), native
    return [e for e in aurora_agents if e in scope], None


def swap_reason(source_compatible: str, engine: str) -> str:
    """Why ``engine`` is the relational candidate in place of ``source_compatible``."""
    return (
        f"The customer's engine scope leaves out {display_engine(source_compatible)}, the "
        f"source-compatible engine, so {display_engine(engine)} is the relational candidate"
    )


def scope_aurora_choice(
    source_engine: str, analysed: Iterable[str], scope: frozenset[str] | None
) -> AuroraEngineChoice | None:
    """The Aurora engine the scope chose, when it chose one (``basis="engine_scope"``).

    That is when exactly one Aurora engine was analysed and it is not the source's
    own: the scope left out the source-compatible engine of a homogeneous source, or
    left one Aurora engine in for a heterogeneous source (or a source with no engine
    recorded), so no workload comparison was made (Decision 6 compares two).
    """
    if scope is None:
        return None
    from src.agents.referee.aurora_choice import AURORA_ENGINES  # import cycle
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    aurora = AURORA_ENGINES.intersection(analysed)
    native = SOURCE_ENGINE_TO_AURORA.get(source_engine)
    if len(aurora) != 1:
        return None
    (engine,) = aurora
    # The scope chose only when it left that one Aurora engine in (a missing analysis
    # of an in-scope Aurora engine is no choice of the scope's).
    if engine == native or AURORA_ENGINES & scope != {engine}:
        return None
    if native:
        reason = swap_reason(native, engine)
    else:
        reason = (
            f"The customer's engine scope includes {display_engine(engine)} and no other "
            "Aurora engine, so it is the relational candidate"
        )
    return AuroraEngineChoice(
        source_engine=source_engine, engine=engine, basis=SCOPE_BASIS, reason=reason
    )


def scope_from_store(store: Any, database_name: str, job_id: str) -> frozenset[str] | None:
    """The scope the job's ``triage.json`` recorded (``None`` with no triage or no
    scope), for a writer that has the store but not the triage document."""
    key = f"{database_name}/{job_id}/referee-triage/triage.json"
    return scope_of(store.read_json(key)) if store.exists(key) else None


def scope_text(scope: Iterable[str]) -> str:
    """The scope's engines by display name, for a message ("Aurora PostgreSQL, DynamoDB")."""
    return ", ".join(sorted(display_engine(e) for e in scope))


def _fields(qa: Mapping[str, Any] | QueryAssignment) -> tuple[str, list[str], str | None]:
    """The query id, the engines it sits on or ties with, and the engine caching it."""
    if isinstance(qa, Mapping):
        engines = [qa.get("assigned_engine"), *(qa.get("tied_engines") or [])]
        cache = qa.get("cache_engine")
        return (
            str(qa.get("query_id")),
            [str(e) for e in engines if e],
            str(cache) if cache else None,
        )
    engines = [qa.assigned_engine, *(qa.tied_engines or [])]
    return qa.query_id, [str(e) for e in engines if e], qa.cache_engine or None


def check_in_scope(
    query_assignments: Iterable[Mapping[str, Any] | QueryAssignment],
    scope: Iterable[str] | None,
    writer: str,
    *,
    error: type[OutOfScopeAssignment] = OutOfScopeAssignment,
) -> None:
    """Raise ``error`` (:class:`OutOfScopeAssignment` or a subclass) when a query sits
    on, ties with or is cached by (``cache_engine``, the cache overlay) an engine
    outside ``scope``; nothing to check with no scope.

    ``writer`` names the pass in the message, which lists every such query with the
    engine and its role ("Aurora MySQL is outside the scope", "ElastiCache, its
    cache, is outside the scope"), so the version is not written and no query is
    forced onto an engine the customer left out. A writer the customer drives (the
    customer gate, the source-of-truth reroute) passes its own subclass, which the API
    and the AWS Transform tools report as a refused edit.
    """
    if scope is None:
        return
    allowed = set(scope)
    outside: list[str] = []
    for qa in query_assignments:
        qid, engines, cache = _fields(qa)
        said = [f"{display_engine(e)} is outside the scope" for e in sorted(set(engines) - allowed)]
        if cache and cache not in allowed:
            said.append(f"{display_engine(cache)}, its cache, is outside the scope")
        if said:
            outside.append(f"{qid}: {'; '.join(said)}")
    if outside:
        listed = "; ".join(outside[:20]) + (
            f"; and {len(outside) - 20} more" if len(outside) > 20 else ""
        )
        noun = "1 query" if len(outside) == 1 else f"{len(outside)} queries"
        raise error(
            f"{writer}: {noun} would be placed outside the customer's engine scope "
            f"({scope_text(allowed)}), so nothing was written. {listed}. Add the engine "
            "to the scope to use it; a query is never forced onto an engine outside it."
        )
