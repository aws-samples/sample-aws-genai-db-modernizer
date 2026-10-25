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
refuses a version outside the scope. A query no in-scope engine can serve is unservable
(#1329): the first assignment records a Move to ``unservable`` for it
(``move_ledger.apply_unservable``) instead of the Aurora fallback the scope leaves out,
and it leaves ``query_assignments`` for ``Assignment.unservable_queries``
(:func:`unservable_record`), which every later version carries; it has no engine, so
:func:`check_in_scope` has nothing of it to refuse. The customer gate refuses a pick or
a cache pin onto an engine the scope leaves out before anything is applied, and any
edit of an unservable query.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from src.contracts.assignment_models import AuroraEngineChoice, QueryAssignment
from src.shared.engine_names import display_engine
from src.shared.target_databases import TargetDatabasesError, normalize_target_databases

# The ``skipped_agents`` reason of every engine the scope leaves out.
SCOPE_SKIPPED_REASON = "Not in the customer's engine scope"

# ``AuroraEngineChoice.basis`` of a choice the scope made (the workload made none).
SCOPE_BASIS: Literal["engine_scope"] = "engine_scope"


# The setting's one validation (#1328): a refused scope raises the setting's error, so
# the library path, triage and every entry point refuse the same things the same way.
EngineScopeError = TargetDatabasesError


class OutOfScopeAssignment(RuntimeError):
    """A pass placed a query on an engine outside the customer's engine scope."""


def normalize_engine_scope(scope: Iterable[str] | None) -> tuple[str, ...] | None:
    """The scope as sorted engine ids, or ``None`` (every engine) when there is none.

    The one rule of :func:`src.shared.target_databases.normalize_target_databases`:
    ids lower-cased and trimmed, a repeated id counted once, active catalog engines
    only (the engines triage can analyse). An empty scope or any other id raises
    :class:`EngineScopeError` (the setting's ``TargetDatabasesError``) naming it.
    """
    ids = normalize_target_databases(scope, setting="the engine scope")
    return tuple(ids) if ids is not None else None


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
            f"({scope_text(allowed)}), so nothing was written. {listed}. A job's scope is "
            "fixed once it starts: to use another engine, start a new job whose scope adds it; "
            "a query is never forced onto an engine outside the scope."
        )


# The job's recorded scope (#1328): the one ``target_databases`` setting, as given to
# the API, ``scripts/run_assessment.py --target-databases`` or the orchestrator.
DECISION_FILE = "decisions/engine_scope.json"


def decision_key(database_name: str, job_id: str) -> str:
    """The artifact key of the job's engine scope decision."""
    return f"{database_name}/{job_id}/{DECISION_FILE}"


def record_engine_scope(
    store: Any,
    database_name: str,
    job_id: str,
    target_databases: Iterable[str],
    via: Literal["api", "cli", "library"],
) -> list[str]:
    """Record the customer's scope for the job (``decisions/engine_scope.json``) and
    return it normalized; triage reads it when it is not handed a scope.

    Raises :class:`~src.shared.target_databases.TargetDatabasesError` (a ValueError)
    for a value the setting refuses, before anything is written.
    """
    from datetime import UTC, datetime

    from src.contracts.engine_scope_decision import EngineScopeDecision
    from src.shared.target_databases import normalize_target_databases

    # A list in gives a list out (``None`` only means the setting was left out).
    engines = normalize_target_databases(list(target_databases)) or []
    decision = EngineScopeDecision(
        job_id=job_id,
        database_name=database_name,
        engines=engines,
        via=via,
        decided_at=datetime.now(UTC),
    )
    store.write_json(
        f"{database_name}/{job_id}/decisions/engine_scope.json", decision.model_dump(mode="json")
    )
    return engines


def recorded_engine_scope(store: Any, database_name: str, job_id: str) -> list[str] | None:
    """The scope the job's decision recorded, or ``None`` (no decision: every engine)."""
    from src.contracts.engine_scope_decision import EngineScopeDecision
    from src.shared.contract_versioning import load_contract

    key = decision_key(database_name, job_id)
    if not store.exists(key):
        return None
    decision = load_contract(EngineScopeDecision, store.read_json(key), artifact=key)
    return list(decision.engines)


# ---------------------------------------------------------------------------
# Queries no engine in the scope can serve (#1329)
# ---------------------------------------------------------------------------


def _gate_phrase(gate: Mapping[str, Any], group: bool) -> str:
    text = str(gate.get("gate_text") or "") or "not eligible for this query"
    if group and gate.get("query_id"):
        text = f"for one of its queries: {text}"
    return f"{display_engine(str(gate.get('engine') or ''))} ({text})"


def unservable_reason(
    scope: Iterable[str],
    closest_engine: str | None,
    gates: Iterable[Mapping[str, Any]],
    *,
    group: str | None = None,
) -> str:
    """Why a query is unservable in ``scope``, in customer-facing words.

    ``group`` names the unit it moves with ("write group", "co-dependency group") when
    no in-scope engine can serve every member; ``gates`` are each in-scope engine's
    gate (``UnservableGate`` as JSON). ``closest_engine`` is the engine the fallback
    rule names, which the scope leaves out.
    """
    what = f"every query of its {group}, which moves as one" if group else "this query"
    out = f"No engine in the customer's engine scope ({scope_text(scope)}) can serve {what}"
    ruled = [_gate_phrase(g, bool(group)) for g in gates]
    out += f": {'; '.join(ruled)}." if ruled else "."
    if closest_engine:
        name = display_engine(closest_engine)
        out += (
            f" {name}, the engine it would fall back to, is outside the scope, so it has "
            f"no engine; a new assessment whose scope adds {name} can serve it."
        )
    else:
        out += " It has no engine; a new assessment with a wider scope can serve it."
    return out


def unservable_gates(
    candidates: Iterable[Any], group_gates: Iterable[Any] = ()
) -> list[dict[str, Any]]:
    """The in-scope engines that ruled a query out, by engine: a group's gates
    (``MoveEvidence.group_gates``, naming the member) when it has them, else the
    query's own gated candidates (``MoveEvidence.candidates``)."""

    from src.shared.move_reasons import gate_text

    def _field(item: Any, key: str) -> Any:
        return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)

    def _gate(item: Any, member: Any) -> dict[str, Any]:
        gate = str(_field(item, "gate"))
        return {
            "engine": str(_field(item, "engine")),
            "gate": gate,
            "query_id": member,
            "gate_text": gate_text(gate) or "",
        }

    by_group = [_gate(g, _field(g, "query_id")) for g in group_gates]
    if by_group:
        return sorted(by_group, key=lambda g: g["engine"])
    return sorted(
        (_gate(c, None) for c in candidates if _field(c, "gate")),
        key=lambda g: g["engine"],
    )


def unservable_record(
    query_id: str,
    source_tables: Iterable[str],
    *,
    rule: str,
    scope: Iterable[str],
    closest_engine: str | None,
    gates: list[dict[str, Any]],
    group_key: str | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """``Assignment.unservable_queries``' entry for ``query_id`` (as JSON)."""
    from src.contracts.assignment_models import UnservableGate, UnservableQueryAssignment

    return UnservableQueryAssignment(
        query_id=query_id,
        source_tables=[str(t) for t in source_tables],
        rule=rule,
        reason=unservable_reason(scope, closest_engine, gates, group=group),
        closest_engine=closest_engine,
        gates=[UnservableGate.model_validate(g) for g in gates],
        group_key=group_key,
    ).model_dump(mode="json")


def scope_unservable_rows(
    assignment: Mapping[str, Any] | None,
    query_capabilities: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """``report.json``'s ``scope_unservable`` (#1329): the effective assignment's
    ``unservable_queries``, by query id, each with the capabilities triage found it
    needs (``ScopeUnservableQuery`` as JSON)."""
    caps = query_capabilities or {}
    rows = [
        {
            **dict(u),
            "required_capabilities": sorted({str(c) for c in caps.get(str(u["query_id"])) or []}),
        }
        for u in (assignment or {}).get("unservable_queries") or []
        if isinstance(u, Mapping) and u.get("query_id")
    ]
    return sorted(rows, key=lambda r: str(r["query_id"]))


def without_unservable_groups(
    co_dependency_groups: Iterable[Iterable[str]],
    write_groups: Mapping[str, Iterable[str]],
    unservable: Iterable[Any],
) -> tuple[list[list[str]], dict[str, list[str]]]:
    """The co-dependency groups and write groups of an assignment's assigned queries.

    A group is unservable as one unit (#1329, ADR-031 Decision 4 and Amendment 9: it
    falls back as one), so a group whose every member is unservable is left out and
    every view of the groups reads assigned queries only. ``unservable`` is the
    assignment's ``unservable_queries`` (records or query ids)."""
    ids = {
        str(u.get("query_id") if isinstance(u, Mapping) else getattr(u, "query_id", u))
        for u in unservable
    }
    groups = [list(g) for g in co_dependency_groups]
    writes = {str(t): list(m) for t, m in write_groups.items()}
    if not ids:
        return groups, writes
    return (
        [g for g in groups if not set(g) <= ids],
        {t: m for t, m in writes.items() if not set(m) <= ids},
    )


# ---------------------------------------------------------------------------
# Analysis text a designer reads, inside the scope (#1574)
# ---------------------------------------------------------------------------

# A concern's leading anti-pattern label: "single-access-pattern-table: ..." (Aurora,
# DynamoDB) or "[full-text-search] ..." (DocumentDB).
_LABEL = re.compile(r"^((?:[a-z0-9][a-z0-9_-]*:|\[[a-z0-9][a-z0-9_-]*\])\s+)")


def scope_filtered_text(text: str | None, scope: Iterable[str] | None) -> str | None:
    """``text`` without the sentences that name an engine outside ``scope``.

    The catalog's guidance suggests other engines ("If it's a hot lookup, ElastiCache is
    more appropriate"); a designer of a scoped job must not read a suggestion the scope
    rules out. A leading ``label: `` is kept with the text after it; text with nothing
    left in scope is ``""``. With no scope, or no engine outside it named, ``text`` is
    returned as is.
    """
    if scope is None or not text:
        return text
    from src.agents.referee.synthesis_grounding import (  # import cycle
        engine_mentions,
        split_sentences,
    )

    allowed = set(scope)

    def inside(part: str) -> bool:
        return {e for e, _, _ in engine_mentions(part)} <= allowed

    if inside(text):
        return text
    if "\n" in text:
        # Line by line, so a bullet inside the scope survives next to one outside it
        # and the field keeps its lines.
        lines = [scope_filtered_text(line, allowed) or "" for line in text.split("\n")]
        return "\n".join(line for line in lines if line.strip())
    m = _LABEL.match(text)
    label = m.group(1) if m else ""
    kept = [s for s in split_sentences(text[len(label) :]) if inside(s)]
    return label + " ".join(kept) if kept else ""


def scope_filtered_analysis(
    analysis: Mapping[str, Any], scope: Iterable[str] | None
) -> dict[str, Any]:
    """A copy of an engine's ``analysis.json`` whose prose names no engine outside
    ``scope`` (:func:`scope_filtered_text`): each table recommendation's ``rationale``
    and ``concerns`` and each detected pattern's and anti-pattern's ``description`` and
    ``recommendation``. A concern left empty is dropped; an emptied field is None. With
    no scope the analysis is returned unchanged."""
    if scope is None:
        return dict(analysis)
    allowed = tuple(scope)

    def text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return scope_filtered_text(value, allowed) or None

    def rec(r: Any) -> Any:
        if not isinstance(r, Mapping):
            return r
        out = dict(r)
        if "rationale" in out:
            out["rationale"] = text(out["rationale"])
        if isinstance(out.get("concerns"), list):
            out["concerns"] = [c for c in (text(c) for c in out["concerns"]) if c]
        return out

    def pattern(p: Any) -> Any:
        if not isinstance(p, Mapping):
            return p
        out = dict(p)
        for key in ("description", "recommendation"):
            if key in out:
                out[key] = text(out[key])
        return out

    out = dict(analysis)
    if isinstance(out.get("table_recommendations"), list):
        out["table_recommendations"] = [rec(r) for r in out["table_recommendations"]]
    workload = out.get("workload_analysis")
    if isinstance(workload, Mapping):
        workload = dict(workload)
        for key in ("patterns_detected", "anti_patterns_detected"):
            if isinstance(workload.get(key), list):
                workload[key] = [pattern(p) for p in workload[key]]
        out["workload_analysis"] = workload
    return out
