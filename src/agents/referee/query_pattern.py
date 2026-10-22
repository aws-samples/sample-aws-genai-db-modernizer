"""Each query's own pattern score (ADR-031 Decision 14, Amendment 5, #728).

A query's pattern dimension on an engine is the score of its own access-pattern
class (:func:`src.agents.referee.access_pattern.classify_query`) on that engine, read
from the pattern catalog (:func:`src.shared.catalog.access_classes.class_engine_scores`),
never the pattern score of its tables. :class:`QueryPatternModel` holds what that needs
for one collection -- the schema's keys, the source engine's dialect and the dimension
weights each engine's analysis used for each table -- and classifies a query the first
time it is scored. :meth:`QueryPatternModel.from_pipeline` is the one way production
code builds one, and :meth:`src.agents.referee.query_scoring.ScoringContext.from_pipeline`
the one place that calls it, so every per-query score in the pipeline reads the same
classes.

:func:`access_patterns_record` records, per query, the class routing used, why, the facts
its score read, its score on each engine, per engine whose score is a parity anchor
(#936) the anchor note, and each column the redesign considered as a key, accepted or
refused and why (#1361): the assignment writes it next to each version
(``assignment/v<N>/access_patterns.json``); routing reads none of it.

A ``utility`` or ``unparsed`` statement has no class score
(:meth:`QueryPatternModel.has_class_score`); the scorer keeps the pre-switch rule for
it alone (``query_scoring._unclassified_confidence``), as ADR-031 Decision 14
Amendment 5 states.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.agents.referee.access_pattern import (
    SCORED_CLASSES,
    AccessClassification,
    SchemaKeys,
    classify_query,
)
from src.agents.referee.redesign_findings import (
    FIXABLE,
    VIA_EQUALITY,
    QueryRedesign,
    RedesignModel,
)
from src.shared.catalog.access_classes import class_engine_scores, class_parity_anchors
from src.tools.analysis.score_weights import table_weights
from src.tools.sql.query_facts import remember_collected_facts, source_engine_of


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


@dataclass(frozen=True)
class QueryPatternModel:
    """The per-query pattern dimension for one collection (see the module docstring).

    ``keys``: the collected schema's keys (``SchemaKeys.from_collector``); empty keys
    classify every query as if no column were a key. ``source_engine``: the dialect
    the query text is read in. ``weights``: ``(engine, table_id) -> dimension
    weights`` each engine's analysis used for that table, read from ``analysis.json``
    (the referee never reads the decision trace, ADR-017 Decision 3); a missing pair
    uses the default weights.
    """

    keys: SchemaKeys = field(default_factory=SchemaKeys)
    source_engine: str = ""
    weights: Mapping[tuple[str, str], Mapping[str, float]] = field(default_factory=dict)
    # Each query read after redesign (#1231, ADR-031 Decision 2 Amendment 13): built with
    # the same keys by :meth:`from_pipeline`; the bare default redesigns nothing.
    redesigns: RedesignModel = field(default_factory=RedesignModel)
    # query_id and text -> class, filled the first time a query is scored. The class
    # depends on the query's own text and the schema only, so it never goes stale.
    _classes: dict[tuple[str, str], AccessClassification] = field(
        default_factory=dict, compare=False, repr=False
    )

    @classmethod
    def from_pipeline(
        cls, collector_output: Mapping | None, analysis_outputs: Mapping[str, Mapping]
    ) -> QueryPatternModel:
        """The model for one pipeline run's collector and analysis outputs."""
        collector = collector_output or {}
        keys = SchemaKeys.from_collector(collector)
        return cls(
            keys=keys,
            source_engine=source_engine_of(collector),
            weights=weights_from_analysis(analysis_outputs),
            redesigns=RedesignModel.from_collector(collector, keys),
        )

    def classification(self, query: Mapping) -> AccessClassification:
        """The access-pattern class of ``query`` (computed once per query)."""
        memo_key = (str(query.get("query_id") or ""), str(query.get("query_text") or ""))
        found = self._classes.get(memo_key)
        if found is None:
            found = classify_query(query, self.keys, self.source_engine)
            self._classes[memo_key] = found
        return found

    def has_class_score(self, query: Mapping) -> bool:
        """Whether ``query`` has an access-pattern class with a score (not ``utility``
        or ``unparsed``)."""
        return self.classification(query).name in SCORED_CLASSES

    def redesign(self, query: Mapping) -> QueryRedesign | None:
        """The redesign that applies to ``query``, or None (``RedesignModel.redesign``)."""
        if not self.has_class_score(query):
            return None
        return self.redesigns.redesign(query)

    def pattern_score(self, query: Mapping, engine: str) -> int:
        """``query``'s pattern score on ``engine``, as routing reads it: the better of its
        class's score as written and, when a redesign applies (#1231), its redesigned
        class's score there, since a redesign never makes an engine worse at a query (0
        for a statement with no class score, ``utility`` or ``unparsed``)."""
        written = self.written_pattern_score(query, engine)
        redesigned = self.redesigned_pattern_score(query, engine)
        return written if redesigned is None else max(written, redesigned)

    def redesigned_pattern_score(self, query: Mapping, engine: str) -> int | None:
        """``query``'s redesigned class score on ``engine``, or None with no redesign."""
        found = self.redesign(query)
        if found is None:
            return None
        result = found.redesigned
        return int(class_engine_scores(result.facts)[result.name].get(engine, 0))

    def relies_on_redesign(self, query: Mapping, engine: str) -> bool:
        """Whether ``engine``'s pattern score for ``query`` is the redesigned one: its
        redesigned score beats its written one, so a plan on ``engine`` depends on it."""
        redesigned = self.redesigned_pattern_score(query, engine)
        return redesigned is not None and redesigned > self.written_pattern_score(query, engine)

    def written_pattern_score(self, query: Mapping, engine: str) -> int:
        """``query``'s pattern score on ``engine`` as written: its class's score there."""
        result = self.classification(query)
        if result.name not in SCORED_CLASSES:
            return 0
        return int(class_engine_scores(result.facts)[result.name].get(engine, 0))


ACCESS_PATTERNS_FILE = "access_patterns.json"


def read_analysis_outputs(store, database_name: str, job_id: str) -> dict[str, dict]:
    """Every ``analysis-<engine>/analysis.json`` the job wrote, placeholder engines left out."""
    from src.shared.engine_names import without_placeholder_engines

    prefix = f"{database_name}/{job_id}/"
    engines: set[str] = set()
    for key in store.list_prefix(prefix):
        relative = key.replace(prefix, "")
        if relative.startswith("analysis-"):
            engines.add(relative.split("/")[0].replace("analysis-", ""))
    outputs: dict[str, dict] = {}
    for engine in sorted(engines):
        path = f"{prefix}analysis-{engine}/analysis.json"
        if store.exists(path):
            outputs[engine] = store.read_json(path)
    return without_placeholder_engines(outputs)


def write_access_patterns(
    store,
    database_name: str,
    job_id: str,
    version: int,
    collector_output: Mapping | None = None,
    analysis_outputs: Mapping[str, Mapping] | None = None,
) -> str:
    """Write ``assignment/v<version>/access_patterns.json`` next to every assignment
    version a writer creates (#728, #936, #1231); returns its key. ``collector_output``
    and ``analysis_outputs`` are the job's when the writer holds them, else they are read
    from the store."""
    prefix = f"{database_name}/{job_id}"
    collector = (
        collector_output
        if collector_output is not None
        else store.read_json(f"{prefix}/collector/output.json")
    )
    analyses = (
        analysis_outputs
        if analysis_outputs is not None
        else read_analysis_outputs(store, database_name, job_id)
    )
    key = f"{prefix}/assignment/v{version}/{ACCESS_PATTERNS_FILE}"
    store.write_json(key, access_patterns_record(collector, analyses))
    return key


def access_patterns_record(
    collector_output: Mapping, analysis_outputs: Mapping[str, Mapping] | None = None
) -> dict:
    """Each query's access-pattern class, as routing scored it (see the module docstring).

    Per query: the class and its reason, the query facts its class score read, its
    tables, the class score on each active engine, and ``parity_anchors``: for each
    engine whose class score is a parity anchor (no engine-specific evidence yet, #936),
    the note and the borrowed ``(<pattern>, <score>)``. A ``utility`` or ``unparsed``
    statement has neither scores nor anchors. ``anti_pattern_findings``: every penalised
    anti-pattern finding on the query (``analysis_outputs``'), labelled ``fixable`` or
    ``intrinsic`` with its evidence, as the scorer labelled it
    (``query_scoring.anti_pattern_findings``, ADR-031 Decision 2 Amendment 13, #1231):
    only an intrinsic finding costs its engine points, and the designer of the engine
    that serves the query is given its fixable ones.
    """
    from src.agents.referee.query_scoring import anti_pattern_findings

    remember_collected_facts(collector_output)
    model = QueryPatternModel.from_pipeline(collector_output, {})
    findings: dict[str, list[dict]] = {}
    for finding in anti_pattern_findings(analysis_outputs or {}, collector_output):
        findings.setdefault(finding.query_id, []).append(finding.as_record())
    rows = []
    by_class: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    redesigned = 0
    for query in (collector_output.get("queries") or {}).get("query_patterns") or []:
        result = model.classification(query)
        by_class[result.name] += 1
        scored = model.has_class_score(query)
        own = findings.get(str(query.get("query_id") or ""), [])
        by_kind.update(f["kind"] for f in own)
        redesigned += model.redesign(query) is not None
        rows.append(
            {
                "query_id": str(query.get("query_id") or ""),
                "class": result.name,
                "reason": result.reason,
                "facts": sorted(result.facts),
                "tables": list(query.get("tables_accessed") or []),
                "class_scores": (
                    dict(class_engine_scores(result.facts)[result.name]) if scored else {}
                ),
                "parity_anchors": (
                    dict(class_parity_anchors(result.name, result.facts)) if scored else {}
                ),
                "anti_pattern_findings": [
                    {k: v for k, v in f.items() if k != "query_id"} for f in own
                ],
                "redesign": _redesign_record(model.redesign(query)),
                "candidate_keys": (
                    [c.as_record() for c in model.redesigns.candidate_keys(query)] if scored else []
                ),
            }
        )
    return {
        # Version 2 (#1231) adds each query's ``anti_pattern_findings``; a version 1
        # record has none, and every reader treats a missing list as no finding.
        # Version 3 (#1361) adds each query's ``candidate_keys``; an older record has
        # none, and ``query_trace.candidate_keys_by_query`` reads a missing list as none.
        "schema_version": 3,
        "about": (
            "Each query's access-pattern class and class scores, as routing scored its "
            "pattern dimension as written (ADR-031 Decision 14, Amendments 5 and 7, #728); "
            "for a query a redesign applies to, ``redesign``: the keys it assumes and the "
            "class and class scores routing scored instead; its anti-pattern findings, "
            "each fixable by redesign or intrinsic (ADR-031 Decision 2 Amendment 13, "
            "#1231); and ``candidate_keys``: each column the redesign considered as a key, "
            "accepted or refused with every guard that refused it (#1361). Routing does not "
            "read this file."
        ),
        "by_class": dict(sorted(by_class.items())),
        "findings_by_kind": dict(sorted(by_kind.items())),
        "redesigned_queries": redesigned,
        "queries": rows,
    }


def _redesign_record(found: QueryRedesign | None) -> dict | None:
    """A query's redesign as the record holds it, or None when none applies."""
    if found is None:
        return None
    result = found.redesigned
    return {
        "assumed_keys": [k.as_record() for k in found.keys],
        "class": result.name,
        "reason": result.reason,
        "facts": sorted(result.facts),
        "class_scores": dict(class_engine_scores(result.facts)[result.name]),
    }


def _relies(row: Mapping, engine: str) -> bool:
    """Whether a record row's redesigned class score beats its written one on ``engine``:
    routing then relies on the redesign there (``QueryPatternModel.relies_on_redesign``)."""
    redesign = row.get("redesign") or {}
    after = (redesign.get("class_scores") or {}).get(engine)
    before = (row.get("class_scores") or {}).get(engine, 0)
    return isinstance(after, (int, float)) and after > before


def redesign_requirements(record: Mapping | None, engine: str) -> list[dict]:
    """What a design of ``engine`` must give the queries routing scored after redesign
    (#1231), from an access-pattern record: one entry per query with a redesign (the
    keys it assumes, its class as written and after) or with a fixable anti-pattern
    finding against ``engine`` (which it lifts), sorted by query. The caller keeps the
    entries of its own queries. A record written before #1231 holds none."""
    out = []
    for row in (record or {}).get("queries") or []:
        qid = str(row.get("query_id") or "")
        redesign = row.get("redesign") or None
        lifted = [
            f
            for f in row.get("anti_pattern_findings") or []
            if f.get("kind") == FIXABLE and f.get("engine") == engine
        ]
        if redesign is None and not lifted:
            continue
        keys = list((redesign or {}).get("assumed_keys") or [])
        seen = {k.get("column") for k in keys}
        for f in lifted:
            for column in f.get("new_columns") or []:
                if column not in seen:
                    seen.add(column)
                    keys.append({"column": column, "via": VIA_EQUALITY, "cardinality": "unknown"})
        out.append(
            {
                "query_id": qid,
                # Routing relies on the keys only where this engine's redesigned score won,
                # or a lifted finding stopped costing it points; otherwise they are an
                # index the design may add (#1231).
                "routing_depends": bool(lifted) or _relies(row, engine),
                "assumed_keys": [
                    {
                        "column": str(k.get("column")),
                        "via": str(k.get("via") or VIA_EQUALITY),
                        "cardinality": k.get("cardinality", "unknown"),
                    }
                    for k in keys
                ],
                "class_as_written": str(row.get("class") or ""),
                "class_after_redesign": (redesign or {}).get("class"),
                "lifted_anti_patterns": sorted({str(f.get("pattern")) for f in lifted}),
                "evidence": sorted({str(f.get("evidence")) for f in lifted}),
            }
        )
    return sorted(out, key=lambda r: r["query_id"])


def fixable_findings(record: Mapping | None, engine: str | None = None) -> list[dict]:
    """The fixable findings an access-pattern record holds against ``engine`` (every
    engine when None), each with its ``query_id``, sorted by query and pattern (#1231). A
    record written before the findings existed (schema version 1), or none, holds none."""
    out = []
    for row in (record or {}).get("queries") or []:
        for f in row.get("anti_pattern_findings") or []:
            if f.get("kind") == FIXABLE and engine in (None, f.get("engine")):
                out.append({"query_id": str(row.get("query_id") or ""), **f})
    return sorted(
        out,
        key=lambda f: (f["query_id"], str(f.get("engine") or ""), str(f.get("pattern") or "")),
    )
