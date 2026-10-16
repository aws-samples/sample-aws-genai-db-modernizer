"""Shadow run of the per-query access-pattern score (ADR-031 Decision 14, Amendment 5, #728).

The first assignment is resolved twice from the same artifacts: once as it is today,
and once with each query's pattern dimension taken from its own access-pattern class
(:class:`~src.agents.referee.query_scoring.QueryPatternModel`), through the same
resolver, gates, penalties, co-dependency groups and tie-breaks. Nothing the
pipeline writes reads the second run: :func:`write_access_pattern_shadow` stores it
as ``assignment/v<N>/access_pattern_shadow.json`` next to the assignment it shadows,
so the drift can be reviewed per job before routing switches (like the sqlglot
shadow of #557). A failure here is logged and never fails the assignment.

Per query the artifact records the class and why, the current engine and per-engine
scores, and the engine and scores the class model would give, with the rule that
placed the query in the shadow run (its last Move, ``pass:reason_code``). The summary
counts queries per class, engine changes, and changes per ``from -> to`` pair and per
class. A third, control run keeps every query's tables' own pattern scores through the
same path (``QueryPatternModel.identity``): a query that moves there too moves because of
the per-query table rule (OpenSearch is scored on the query's own tables, not on the
tables its patterns name), not because of its class, so the summary lists it under
``rule_moves`` and keeps it out of ``moves_by_class`` (ADR-031 Decision 10). ``tests/e2e/access_pattern_shadow.py`` prints the diff for the bundled samples.
"""

from __future__ import annotations

import copy
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace

from src.agents.referee.access_pattern import (
    SCORED_CLASSES,
    AccessClassification,
    SchemaKeys,
    classify_query,
)
from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.query_scoring import QueryPatternModel
from src.agents.referee.routing_trace import RoutingTrace
from src.shared.catalog.access_classes import class_engine_scores
from src.tools.analysis.score_weights import table_weights

logger = logging.getLogger(__name__)

SHADOW_FILE = "access_pattern_shadow.json"


def weights_from_analysis(
    analysis_outputs: Mapping[str, Mapping],
) -> dict[tuple[str, str], dict[str, float]]:
    """``(engine, table_id) -> weights`` each engine's analysis combined that table's
    dimensions with (:func:`src.tools.analysis.score_weights.table_weights`)."""
    return {
        (engine, str(rec["table_id"])): table_weights(engine, rec)
        for engine, analysis in analysis_outputs.items()
        for rec in (analysis or {}).get("table_recommendations") or []
        if rec.get("table_id")
    }


def build_pattern_model(
    collector_output: Mapping, weights: Mapping[tuple[str, str], Mapping[str, float]]
) -> tuple[QueryPatternModel, dict[str, AccessClassification]]:
    """The class of every query, and the model that scores the scored classes.

    A ``utility`` or ``unparsed`` query is left out of the model, so it keeps
    today's score in the shadow run.
    """
    keys = SchemaKeys.from_collector(collector_output)
    source = source_database_engine(collector_output)
    scores = class_engine_scores()
    classes: dict[str, AccessClassification] = {}
    pattern_scores: dict[str, dict[str, int]] = {}
    for query in (collector_output.get("queries") or {}).get("query_patterns") or []:
        qid = str(query.get("query_id") or "")
        result = classify_query(query, keys, source)
        classes[qid] = result
        if result.name in SCORED_CLASSES:
            by_class = class_engine_scores(result.facts) if result.facts else scores
            pattern_scores[qid] = dict(by_class[result.name])
    return QueryPatternModel(pattern_scores=pattern_scores, weights=dict(weights)), classes


def _ladder(entry: object) -> int | str:
    """A routing-trace ladder entry as a score, or the gate that zeroed it."""
    if isinstance(entry, (int, float)):
        return int(entry)
    if isinstance(entry, Mapping):
        if entry.get("gate") and "used" not in entry:
            return str(entry["gate"])
        if entry.get("gate"):
            return f"{entry['gate']}:{int(entry.get('used') or 0)}"
        return int(entry.get("used") or 0)
    return 0


def _run(triage: Mapping, analysis_outputs: Mapping, collector_output: Mapping, model=None):
    from src.agents.referee.assignment_resolver import AssignmentResolver

    trace = RoutingTrace()
    assignment = AssignmentResolver().resolve(
        copy.deepcopy(dict(triage)),
        copy.deepcopy(dict(analysis_outputs)),
        copy.deepcopy(dict(collector_output)),
        routing_trace=trace,
        pattern_model=model,
    )
    last_rule: dict[str, str] = {}
    for move in sorted(assignment.moves, key=lambda m: m.seq):
        last_rule[move.query_id] = f"{move.pass_.value}:{move.reason_code.value}"
    return assignment, trace, last_rule


def build_shadow(
    triage: Mapping,
    analysis_outputs: Mapping,
    collector_output: Mapping,
    weights: Mapping[tuple[str, str], Mapping[str, float]],
) -> dict:
    """The shadow artifact (see the module docstring). Reads its inputs, never changes them."""
    model, classes = build_pattern_model(collector_output, weights)
    current, current_trace, current_rule = _run(triage, analysis_outputs, collector_output)
    shadow, shadow_trace, shadow_rule = _run(triage, analysis_outputs, collector_output, model)
    control_model = replace(model, pattern_scores={q: {} for q in classes}, identity=True)
    control, _control_trace, _ = _run(triage, analysis_outputs, collector_output, control_model)
    current_engine = {qa.query_id: qa.assigned_engine for qa in current.query_assignments}
    shadow_engine = {qa.query_id: qa.assigned_engine for qa in shadow.query_assignments}
    control_engine = {qa.query_id: qa.assigned_engine for qa in control.query_assignments}
    rule_moves: Counter[str] = Counter()

    rows = []
    by_class: Counter[str] = Counter()
    moves: Counter[str] = Counter()
    moves_by_class: dict[str, Counter[str]] = {}
    for query in (collector_output.get("queries") or {}).get("query_patterns") or []:
        qid = str(query.get("query_id") or "")
        result = classes[qid]
        by_class[result.name] += 1
        before, after = current_engine.get(qid, ""), shadow_engine.get(qid, "")
        if before != after:
            key = f"{before} -> {after}"
            moves[key] += 1
            if control_engine.get(qid, "") != before:
                # The control moves it too: the per-query table rule, not the class.
                rule_moves[key] += 1
            else:
                moves_by_class.setdefault(result.name, Counter())[key] += 1
        rows.append(
            {
                "query_id": qid,
                "class": result.name,
                "reason": result.reason,
                "facts": sorted(result.facts),
                "tables": list(query.get("tables_accessed") or []),
                "current": {
                    "engine": before,
                    "scores": {
                        e: _ladder(v)
                        for e, v in (current_trace.by_query.get(qid) or {})
                        .get("engines", {})
                        .items()
                    },
                },
                "shadow": {
                    "engine": after,
                    "scores": {
                        e: _ladder(v)
                        for e, v in (shadow_trace.by_query.get(qid) or {})
                        .get("engines", {})
                        .items()
                    },
                    "rule": shadow_rule.get(qid, ""),
                    "current_rule": current_rule.get(qid, ""),
                    "control_engine": control_engine.get(qid, ""),
                },
            }
        )
    return {
        "schema_version": 1,
        "about": (
            "Shadow of the per-query access-pattern score (ADR-031 Decision 14, "
            "Amendment 5, #728). Routing does not read this file."
        ),
        "summary": {
            "queries": len(rows),
            "by_class": dict(sorted(by_class.items())),
            "engine_changes": sum(moves.values()),
            "moves": dict(sorted(moves.items())),
            "moves_by_class": {
                c: dict(sorted(m.items())) for c, m in sorted(moves_by_class.items())
            },
            "rule_moves": dict(sorted(rule_moves.items())),
            "control_moves": dict(
                sorted(
                    Counter(
                        f"{current_engine[q]} -> {control_engine.get(q, '')}"
                        for q in current_engine
                        if control_engine.get(q, "") != current_engine[q]
                    ).items()
                )
            ),
            "current_distribution": dict(sorted(Counter(current_engine.values()).items())),
            "shadow_distribution": dict(sorted(Counter(shadow_engine.values()).items())),
        },
        "class_scores": class_engine_scores(),
        "queries": rows,
    }


def write_access_pattern_shadow(
    store,
    database_name: str,
    job_id: str,
    version: int,
    triage: Mapping,
    analysis_outputs: Mapping,
    collector_output: Mapping,
) -> str | None:
    """Write ``assignment/v<version>/access_pattern_shadow.json``; None when it failed.

    The shadow only observes, so any error is logged and swallowed.
    """
    prefix = f"{database_name}/{job_id}"
    path = f"{prefix}/assignment/v{version}/{SHADOW_FILE}"
    try:
        shadow = build_shadow(
            triage, analysis_outputs, collector_output, weights_from_analysis(analysis_outputs)
        )
        store.write_json(path, shadow)
    except Exception:  # noqa: BLE001 -- the shadow only observes
        logger.warning("access-pattern shadow: could not write %s", path, exc_info=True)
        return None
    s = shadow["summary"]
    logger.info(
        "access-pattern shadow: %d queries, %d would change engine, classes %s",
        s["queries"],
        s["engine_changes"],
        s["by_class"],
    )
    return path
