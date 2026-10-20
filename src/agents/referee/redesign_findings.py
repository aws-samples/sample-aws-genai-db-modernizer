"""Each query scored as a redesigned target would serve it (ADR-031 Decision 2 Amendment 13, #1231).

Routing used to score a query as the source runs it. A filter on a column with no index
is an ad hoc filter, which a key-value engine rates 0, and a scan a missing index causes
is an anti-pattern penalty. Both model the source schema, not the target the designer
builds: a redesign gives the filtered column the key the access needs. So each query is
read twice, through this one module:

- **As written**: its access-pattern class from the collected schema's keys.
- **After redesign**: the class once the design gives its own filters the key they need
  (``RedesignModel.redesign``): an equality-filtered column, each branch of an ``OR`` of
  plain equalities (one index per branch), or a column tested ``IS NOT NULL`` (a sparse
  index) when a partial index's predicate names it, the only selectivity signal the
  collection carries. ``IS NULL`` never counts: a sparse index leaves out exactly the rows
  it reads (an anti-join's ``IS NULL`` included). A column qualifies only where a key
  works (``access_pattern.is_redesign_key_eligible``: never a date, time, true/false,
  enum or category column, none the statistics show low-cardinality), never a column the
  statement's own write rewrites, never a later column of a composite key alone, and never
  a pin whose value is another scope's column (a correlated join edge). A keyset batch
  walk (a range over a key) reads every row whatever is indexed, so it gets no sparse
  index.

A redesign *applies* when it turns the query into a key lookup or a key-scoped read it
was not as written. Each engine's pattern dimension is then the better of its two class
scores (``query_pattern.QueryPatternModel.pattern_score``): a redesign never makes an
engine worse at a query, and it is what routing relies on only for an engine whose
redesigned score is the higher one. Both classes and the assumed keys
are recorded (``access_patterns.json``, the assignment reason), every redesigned query
reaches its engine's designer with the assumed keys as a requirement, and the risk
register raises the redesign the plan relies on.

The same redesign labels each penalised anti-pattern finding (``classify``): ``fixable``
only where the redesign removes the read the penalty is about, as the pattern's catalog
entry says (``removed_by_redesign``): ``rows`` when the redesign applies, or for a whole
read of a small reference table (the reference-read class, at most the catalog's
``small_reference_table_max_rows``); ``write_shape`` when, with the redesign's keys, ADR-031
Decision 3's own shape rule (``transaction_shape``) finds the write key scoped, so a
write a range, join or subquery picks keeps its penalty. Everything else is
``intrinsic`` and keeps its penalty. The module names no engine.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.agents.referee.access_pattern import (
    KEY_LOOKUP,
    KEY_SCOPED,
    REFERENCE_READ,
    AccessClassification,
    SchemaKeys,
    classify_facts,
    classify_query,
    is_redesign_key_eligible,
    query_access,
    unanchored_rows,
)
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.transaction_shape import transaction_shape
from src.shared.catalog.patterns import removed_by_redesign
from src.tools.sql.query_facts import query_facts, source_engine_of

FIXABLE = "fixable"
INTRINSIC = "intrinsic"
KINDS: tuple[str, ...] = (FIXABLE, INTRINSIC)

# How a redesign key serves the filter it is assumed for.
VIA_EQUALITY = "equality"
VIA_NOT_NULL_TEST = "not_null_test"
VIA_OR_BRANCH = "or_branch"

_ANCHORED = frozenset({KEY_LOOKUP, KEY_SCOPED})
# Pattern type (and alias) -> what a redesign removes, from the pattern catalog.
_REMOVED: dict[str, str] = removed_by_redesign()


def _short(table_id: str) -> str:
    return table_id.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class AssumedKey:
    """A key the redesign gives a column: ``table`` (collected id), ``column``, how it
    serves the filter (``via``) and the collected cardinality, or ``"unknown"``."""

    table: str
    column: str
    via: str
    cardinality: int | str = "unknown"

    @property
    def label(self) -> str:
        return f"{_short(self.table)}.{self.column}"

    def as_record(self) -> dict:
        return {
            "column": self.label,
            "table_id": self.table,
            "via": self.via,
            "cardinality": self.cardinality,
        }


@dataclass(frozen=True)
class QueryRedesign:
    """A redesign that applies to one query: the keys it assumes, and its class as
    written and after the redesign (an anchored class it was not)."""

    keys: tuple[AssumedKey, ...]
    written: AccessClassification
    redesigned: AccessClassification

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(k.label for k in self.keys)

    def note(self) -> str:
        """The decision-trace sentence for a query routed on this redesign."""
        return (
            f"scored after redesign: assumes a key on {', '.join(self.labels)}, which the "
            f"source does not have ({self.written.label} as written, "
            f"{self.redesigned.label} after the redesign)"
        )


@dataclass(frozen=True)
class FindingKind:
    """A finding's label and the evidence for it; ``columns`` the keys it assumes."""

    kind: str
    evidence: str
    columns: tuple[str, ...] = ()
    new_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Candidates:
    added: Mapping[str, frozenset[str]]
    sparse: Mapping[str, frozenset[str]]
    keys: tuple[AssumedKey, ...]
    refused: tuple[tuple[str, str], ...]  # (label, why no key)
    unknown: tuple[str, ...]


@dataclass(frozen=True)
class RedesignModel:
    """What the redesign reads, for one collection: the schema's keys (workload keys
    included), each table's collected column metadata and row count, and the source.
    The bare default has no schema, so no redesign applies and every finding is
    intrinsic."""

    keys: SchemaKeys = field(default_factory=SchemaKeys)
    source_engine: str = ""
    columns: Mapping[str, tuple[Mapping[str, Mapping], object]] = field(default_factory=dict)
    source: SourceScope | None = None
    # Per table id: the later columns of a composite primary or unique key, never a key
    # alone; and the columns a partial index's predicate names, the selectivity signal an
    # IS NOT NULL test needs.
    composite_tails: Mapping[str, frozenset[str]] = field(default_factory=dict)
    partial: Mapping[str, frozenset[str]] = field(default_factory=dict)
    _redesigns: dict[tuple[str, str], QueryRedesign | None] = field(
        default_factory=dict, compare=False, repr=False
    )
    _candidates: dict[tuple[str, str], _Candidates | None] = field(
        default_factory=dict, compare=False, repr=False
    )

    @classmethod
    def from_collector(
        cls, collector_output: Mapping | None, keys: SchemaKeys | None = None
    ) -> RedesignModel:
        collector = collector_output or {}
        columns: dict[str, tuple[Mapping[str, Mapping], object]] = {}
        tails: dict[str, frozenset[str]] = {}
        partial: dict[str, frozenset[str]] = {}
        for table in (collector.get("database_schema") or {}).get("tables") or []:
            table_id = str(table.get("table_id") or "")
            if not table_id:
                continue
            by_name = {
                str(c.get("column_name") or "").lower(): c for c in table.get("columns") or []
            }
            columns[table_id] = (by_name, table.get("row_count"))
            composites = [[str(c).lower() for c in table.get("primary_key") or []]]
            composites += [
                [str(c).lower() for c in i.get("columns") or []]
                for i in table.get("indexes") or []
                if i.get("is_unique") or i.get("is_primary")
            ]
            leading = {c[0] for c in composites if c}
            tails[table_id] = frozenset(
                col for c in composites if len(c) > 1 for col in c[1:] if col not in leading
            )
            predicates = " ".join(
                str(i.get("predicate") or "").lower() for i in table.get("indexes") or []
            )
            # Only a predicate that selects the column's non-null rows signals that an
            # IS NOT NULL on it reads few of them.
            partial[table_id] = frozenset(
                name
                for name in by_name
                if name and re.search(rf"\b{re.escape(name)}\s+is\s+not\s+null\b", predicates)
            )
        return cls(
            keys=keys if keys is not None else SchemaKeys.from_collector(collector),
            source_engine=source_engine_of(collector),
            columns=columns,
            source=SourceScope.from_collector(collector) if collector else None,
            composite_tails=tails,
            partial=partial,
        )

    # --- the redesign ---------------------------------------------------------------

    def _candidate_keys(self, query: Mapping) -> _Candidates | None:
        memo = (str(query.get("query_id") or ""), str(query.get("query_text") or ""))
        if memo in self._candidates:
            return self._candidates[memo]
        found = self._read_candidates(query)
        self._candidates[memo] = found
        return found

    def _read_candidates(self, query: Mapping) -> _Candidates | None:
        facts = query_facts(str(query.get("query_text") or ""), self.source_engine).access_facts()
        if facts.parsed_by != "sqlglot" or facts.truncated_read:
            return None
        rewritten = set(facts.assigned_columns)
        added: dict[str, set[str]] = {}
        sparse: dict[str, set[str]] = {}
        keys: dict[tuple[str, str], AssumedKey] = {}
        refused: dict[str, str] = {}
        unknown: set[str] = set()
        for scope in query_access(query, self.keys, self.source_engine).scopes:
            for table in scope.tables:
                table_keys = self.keys.tables.get(table)
                if table_keys is None:
                    continue
                walks_a_key = bool(scope.ranged.get(table, frozenset()) & table_keys.key_columns)
                correlated = scope.correlated.get(table, frozenset())
                wanted: list[tuple[str, str]] = [
                    (c, VIA_EQUALITY)
                    for c in sorted(scope.pinned.get(table, ()))
                    if c not in correlated
                ]
                for branch in scope.or_pinned:
                    wanted += [
                        (c, VIA_OR_BRANCH)
                        for c in sorted(branch.get(table, ()))
                        if c not in correlated
                    ]
                if not walks_a_key:
                    wanted += [
                        (c, VIA_NOT_NULL_TEST) for c in sorted(scope.not_null_tested.get(table, ()))
                    ]
                by_name, row_count = self.columns.get(table, ({}, None))
                tails = self.composite_tails.get(table, frozenset())
                for column, via in wanted:
                    label = f"{_short(table)}.{column}"
                    if column in table_keys.key_columns or (table, column) in keys:
                        continue
                    info = by_name.get(column)
                    if info is None:
                        unknown.add(label)
                    elif column in rewritten:
                        refused[label] = "a column the statement's own write rewrites"
                    elif column in tails:
                        refused[label] = "a later column of a composite key, not a key alone"
                    elif via == VIA_NOT_NULL_TEST and column not in self.partial.get(
                        table, frozenset()
                    ):
                        refused[label] = (
                            "an IS NOT NULL test with no selectivity signal (no partial index "
                            "or statistics), which may read most rows"
                        )
                    elif not is_redesign_key_eligible(info, row_count):
                        refused[label] = (
                            "a date, time, true/false, enum or category column, or one the "
                            "statistics show low-cardinality"
                        )
                    else:
                        added.setdefault(table, set()).add(column)
                        if via == VIA_NOT_NULL_TEST:
                            sparse.setdefault(table, set()).add(column)
                        cardinality = info.get("cardinality")
                        keys[(table, column)] = AssumedKey(
                            table,
                            column,
                            via,
                            cardinality if isinstance(cardinality, int) else "unknown",
                        )
        return _Candidates(
            added={t: frozenset(c) for t, c in added.items()},
            sparse={t: frozenset(c) for t, c in sparse.items()},
            keys=tuple(keys[k] for k in sorted(keys)),
            refused=tuple(sorted(refused.items())),
            unknown=tuple(sorted(unknown)),
        )

    def redesign(self, query: Mapping) -> QueryRedesign | None:
        """The redesign that applies to ``query``, or None (see the module docstring)."""
        memo = (str(query.get("query_id") or ""), str(query.get("query_text") or ""))
        if memo in self._redesigns:
            return self._redesigns[memo]
        found = self._redesign(query)
        self._redesigns[memo] = found
        return found

    def _redesign(self, query: Mapping) -> QueryRedesign | None:
        candidates = self._candidate_keys(query)
        if candidates is None or not candidates.keys:
            return None
        text = str(query.get("query_text") or "")
        written = classify_query(query, self.keys, self.source_engine)
        facts = query_facts(text, self.source_engine).access_facts()
        redesigned = classify_facts(
            facts,
            self.keys.with_key_columns(candidates.added),
            str(query.get("query_type") or ""),
            redesign=candidates.sparse,
        )
        if redesigned.name not in _ANCHORED or redesigned.name == written.name:
            return None
        # The redesigned class keeps the SQL features the query shows (#985).
        redesigned = AccessClassification(
            redesigned.name,
            redesigned.reason,
            redesigned.facts | (written.facts - _key_dependent_facts(written)),
        )
        return QueryRedesign(candidates.keys, written, redesigned)

    def _why_no_redesign(self, query: Mapping) -> str:
        candidates = self._candidate_keys(query)
        if candidates is None:
            return "its predicates cannot all be read, so no key is evidenced"
        facts = query_facts(str(query.get("query_text") or ""), self.source_engine).access_facts()
        notes = []
        for label, why in candidates.refused:
            notes.append(f"{label}: {why}, which no redesign keys")
        if candidates.unknown:
            notes.append(f"{', '.join(candidates.unknown)}: not in the collected schema")
        if candidates.keys:
            labels = ", ".join(k.label for k in candidates.keys)
            head = f"with a key on {labels} the rows are still not anchored by a key"
        else:
            head = unanchored_rows(facts, self.keys) or "no filter a key could carry"
        return head + (f" ({'; '.join(notes)})" if notes else "")

    # --- the anti-pattern label ------------------------------------------------------

    def classify(self, query: Mapping, pattern_type: str) -> FindingKind:
        """The label of the ``pattern_type`` finding on ``query`` (the module docstring)."""
        removed = _REMOVED.get(pattern_type)
        if removed is None:
            return FindingKind(
                INTRINSIC,
                "no redesign removes what this pattern's penalty is about (a capability "
                "the engine lacks, which engine the access suits, or a shape a key does "
                "not change)",
            )
        if removed == "write_shape":
            return self._write_shape(query)
        found = self.redesign(query)
        if found is not None:
            return FindingKind(
                FIXABLE,
                f"a key on {', '.join(found.labels)} anchors the rows it reads: it is a "
                f"{found.redesigned.label} after the redesign, a {found.written.label} as "
                "written",
                found.labels,
                found.labels,
            )
        written = classify_query(query, self.keys, self.source_engine)
        if written.name == REFERENCE_READ:
            return FindingKind(
                FIXABLE,
                "a whole read of a small reference table, which the design keeps as one "
                "item collection or caches",
            )
        return FindingKind(
            INTRINSIC, f"no redesign anchors the rows: {self._why_no_redesign(query)}"
        )

    def _write_shape(self, query: Mapping) -> FindingKind:
        candidates = self._candidate_keys(query)
        written = transaction_shape(query, self.source, self.keys)
        if written is None or not written.needs_redesign:
            return FindingKind(INTRINSIC, "the write's shape needs no redesign as written")
        if candidates is None or not candidates.keys:
            return FindingKind(
                INTRINSIC,
                f"the write stays unbounded: {written.reason} (ADR-031 Decision 3); "
                f"{self._why_no_redesign(query)}",
            )
        redesigned = transaction_shape(
            query, self.source, self.keys.with_key_columns(candidates.added)
        )
        labels = tuple(k.label for k in candidates.keys)
        if redesigned is None or redesigned.needs_redesign:
            return FindingKind(
                INTRINSIC,
                f"the write stays unbounded with a key on {', '.join(labels)}: "
                f"{(redesigned or written).reason} (ADR-031 Decision 3)",
            )
        return FindingKind(
            FIXABLE,
            f"with a key on {', '.join(labels)} the write is key scoped "
            f"({redesigned.reason}): one Query on that key and a batch write",
            labels,
            labels,
        )


def _key_dependent_facts(classification: AccessClassification) -> frozenset[str]:
    """The class facts that follow the keys, which the redesigned reading recomputes."""
    return classification.facts & {"partition_range", "key_scoped_count"}


@dataclass(frozen=True)
class AntiPatternFinding:
    """One penalised anti-pattern finding on a query, labelled (see the module docstring).

    ``penalty`` is the points the finding costs ``engine`` when intrinsic (the catalog's,
    after its capability scoping); a fixable finding costs nothing."""

    query_id: str
    engine: str
    pattern: str
    penalty: int
    kind: str
    evidence: str
    columns: tuple[str, ...] = ()
    new_columns: tuple[str, ...] = ()

    @property
    def fixable(self) -> bool:
        return self.kind == FIXABLE

    def as_record(self) -> dict:
        """The finding as the access-pattern record carries it."""
        return {
            "query_id": self.query_id,
            "engine": self.engine,
            "pattern": self.pattern,
            "penalty": self.penalty,
            "kind": self.kind,
            "evidence": self.evidence,
            "columns": list(self.columns),
            "new_columns": list(self.new_columns),
        }


def fixable_keys(findings: tuple[AntiPatternFinding, ...]) -> frozenset[tuple[str, str, str]]:
    """``(query_id, engine, pattern)`` of every fixable finding: the ones the scorer skips."""
    return frozenset((f.query_id, f.engine, f.pattern) for f in findings if f.fixable)


__all__ = [
    "FIXABLE",
    "INTRINSIC",
    "KINDS",
    "VIA_EQUALITY",
    "VIA_NOT_NULL_TEST",
    "VIA_OR_BRANCH",
    "AntiPatternFinding",
    "AssumedKey",
    "FindingKind",
    "QueryRedesign",
    "RedesignModel",
    "fixable_keys",
]
