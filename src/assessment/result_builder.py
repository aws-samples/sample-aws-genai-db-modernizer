"""Build an :class:`~src.assessment.models.AssessmentResult` from a job's artifacts.

One function per block of the result (:func:`build_statements`,
:func:`build_summary`, :func:`build_groups`, :func:`build_artifacts`), each reading
the internal contracts the run wrote (:class:`RunState`) and computing nothing
routing did not already decide:

- a statement's engine, tie and reason text are its effective assignment's; its
  ``reason_code`` is the public code of the last Move recorded for it across the
  assignment lineage (``src.assessment.reason_codes``);
- ``fit`` is the routing scorer's own score per engine: the candidate ladder routing
  recorded on the statement's first Move, or, at ``run_through="analysis"`` (nothing
  is placed yet), the same scorer called the way routing calls it
  (``ScoringContext.from_pipeline`` and ``score_query_for_engine``);
- ``summary.tables`` are the facts layer's table owner lines
  (``src.shared.report_facts``), from the same assignment usage synthesis reads;
- ``groups`` are the assignment's co-dependency groups and write groups.

The builder is pure over :class:`RunState`, so the next phases and fields are each a
small addition: a field reads one more input here, a phase loads one more artifact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.assessment.models import (
    Artifacts,
    AssessmentRequest,
    AssessmentResult,
    Group,
    PublicReasonCode,
    RelationalEngine,
    StatementResult,
    Summary,
    TableLine,
)
from src.assessment.reason_codes import CONSOLIDATION_CODES, public_reason_code
from src.assessment.ruleset import ruleset_hash, ruleset_version
from src.contracts.move_ledger import Move
from src.shared.engine_names import display_engine
from src.shared.package_version import get_package_version

# --------------------------------------------------------------------------- inputs


@dataclass
class RunState:
    """The internal contracts one run wrote, as the builder reads them."""

    run_through: str
    collector: dict[str, Any]
    triage: dict[str, Any]
    analyses: dict[str, dict[str, Any]] = field(default_factory=dict)
    decision_traces: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The assignment versions the effective one descends from, oldest first; empty
    # before ``reality_check``.
    lineage: list[dict[str, Any]] = field(default_factory=list)
    reality_check: dict[str, Any] | None = None

    @property
    def assignment(self) -> dict[str, Any] | None:
        """The effective assignment (the newest version of the lineage), if any."""
        return self.lineage[-1] if self.lineage else None

    @property
    def query_patterns(self) -> list[dict[str, Any]]:
        queries = (self.collector.get("queries") or {}).get("query_patterns") or []
        return [q for q in queries if isinstance(q, dict) and q.get("query_id")]

    @property
    def source_engine(self) -> str:
        from src.agents.referee.aurora_choice import source_database_engine

        return source_database_engine(self.collector)


def _analysed_engines(triage: Mapping[str, Any]) -> list[str]:
    engines: list[str] = []
    for a in triage.get("selected_agents") or []:
        engine = a.get("agent_type") if isinstance(a, dict) else a
        if engine and str(engine) not in engines:
            engines.append(str(engine))
    return engines


def load_run_state(store: Any, database_name: str, job_id: str, run_through: str) -> RunState:
    """Read the artifacts of the phases up to ``run_through`` for one job."""
    from src.shared.query_trace import assignment_lineage
    from src.storage.assignment_versioning import (
        read_assignment,
        read_reality_check_output,
        resolve_effective_assignment_version,
    )

    prefix = f"{database_name}/{job_id}"
    triage = store.read_json(f"{prefix}/referee-triage/triage.json")
    state = RunState(
        run_through=run_through,
        collector=store.read_json(f"{prefix}/collector/output.json"),
        triage=triage,
    )
    for engine in _analysed_engines(triage):
        key = f"{prefix}/analysis-{engine}/analysis.json"
        if store.exists(key):
            state.analyses[engine] = store.read_json(key)
        trace_key = f"{prefix}/analysis-{engine}/decision-trace.json"
        if store.exists(trace_key):
            state.decision_traces[engine] = store.read_json(trace_key)
    if run_through == "analysis":
        return state

    effective = resolve_effective_assignment_version(store, database_name, job_id)

    def read(version: int) -> dict | None:
        return read_assignment(store, database_name, job_id, version)

    state.lineage = assignment_lineage(read, effective) if effective else []
    # The record of the reality-check run the effective assignment descends from, never
    # one from another lineage.
    state.reality_check = read_reality_check_output(store, database_name, job_id, effective)
    return state


# --------------------------------------------------------------------------- statements


def _moves_by_query(state: RunState) -> dict[str, list[Move]]:
    from src.shared.query_trace import ledger_moves

    return {qid: [m for _, m in moves] for qid, moves in ledger_moves(state.lineage).items()}


def _ladder_fit(moves: list[Move]) -> tuple[dict[str, int], set[str]]:
    """The scorer's score per engine and the gated engines, from the first Move that
    carries a candidate ladder, plus any gate a later ladder added (a model's flag)."""
    fit: dict[str, int] = {}
    gated: set[str] = set()
    first = next((m for m in moves if m.evidence.candidates), None)
    for move in moves:
        for c in move.evidence.candidates:
            if c.gate:
                gated.add(c.engine)
            if move is first and c.score is not None:
                fit[c.engine] = int(c.score)
    return fit, gated


def _scorer_fit(state: RunState) -> Callable[[Mapping[str, Any]], tuple[dict[str, int], set[str]]]:
    """Score a statement on every analysed engine with the routing scorer, set up the
    way the assignment resolver sets it up (used before anything is placed)."""
    from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
    from src.shared.engine_names import without_placeholder_engines
    from src.tools.sql.query_facts import remember_collected_facts

    analyses = without_placeholder_engines(state.analyses)
    remember_collected_facts(state.collector)
    ctx = ScoringContext.from_pipeline(analyses, state.triage, state.collector)

    def score(query: Mapping[str, Any]) -> tuple[dict[str, int], set[str]]:
        fit: dict[str, int] = {}
        gated: set[str] = set()
        for engine in analyses:
            result = score_query_for_engine(query, engine, ctx)
            if result.score is None:
                gated.add(engine)
            else:
                fit[engine] = int(result.score)
        return fit, gated

    return score


def _signals_by_query(triage: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in triage.get("signals") or []:
        if not isinstance(s, dict) or not s.get("signal"):
            continue
        for qid in s.get("query_ids") or []:
            names = out.setdefault(str(qid), [])
            if s["signal"] not in names:
                names.append(str(s["signal"]))
    return out


def _patterns_by_query(traces: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, list[str]]]:
    """``{query_id: {engine: [pattern id, ...]}}`` from each engine's decision trace."""
    out: dict[str, dict[str, list[str]]] = {}
    for engine in sorted(traces):
        for match in traces[engine].get("query_matches") or []:
            if not isinstance(match, dict) or not match.get("query_id"):
                continue
            ids = [str(p) for p in match.get("matched_patterns") or [] if p]
            if ids:
                out.setdefault(str(match["query_id"]), {})[engine] = ids
    return out


def _scope_excluded(triage: Mapping[str, Any]) -> set[str]:
    """Engines the customer's engine scope left out (triage records each as skipped)."""
    from src.agents.referee.engine_scope import SCOPE_SKIPPED_REASON

    return {
        str(a.get("agent_type"))
        for a in triage.get("skipped_agents") or []
        if isinstance(a, dict) and a.get("reason") == SCOPE_SKIPPED_REASON and a.get("agent_type")
    }


def _rewritten(request: AssessmentRequest) -> set[str]:
    return set(request.scope.rewritten_query_ids) if request.scope else set()


@dataclass(frozen=True)
class _Placement:
    """What the effective assignment and its Moves say about one placed statement."""

    engine: str | None
    confidence: int | None
    reason_code: PublicReasonCode | None
    consolidated_from: str | None
    tied_engines: list[str]
    reason_text: str | None
    unservable_reason: str | None = None


def _unservable(uq: Mapping[str, Any]) -> _Placement:
    """A statement no engine in the engine scope can serve: no engine, its reason."""
    return _Placement(
        engine=None,
        confidence=None,
        reason_code="unservable",
        consolidated_from=None,
        tied_engines=[],
        reason_text=None,
        unservable_reason=str(uq.get("reason") or "") or None,
    )


def _placement(qa: Mapping[str, Any], history: list[Move]) -> _Placement:
    engine = str(qa.get("assigned_engine") or "") or None
    last = history[-1] if history else None
    consolidated = next(
        (
            m
            for m in reversed(history)
            if m.reason_code in CONSOLIDATION_CODES and m.to_engine == engine
        ),
        None,
    )
    return _Placement(
        engine=engine,
        # As the assignment stores it (#1373 makes it follow the final engine).
        confidence=qa.get("confidence"),
        reason_code=public_reason_code(last) if last else None,
        consolidated_from=consolidated.from_engine if consolidated else None,
        tied_engines=[str(e) for e in qa.get("tied_engines") or []],
        reason_text=qa.get("assignment_reason"),
    )


def build_statements(state: RunState, request: AssessmentRequest) -> list[StatementResult]:
    """One entry per statement: every collected statement at ``analysis``; from
    ``reality_check`` on, every placed (in-scope) statement of the effective assignment,
    then every statement it records as unservable."""
    patterns = {q["query_id"]: q for q in state.query_patterns}
    signals = _signals_by_query(state.triage)
    matched = _patterns_by_query(state.decision_traces)
    scope_out = _scope_excluded(state.triage)
    rewritten = _rewritten(request)

    def statement(
        qid: str,
        tables: list[Any],
        scored: tuple[dict[str, int], set[str]],
        placed: _Placement | None = None,
    ) -> StatementResult:
        q = patterns.get(qid) or {}
        fit, gated = scored
        return StatementResult(
            query_id=qid,
            query_tags=[str(t) for t in q.get("query_tags") or []],
            tables=[str(t) for t in tables],
            calls_per_second=q.get("calls_per_second"),
            rewritten_by_caller=qid in rewritten,
            engine=placed.engine if placed else None,
            confidence=placed.confidence if placed else None,
            reason_code=placed.reason_code if placed else None,
            consolidated_from=placed.consolidated_from if placed else None,
            excluded=sorted(gated | scope_out),
            fit=fit,
            signals=signals.get(qid, []),
            matched_patterns=matched.get(qid, {}),
            unservable_reason=placed.unservable_reason if placed else None,
            tied_engines=placed.tied_engines if placed else [],
            reason_text=placed.reason_text if placed else None,
        )

    assignment = state.assignment
    if assignment is None:
        score = _scorer_fit(state)
        return [
            statement(qid, q.get("tables_accessed") or [], score(q)) for qid, q in patterns.items()
        ]

    moves = _moves_by_query(state)
    out: list[StatementResult] = []
    for qa in assignment.get("query_assignments") or []:
        if not isinstance(qa, dict) or not qa.get("in_scope", True):
            continue
        qid = str(qa["query_id"])
        history = moves.get(qid, [])
        out.append(
            statement(
                qid, qa.get("source_tables") or [], _ladder_fit(history), _placement(qa, history)
            )
        )
    # Statements no engine in the engine scope can serve: placed nowhere, never counted
    # against an engine; the in-scope engines that were ruled out are in ``excluded``.
    for uq in assignment.get("unservable_queries") or []:
        if not isinstance(uq, dict) or not uq.get("query_id"):
            continue
        qid = str(uq["query_id"])
        fit, gated = _ladder_fit(moves.get(qid, []))
        gated |= {str(g["engine"]) for g in uq.get("gates") or [] if g.get("engine")}
        out.append(statement(qid, uq.get("source_tables") or [], (fit, gated), _unservable(uq)))
    return out


# --------------------------------------------------------------------------- summary


def build_relational_engine(state: RunState) -> RelationalEngine | None:
    """The Aurora engine for the source: the assignment's recorded choice; the engine
    scope's choice; else a homogeneous source's own Aurora engine."""
    from src.agents.referee.engine_scope import scope_aurora_choice, scope_of
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    choice = (state.assignment or {}).get("aurora_engine_choice")
    if isinstance(choice, dict) and choice.get("engine"):
        engine = str(choice["engine"])
        reason = str(choice.get("reason") or "").strip() or (
            f"{display_engine(engine)} is the Aurora engine routing chose for the source"
        )
        return RelationalEngine(engine=engine, reason=reason)
    source = state.source_engine
    scoped = scope_aurora_choice(source, state.analyses, scope_of(state.triage))
    if scoped is not None:
        return RelationalEngine(engine=scoped.engine, reason=scoped.reason)
    native = SOURCE_ENGINE_TO_AURORA.get(source)
    if native and native in state.analyses:
        return RelationalEngine(
            engine=native,
            reason=(
                f"{display_engine(native)} is the Aurora engine compatible with the "
                f"source ({source})"
            ),
        )
    return None


def build_table_lines(state: RunState, retained_engine: str | None) -> list[TableLine]:
    """Each source table the effective assignment derives a row for, with its owner
    line from the facts layer (``assignment_table_usage`` and ``table_owner_line``).
    Empty before ``reality_check``."""
    from src.shared.report_facts import assignment_table_usage, table_owner_line

    assignment = state.assignment
    if assignment is None:
        return []
    usage = assignment_table_usage(assignment)
    tables = sorted(
        {
            str(t["table_id"])
            for t in assignment.get("table_assignments") or []
            if isinstance(t, dict) and t.get("table_id")
        }
    )
    lines: list[TableLine] = []
    for table in tables:
        u = usage.get(table) or {}
        line = table_owner_line(
            u.get("writes") or {},
            u.get("queries") or {},
            u.get("cached_by") or [],
            (),
            retained_engine=retained_engine,
        )
        lines.append(TableLine(table_id=table, **line))
    return lines


def build_summary(state: RunState, statements: list[StatementResult]) -> Summary:
    from src.agents.referee.reality_check_rules import UNIQUE_DELTA_THRESHOLD

    per_engine: dict[str, int] = {}
    for s in statements:
        if s.engine:
            per_engine[s.engine] = per_engine.get(s.engine, 0) + 1
    # Statements no in-scope engine can serve (recorded by the assignment when routing
    # reports them; none are placed, so none is in ``statements_per_engine``).
    unservable = len((state.assignment or {}).get("unservable_queries") or [])
    relational = build_relational_engine(state)
    return Summary(
        run_through_reached=state.run_through,  # type: ignore[arg-type]
        statement_count=len(statements),
        statements_per_engine=dict(sorted(per_engine.items())),
        unservable_count=unservable,
        unique_delta_threshold=int(UNIQUE_DELTA_THRESHOLD),
        relational_engine=relational,
        tables=build_table_lines(state, relational.engine if relational else None),
    )


# --------------------------------------------------------------------------- groups

_CO_DEPENDENCY_REASON = "These statements join the same tables, so they stay on one engine."


def _write_group_reason(table: str) -> str:
    return f"Every statement that writes {table}: a table has one writer, so they share an engine."


def _group_sources(state: RunState) -> tuple[list[list[str]], dict[str, list[str]]]:
    """The co-dependency groups and write groups: the assignment's, or (before
    anything is placed) built from the collection by the same builders routing uses."""
    assignment = state.assignment
    if assignment is not None:
        return (
            [[str(q) for q in g] for g in assignment.get("co_dependency_groups") or []],
            {
                str(t): [str(q) for q in m]
                for t, m in (assignment.get("write_groups") or {}).items()
            },
        )
    from src.agents.referee.co_dependency import build_co_dependency_groups
    from src.agents.referee.table_resolution import SourceScope
    from src.agents.referee.write_groups import build_write_groups

    queries = state.query_patterns
    tables = (state.collector.get("database_schema") or {}).get("tables") or []
    return (
        build_co_dependency_groups(queries, tables),
        build_write_groups(
            queries, state.source_engine, SourceScope.from_collector(state.collector)
        ),
    )


def build_groups(state: RunState, statements: list[StatementResult]) -> list[Group]:
    """One entry per co-dependency group (``cd-<n>``) and per write group of two or
    more statements (``wg-<table>``)."""
    by_id = {s.query_id: s for s in statements}
    co_dependency, write_groups = _group_sources(state)

    def engine_of(members: list[str]) -> str | None:
        engines = {by_id[q].engine for q in members if q in by_id}
        return engines.pop() if len(engines) == 1 else None

    def tables_of(members: list[str]) -> list[str]:
        tables: dict[str, None] = {}
        for q in members:
            for t in by_id[q].tables if q in by_id else []:
                tables.setdefault(t, None)
        return sorted(tables)

    groups: list[Group] = []
    for n, members in enumerate(co_dependency, start=1):
        groups.append(
            Group(
                group_id=f"cd-{n}",
                kind="co_dependency",
                query_ids=members,
                tables=tables_of(members),
                engine=engine_of(members),
                reason=_CO_DEPENDENCY_REASON,
            )
        )
    for table, members in sorted(write_groups.items()):
        if len(members) < 2:
            continue
        groups.append(
            Group(
                group_id=f"wg-{table}",
                kind="write_group",
                query_ids=members,
                tables=[table],
                engine=engine_of(members),
                reason=_write_group_reason(table),
            )
        )
    return groups


# --------------------------------------------------------------------------- artifacts


def build_artifacts(state: RunState) -> Artifacts:
    """The internal contracts, verbatim, each with its own ``contract_version``."""
    blocks: dict[str, Any] = {
        "collector_output": state.collector,
        "triage": state.triage,
        "analyses": state.analyses,
        "decision_traces": state.decision_traces,
    }
    if state.assignment is not None:
        blocks["assignment"] = state.assignment
    if state.reality_check is not None:
        blocks["reality_check"] = state.reality_check
    return Artifacts(**blocks)


# --------------------------------------------------------------------------- result


def build_result(state: RunState, request: AssessmentRequest) -> AssessmentResult:
    statements = build_statements(state, request)
    return AssessmentResult(
        library_version=get_package_version(),
        ruleset_version=ruleset_version(),
        ruleset_hash=ruleset_hash(),
        options=request.options,
        scope=request.scope,
        summary=build_summary(state, statements),
        statements=statements,
        groups=build_groups(state, statements),
        artifacts=build_artifacts(state) if request.options.include_artifacts else None,
    )
