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
  (``src.shared.report_facts``), from the same assignment usage synthesis reads; at
  ``run_through="synthesis"``, the report's own table owner lines
  (``facts.table_owner_lines``) for every table it maps;
- ``summary.engines`` are the analysed engines with the facts layer's per-engine line
  (the executive report's role and "why" text, from synthesis's own ranking of the
  effective assignment and any schema design of it; at ``synthesis``, the report's
  ``facts.engines``) and the reality check's unique-value figures;
  ``summary.consolidations`` are the reality check's consolidations;
- a statement's ``matched_patterns`` are its assigned engine's decision-trace matches,
  named by the pattern catalog (``src/catalog/patterns``);
- ``groups`` are the assignment's co-dependency groups and write groups;
- ``designs`` (a designed result only) are each designed engine's target tables, keys
  and indexes, copied from its schema output (:func:`build_designs`);
- a statement's ``post_schema_terminal`` is the post-schema router's terminal record
  for it (``post-schema-router/router_output.json`` ``terminal``), while the effective
  assignment keeps it in scope on the flagged engine, the rule synthesis applies to the
  same records (``src.agents.referee.flagged_unservable``);
- a statement's ``fit_breakdown`` (contract 1.2) is the routing scorer's own account of
  each score in ``fit`` (``score_query_for_engine(..., explain=True)``, the same
  arithmetic), and its ``matched_anti_patterns`` the scorer's labelled findings
  (``ScoringContext.anti_pattern_findings``);
- ``summary.table_facts`` (contract 1.2) are the collector output's tables, as the
  library read them from the collection.

The builder is pure over :class:`RunState`, so the next phases and fields are each a
small addition: a field reads one more input here, a phase loads one more artifact.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, get_args

from src.assessment.models import (
    AntiPatternRef,
    Artifacts,
    AssessmentRequest,
    AssessmentResult,
    AuroraDesign,
    AuroraTable,
    CacheKey,
    Consolidation,
    DocumentDBCollection,
    DocumentDBDesign,
    DocumentDBEmbed,
    DocumentDBIndex,
    DocumentDBIndexKey,
    DynamoDBDesign,
    DynamoDBEntity,
    DynamoDBIndex,
    DynamoDBKey,
    DynamoDBTable,
    ElastiCacheDesign,
    EngineDesign,
    EngineLine,
    FitBreakdown,
    FlagReasonCode,
    Group,
    OpenSearchDataStream,
    OpenSearchDesign,
    OpenSearchField,
    OpenSearchIndex,
    PatternRef,
    PostSchemaTerminal,
    PublicReasonCode,
    RelationalEngine,
    StatementResult,
    Summary,
    TableColumn,
    TableFacts,
    TableForeignKey,
    TableLine,
    result_label,
)
from src.assessment.reason_codes import CONSOLIDATION_CODES, public_reason_code
from src.assessment.ruleset import ruleset_hash, ruleset_version
from src.contracts.move_ledger import Move
from src.shared.engine_names import display_engine
from src.shared.model_usage import USAGE_KEY
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
    # The facts layer's per-engine lines (``report_facts.engine_facts``) for the
    # effective assignment, by engine: the executive report's role and "why" text.
    # Empty before ``reality_check``.
    report_engines: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Each designed engine's schema output at the effective version, by engine, in
    # design order; empty before ``schema_design``.
    schema_designs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The synthesis report of the effective version; None before ``synthesis``.
    synthesis_report: dict[str, Any] | None = None
    # The post-schema router's terminal records (``TerminalQuery``, as written); empty
    # before ``schema_design``.
    router_terminal: list[dict[str, Any]] = field(default_factory=list)
    # The store key each design and the report were read from.
    artifact_keys: dict[str, Any] = field(default_factory=dict)
    # The run's model token usage by phase and model (``_logs/model_usage.json``, #1494);
    # None when the run called no model.
    model_usage: dict[str, Any] | None = None

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
    usage_key = f"{prefix}/{USAGE_KEY}"
    if store.exists(usage_key):
        state.model_usage = store.read_json(usage_key)
        state.artifact_keys["model_usage"] = usage_key
    if run_through == "analysis":
        return state

    effective = resolve_effective_assignment_version(store, database_name, job_id)

    def read(version: int) -> dict | None:
        return read_assignment(store, database_name, job_id, version)

    state.lineage = assignment_lineage(read, effective) if effective else []
    # The record of the reality-check run the effective assignment descends from, never
    # one from another lineage.
    state.reality_check = read_reality_check_output(store, database_name, job_id, effective)
    if not effective:
        return state
    designed = run_through in ("schema_design", "synthesis")
    if designed:
        _load_designs(state, store, database_name, job_id, effective)
        router_key = f"{database_name}/{job_id}/{ROUTER_OUTPUT}"
        if store.exists(router_key):
            state.router_terminal = list(store.read_json(router_key).get("terminal") or [])
    if run_through == "synthesis":
        # Where ``run_synthesis_core`` writes a versioned report.
        key = f"{database_name}/{job_id}/synthesis/v{effective}/report.json"
        state.synthesis_report = store.read_json(key)
        state.artifact_keys["synthesis_report"] = key
        facts = state.synthesis_report.get("facts") or {}
        state.report_engines = {
            str(r["engine"]): r for r in facts.get("engines") or [] if isinstance(r, dict)
        }
    else:
        state.report_engines = report_engine_lines(store, database_name, job_id, effective)
    return state


#: Where the post-schema routing writes its cumulative output, under the job.
ROUTER_OUTPUT = "post-schema-router/router_output.json"


def _load_designs(
    state: RunState, store: Any, database_name: str, job_id: str, version: int
) -> None:
    """The schema outputs the designers wrote at ``version``, for the engines schema
    design ran for."""
    from src.storage.assignment_versioning import schema_design_engines

    keys: dict[str, str] = {}
    for engine in schema_design_engines(store, database_name, job_id, version):
        key = f"{database_name}/{job_id}/schema-{engine}/v{version}/schema_output.json"
        if store.exists(key):
            state.schema_designs[engine] = store.read_json(key)
            keys[engine] = key
    state.artifact_keys["schema_designs"] = keys


def report_engine_lines(
    store: Any, database_name: str, job_id: str, version: int
) -> dict[str, dict[str, Any]]:
    """The executive report's per-engine lines for assignment ``version``: synthesis's
    own ranking of it (``build_ranking`` over ``load_synthesis_data``, as the report
    builds it, with any schema design of it) through the facts layer
    (``engine_facts``). Only the engines that keep a part in the architecture are in
    it (an owner or the cache layer)."""
    from src.agents.referee.synthesis_data import load_synthesis_data
    from src.agents.referee.synthesis_report import build_ranking
    from src.shared.report_facts import engine_facts

    # A missing design is no warning here: before schema design none can exist, and
    # after it ``run_schema_design_core`` has checked that every design was written.
    data = load_synthesis_data(store, job_id, database_name, version, schema_design_expected=False)
    rows = engine_facts({"ranking": build_ranking(data)}, [])
    return {str(r["engine"]): r for r in rows}


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


def _first_ladder(moves: list[Move]) -> dict[str, str | None]:
    """``{engine: gate kind or None}`` of the first Move that carries a candidate ladder
    (the one ``fit`` is read from)."""
    first = next((m for m in moves if m.evidence.candidates), None)
    return {c.engine: c.gate for c in first.evidence.candidates} if first else {}


#: A statement's scores: ``(fit, gated engines, fit_breakdown)``.
Scored = tuple[dict[str, int], set[str], dict[str, FitBreakdown]]


class _Scorer:
    """The routing scorer set up the way the assignment resolver sets it up
    (``ScoringContext.from_pipeline`` over the analysed engines), for the scores before
    anything is placed and for every statement's fit breakdown and anti-patterns."""

    def __init__(self, state: RunState) -> None:
        from src.agents.referee.query_scoring import ScoringContext
        from src.shared.engine_names import without_placeholder_engines
        from src.tools.sql.query_facts import remember_collected_facts

        self.analyses = without_placeholder_engines(state.analyses)
        remember_collected_facts(state.collector)
        self.ctx = ScoringContext.from_pipeline(self.analyses, state.triage, state.collector)
        self.queries = {q["query_id"]: q for q in state.query_patterns}

    def score(self, query: Mapping[str, Any]) -> Scored:
        """Every analysed engine's score or gate, and its breakdown, from one scorer
        call per engine (before anything is placed)."""
        from src.agents.referee.query_scoring import score_query_for_engine

        fit: dict[str, int] = {}
        gated: set[str] = set()
        breakdown: dict[str, FitBreakdown] = {}
        for engine in self.analyses:
            result = score_query_for_engine(query, engine, self.ctx, explain=True)
            if result.score is None:
                gated.add(engine)
            else:
                fit[engine] = int(result.score)
            breakdown[engine] = FitBreakdown.model_validate(result.breakdown)
        return fit, gated, breakdown

    def explain(
        self, qid: str, ladder: Mapping[str, str | None], fit: Mapping[str, int]
    ) -> dict[str, FitBreakdown]:
        """The breakdown of each engine on a placed statement's candidate ladder
        (``{engine: gate kind or None}``, its first Move's). An engine with a score in
        ``fit`` is explained past the gates: routing scored it (a capability waiver,
        or the guarantee for a homogeneous source's own Aurora engine, can lift a gate
        the plain scorer applies), so its breakdown has its ``fit``. A gated one has
        the gate the ladder records, with the scorer's reason for it. A rung with neither
        a score nor a gate has nothing to explain."""
        from src.agents.referee.query_scoring import (
            GATE_KIND_MODEL_FLAG,
            score_query_for_engine,
        )

        query = self.queries.get(qid)
        if query is None:
            return {}
        out: dict[str, FitBreakdown] = {}
        for engine in sorted({e for e, gate in ladder.items() if gate} | set(fit)):
            if engine in fit:
                result = score_query_for_engine(
                    query, engine, self.ctx, explain=True, apply_gates=False
                )
                out[engine] = FitBreakdown.model_validate(result.breakdown)
                continue
            kind = str(ladder[engine])
            ctx = (
                self.ctx.with_flag_gaps({qid: [engine]})
                if kind == GATE_KIND_MODEL_FLAG
                else self.ctx
            )
            result = score_query_for_engine(query, engine, ctx, explain=True)
            reason = (
                result.gate_reason
                if result.gate_kind == kind and result.gate_reason
                else f"{display_engine(engine)} was ruled out for this statement."
            )
            out[engine] = FitBreakdown.model_validate({"gate": {"kind": kind, "reason": reason}})
        return out

    def anti_patterns(self) -> dict[str, list[AntiPatternRef]]:
        """``{query_id: [anti-pattern, ...]}``: each statement's penalised findings, one
        entry per pattern (a finding's kind is the statement's, whatever the engine),
        in catalog id order."""
        from src.shared.catalog.patterns import penalty_pattern

        out: dict[str, dict[str, AntiPatternRef]] = {}
        for finding in self.ctx.anti_pattern_findings:
            entry = penalty_pattern(finding.pattern)
            pid = entry.id if entry else finding.pattern
            found = out.setdefault(finding.query_id, {})
            if pid not in found:
                found[pid] = AntiPatternRef(
                    id=pid, name=entry.name if entry else pid, intrinsic=not finding.fixable
                )
        return {qid: [found[p] for p in sorted(found)] for qid, found in out.items()}


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
    """``{query_id: {engine: [catalog id, ...]}}`` from each engine's decision trace."""
    out: dict[str, dict[str, list[str]]] = {}
    for engine in sorted(traces):
        for match in traces[engine].get("query_matches") or []:
            if not isinstance(match, dict) or not match.get("query_id"):
                continue
            ids = [str(p) for p in match.get("matched_patterns") or [] if p]
            if ids:
                out.setdefault(str(match["query_id"]), {})[engine] = ids
    return out


def _named_patterns(engine: str | None, catalog_ids: list[str]) -> list[PatternRef]:
    """The catalog patterns behind ``engine``'s matched ``catalog_ids`` (each engine
    catalog id belongs to one pattern file), once each, in the trace's order."""
    from src.shared.catalog.patterns import engine_pattern

    if not engine:
        return []
    out: dict[str, PatternRef] = {}
    for cid in catalog_ids:
        entry = engine_pattern(engine, cid)
        if entry is None:
            raise ValueError(f"{engine} decision trace names {cid!r}, which no pattern file has")
        out.setdefault(entry.id, PatternRef(id=entry.id, name=entry.name))
    return list(out.values())


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


_FLAG_REASONS = frozenset(get_args(FlagReasonCode))


def post_schema_terminals(state: RunState) -> dict[str, PostSchemaTerminal]:
    """``{query_id: marker}`` for each post-schema router terminal record whose
    statement the effective assignment keeps in scope on the flagged engine (a later
    move clears it, as synthesis's ``flagged_unservable`` rows are cleared). The
    sentence is the one the deliverables state for it."""
    from src.agents.referee.flagged_unservable import (
        SOURCE_DESIGN_REVIEW,
        flagged_unservable_explanation,
    )
    from src.contracts.post_schema_router_output import TerminalQuery

    engine_of = {
        str(qa["query_id"]): qa.get("assigned_engine")
        for qa in (state.assignment or {}).get("query_assignments") or []
        if isinstance(qa, dict) and qa.get("query_id") and qa.get("in_scope", True)
    }
    out: dict[str, PostSchemaTerminal] = {}
    for raw in state.router_terminal:
        record = TerminalQuery.model_validate(raw)
        if record.query_id in out or engine_of.get(record.query_id) != record.flagged_engine:
            continue
        category = str(record.reason_category or "other")
        if category not in _FLAG_REASONS:
            category = "other"
        out[record.query_id] = PostSchemaTerminal(
            engine=record.flagged_engine,
            flag_reason=category,  # type: ignore[arg-type]
            cause=record.cause,
            reason=flagged_unservable_explanation(
                SOURCE_DESIGN_REVIEW, record.cause, record.flagged_engine, category
            ),
        )
    return out


def build_statements(state: RunState, request: AssessmentRequest) -> list[StatementResult]:
    """One entry per statement: every collected statement at ``analysis``; from
    ``reality_check`` on, every placed (in-scope) statement of the effective assignment,
    then every statement it records as unservable."""
    patterns = {q["query_id"]: q for q in state.query_patterns}
    signals = _signals_by_query(state.triage)
    matched = _patterns_by_query(state.decision_traces)
    scope_out = _scope_excluded(state.triage)
    rewritten = _rewritten(request)
    terminal = post_schema_terminals(state)
    scorer = _Scorer(state)
    anti_patterns = scorer.anti_patterns()

    def statement(
        qid: str,
        tables: list[Any],
        scored: Scored,
        placed: _Placement | None = None,
    ) -> StatementResult:
        q = patterns.get(qid) or {}
        fit, gated, breakdown = scored
        engine = placed.engine if placed else None
        return StatementResult(
            query_id=qid,
            query_tags=[str(t) for t in q.get("query_tags") or []],
            query_type=q.get("query_type"),
            join_count=q.get("join_count"),
            tables=[str(t) for t in tables],
            calls_per_second=q.get("calls_per_second"),
            rewritten_by_caller=qid in rewritten,
            engine=engine,
            confidence=placed.confidence if placed else None,
            reason_code=placed.reason_code if placed else None,
            consolidated_from=placed.consolidated_from if placed else None,
            excluded=sorted(gated | scope_out),
            fit=fit,
            signals=signals.get(qid, []),
            matched_patterns=_named_patterns(
                engine, (matched.get(qid) or {}).get(engine or "", [])
            ),
            unservable_reason=placed.unservable_reason if placed else None,
            tied_engines=placed.tied_engines if placed else [],
            reason_text=placed.reason_text if placed else None,
            post_schema_terminal=terminal.get(qid) if placed and engine else None,
            query_text=q.get("query_text"),
            matched_anti_patterns=anti_patterns.get(qid, []),
            fit_breakdown=breakdown,
        )

    def from_ladder(qid: str, history: list[Move], extra_gates: Collection[str] = ()) -> Scored:
        fit, gated = _ladder_fit(history)
        return fit, gated | set(extra_gates), scorer.explain(qid, _first_ladder(history), fit)

    assignment = state.assignment
    if assignment is None:
        return [
            statement(qid, q.get("tables_accessed") or [], scorer.score(q))
            for qid, q in patterns.items()
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
                qid,
                qa.get("source_tables") or [],
                from_ladder(qid, history),
                _placement(qa, history),
            )
        )
    # Statements no engine in the engine scope can serve: placed nowhere, never counted
    # against an engine; the in-scope engines that were ruled out are in ``excluded``.
    for uq in assignment.get("unservable_queries") or []:
        if not isinstance(uq, dict) or not uq.get("query_id"):
            continue
        qid = str(uq["query_id"])
        gates = {str(g["engine"]) for g in uq.get("gates") or [] if g.get("engine")}
        scored = from_ladder(qid, moves.get(qid, []), gates)
        out.append(statement(qid, uq.get("source_tables") or [], scored, _unservable(uq)))
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
    from src.shared.report_facts import (
        assignment_table_usage,
        table_owner_line,
        table_owner_reason,
    )

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
        writes, queries = u.get("writes") or {}, u.get("queries") or {}
        line = table_owner_line(
            writes, queries, u.get("cached_by") or [], (), retained_engine=retained_engine
        )
        reason = table_owner_reason(line, writes, queries, retained_engine)
        lines.append(TableLine(table_id=table, **line, reason=reason))
    stated = _report_table_lines(state)
    if not stated:
        return lines
    # At synthesis the report's own lines, for every table it maps (a table only a
    # schema design maps included); the others keep the assignment's line.
    by_table = {line.table_id: line for line in lines}
    for table, row in stated.items():
        u = usage.get(table) or {}
        line = {k: row.get(k) for k in _OWNER_FIELDS}
        line["also_read_by"] = [str(e) for e in line["also_read_by"] or []]
        reason = table_owner_reason(
            line, u.get("writes") or {}, u.get("queries") or {}, retained_engine
        )
        by_table[table] = TableLine(table_id=table, **line, reason=reason)
    return [by_table[t] for t in sorted(by_table)]


_OWNER_FIELDS = ("owner", "owner_basis", "also_read_by", "cached_by")


def _report_table_lines(state: RunState) -> dict[str, dict[str, Any]]:
    """``{table: owner line}`` from the synthesis report's ``facts.table_owner_lines``
    (``table_ownership_facts``); empty before synthesis or for a report with no map."""
    facts = (state.synthesis_report or {}).get("facts") or {}
    owner_lines = facts.get("table_owner_lines") or {}
    return {
        str(row["table"]): row
        for row in owner_lines.get("table_lines") or []
        if isinstance(row, dict) and row.get("table")
    }


def build_consolidations(state: RunState) -> list[Consolidation]:
    """The reality check's consolidations, as it recorded them."""
    return [
        Consolidation(
            from_engine=str(c["from_engine"]),
            to_engine=str(c["to_engine"]),
            query_count=int(c.get("query_count") or 0),
            reason=str(c.get("reason") or ""),
            # The record's own default: a record with no action is a full one.
            action=c.get("action") or "full",
            queries_retained=[str(q) for q in c.get("queries_retained") or []],
        )
        for c in (state.reality_check or {}).get("consolidations") or []
        if isinstance(c, dict) and c.get("from_engine") and c.get("to_engine")
    ]


def _not_selected_reason(engine: str, relational: RelationalEngine | None) -> str:
    from src.agents.referee.aurora_choice import AURORA_ENGINES

    if relational is not None and engine in AURORA_ENGINES and engine != relational.engine:
        return (
            f"Not the Aurora engine chosen for the source; {display_engine(relational.engine)} "
            f"was chosen: {relational.reason.rstrip('.')}."
        )
    return f"Analysed; no statement was placed on {display_engine(engine)}."


def build_engine_lines(
    state: RunState,
    per_engine: Mapping[str, int],
    consolidations: list[Consolidation],
    relational: RelationalEngine | None,
) -> list[EngineLine]:
    """One line per analysed engine (the analyses' order). Its role: ``owner`` when
    statements are placed on it; ``cache_layer`` when the facts layer's line for it is
    the cache layer; ``consolidated`` when synthesis's elimination rule
    (``eliminated_engines``) names it, with the engine that absorbed it; else
    ``not_selected``. Empty before ``reality_check``."""
    from src.agents.referee.synthesis_grounding import eliminated_engines
    from src.shared.engine_names import without_placeholder_engines
    from src.shared.report_facts import CACHE_LAYER

    assignment = state.assignment
    if assignment is None:
        return []
    effective = {
        str(e)
        for qa in assignment.get("query_assignments") or []
        if isinstance(qa, dict) and qa.get("in_scope", True)
        for e in (qa.get("assigned_engine"), qa.get("cache_engine"))
        if e
    }
    # No ``later_moves`` (synthesis passes the moves since the reality check): assess()
    # applies no customer edit after it, so the check's own consolidations decide the
    # absorber. A path that reads an edited lineage must pass them, as synthesis does.
    absorbed_by = eliminated_engines(effective, state.reality_check)
    unique = (state.reality_check or {}).get("unique_value_assessment") or {}
    first_reason: dict[str, str] = {}
    for c in consolidations:
        first_reason.setdefault(c.from_engine, c.reason)
    lines: list[EngineLine] = []
    for engine in without_placeholder_engines(state.analyses):
        count = int(per_engine.get(engine, 0))
        report = state.report_engines.get(engine) or {}
        uva = unique.get(engine) if isinstance(unique.get(engine), dict) else None
        into: str | None = None
        if count:
            role = "owner"
        elif report.get("role") == CACHE_LAYER:
            role = "cache_layer"
        elif engine in absorbed_by:
            role, into = "consolidated", absorbed_by[engine]
        else:
            role = "not_selected"
        if role in ("owner", "cache_layer"):
            reason = str(report.get("rationale") or "")
        elif role == "consolidated":
            reason = first_reason.get(engine, "")
        else:
            reason = _not_selected_reason(engine, relational)
        if not reason:
            # Not reached on any sample: a line the report or the check left without text.
            name = display_engine(engine)
            reason = {
                "owner": f"{name} serves {count} {'statement' if count == 1 else 'statements'}.",
                "cache_layer": f"{name} is the cache in front of other engines' reads.",
                "consolidated": "The reality check moved its statements to "
                + (f"{display_engine(into)}." if into else "other engines."),
            }[role]
        lines.append(
            EngineLine(
                engine=engine,
                role=role,  # type: ignore[arg-type]
                statement_count=count,
                consolidated_into=into,
                unique_queries=len(uva.get("unique_queries") or []) if uva else None,
                unique_query_ids=[str(q) for q in uva.get("unique_queries") or []] if uva else None,
                avg_delta=(
                    float(uva["avg_delta"]) if uva and uva.get("avg_delta") is not None else None
                ),
                reason=reason,
            )
        )
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
    consolidations = build_consolidations(state)
    version = (state.assignment or {}).get("version")
    return Summary(
        run_through_reached=state.run_through,  # type: ignore[arg-type]
        source_engine=state.source_engine,
        assignment_version=int(version) if version else None,
        statement_count=len(statements),
        statements_per_engine=dict(sorted(per_engine.items())),
        unservable_count=unservable,
        post_schema_terminal_count=sum(1 for s in statements if s.post_schema_terminal),
        unique_delta_threshold=int(UNIQUE_DELTA_THRESHOLD),
        relational_engine=relational,
        engines=build_engine_lines(state, per_engine, consolidations, relational),
        consolidations=consolidations,
        tables=build_table_lines(state, relational.engine if relational else None),
        **_design_lines(state),
        table_facts=build_table_facts(state),
    )


def _referenced_table_id(
    fk: Mapping[str, Any], schema: str | None, tables: list[Mapping[str, Any]]
) -> str | None:
    """The ``table_id`` a foreign key's ``referenced_table`` names: a table id as is; else
    the table of that name in the referencing table's schema; else the one collected
    table of that name. None when the collection holds no such table, or several."""
    ref = str(fk.get("referenced_table") or "")
    ids = {str(t.get("table_id")) for t in tables}
    if ref in ids:
        return ref
    named = [t for t in tables if str(t.get("table_name") or "") == ref]
    same_schema = [t for t in named if schema and t.get("schema_name") == schema]
    for found in (same_schema, named):
        if len(found) == 1:
            return str(found[0]["table_id"])
    return None


def build_table_facts(state: RunState) -> list[TableFacts]:
    """One entry per collected source table (``database_schema.tables``), by table id:
    the facts the collector read from the collection, with each foreign key's
    referenced table resolved to its table id. Available from ``analysis`` on."""
    tables = [
        t
        for t in (state.collector.get("database_schema") or {}).get("tables") or []
        if isinstance(t, Mapping) and t.get("table_id")
    ]
    out: list[TableFacts] = []
    for t in sorted(tables, key=lambda t: str(t["table_id"])):
        columns = sorted(
            (c for c in t.get("columns") or [] if isinstance(c, Mapping) and c.get("column_name")),
            key=lambda c: (
                c.get("ordinal_position") is None,
                c.get("ordinal_position") or 0,
                str(c["column_name"]),
            ),
        )
        out.append(
            TableFacts(
                table_id=str(t["table_id"]),
                row_count=t.get("row_count"),
                size_mb=t.get("size_mb"),
                columns=[
                    TableColumn(
                        name=str(c["column_name"]),
                        data_type=str(c.get("data_type") or ""),
                        nullable=c.get("nullable"),
                    )
                    for c in columns
                ],
                primary_key=_strs(t.get("primary_key")),
                foreign_keys=[
                    TableForeignKey(
                        constraint_name=str(fk.get("constraint_name") or ""),
                        columns=_strs(fk.get("columns")),
                        referenced_table=_referenced_table_id(fk, t.get("schema_name"), tables),
                        referenced_columns=_strs(fk.get("referenced_columns")),
                    )
                    for fk in t.get("foreign_keys") or []
                    if isinstance(fk, Mapping)
                ],
            )
        )
    return out


NO_DESIGN_NOTE = (
    "No schema design ran: no engine in the engine scope serves a statement or fronts "
    "its reads, so the result has no data model."
)


def _design_lines(state: RunState) -> dict[str, Any]:
    """``designed_engines`` (the engines with a schema design that is not a skipped
    placeholder) and, when a model phase designed none, ``design_note``."""
    if state.run_through not in ("schema_design", "synthesis"):
        return {}
    designed = [e for e, d in state.schema_designs.items() if d.get("status") != "skipped"]
    return {"designed_engines": designed, "design_note": None if designed else NO_DESIGN_NOTE}


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


# --------------------------------------------------------------------------- designs
#
# Each reads one engine's schema output (its internal contract) and copies the values a
# caller needs; nothing is recomputed. Fields a design leaves out (an older or partial
# design) read as empty; an entry with no name or no key is left out, since the stable
# shape needs one.


def _strs(values: Any) -> list[str]:
    return [str(v) for v in values or [] if v is not None]


def _ids(patterns: list[Mapping[str, Any]], field_name: str) -> list[str]:
    """The query ids of ``patterns``, first seen first, each once."""
    ids: dict[str, None] = {}
    for p in patterns:
        for q in _strs(p.get(field_name)):
            ids.setdefault(q, None)
    return list(ids)


def _in_scope(design: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The design's access patterns it serves (``in_scope`` defaults to true)."""
    return [
        p
        for p in design.get("access_patterns") or []
        if isinstance(p, Mapping) and p.get("in_scope", True) is not False
    ]


def _dynamodb_key(key: Any) -> DynamoDBKey | None:
    if not isinstance(key, Mapping) or not key.get("attribute_name"):
        return None
    return DynamoDBKey.model_validate(
        {"name": str(key["attribute_name"]), "type": key.get("attribute_type")}
    )


def _dynamodb_keys(keys: Any) -> list[DynamoDBKey]:
    return [k for k in (_dynamodb_key(key) for key in keys or []) if k is not None]


#: The aggregate patterns the stable entry names (the DynamoDB design contract's).
_AGGREGATE_PATTERNS = frozenset({"identifying_relationship", "item_collection", "separate"})


def _dynamodb_design(design: Mapping[str, Any]) -> DynamoDBDesign:
    patterns = _in_scope(design)

    def served(table: str, gsi: str | None) -> list[str]:
        return _ids(
            [p for p in patterns if p.get("table_name") == table and p.get("gsi_name") == gsi],
            "query_ids",
        )

    tables: list[DynamoDBTable] = []
    for t in design.get("table_definitions") or []:
        partition_key = _dynamodb_key(t.get("partition_key"))
        if not t.get("table_name") or partition_key is None:
            continue
        name = str(t["table_name"])
        gsis = [
            DynamoDBIndex.model_validate(
                {
                    "name": str(g["gsi_name"]),
                    "partition_key": _dynamodb_keys(g.get("partition_key")),
                    "sort_key": _dynamodb_keys(g.get("sort_key")),
                    "projection": g.get("projection"),
                    "projected_attributes": _strs(g.get("projected_attributes")),
                    "query_ids": served(name, str(g["gsi_name"])),
                }
            )
            for g in t.get("gsis") or []
            if isinstance(g, Mapping) and g.get("gsi_name")
        ]
        tables.append(
            DynamoDBTable(
                name=name,
                source_tables=_strs(t.get("source_tables")),
                partition_key=partition_key,
                sort_key=_dynamodb_key(t.get("sort_key")),
                gsis=gsis,
                query_ids=served(name, None),
                aggregate_pattern=(
                    t["aggregate_pattern"]
                    if t.get("aggregate_pattern") in _AGGREGATE_PATTERNS
                    else None
                ),
                entities=[
                    DynamoDBEntity(
                        source_table=str(e["source_table"]),
                        pk_template=str(e.get("pk_template") or ""),
                        sk_template=str(e.get("sk_template") or ""),
                    )
                    for e in t.get("entities") or []
                    if isinstance(e, Mapping) and e.get("source_table")
                ],
            )
        )
    return DynamoDBDesign(
        engine="dynamodb", validation_passed=design.get("validation_passed") is True, tables=tables
    )


def _aurora_design(engine: str, design: Mapping[str, Any]) -> AuroraDesign:
    tables = [
        AuroraTable(
            name=str(t["table_name"]),
            primary_key=_strs(t.get("primary_key")),
            indexes=_strs(t.get("indexes")),
        )
        for t in design.get("table_definitions") or []
        if isinstance(t, Mapping) and t.get("table_name")
    ]
    return AuroraDesign(
        engine=engine,  # type: ignore[arg-type]
        validation_passed=design.get("validation_passed") is True,
        tables=tables,
        ddl_artifact_path=f"artifacts.schema_designs.{engine}.generated_ddl",
    )


def _documentdb_design(design: Mapping[str, Any]) -> DocumentDBDesign:
    patterns = _in_scope(design)
    collections: list[DocumentDBCollection] = []
    for c in design.get("collections") or []:
        if not isinstance(c, Mapping) or not c.get("collection_name"):
            continue
        name = str(c["collection_name"])
        collections.append(
            DocumentDBCollection(
                name=name,
                source_tables=_strs(c.get("source_tables")),
                embedded=[
                    DocumentDBEmbed.model_validate(
                        {
                            "source_table": str(e["source_table"]),
                            "path": str(e.get("embed_path") or ""),
                            "strategy": e.get("strategy"),
                        }
                    )
                    for e in c.get("embedded_entities") or []
                    if isinstance(e, Mapping) and e.get("source_table")
                ],
                referenced_collections=_strs(c.get("referenced_collections")),
                indexes=[
                    DocumentDBIndex(
                        name=str(i["index_name"]),
                        keys=[
                            DocumentDBIndexKey(field=str(f), direction=d)
                            for f, d in (i.get("keys") or {}).items()
                        ],
                        # The contract's own rule: a null type reads as compound.
                        type=i.get("index_type") or "compound",
                        query_ids=_strs(i.get("source_query_ids")),
                    )
                    for i in c.get("indexes") or []
                    if isinstance(i, Mapping) and i.get("index_name")
                ],
                query_ids=_ids(
                    [p for p in patterns if p.get("collection_name") == name], "source_query_ids"
                ),
            )
        )
    return DocumentDBDesign(
        engine="documentdb",
        validation_passed=design.get("validation_passed") is True,
        collections=collections,
    )


def _elasticache_design(design: Mapping[str, Any]) -> ElastiCacheDesign:
    patterns = _in_scope(design)
    keys = [
        CacheKey(
            pattern=str(k["key_pattern"]),
            data_type=str(k.get("data_type") or ""),
            ttl_seconds=k.get("ttl_seconds"),
            source_tables=_strs(k.get("source_tables")),
            query_ids=_ids(
                [p for p in patterns if p.get("key_pattern") == k["key_pattern"]],
                "source_query_ids",
            ),
        )
        for k in design.get("key_designs") or []
        if isinstance(k, Mapping) and k.get("key_pattern")
    ]
    return ElastiCacheDesign(
        engine="elasticache", validation_passed=design.get("validation_passed") is True, keys=keys
    )


def _opensearch_fields(mappings: Any) -> list[OpenSearchField]:
    return [
        OpenSearchField(name=str(f["field_name"]), type=str(f["field_type"]))
        for f in mappings or []
        if isinstance(f, Mapping) and f.get("field_name") and f.get("field_type")
    ]


def _template_fields(template: Any) -> Any:
    """A data stream's mapped fields: its index template's."""
    return template.get("field_mappings") if isinstance(template, Mapping) else None


def _opensearch_design(design: Mapping[str, Any]) -> OpenSearchDesign:
    patterns = _in_scope(design)

    def served(name: str) -> list[str]:
        return _ids([p for p in patterns if p.get("index_or_stream") == name], "query_ids")

    indexes = [
        OpenSearchIndex(
            name=str(i["index_name"]),
            source_tables=_strs(i.get("source_tables")),
            fields=_opensearch_fields(i.get("field_mappings")),
            aliases=_strs(i.get("aliases")),
            query_ids=served(str(i["index_name"])),
        )
        for i in design.get("index_designs") or []
        if isinstance(i, Mapping) and i.get("index_name")
    ]
    data_streams = [
        OpenSearchDataStream(
            name=str(s["data_stream_name"]),
            source_tables=_strs(s.get("source_tables")),
            fields=_opensearch_fields(_template_fields(s.get("index_template"))),
            query_ids=served(str(s["data_stream_name"])),
        )
        for s in design.get("data_stream_designs") or []
        if isinstance(s, Mapping) and s.get("data_stream_name")
    ]
    return OpenSearchDesign(
        engine="opensearch",
        validation_passed=design.get("validation_passed") is True,
        indexes=indexes,
        data_streams=data_streams,
    )


#: How each designed engine's stable design is read from its schema output.
_DESIGN_READERS: dict[str, Callable[[Mapping[str, Any]], EngineDesign]] = {
    "dynamodb": _dynamodb_design,
    "aurora_mysql": lambda d: _aurora_design("aurora_mysql", d),
    "aurora_postgresql": lambda d: _aurora_design("aurora_postgresql", d),
    "documentdb": _documentdb_design,
    "elasticache": _elasticache_design,
    "opensearch": _opensearch_design,
}


def build_designs(state: RunState, designed_engines: list[str]) -> list[EngineDesign]:
    """One stable design per engine of ``designed_engines``, in that order, from the
    engine's schema output at the effective version. Every active catalog engine has a
    reader (``tests/unit/assessment/test_design_block.py`` checks it); an engine with
    none, such as a planned one, has no entry."""
    return [
        _DESIGN_READERS[engine](state.schema_designs[engine])
        for engine in designed_engines
        if engine in _DESIGN_READERS and engine in state.schema_designs
    ]


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
    if state.run_through in ("schema_design", "synthesis"):
        blocks["schema_designs"] = state.schema_designs
    if state.synthesis_report is not None:
        blocks["synthesis_report"] = state.synthesis_report
    if state.model_usage is not None:
        blocks["model_usage"] = state.model_usage
    if state.artifact_keys:
        blocks["artifact_keys"] = state.artifact_keys
    return Artifacts(**blocks)


# --------------------------------------------------------------------------- result


def build_result(state: RunState, request: AssessmentRequest) -> AssessmentResult:
    statements = build_statements(state, request)
    summary = build_summary(state, statements)
    label = result_label(state.run_through, summary.designed_engines)
    return AssessmentResult(
        label=label,  # type: ignore[arg-type]
        library_version=get_package_version(),
        ruleset_version=ruleset_version(),
        ruleset_hash=ruleset_hash(),
        options=request.options,
        scope=request.scope,
        summary=summary,
        statements=statements,
        groups=build_groups(state, statements),
        designs=build_designs(state, summary.designed_engines) if label == "designed" else None,
        artifacts=build_artifacts(state) if request.options.include_artifacts else None,
    )
