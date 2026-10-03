"""Schema design scope validation (issue #203).

A schema design for an engine is produced from the collector input filtered to
the queries the effective assignment puts in scope for that engine (see
``filter_collector_for_assignment``). A model can still drift outside that
scope and design tables or claim queries that belong to another engine. This
module checks a design against the assignment so finalize, ``--merge`` and the
Bedrock path can reject it.

Only the fields that *design* something are violations, per engine contract
(``src/contracts/<engine>_model_output.py``):

- dynamodb: ``table_definitions[].source_tables``,
  ``table_definitions[].entities[].source_table``,
  ``access_patterns[].source_tables`` / ``query_ids``
- documentdb: ``collections[].source_tables``,
  ``collections[].embedded_entities[].source_table``,
  ``collections[].indexes[].source_query_ids``,
  ``access_patterns[].source_tables`` / ``source_query_ids``
- opensearch: ``index_designs[].source_tables``,
  ``data_stream_designs[].source_tables``,
  ``access_patterns[].source_tables`` / ``query_ids``
- elasticache: ``key_designs[].source_tables``,
  ``access_patterns[].source_tables`` / ``source_query_ids``
- aurora_mysql / aurora_postgresql: ``table_definitions[].table_name`` (Aurora
  tables carry over 1:1 from the source, so the name is the source table)

``unsupported_patterns[]`` query IDs are only *warnings*: an unsupported entry
designs nothing, it records that a query cannot be served, so naming an
out-of-scope or unknown query there is noise to report, not a design to reject.

Never checked, because they legitimately name other engines' tables and
queries: ``trade_offs``, ``migration_notes``, ElastiCache
``cache_invalidation[].source_write_query_ids`` (the write runs on the engine
that owns it), DynamoDB attribute-level ``source_table`` (denormalized copies)
and Aurora ``foreign_keys`` / ``indexes`` DDL strings.

Cascade path: ``run_schema_design_with_injected`` (post-schema router) designs
queries that were rerouted to this engine *without* being in its assignment
scope, and merges them into an existing output. That path deliberately does not
run this check: the injected query IDs would all be reported out of scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NamedTuple

from src.storage.assignment_versioning import engine_scope

SCOPE_PREFIX = "Out of scope for "
"""Every violation message starts with this, so a re-check can strip stale ones."""

SCOPE_WARNING_PREFIX = "Scope warning for "

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
    ),
    "opensearch": (
        ("index_designs", "index_name", ("source_tables",), (), ()),
        ("data_stream_designs", "data_stream_name", ("source_tables",), (), ()),
        ("access_patterns", "pattern_id", ("source_tables",), ("query_ids",), ()),
    ),
    "elasticache": (
        ("key_designs", "key_pattern", ("source_tables",), (), ()),
        ("access_patterns", "pattern_id", ("source_tables",), ("source_query_ids",), ()),
    ),
    "aurora_mysql": _AURORA,
    "aurora_postgresql": _AURORA,
}

# Warning-only references: ``unsupported_patterns[]`` query-id field per engine.
_WARNING_FIELDS: dict[str, tuple[_Spec, ...]] = {
    "dynamodb": (("unsupported_patterns", None, (), ("query_ids",), ()),),
    "documentdb": (("unsupported_patterns", None, (), ("source_query_ids",), ()),),
    "opensearch": (("unsupported_patterns", None, (), ("query_ids",), ()),),
    "elasticache": (("unsupported_patterns", None, (), ("source_query_ids",), ()),),
}


class ScopeReport(NamedTuple):
    """Result of a scope check: ``violations`` fail validation, ``warnings`` do not."""

    violations: list[str]
    warnings: list[str]


EMPTY_REPORT = ScopeReport([], [])


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


def _references(specs: tuple[_Spec, ...], schema_output: dict) -> Iterator[tuple[str, str, str]]:
    """Yield ``(kind, value, location)`` for each table/query ``specs`` reach."""
    for field, label_field, table_fields, query_fields, nested in specs:
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


def _query_owners(assignment: dict) -> dict[str, str]:
    """Return ``query_id -> assigned_engine`` for the whole assignment."""
    owners: dict[str, str] = {}
    for qa in assignment.get("query_assignments") or []:
        if isinstance(qa, dict) and qa.get("query_id") is not None:
            owners[str(qa["query_id"]).strip()] = str(qa.get("assigned_engine") or "")
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


def _collect(
    specs: tuple[_Spec, ...],
    schema_output: dict,
    in_scope_tables: set[str],
    in_scope_queries: set[str],
) -> tuple[dict[str, tuple[str, list[str]]], dict[str, list[str]]]:
    """Group offending tables/queries with their locations (each reported once)."""
    bad_tables: dict[str, tuple[str, list[str]]] = {}
    bad_queries: dict[str, list[str]] = {}
    for kind, value, location in _references(specs, schema_output):
        if kind == "table":
            key = normalize_table_name(value)
            if not key or key in in_scope_tables:
                continue
            locations = bad_tables.setdefault(key, (value, []))[1]
        else:
            qid = value.strip()
            if qid in in_scope_queries:
                continue
            locations = bad_queries.setdefault(qid, [])
        if location not in locations:
            locations.append(location)
    return bad_tables, bad_queries


def _query_reason(qid: str, engine: str, owners: dict[str, str], version: Any) -> str:
    where = f"assignment v{version}" if version else "the assignment"
    owner = owners.get(qid)
    if owner is None:
        return "is not in the assignment" + (f" (v{version})" if version else "")
    if owner != engine:
        return f"is assigned to {owner or 'no engine'} in {where}, not to {engine}"
    return f"is out of scope in {where}"


def assess_schema_scope(engine: str, schema_output: dict, assignment: dict) -> ScopeReport:
    """Check ``schema_output`` against the scope ``assignment`` gives ``engine``.

    ``violations``: one message per source table or query ID a designed entity
    references outside the engine's scope. ``warnings``: out-of-scope or
    unknown query IDs that only ``unsupported_patterns`` mention.

    Never raises on a malformed design or assignment: unexpected shapes are
    skipped, since contract validation reports those separately.
    """
    if not isinstance(schema_output, dict) or not isinstance(assignment, dict):
        return EMPTY_REPORT

    scope = engine_scope(assignment, engine)
    in_scope_tables = {normalize_table_name(t) for t in scope.source_tables}
    in_scope_queries = {q.strip() for q in scope.query_ids}
    version = assignment.get("version")
    where = f"assignment v{version}" if version else "the assignment"
    query_owners = _query_owners(assignment)

    bad_tables, bad_queries = _collect(
        _ENGINE_FIELDS.get(engine, ()), schema_output, in_scope_tables, in_scope_queries
    )
    table_owners = _table_owners(assignment)

    violations: list[str] = []
    for key in sorted(bad_tables):
        shown, locations = bad_tables[key]
        owners = sorted(table_owners.get(key, set()) - {engine})
        reason = (
            f"is assigned to {', '.join(owners)} in {where}"
            if owners
            else f"is not accessed by any in-scope query in {where}"
        )
        violations.append(
            f"{SCOPE_PREFIX}{engine}: source table '{shown}' "
            f"(referenced by {', '.join(locations)}) {reason}, not to {engine}. "
            "Remove it from the design."
        )
    for qid in sorted(bad_queries):
        violations.append(
            f"{SCOPE_PREFIX}{engine}: query '{qid}' "
            f"(referenced by {', '.join(bad_queries[qid])}) "
            f"{_query_reason(qid, engine, query_owners, version)}. Remove it from the design."
        )

    _, warn_queries = _collect(
        _WARNING_FIELDS.get(engine, ()), schema_output, in_scope_tables, in_scope_queries
    )
    warnings = [
        f"{SCOPE_WARNING_PREFIX}{engine}: query '{qid}' "
        f"(listed in {', '.join(warn_queries[qid])}) "
        f"{_query_reason(qid, engine, query_owners, version)}."
        for qid in sorted(warn_queries)
    ]
    return ScopeReport(violations, warnings)


def validate_schema_scope(engine: str, schema_output: dict, assignment: dict) -> list[str]:
    """Return the scope violations of ``schema_output`` (see :func:`assess_schema_scope`)."""
    return assess_schema_scope(engine, schema_output, assignment).violations


def apply_scope_violations(schema_output: dict, violations: list[str]) -> dict:
    """Return a copy of ``schema_output`` with its scope verdict replaced by ``violations``.

    Scope messages from an earlier check (they start with :data:`SCOPE_PREFIX`)
    are removed first, so a design fixed by hand and re-finalized does not keep
    stale failures. With new violations: ``validation_passed=false`` and the
    messages appended to ``validation_failures``. Without: if stale scope
    messages were the only failures, ``validation_passed`` is restored to true;
    any other failure keeps whatever ``validation_passed`` the design had.
    """
    result = dict(schema_output)
    existing = result.get("validation_failures")
    failures = [f for f in existing if isinstance(f, str)] if isinstance(existing, list) else []
    kept = [f for f in failures if not f.startswith(SCOPE_PREFIX)]
    stripped = len(kept) != len(failures)

    if violations:
        result["validation_failures"] = kept + [v for v in violations if v not in kept]
        result["validation_passed"] = False
    elif stripped:
        result["validation_failures"] = kept
        if not kept:
            result["validation_passed"] = True
    return result
