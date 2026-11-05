# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Views resolved to their base tables, for every statement that reads one (#1875).

A view holds no rows, so it has no table-level evidence (row count, size, access
statistics) and every engine scores a statement on it 0. The collection does hold
each view's definition, so the collector resolves it here, once, and every later
step sees the base tables:

- each view's definition is read with the source dialect (``facade.read_view_definition``);
  each relation it names resolves to a collected table or view
  (:class:`~src.agents.referee.table_resolution.TableNameResolver`), or is kept as
  written, as a statement's own unknown names are;
- a view on views resolves transitively. A view on a cycle of views (``a`` reads
  ``b``, ``b`` reads ``a``) cannot run, so it is ``unresolved``;
- a statement's ``tables_accessed`` names each resolved view's base tables in its
  place, and ``views_read`` records which views stood for which tables, so the
  statement is scored on those tables exactly as a statement that joins them is.
  Its ``join_count`` (and ``has_joins``) adds the view's joins;
- the statement's text with its views resolved (``facade.resolve_view_text``) is kept
  as ``view_resolved_text``, and the access-pattern class, the triage signals and the
  capability gates read it (:func:`access_text`): a read whose views merge is the direct
  join of their base tables; a view that does not merge (an aggregation, ``GROUP BY``,
  ``DISTINCT``, ``TOP``/``LIMIT``, ``UNION``) is its body as a derived table, so its
  signals are the statement's (``has_aggregations`` and ``has_subqueries`` too); a write
  through a column-preserving view is written onto its base table. A statement that
  cannot be rewritten keeps its own text, and ``view_text_fallback`` says why (the
  classification's reason says so too);
- a view with no definition, one whose definition names no collected table, or one on
  a cycle is ``unresolved`` with a plain reason (``View.lineage_reason``), and a
  statement on it keeps the view's name: it has no table to score, and the pending
  engine choice says why instead of showing a silent 0-score tie;
- a write through a view (``UPDATE v SET ...``, ``INSERT INTO v (...)``) writes one
  base table: the one its written columns come from, as SQL Server and MySQL require
  of an updatable view, else (columns unknown) every base table, which keeps a
  table's writes together (``ViewRead.written_tables``, read by
  ``write_groups.written_tables``).

:func:`apply_view_lineage` does all of it on a collector output, a contract model or
its JSON form alike.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.tools.sql import facade
from src.tools.sql.refs import TableRef

if TYPE_CHECKING:
    from src.agents.referee.table_resolution import TableNameResolver

logger = logging.getLogger(__name__)

__all__ = [
    "PARTIAL",
    "RESOLVED",
    "UNRESOLVED",
    "Lineage",
    "ViewLineages",
    "access_text",
    "apply_view_lineage",
]

RESOLVED = "resolved"
PARTIAL = "partial"
UNRESOLVED = "unresolved"

NO_DEFINITION = "the collection holds no definition for this view"

_TABLE = "table"
_VIEW = "view"
_OTHER = "other"


@dataclass(frozen=True)
class Lineage:
    """One view resolved to its base tables.

    ``base_tables``: the tables its definition reads, through every view it reads, in
    first-seen order (a name the collection has no table or view for kept as written).
    ``views``: the views it reads, transitively. ``join_count``: its joins and those of
    the views it reads. ``status`` and ``reason``: see :data:`RESOLVED`, :data:`PARTIAL`
    and :data:`UNRESOLVED`.
    """

    view_id: str
    base_tables: tuple[str, ...]
    views: tuple[str, ...]
    join_count: int
    status: str
    reason: str | None = None
    # What the view's own body reads: (kind, id) per relation, and per projected
    # column the (kind, id) and column it shows, for a write through the view.
    direct: tuple[tuple[str, str], ...] = ()
    columns: Mapping[str, tuple[tuple[str, str], str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Parsed:
    view_id: str
    direct: tuple[tuple[str, str], ...]
    columns: Mapping[str, tuple[tuple[str, str], str]]
    join_count: int
    fallback_reason: str | None
    has_definition: bool


def _get(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _set(record: Any, name: str, value: Any) -> None:
    if isinstance(record, MutableMapping):
        record[name] = value
    else:
        setattr(record, name, value)


def _bare(name: str) -> str:
    return str(name).rsplit(".", 1)[-1].strip('[]"`').lower()


def _names_view(ref: TableRef, view_name: str, schema_name: str | None) -> bool:
    """Whether ``ref`` may be the view's own name (the ``CREATE VIEW`` target): its name,
    unqualified or in the view's own schema. An unqualified one may also be a table of
    that name the body reads (``CREATE VIEW reporting.users AS SELECT ... FROM users``),
    which the caller checks."""
    if ref.name.lower() != view_name.lower():
        return False
    return not ref.schema or not schema_name or ref.schema.lower() == schema_name.lower()


class ViewLineages:
    """Every collected view of one collection, resolved to its base tables."""

    def __init__(
        self,
        lineages: Mapping[str, Lineage],
        resolver: TableNameResolver | None,
        view_ids: frozenset[str],
        merge_views: Mapping[str, str] | None = None,
        merge_columns: Mapping[str, tuple[str, ...]] | None = None,
    ):
        self._lineages = dict(lineages)
        self._resolver = resolver
        self._view_ids = view_ids
        # What facade.resolve_view_text reads: each resolved view's definition and each
        # table's columns, by bare lower-case name (a name two of them share left out).
        self._merge_views = dict(merge_views or {})
        self._merge_columns = dict(merge_columns or {})

    def __bool__(self) -> bool:
        return bool(self._lineages)

    def __getitem__(self, view_id: str) -> Lineage:
        return self._lineages[view_id]

    def __iter__(self) -> Iterator[Lineage]:
        return iter(self._lineages.values())

    @classmethod
    def build(cls, tables: Iterable[Any], views: Iterable[Any], source_engine: str) -> ViewLineages:
        """The lineages of ``views`` (``View`` models or their dicts) against ``tables``
        (``Table`` models or dicts), read in ``source_engine``'s dialect."""
        from src.agents.referee.table_resolution import TableNameResolver

        tables = list(tables)
        table_rows = [
            {"table_id": str(_get(t, "table_id")), "table_name": _get(t, "table_name")}
            for t in tables
            if _get(t, "table_id")
        ]
        view_rows = [v for v in views if _get(v, "view_id")]
        if not view_rows:
            return cls({}, None, frozenset())
        resolver = TableNameResolver.from_collector(
            {
                "database_schema": {
                    "tables": table_rows,
                    "views": [
                        {"view_id": str(_get(v, "view_id")), "view_name": _get(v, "view_name")}
                        for v in view_rows
                    ],
                }
            }
        )
        table_ids = frozenset(r["table_id"] for r in table_rows)
        view_ids = frozenset(str(_get(v, "view_id")) for v in view_rows)

        tables_only = TableNameResolver.from_collector({"database_schema": {"tables": table_rows}})

        def own_name(ref: TableRef, name: str, schema: str | None) -> bool:
            """The view's own name, not a collected table of that name it reads."""
            if not _names_view(ref, name, schema):
                return False
            if ref.schema:
                return True
            return tables_only is None or tables_only.resolve(ref.written) is None

        def relation(ref: TableRef, name: str, schema: str | None) -> tuple[str, str]:
            if ref.name.lower() == name.lower() and not ref.schema and tables_only is not None:
                found = tables_only.resolve(ref.written)
                if found is not None:
                    return _TABLE, found  # a table that shares the view's name
            return kind_of(ref)

        def kind_of(ref: TableRef) -> tuple[str, str]:
            found = resolver.resolve(ref.written) if resolver is not None else None
            if found is None:
                return _OTHER, ref.written
            if found in view_ids and found not in table_ids:
                return _VIEW, found
            if found in table_ids:
                return _TABLE, found
            return _OTHER, ref.written

        parsed: dict[str, _Parsed] = {}
        for v in view_rows:
            view_id = str(_get(v, "view_id"))
            definition = str(_get(v, "definition") or "")
            if not definition.strip():
                parsed[view_id] = _Parsed(view_id, (), {}, 0, None, False)
                continue
            read = facade.read_view_definition(definition, source_engine)
            name = str(_get(v, "view_name") or _bare(view_id))
            schema = _get(v, "schema_name")
            direct = tuple(
                dict.fromkeys(
                    relation(ref, name, schema)
                    for ref in read.reading.tables.tables
                    if not own_name(ref, name, schema)
                )
            )
            columns = {c.name: (relation(c.source, name, schema), c.column) for c in read.columns}
            parsed[view_id] = _Parsed(
                view_id,
                direct,
                columns,
                int(read.reading.facts.join_count or 0),
                read.reading.tables.fallback_reason,
                True,
            )
        lineages = _resolve_all(parsed)
        names: dict[str, list[str]] = {}
        for t in tables:
            if _get(t, "table_name"):
                names.setdefault(str(_get(t, "table_name")).lower(), []).append("table")
        for v in view_rows:
            names.setdefault(_bare(str(_get(v, "view_name") or _get(v, "view_id"))), []).append(
                "view"
            )
        merge_views = {
            _bare(str(_get(v, "view_name") or _get(v, "view_id"))): str(_get(v, "definition"))
            for v in view_rows
            if lineages[str(_get(v, "view_id"))].status != UNRESOLVED
        }
        merge_columns = {
            str(_get(t, "table_name")).lower(): tuple(
                str(_get(c, "column_name")).lower()
                for c in _get(t, "columns") or []
                if _get(c, "column_name")
            )
            for t in tables
            if _get(t, "table_name")
        }
        unique = {name for name, kinds in names.items() if len(kinds) == 1}
        return cls(
            lineages,
            resolver,
            view_ids,
            {n: d for n, d in merge_views.items() if n in unique},
            {n: c for n, c in merge_columns.items() if n in unique},
        )

    def resolved_text(self, text: str, source_engine: str) -> facade.ViewText:
        """``text`` with its views resolved (``facade.resolve_view_text``)."""
        if not self._merge_views or not text:
            return facade.ViewText(None, fallback="it names no view the collector could read")
        return facade.resolve_view_text(text, source_engine, self._merge_views, self._merge_columns)

    def view_id(self, name: str) -> str | None:
        """The collected view ``name`` (a ``tables_accessed`` entry) names, or None for a
        table, an unknown name or an ambiguous one."""
        if self._resolver is None or not name or name == "unknown":
            return None
        found = self._resolver.resolve(str(name))
        if found is None or found not in self._view_ids or found not in self._lineages:
            return None
        return found

    def written_through(self, view_id: str, columns: tuple[str, ...] | None) -> list[str]:
        """The base tables a write through ``view_id`` of ``columns`` changes (see the
        module docstring); empty for an unresolved view."""
        return self._written_through(view_id, columns, set())

    def _written_through(
        self, view_id: str, columns: tuple[str, ...] | None, seen: set[str]
    ) -> list[str]:
        lineage = self._lineages.get(view_id)
        if lineage is None or lineage.status == UNRESOLVED or view_id in seen:
            return []
        seen.add(view_id)
        target: tuple[str, str] | None = None
        mapped: tuple[str, ...] | None = None
        if len(lineage.direct) == 1:
            target = lineage.direct[0]
            if columns is not None:
                mapped = tuple(
                    lineage.columns[c][1] if c in lineage.columns else c for c in columns
                )
        elif columns and all(c in lineage.columns for c in columns):
            sources = {lineage.columns[c][0] for c in columns}
            if len(sources) == 1:
                (target,) = sources
                mapped = tuple(lineage.columns[c][1] for c in columns)
        if target is None:
            return list(lineage.base_tables)
        kind, rel = target
        if kind == _VIEW:
            return self._written_through(rel, mapped, seen) or list(lineage.base_tables)
        return [rel]


def _cyclic(parsed: Mapping[str, _Parsed]) -> set[str]:
    """The views that can reach themselves through the views they read."""
    edges = {
        vid: [rel for kind, rel in p.direct if kind == _VIEW and rel in parsed]
        for vid, p in parsed.items()
    }
    out: set[str] = set()
    for start in edges:
        stack = list(edges[start])
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                out.add(start)
                break
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, ()))
    return out


def _resolve_all(parsed: Mapping[str, _Parsed]) -> dict[str, Lineage]:
    cyclic = _cyclic(parsed)
    done: dict[str, Lineage] = {}

    def resolve(view_id: str) -> Lineage:
        if view_id in done:
            return done[view_id]
        p = parsed[view_id]
        if not p.has_definition:
            done[view_id] = Lineage(view_id, (), (), 0, UNRESOLVED, NO_DEFINITION)
            return done[view_id]
        if view_id in cyclic:
            others = sorted(
                rel for kind, rel in p.direct if kind == _VIEW and rel in cyclic and rel != view_id
            )
            names = ", ".join([view_id, *others])
            done[view_id] = Lineage(
                view_id,
                (),
                tuple(rel for kind, rel in p.direct if kind == _VIEW),
                0,
                UNRESOLVED,
                f"it is on a cycle of views that read each other ({names}), so it cannot run",
                p.direct,
                p.columns,
            )
            return done[view_id]
        base: list[str] = []
        views: list[str] = []
        joins = p.join_count
        problems: list[str] = []
        if p.fallback_reason is not None:
            problems.append(
                f"its definition could not be parsed ({p.fallback_reason}), so its base tables "
                "are the regex scan's reading"
            )
        for kind, rel in p.direct:
            if kind == _VIEW:
                views.append(rel)
                sub = resolve(rel)
                base.extend(sub.base_tables)
                views.extend(sub.views)
                joins += sub.join_count
                if sub.status != RESOLVED:
                    problems.append(f"it reads view {rel}, which is {sub.status}: {sub.reason}")
            else:
                base.append(rel)
                if kind == _OTHER:
                    problems.append(f"it reads {rel}, which the collection has no table for")
        if not any(kind == _TABLE for kind, _ in p.direct) and not any(
            done[rel].base_tables for kind, rel in p.direct if kind == _VIEW and rel in done
        ):
            reason = "; ".join(problems) or "its definition reads no collected table"
            done[view_id] = Lineage(
                view_id,
                (),
                tuple(dict.fromkeys(views)),
                0,
                UNRESOLVED,
                reason,
                p.direct,
                p.columns,
            )
            return done[view_id]
        done[view_id] = Lineage(
            view_id,
            tuple(dict.fromkeys(base)),
            tuple(dict.fromkeys(views)),
            joins,
            PARTIAL if problems else RESOLVED,
            "; ".join(problems) or None,
            p.direct,
            p.columns,
        )
        return done[view_id]

    for view_id in parsed:
        resolve(view_id)
    return done


def access_text(query: Mapping) -> str:
    """The text a statement's access pattern is read from: its ``view_resolved_text`` (the
    statement with its views resolved, #1875) when the collector wrote one, else its
    ``query_text`` (``view_text_fallback`` says why, for a statement on a view)."""
    return str(query.get("view_resolved_text") or query.get("query_text") or "")


def apply_view_lineage(collector_output: Any, source_engine: str | None = None) -> Any:
    """``collector_output`` (a ``CollectorOutputContract`` or its dict) with its views
    resolved and every statement on a view scored on its base tables (the module
    docstring). Changed in place and returned. A collection with no view is returned
    unchanged."""
    schema = _get(collector_output, "database_schema")
    views = list(_get(schema, "views") or []) if schema is not None else []
    if not views:
        return collector_output
    if source_engine is None:
        metadata = _get(collector_output, "metadata")
        source = _get(metadata, "source_database") if metadata is not None else None
        engine = _get(source, "engine") if source is not None else None
        source_engine = str(getattr(engine, "value", engine) or "").strip().lower()
    lineages = ViewLineages.build(_get(schema, "tables") or [], views, source_engine or "")
    for view in views:
        lineage = lineages[str(_get(view, "view_id"))]
        _set(view, "referenced_tables", list(lineage.base_tables))
        _set(view, "referenced_views", list(lineage.views) or None)
        _set(view, "lineage_status", lineage.status)
        _set(view, "lineage_reason", lineage.reason)
    unresolved = [lin.view_id for lin in lineages if lin.status == UNRESOLVED]
    if unresolved:
        logger.info(
            "view_lineage: %d of %d views have no resolved base table: %s",
            len(unresolved),
            len(views),
            ", ".join(sorted(unresolved)[:10]),
        )
    queries = _get(collector_output, "queries")
    for pattern in list(_get(queries, "query_patterns") or []) if queries is not None else []:
        _apply_to_pattern(pattern, lineages, source_engine or "")
    return collector_output


def _apply_to_pattern(pattern: Any, lineages: ViewLineages, source_engine: str) -> None:
    from src.contracts.collector_output import ViewRead
    from src.tools.sql.query_facts import query_facts

    if _get(pattern, "views_read") is not None:
        return  # already resolved: its view names are its base tables now
    tables = [str(t) for t in _get(pattern, "tables_accessed") or []]
    found = [(name, vid) for name in tables if (vid := lineages.view_id(name)) is not None]
    if not found:
        return
    text = str(_get(pattern, "query_text") or "")
    targets = set(query_facts(text, source_engine).write_targets) if text else set()
    columns = facade.written_columns(text, source_engine) if targets else None
    out: list[str] = []
    reads: list[ViewRead] = []
    joins = 0
    of_name = dict(found)
    for name in tables:
        vid = of_name.get(name)
        if vid is None:
            out.append(name)
            continue
        lineage = lineages[vid]
        written = None
        if _bare(name) in targets or _bare(vid) in targets:
            written = lineages.written_through(vid, columns)
        reads.append(
            ViewRead(view_id=vid, base_tables=list(lineage.base_tables), written_tables=written)
        )
        if lineage.status == UNRESOLVED:
            out.append(name)
            continue
        out.extend(lineage.base_tables)
        out.extend(v for v in lineage.views if lineages[v].status == UNRESOLVED)
        joins += lineage.join_count
    _set(pattern, "tables_accessed", list(dict.fromkeys(out)) or ["unknown"])
    unresolved = [vid for _, vid in found if lineages[vid].status == UNRESOLVED]
    if unresolved:
        vid = unresolved[0]
        fallback = f"it reads view {vid}, which is unresolved: {lineages[vid].reason}"
        resolved = facade.ViewText(None, fallback=fallback)
    else:
        resolved = lineages.resolved_text(text, source_engine)
    _set(pattern, "view_resolved_text", resolved.text)
    _set(pattern, "view_text_form", resolved.form)
    _set(pattern, "view_text_fallback", resolved.fallback)
    if resolved.text is not None:
        # The view's body is the statement's too: its aggregation and subqueries.
        body = facade.sql_facts(resolved.text, source_engine)
        if body.has_aggregation:
            _set(pattern, "has_aggregations", True)
        if body.has_subquery:
            _set(pattern, "has_subqueries", True)
    as_records = isinstance(pattern, MutableMapping)
    _set(
        pattern,
        "views_read",
        [r.model_dump(mode="json") for r in reads] if as_records else reads,
    )
    if joins:
        _set(pattern, "join_count", int(_get(pattern, "join_count") or 0) + joins)
        _set(pattern, "has_joins", True)
