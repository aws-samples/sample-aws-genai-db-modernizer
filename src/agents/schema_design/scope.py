"""Schema design scope validation (issue #203).

A schema design for an engine is produced from the collector input filtered to
the queries the effective assignment puts in scope for that engine (see
``filter_collector_for_assignment``). A model can still drift outside that
scope and design tables or claim queries that belong to another engine. This
module checks a design against the assignment so finalize and ``--merge`` can
reject it.

Only the fields that *design* something are checked, per engine contract
(``src/contracts/<engine>_model_output.py``):

- dynamodb: ``table_definitions[].source_tables``,
  ``table_definitions[].entities[].source_table``,
  ``access_patterns[].source_tables`` / ``query_ids``,
  ``unsupported_patterns[].query_ids``
- documentdb: ``collections[].source_tables``,
  ``collections[].embedded_entities[].source_table``,
  ``collections[].indexes[].source_query_ids``,
  ``access_patterns[].source_tables`` / ``source_query_ids``,
  ``unsupported_patterns[].source_query_ids``
- opensearch: ``index_designs[].source_tables``,
  ``data_stream_designs[].source_tables``,
  ``access_patterns[].source_tables`` / ``query_ids``,
  ``unsupported_patterns[].query_ids``
- elasticache: ``key_designs[].source_tables``,
  ``access_patterns[].source_tables`` / ``source_query_ids``,
  ``unsupported_patterns[].source_query_ids``
- aurora_mysql / aurora_postgresql: ``table_definitions[].table_name`` (Aurora
  tables carry over 1:1 from the source, so the name is the source table)

Trade-offs, migration notes and ElastiCache ``cache_invalidation`` write query
IDs are deliberately not checked: they legitimately name other engines' tables
and queries (e.g. "orders stays in Aurora", "invalidate on the Aurora write").
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from src.storage.assignment_versioning import engine_scope

# (list field, label field, table fields, query-id fields, nested lists)
# A nested list entry is (field, label field, table fields, query-id fields).
_Spec = tuple[
    str,
    str | None,
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, str | None, tuple[str, ...], tuple[str, ...]], ...],
]

_AURORA: tuple[_Spec, ...] = (("table_definitions", "table_name", ("table_name",), (), ()),)

_ENGINE_FIELDS: dict[str, tuple[_Spec, ...]] = {
    "dynamodb": (
        (
            "table_definitions",
            "table_name",
            ("source_tables",),
            (),
            (("entities", "entity_type", ("source_table",), ()),),
        ),
        ("access_patterns", "pattern_id", ("source_tables",), ("query_ids",), ()),
        ("unsupported_patterns", None, (), ("query_ids",), ()),
    ),
    "documentdb": (
        (
            "collections",
            "collection_name",
            ("source_tables",),
            (),
            (
                ("embedded_entities", "source_table", ("source_table",), ()),
                ("indexes", "index_name", (), ("source_query_ids",)),
            ),
        ),
        ("access_patterns", "pattern_id", ("source_tables",), ("source_query_ids",), ()),
        ("unsupported_patterns", None, (), ("source_query_ids",), ()),
    ),
    "opensearch": (
        ("index_designs", "index_name", ("source_tables",), (), ()),
        ("data_stream_designs", "data_stream_name", ("source_tables",), (), ()),
        ("access_patterns", "pattern_id", ("source_tables",), ("query_ids",), ()),
        ("unsupported_patterns", None, (), ("query_ids",), ()),
    ),
    "elasticache": (
        ("key_designs", "key_pattern", ("source_tables",), (), ()),
        ("access_patterns", "pattern_id", ("source_tables",), ("source_query_ids",), ()),
        ("unsupported_patterns", None, (), ("source_query_ids",), ()),
    ),
    "aurora_mysql": _AURORA,
    "aurora_postgresql": _AURORA,
}


def normalize_table_name(name: str) -> str:
    """Canonical table key: unquoted, lower-case, without any ``<db>.``/schema prefix.

    ``wordpress.wp_posts``, ```WordPress`.`wp_posts```` and ``wp_posts`` all
    normalise to ``wp_posts``, so designs and assignments match whether or not
    either side spells the prefix.
    """
    cleaned = "".join(c for c in name if c not in '`"[]').strip().lower()
    return cleaned.rsplit(".", 1)[-1].strip()


def _values(item: dict, field: str) -> list[str]:
    """Return the string(s) in ``item[field]`` (a list or a single string)."""
    value = item.get(field)
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _label(item: dict, label_field: str | None, index: int) -> str:
    if label_field:
        value = item.get(label_field)
        if isinstance(value, str) and value:
            return value
    return str(index)


def _dicts(value: Any) -> Iterator[tuple[int, dict]]:
    if isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict):
                yield i, item


def _references(engine: str, schema_output: dict) -> Iterator[tuple[str, str, str]]:
    """Yield ``(kind, value, location)`` for each table/query the design references."""
    for field, label_field, table_fields, query_fields, nested in _ENGINE_FIELDS.get(engine, ()):
        for i, item in _dicts(schema_output.get(field)):
            location = f"{field}[{_label(item, label_field, i)}]"
            for tf in table_fields:
                for value in _values(item, tf):
                    yield "table", value, location
            for qf in query_fields:
                for value in _values(item, qf):
                    yield "query", value, location
            for n_field, n_label, n_tables, n_queries in nested:
                for j, sub in _dicts(item.get(n_field)):
                    sub_location = f"{location}.{n_field}[{_label(sub, n_label, j)}]"
                    for tf in n_tables:
                        for value in _values(sub, tf):
                            yield "table", value, sub_location
                    for qf in n_queries:
                        for value in _values(sub, qf):
                            yield "query", value, sub_location


def _query_owners(assignment: dict) -> dict[str, tuple[str, bool]]:
    """Return ``query_id -> (assigned_engine, in_scope)`` for the whole assignment."""
    owners: dict[str, tuple[str, bool]] = {}
    for qa in assignment.get("query_assignments") or []:
        if isinstance(qa, dict) and qa.get("query_id"):
            owners[str(qa["query_id"]).strip()] = (
                str(qa.get("assigned_engine", "")),
                bool(qa.get("in_scope", True)),
            )
    return owners


def _table_owners(assignment: dict) -> dict[str, set[str]]:
    """Return ``normalised table -> {engine, ...}`` with in-scope queries on it."""
    owners: dict[str, set[str]] = {}
    for qa in assignment.get("query_assignments") or []:
        if not isinstance(qa, dict) or not qa.get("in_scope", True):
            continue
        for table in _values(qa, "source_tables"):
            owners.setdefault(normalize_table_name(table), set()).add(
                str(qa.get("assigned_engine"))
            )
    return owners


def validate_schema_scope(engine: str, schema_output: dict, assignment: dict) -> list[str]:
    """Return one message per source table or query ID ``schema_output`` references
    that ``assignment`` does not put in scope for ``engine``.

    Empty when the design stays in scope (or the engine has no known contract).
    Never raises on a malformed design: unexpected shapes are skipped, since
    contract validation reports those separately.
    """
    if not isinstance(schema_output, dict) or not isinstance(assignment, dict):
        return []

    scope = engine_scope(assignment, engine)
    in_scope_tables = {normalize_table_name(t) for t in scope.source_tables}
    in_scope_queries = {q.strip() for q in scope.query_ids}

    # Group locations per offending table/query so each is reported once.
    bad_tables: dict[str, tuple[str, list[str]]] = {}
    bad_queries: dict[str, list[str]] = {}
    for kind, value, location in _references(engine, schema_output):
        if kind == "table":
            key = normalize_table_name(value)
            if key and key not in in_scope_tables:
                shown, locations = bad_tables.setdefault(key, (value, []))
                if location not in locations:
                    locations.append(location)
        else:
            qid = value.strip()
            if qid not in in_scope_queries:
                locations = bad_queries.setdefault(qid, [])
                if location not in locations:
                    locations.append(location)

    version = assignment.get("version")
    where = f"assignment v{version}" if version else "the assignment"
    table_owners = _table_owners(assignment)
    query_owners = _query_owners(assignment)

    messages: list[str] = []
    for key in sorted(bad_tables):
        shown, locations = bad_tables[key]
        owners = sorted(table_owners.get(key, set()) - {engine})
        reason = (
            f"is assigned to {', '.join(owners)} in {where}"
            if owners
            else f"is not accessed by any in-scope query in {where}"
        )
        messages.append(
            f"Out of scope for {engine}: source table '{shown}' "
            f"(referenced by {', '.join(locations)}) {reason}, not to {engine}. "
            "Remove it from the design."
        )
    for qid in sorted(bad_queries):
        locations = bad_queries[qid]
        owner = query_owners.get(qid)
        if owner is None:
            reason = "is not in the assignment" + (f" (v{version})" if version else "")
        elif owner[0] != engine:
            reason = f"is assigned to {owner[0] or 'no engine'} in {where}, not to {engine}"
        else:
            reason = f"is out of scope in {where}"
        messages.append(
            f"Out of scope for {engine}: query '{qid}' "
            f"(referenced by {', '.join(locations)}) {reason}. Remove it from the design."
        )
    return messages


def apply_scope_violations(schema_output: dict, violations: list[str]) -> dict:
    """Return ``schema_output`` with ``validation_passed=false`` and ``violations``
    appended to ``validation_failures`` (no-op copy when there are none)."""
    result = dict(schema_output)
    if not violations:
        return result
    existing = result.get("validation_failures")
    failures = [f for f in existing if isinstance(f, str)] if isinstance(existing, list) else []
    result["validation_failures"] = failures + [v for v in violations if v not in failures]
    result["validation_passed"] = False
    return result
