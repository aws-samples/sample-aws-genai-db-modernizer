"""Write-group members their group's engine serves poorly, and how the target should (#1362).

A table has one writer (ADR-031 Decision 1 Amendment 9): every statement that writes it
lands on the table's source of truth, chosen on the members' call-weighted mean. So a
write group can carry a member whose own score on that engine is low: on the bundled
samples, 14 rarely run maintenance writes (retention deletes, orphan clean-ups,
consistency repair sweeps) sit on DynamoDB with scores of 11 to 26. Routing is right to
keep the group together (#1359, option (c)); what the designer and the customer need is
the statement's intent and the target-native way to serve it, so the design models the
intent instead of replaying the source's set-based statement.

:func:`carried_writes` finds them, deterministically, once per assignment version: for
each written table with a write group of two or more statements, each member whose own
score on the table's source of truth (the shared scorer, ``score_query_for_engine``, with
that version's sources of truth) is below the catalog's floor
(``src/catalog/carried_writes.toml``). :func:`classify_write_intent` reads the statement's
intent from its parsed facts (the facade's ``AccessFacts`` and tokens, in the source
dialect), and the write-intent catalog (``src/catalog/write_intents/*.toml``) gives the
engine's approach. The schema-design step gives each design call the ones it serves
(``AnalysisOutputContract.carried_writes``); synthesis raises one ``design.carried_writes``
risk per engine from the same function (ADR-032: computed once, the deliverables format
it). Routing reads none of it, and nothing here names an engine.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.shared.catalog.write_intents import carried_write_floor, write_intent
from src.tools.sql.facade import sql_tokens
from src.tools.sql.query_facts import is_timestamp_type, query_facts, source_engine_of

# The intents, one catalog file each, in the order the classifier tries them (the first
# that matches wins; ``SET_BASED_WRITE`` is the rest).
DUPLICATE_CLEANUP = "duplicate-cleanup"
MISSING_PARENT_CLEANUP = "orphan-cleanup"
CONSISTENCY_REPAIR = "consistency-repair"
RETENTION_DELETE = "retention-delete"
MULTI_REFERENCE_DELETE = "multi-reference-delete"
BULK_STATUS_FLIP = "bulk-status-flip"
SET_BASED_WRITE = "set-based-write"
INTENTS: tuple[str, ...] = (
    DUPLICATE_CLEANUP,
    MISSING_PARENT_CLEANUP,
    CONSISTENCY_REPAIR,
    RETENTION_DELETE,
    MULTI_REFERENCE_DELETE,
    BULK_STATUS_FLIP,
    SET_BASED_WRITE,
)


def _short(name: object) -> str:
    return str(name or "").rsplit(".", 1)[-1].lower()


def _has(tokens: tuple[str, ...], *sequence: str) -> bool:
    n = len(sequence)
    return any(tokens[i : i + n] == sequence for i in range(len(tokens) - n + 1))


_STRING = re.compile(r"'(?:[^']|'')*'")
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_QUALIFIER = r'(?:(?P<q>"[^"]+"|\[[^\]]+\]|`[^`]+`|[A-Za-z_][\w$]*)\s*\.\s*)?'


def _column(name: str) -> str:
    n = re.escape(name)
    return rf'(?:"{n}"|\[{n}\]|`{n}`|(?<![\w$]){n}(?![\w$]))'


def _unquoted(name: str) -> str:
    return name.strip('"[]`').lower()


def _older_than(text: str, name: str, aliases: Collection[str]) -> bool:
    """Whether ``text`` compares column ``name`` (bare, or qualified by one of
    ``aliases``) as older than a cut-off: ``name < x``, ``name <= x``, ``x > name`` or
    ``x >= name``, in any identifier quoting, outside string literals and comments."""
    visible = _COMMENT.sub(" ", _STRING.sub("''", text))
    forward = re.compile(_QUALIFIER + _column(name) + r"\s*<(?!>)", re.I)
    backward = re.compile(r"(?<![<>!=-])>\s*=?\s*" + _QUALIFIER + _column(name), re.I)
    for pattern in (forward, backward):
        for match in pattern.finditer(visible):
            qualifier = match.group("q")
            if qualifier is None or _unquoted(qualifier) in aliases:
                return True
    return False


def classify_write_intent(
    query: Mapping,
    table_id: str,
    source_engine: str,
    table_columns: Mapping[str, Mapping] | None = None,
    key_columns: Collection[str] = (),
) -> str:
    """What the write ``query`` to ``table_id`` is for: one of :data:`INTENTS`, read from
    its parsed facts in ``source_engine``'s dialect. ``table_columns`` maps the table's
    lower-case column names to their collected columns (a column's type says whether it
    is a point in time); ``key_columns`` are its primary key's (``id`` when unknown).

    In order, the first that holds:

    - ``duplicate-cleanup``: a ``DELETE`` that joins the table to itself on a column that
      is not its key (a self-join on its own key is a way to write an anti-join);
    - ``orphan-cleanup``: an anti-join picks the rows: a ``DELETE`` with a ``NOT EXISTS``,
      a ``NOT IN (SELECT ...)`` or an outer join tested ``IS NULL``, or an ``UPDATE`` that
      rewrites the column of such an outer join (a dangling reference);
    - ``consistency-repair``: the statement reads another table to decide what to change;
    - ``retention-delete``: a ``DELETE`` that keeps only rows newer than a cut-off on one
      of the table's timestamp columns (``<`` or ``<=``, never ``>`` or ``>=``);
    - ``multi-reference-delete``: a ``DELETE`` whose filter is an ``OR`` of equalities;
    - ``bulk-status-flip``: an ``UPDATE`` of the table alone that no full key value
      addresses;
    - ``set-based-write``: the rest.
    """
    text = str(query.get("query_text") or "")
    facts = query_facts(text, source_engine).access_facts()
    kind = facts.kind
    target = _short(table_id)
    tokens = sql_tokens(text, source_engine).tokens
    sources = [s for scope in facts.scopes for s in scope.sources if s.table is not None]
    own = [s for s in sources if _short(s.table.name) == target]  # type: ignore[union-attr]
    others = {_short(s.table.name) for s in sources} - {target}  # type: ignore[union-attr]

    keys = {k.lower() for k in key_columns} or {"id"}
    if kind == "delete":
        for scope in facts.scopes:
            copies = {
                s.alias.lower()
                for s in scope.sources
                if s.table is not None and _short(s.table.name) == target
            }
            if len(copies) < 2:
                continue
            for left, right in scope.joins:
                lq, rq = (left.qualifier or "").lower(), (right.qualifier or "").lower()
                if (
                    {lq, rq} <= copies
                    and lq != rq
                    and left.name.lower() == right.name.lower()
                    and left.name.lower() not in keys
                ):
                    return DUPLICATE_CLEANUP
    outer = {
        s.alias.lower()
        for scope in facts.scopes
        for s in scope.sources
        if s.optional and s.table is not None
    }
    outer_null = bool(outer) and _has(tokens, "IS", "NULL")
    not_exists = _has(tokens, "NOT", "EXISTS") or _has(tokens, "NOT", "IN", "(", "SELECT")
    if kind == "delete" and (outer_null or not_exists):
        return MISSING_PARENT_CLEANUP
    if kind == "update" and outer_null:
        assigned = set(facts.assigned_columns)
        for scope in facts.scopes:
            for left, right in scope.joins:
                for mine, theirs in ((left, right), (right, left)):
                    if (theirs.qualifier or "") in outer and mine.name in assigned:
                        return MISSING_PARENT_CLEANUP
    if others and kind in ("update", "delete", "merge"):
        return CONSISTENCY_REPAIR
    columns = table_columns or {}
    aliases = {s.alias.lower() for s in own} | {target}
    if kind == "delete":
        # The direction is read from the text: the facts keep a range's column, not which
        # side of the cut-off it keeps, and read none against a typed value
        # (``created_at < TIMESTAMP $1``).
        for name, info in columns.items():
            if is_timestamp_type(info, source_engine) and _older_than(text, name, aliases):
                return RETENTION_DELETE
        if any(len(scope.or_branches) >= 2 for scope in facts.scopes):
            return MULTI_REFERENCE_DELETE
    if kind == "update" and not others:
        pinned = {
            c.name.lower()
            for scope in facts.scopes
            for c in scope.equalities
            if (c.qualifier or target).lower() in aliases
        }
        if not keys <= pinned:
            return BULK_STATUS_FLIP
    return SET_BASED_WRITE


@dataclass(frozen=True)
class CarriedWrite:
    """One write-group member its group's engine serves poorly (see the module docstring)."""

    query_id: str
    table_id: str
    engine: str
    score: float
    floor: float
    group_size: int
    intent: str
    statement: str = ""

    def as_record(self) -> dict[str, Any]:
        """The record the designer input and the risk register read: the intent and the
        engine's approach worded by the catalog."""
        entry = write_intent(self.intent)
        approach = entry.approach(self.engine)
        return {
            "query_id": self.query_id,
            "table_id": self.table_id,
            "engine": self.engine,
            "score": self.score,
            "floor": self.floor,
            "group_size": self.group_size,
            "statement": self.statement,
            "intent": self.intent,
            "intent_name": entry.name,
            "intent_summary": entry.summary,
            "approach": approach.approach if approach else "",
            "docs": approach.docs if approach else "",
        }


def _statement(query: Mapping, source_engine: str) -> str:
    """The statement's kind as SQL names it (``DELETE``, ``UPDATE``, ...)."""
    kind = query_facts(str(query.get("query_text") or ""), source_engine).access_facts().kind
    if kind in ("select", "other"):
        return str(query.get("query_type") or "").upper()
    return str(kind).upper()


def _table_columns(collector: Mapping) -> dict[str, dict[str, Mapping]]:
    out: dict[str, dict[str, Mapping]] = {}
    for table in (collector.get("database_schema") or {}).get("tables") or []:
        table_id = str(table.get("table_id") or "")
        if table_id:
            out[table_id] = {
                str(c.get("column_name") or "").lower(): c for c in table.get("columns") or []
            }
    return out


def carried_writes(
    assignment: Mapping | None,
    collector: Mapping | None,
    analysis_outputs: Mapping[str, Mapping],
    triage: Mapping | None = None,
    floor: float | None = None,
) -> list[CarriedWrite]:
    """Every in-scope write-group member of ``assignment`` whose own score on its
    table's source of truth is below ``floor`` (the catalog's when None), sorted by table
    and query. A table with one write statement carries nothing; an assignment written
    before write groups, or no collector, gives none."""
    from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
    from src.agents.referee.write_groups import assignment_sources_of_truth

    assignment = assignment or {}
    collector = collector or {}
    groups = assignment.get("write_groups") or {}
    if not groups or not collector:
        return []
    floor = carried_write_floor() if floor is None else floor
    queries = {
        str(q.get("query_id")): q
        for q in (collector.get("queries") or {}).get("query_patterns") or []
        if q.get("query_id")
    }
    placed = {
        str(qa["query_id"]): str(qa.get("assigned_engine") or "")
        for qa in assignment.get("query_assignments") or []
        if qa.get("query_id") and qa.get("in_scope", True)
    }
    sources = assignment_sources_of_truth(assignment)
    ctx = ScoringContext.from_pipeline(
        {e: dict(a or {}) for e, a in analysis_outputs.items()}, triage or {}, collector
    ).with_sources_of_truth(sources)
    engine_of_source = source_engine_of(collector)
    columns = _table_columns(collector)
    primary = {
        str(t.get("table_id") or ""): tuple(str(c) for c in t.get("primary_key") or [])
        for t in (collector.get("database_schema") or {}).get("tables") or []
    }
    out: list[CarriedWrite] = []
    for table in sorted(groups):
        members = [str(m) for m in groups[table] or []]
        engine = sources.get(table)
        if len(members) < 2 or not engine:
            continue
        for qid in sorted(set(members)):
            query = queries.get(qid)
            if query is None or placed.get(qid) != engine:
                continue
            score = score_query_for_engine(query, engine, ctx).score
            if score is None or score >= floor:
                continue
            out.append(
                CarriedWrite(
                    query_id=qid,
                    table_id=table,
                    engine=engine,
                    score=float(score),
                    floor=float(floor),
                    group_size=len(members),
                    intent=classify_write_intent(
                        query, table, engine_of_source, columns.get(table), primary.get(table, ())
                    ),
                    statement=_statement(query, engine_of_source),
                )
            )
    return out


def carried_write_records(
    assignment: Mapping | None,
    collector: Mapping | None,
    analysis_outputs: Mapping[str, Mapping],
    triage: Mapping | None = None,
    engine: str | None = None,
) -> list[dict[str, Any]]:
    """:func:`carried_writes` as records, those on ``engine`` only when one is given."""
    return [
        c.as_record()
        for c in carried_writes(assignment, collector, analysis_outputs, triage)
        if engine is None or c.engine == engine
    ]


def by_table(records: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Carried-write records grouped by their table, tables and queries sorted."""
    out: dict[str, list[Mapping[str, Any]]] = {}
    for r in records:
        out.setdefault(str(r.get("table_id") or ""), []).append(r)
    return {t: sorted(out[t], key=lambda r: str(r.get("query_id"))) for t in sorted(out)}


__all__ = [
    "BULK_STATUS_FLIP",
    "CONSISTENCY_REPAIR",
    "DUPLICATE_CLEANUP",
    "INTENTS",
    "MULTI_REFERENCE_DELETE",
    "MISSING_PARENT_CLEANUP",
    "RETENTION_DELETE",
    "SET_BASED_WRITE",
    "CarriedWrite",
    "by_table",
    "carried_write_records",
    "carried_writes",
    "classify_write_intent",
]
