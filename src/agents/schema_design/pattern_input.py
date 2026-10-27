"""Design per access pattern, not per statement (#1602).

A large workload repeats one access pattern in many statement shapes: the same
tables, predicate shape, order, limit class and operation, with different literals,
IN-list lengths or returned columns (``src.tools.sql.query_pattern_groups``, #1146).
For the engines in :data:`PATTERN_ENGINES` the design-group split works on query
patterns, not statements:

- :func:`pattern_units` turns a design call's statements into one unit per query
  pattern, the members in collection order;
- the splitter caps a part by units (``group_splitter.MAX_GROUP_SIZE`` patterns) and
  keeps today's table ownership and context tables, computed on the member statements;
- a part's input lists one entry per unit (:func:`pattern_entry`): a representative
  statement with the pattern's summed traffic, and the pattern's members, combined
  frequency and returned columns in ``queries.query_pattern_groups``
  (:func:`pattern_listing`);
- everything keyed by a query id (the analysis signals, the key-scoped writes and
  counts, the redesign requirements, the carried writes, the tables) still reads every
  member, so no context is dropped;
- ``design_metrics`` gives the pattern one row with its summed rate and its
  ``member_query_ids``, and ``check_design_numbers`` asks an access pattern that serves
  one member to list them all;
- ``--merge`` adds a member no access pattern names to the access pattern that serves
  the rest of its pattern (:func:`complete_pattern_members`), so every statement still
  maps to the design that serves it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.tools.schema.design_metrics import base_rps, projected_members
from src.tools.sql.query_pattern_groups import PATTERN_ID_PREFIX, group_by_query_pattern

#: Engines whose design groups are built and written per query pattern.
PATTERN_ENGINES = frozenset({"dynamodb"})

#: The key of a unit's member statements in a splitter unit (a statement dict).
MEMBERS_KEY = "_members"

# How a representative entry combines its members' figures.
_SUMMED = (
    "total_time_ms",
    "db_load_contribution_percent",
    "lock_time_ms",
    "full_table_scans",
    "queries_without_index",
    "errors",
    "warnings",
)
_WORST = ("rows_returned_p95", "execution_time_ms_p95", "execution_time_ms_p99", "join_count")
_WEIGHTED = (
    "rows_returned_avg",
    "rows_affected_avg",
    "rows_examined_avg",
    "execution_time_ms_avg",
    "scan_efficiency_pct",
    "lock_time_pct",
)
_ANY = (
    "has_joins",
    "has_aggregations",
    "has_subqueries",
    "has_text_search",
    "has_time_range_filter",
)
_UNION = ("filter_columns", "sort_columns", "tables_accessed")
_PCT_CAP = {
    "db_load_contribution_percent": 100.0,
    "scan_efficiency_pct": 100.0,
    "lock_time_pct": 100.0,
}


def _qid(query: Mapping[str, Any]) -> str:
    return str(query.get("query_id") or "")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rate(query: Mapping[str, Any]) -> float:
    return base_rps(
        _number(query.get("calls_per_second")), _number(query.get("frequency_per_hour"))
    )


def pattern_units(queries: Sequence[dict], source_engine: str) -> list[dict]:
    """One unit per query pattern of ``queries``, in the order of each pattern's first
    statement: that statement's dict with every member (itself first) under
    :data:`MEMBERS_KEY`. A statement with no ``query_id`` is a unit of its own."""
    by_id: dict[str, dict] = {}
    for q in queries:
        by_id.setdefault(_qid(q), q)
    groups = group_by_query_pattern(queries, source_engine)
    first_of = {g.query_ids[0]: [by_id[i] for i in g.query_ids] for g in groups}
    units: list[dict] = []
    for q in queries:
        qid = _qid(q)
        if not qid:
            units.append({**q, MEMBERS_KEY: [q]})
        elif qid in first_of:
            units.append({**q, MEMBERS_KEY: first_of.pop(qid)})
    return units


def members(unit: Mapping[str, Any]) -> list[dict]:
    """A splitter unit's member statements (a plain statement is its own only member)."""
    found = unit.get(MEMBERS_KEY)
    return list(found) if isinstance(found, list) else [dict(unit)]


def split_unit(unit: Mapping[str, Any]) -> list[dict]:
    """``unit`` cut into two units of consecutive members, the first half the larger;
    a one-member unit is returned as is. Used when one pattern's members alone are
    over a part's input budget: each half is designed in its own part."""
    found = members(unit)
    if len(found) <= 1:
        return [dict(unit)]
    mid = (len(found) + 1) // 2
    return [{**half[0], MEMBERS_KEY: half} for half in (found[:mid], found[mid:])]


def expand(units: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The member statements of ``units``, in order."""
    return [m for u in units for m in members(u)]


def representative(statements: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The member that stands for the pattern: the busiest, then the first."""
    best = 0
    for n, q in enumerate(statements):
        if _rate(q) > _rate(statements[best]):
            best = n
    return statements[best]


def pattern_entry(statements: Sequence[Mapping[str, Any]], pattern_id: str | None) -> dict:
    """The input entry of one query pattern: its representative statement, with the
    traffic of every member. ``frequency_per_hour`` and ``calls_per_second`` are the
    members' totals (a member with no ``calls_per_second`` counts its
    ``frequency_per_hour`` / 3600), as are the time and count totals; the 95th and 99th
    percentiles are the worst member's; the averages are weighted by call rate; the
    filter, sort and table lists are the union. ``query_pattern_id`` names its
    ``queries.query_pattern_groups`` entry. A one-statement pattern is the statement."""
    rep = dict(representative(statements))
    if len(statements) <= 1:
        return rep
    rates = [_rate(q) for q in statements]
    total = sum(rates)
    entry = dict(rep)
    entry["frequency_per_hour"] = round(
        sum(_number(q.get("frequency_per_hour")) or 0.0 for q in statements), 6
    )
    if any(_number(q.get("calls_per_second")) is not None for q in statements):
        entry["calls_per_second"] = round(total, 6)
    for key in _SUMMED:
        values = [v for v in (_number(q.get(key)) for q in statements) if v is not None]
        if values:
            value = min(sum(values), _PCT_CAP.get(key, float("inf")))
            whole = all(isinstance(q.get(key), int) for q in statements if q.get(key) is not None)
            entry[key] = int(value) if whole else round(value, 6)
    for key in _WORST:
        worst: list[Any] = [q[key] for q in statements if _number(q.get(key)) is not None]
        if worst:
            entry[key] = max(worst)
    for key in _WEIGHTED:
        pairs = [
            (v, r)
            for v, r in ((_number(q.get(key)), r) for q, r in zip(statements, rates, strict=True))
            if v is not None
        ]
        if pairs:
            weight = sum(r for _, r in pairs)
            if weight:
                mean = sum(v * r for v, r in pairs) / weight
            else:
                mean = sum(v for v, _ in pairs) / len(pairs)
            entry[key] = round(min(mean, _PCT_CAP.get(key, float("inf"))), 6)
    for key in _ANY:
        flags: list[bool] = [bool(q[key]) for q in statements if isinstance(q.get(key), bool)]
        if flags:
            entry[key] = any(flags)
    for key in _UNION:
        lists: list[list[Any]] = [q[key] for q in statements if isinstance(q.get(key), list)]
        if lists:
            entry[key] = list(dict.fromkeys(str(x) for xs in lists for x in xs))
    seen = [str(q.get("first_seen")) for q in statements if q.get("first_seen")]
    if seen:
        entry["first_seen"] = min(seen)
    last = [str(q.get("last_seen")) for q in statements if q.get("last_seen")]
    if last:
        entry["last_seen"] = max(last)
    if pattern_id:
        entry["query_pattern_id"] = pattern_id
    return entry


def pattern_listing(
    units: Sequence[Sequence[Mapping[str, Any]]], source_engine: str
) -> list[dict[str, Any]]:
    """``queries.query_pattern_groups`` for a part's units: one entry per unit of more
    than one member, numbered ``PAT-<n>`` by the unit's position, with every member's
    query id, the members' combined frequency and the union of the columns they return."""
    listing: list[dict[str, Any]] = []
    for n, unit in enumerate(units, start=1):
        if len(unit) <= 1:
            continue
        found = group_by_query_pattern(unit, source_engine)
        if not found:
            continue
        group = found[0].to_dict()
        ids = [_qid(q) for q in unit if _qid(q)]
        group["pattern_id"] = f"{PATTERN_ID_PREFIX}{n}"
        group["query_ids"] = ids
        if len(found) > 1:  # one pattern by construction; never lose a member's total
            group["frequency_per_hour"] = round(sum(g.frequency_per_hour for g in found), 6)
            rates = [g.calls_per_second for g in found if g.calls_per_second is not None]
            group["calls_per_second"] = round(sum(rates), 6) if rates else None
            group["returned_columns"] = sorted(
                {c for g in found for c in g.returned_columns}, key=lambda c: (c != "*", c)
            )
        listing.append(group)
    return listing


def listed_members(collector_output: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each entry's query id -> its pattern's member query ids, for a design input
    written per pattern (``design_metrics.projected_members`` on its ``queries``). Empty
    for an input that lists every statement (written before #1602, or an engine
    designed per statement)."""
    queries = collector_output.get("queries")
    return projected_members(queries) if isinstance(queries, Mapping) else {}


def input_query_ids(collector_output: Mapping[str, Any]) -> list[str]:
    """Every statement a design input covers, in order: each entry's query id, and for
    an entry that stands for a query pattern, every member's (:func:`listed_members`)."""
    queries = collector_output.get("queries")
    if not isinstance(queries, Mapping):
        return []
    found = listed_members(collector_output)
    ids: list[str] = []
    for q in queries.get("query_patterns") or []:
        if not isinstance(q, Mapping) or not q.get("query_id"):
            continue
        qid = str(q["query_id"])
        ids.extend(found.get(qid, [qid]))
    return list(dict.fromkeys(ids))


def complete_pattern_members(
    design: dict, patterns: Iterable[Sequence[str]], ids_key: str = "query_ids"
) -> tuple[dict, list[str]]:
    """``design`` with every query pattern's members on the access pattern that serves
    it, and one note per access pattern changed.

    A member no access pattern names is added to the first access pattern that names
    another member of its pattern; a pattern no access pattern serves is left alone
    (the scope and coverage checks report it). A member another access pattern already
    names is never moved. Deterministic; ``design`` is not modified."""
    aps = design.get("access_patterns")
    if not isinstance(aps, list):
        return design, []
    named = {str(q) for ap in aps if isinstance(ap, dict) for q in ap.get(ids_key) or [] if q}
    additions: dict[int, list[str]] = {}
    for pattern in patterns:
        ids = [str(i) for i in pattern]
        missing = [i for i in ids if i not in named]
        if not missing or len(missing) == len(ids):
            continue
        for n, ap in enumerate(aps):
            if isinstance(ap, dict) and set(ids) & {str(q) for q in ap.get(ids_key) or []}:
                additions.setdefault(n, []).extend(missing)
                named.update(missing)
                break
    if not additions:
        return design, []
    patched = list(aps)
    notes: list[str] = []
    for n, added in sorted(additions.items()):
        ap = dict(patched[n])
        ap[ids_key] = [*(ap.get(ids_key) or []), *added]
        patched[n] = ap
        notes.append(
            f"access pattern {ap.get('pattern_id') or n}: added {len(added)} member "
            f"statement(s) of the query pattern it serves to {ids_key}: {', '.join(added)}"
        )
    return {**design, "access_patterns": patched}, notes
