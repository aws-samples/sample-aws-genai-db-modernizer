# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What an Aurora design changes from the source, table by table (#2289).

Synthesis compares each table definition of a saved Aurora design with the table the
collector read, and stores the rows on the design's summary; the facts layer
(``src/shared/design_changes.py``) states each table's kinds of change and the counts.
Nothing here is recorded by the merge: it is a comparison, run again whenever synthesis
runs. Its reference is the deterministic draft the Aurora DDL generator writes for the
same collection (``generate_pg_ddl`` / ``generate_mysql_ddl``): what the source becomes on
the target when nothing is decided on top of it.

- Columns. In a carry-over design a type that is the source's own (as the target spells
  it, ``resolve_source_type``) is ``same``, any other ``type_changed``. In a heterogeneous
  design (``migration_strategy`` ``translate``) a type that is the draft's translation of
  the source type is ``translated``; any other type, a model's override included, is
  ``type_changed``. A design column the source does not have is ``added``; a source column
  the design does not create is ``skipped``.
- Indexes. An index of the source's name is ``same`` when it is the statement the draft
  writes for that source index and the draft noted nothing about it: so its key columns,
  uniqueness, access method, partial-index predicate, INCLUDE columns and Aurora MySQL key
  prefixes are the source's. Any difference is ``changed``, with what differs. Where the
  collection does not give each key's position (``key_order_known``, a collection written
  before #1927), the key columns are compared as a set, so a reordering is never claimed.
  One the design creates under the name an ``index_renamed`` note gives it is ``renamed``.
  Any other design index is ``added``. A source index the design does not create (its
  primary key aside) is ``step1`` when it is an expression index step 1 recreates when it
  moves the table (``recreated_expression_index``, #2381), else ``skipped`` with the
  reason the draft or the design's notes give, when one does.
- Each column carries ``nullable``, ``default`` and ``identity`` as the stored CREATE TABLE
  writes them (``saved_design.column_clauses``, the loader's own checks), or None for a
  design whose script those checks refuse.

A table with no source table is ``added``. One that cannot be compared is stored with its
reason: two collected tables could be its source and the design names no schema
(``ambiguous_source``), the definition has no name (``no_name``), or, in a heterogeneous
design, the deterministic draft of its source table cannot be built (``no_draft``), so no
type can be told translated from changed.

"Recreated by step 1" (``step1``) is the check step 1 runs (``dms_step._expression_indexes``)
over the types step 1 declares; step 1 runs it only for a table it moves, which synthesis
does not know, so the deliverables say it holds when step 1 moves the table.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from src.contracts.aurora_design_delta import INDEX_METHODS
from src.shared.table_keys import normalize_table_name, table_schema
from src.tools.schema.aurora_common.saved_design import column_clauses, index_shape
from src.tools.schema.aurora_common.source_family import (
    classify_type_family,
    migration_strategy,
)
from src.tools.schema.aurora_common.source_types import resolve_source_type

SAME = "same"
TYPE_CHANGED = "type_changed"
TRANSLATED = "translated"
ADDED = "added"
SKIPPED = "skipped"
RENAMED = "renamed"
CHANGED = "changed"
STEP1 = "step1"

AMBIGUOUS_SOURCE = "ambiguous_source"
NO_NAME = "no_name"
NO_DRAFT = "no_draft"

#: The parts of an index compared, in the order a difference is listed.
INDEX_PARTS = ("columns", "unique", "method", "where", "include", "prefixes")


def _norm(text: object) -> str:
    """A type as compared: lower case, one space between words, none after a comma."""
    return " ".join(str(text or "").lower().split()).replace(", ", ",")


def _cleaned(name: object) -> str:
    return "".join(c for c in str(name or "") if c not in '`"[]').strip().lower()


@dataclass
class _DraftTable:
    """The generator's draft of one source table: its column types, its index statements
    in parts by name, the notes it wrote for indexes it did not carry as written, and the
    statements it would recreate an expression index with in step 1."""

    types: dict[str, str] = field(default_factory=dict)
    indexes: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    step1: set[str] = field(default_factory=set)


def _draft(
    engine: str,
    collector: Mapping[str, Any],
    source_engine: str,
    qualified: bool,
    design_types: Mapping[str, Mapping[str, str]],
) -> dict[str, _DraftTable] | None:
    """The deterministic draft of the whole collection, by design table name (lower case);
    None when the collection does not load as a collector output. An expression index counts
    as one step 1 recreates when ``recreated_expression_index`` accepts it over the column
    types step 1 declares: the saved design's (``design_types``, by table and column, lower
    case), and the draft's for a column the design does not list (``step1.target_tables``)."""
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.schema_design_input import project_schema_design_tables
    from src.shared.contract_versioning import ContractVersionError, load_contract
    from src.tools.schema.aurora_common.ddl_generator import (
        design_table_name,
        expression_indexes,
        generate_mysql_ddl,
        generate_pg_ddl,
        recreated_expression_index,
    )

    try:
        loaded = load_contract(CollectorOutputContract, collector, artifact="collector/output.json")
        tables = project_schema_design_tables(loaded)
    except (ValidationError, ContractVersionError, ValueError, TypeError, KeyError):
        return None
    generate = generate_pg_ddl if engine == "aurora_postgresql" else generate_mysql_ddl
    result = generate(tables, source_engine=source_engine, qualified=qualified)
    by_name = {t.table_name.lower(): t for t in result.tables}
    same_engine = engine == "aurora_postgresql" and source_engine == "postgresql"
    out: dict[str, _DraftTable] = {}
    for source in tables:
        name = design_table_name(source, qualified)
        ddl = by_name.get(name.lower())
        draft = _DraftTable()
        if ddl is not None:
            draft.types = {c.name.lower(): c.aurora_type for c in ddl.columns}
            for statement in ddl.index_sql:
                shape = index_shape(engine, statement)
                if shape is not None:
                    draft.indexes[shape["name"]] = shape
            if same_engine:
                schema = source.schema_name if qualified else None
                types = design_types.get(name.lower(), {})
                columns = [
                    dataclasses.replace(c, aurora_type=types.get(c.name.lower(), c.aurora_type))
                    for c in ddl.columns
                ]
                for idx in expression_indexes(source):
                    recreated, _ = recreated_expression_index(
                        source.table_name, idx, columns, schema
                    )
                    if recreated is not None:
                        draft.step1.add(idx.index_name)
        out[name.lower()] = draft
    for note in result.index_notes:
        table = out.get(str(note.get("table") or "").lower())
        if table is not None and note.get("index"):
            table.notes.setdefault(str(note["index"]), str(note.get("reason") or ""))
    return out


def _source_table(
    design: Mapping[str, Any], tables: list[Mapping[str, Any]], source_db: str
) -> Mapping[str, Any] | None | bool:
    """The collected table ``design`` comes from; None when there is none; False when two
    collected tables could be it and nothing tells them apart."""
    name = normalize_table_name(str(design.get("table_name") or ""))
    found = [t for t in tables if normalize_table_name(str(t.get("table_name") or "")) == name]
    schema = table_schema(design)
    if schema is not None:
        found = [t for t in found if _cleaned(t.get("schema_name")) == _cleaned(schema)]
    elif len(found) > 1 and source_db:
        # The source database's own table, when it is one of them.
        own = [t for t in found if _cleaned(t.get("schema_name")) == _cleaned(source_db)]
        found = own or found
    if len(found) > 1:
        return False
    return found[0] if found else None


def _type_change(
    column: Mapping[str, Any],
    aurora_type: str,
    family: str,
    target: str,
    translate: bool,
    drafted: str | None,
) -> str:
    raw = column.get("data_type")
    if _norm(raw) == _norm(aurora_type):
        return SAME
    if translate:
        return (
            TRANSLATED
            if drafted is not None and _norm(drafted) == _norm(aurora_type)
            else (TYPE_CHANGED)
        )
    resolved = resolve_source_type(
        str(raw or ""),
        source_family=family,
        target=target,
        max_length=column.get("max_length"),
        numeric_precision=column.get("numeric_precision"),
        numeric_scale=column.get("numeric_scale"),
        datetime_precision=column.get("datetime_precision"),
    )
    if resolved is not None and _norm(resolved.aurora_type) == _norm(aurora_type):
        return SAME
    return TYPE_CHANGED


def _notes_by_object(notes: object, label: str) -> dict[str, str]:
    """``object`` -> the text of the design's first note on ``label.object``."""
    out: dict[str, str] = {}
    for n in notes if isinstance(notes, list) else []:
        if not isinstance(n, Mapping):
            continue
        obj = str(n.get("source_object") or "")
        if obj.startswith(label + "."):
            out.setdefault(obj[len(label) + 1 :], str(n.get("recommendation") or "").strip())
    return out


def _renamed_by_notes(notes: object, label: str) -> dict[str, str]:
    """Collected index name -> the name the design creates it as, from the design's
    ``index_renamed`` notes for table ``label`` (``Created as <new>: ...``, #2410)."""
    out: dict[str, str] = {}
    for n in notes if isinstance(notes, list) else []:
        if not isinstance(n, Mapping) or n.get("feature") != "index_renamed":
            continue
        obj, text = str(n.get("source_object") or ""), str(n.get("recommendation") or "")
        if obj.startswith(label + ".") and text.startswith("Created as ") and ":" in text:
            out[obj[len(label) + 1 :]] = text[len("Created as ") :].split(":", 1)[0]
    return out


def _is_primary(index: Mapping[str, Any], primary_key: list[str]) -> bool:
    if index.get("is_primary"):
        return True
    cols = [_cleaned(c) for c in index.get("columns") or []]
    return bool(primary_key) and bool(index.get("is_unique")) and cols == primary_key


def _columns(
    design: Mapping[str, Any],
    source: Mapping[str, Any],
    clauses: list[dict[str, Any]] | None,
    family: str,
    target: str,
    translate: bool,
    draft: _DraftTable | None,
) -> list[dict[str, Any]]:
    collected = {
        _cleaned(c.get("column_name")): c
        for c in source.get("columns") or []
        if isinstance(c, Mapping)
    }
    rows: list[dict[str, Any]] = []
    kept: set[str] = set()
    for i, column in enumerate(design.get("columns") or []):
        if not isinstance(column, Mapping):
            continue
        name, aurora_type = str(column.get("name") or ""), str(column.get("aurora_type") or "")
        src = collected.get(_cleaned(name))
        if src is not None:
            kept.add(_cleaned(name))
        clause = clauses[i] if clauses is not None and i < len(clauses) else None
        drafted = draft.types.get(name.lower()) if draft is not None else None
        rows.append(
            {
                "name": name,
                "aurora_type": aurora_type,
                "source_type": str(src["data_type"]) if src and src.get("data_type") else None,
                "nullable": clause["nullable"] if clause else None,
                "default": clause["default"] if clause else None,
                "identity": clause["identity"] if clause else None,
                "change": (
                    ADDED
                    if src is None
                    else _type_change(src, aurora_type, family, target, translate, drafted)
                ),
            }
        )
    for key, src in collected.items():
        if key not in kept:
            rows.append(
                {
                    "name": str(src.get("column_name") or ""),
                    "aurora_type": None,
                    "source_type": str(src["data_type"]) if src.get("data_type") else None,
                    "nullable": None,
                    "default": None,
                    "identity": None,
                    "change": SKIPPED,
                }
            )
    return rows


def _index_row(
    shape: Mapping[str, Any],
    change: str,
    *,
    source_name: str | None = None,
    differences: list[str] | None = None,
    source: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "name": shape["name"],
        "columns": list(shape["columns"]),
        "unique": bool(shape["unique"]),
        "change": change,
        "source_name": source_name,
        # What differs from the source, in INDEX_PARTS order, and the source's own values.
        "differences": differences or [],
        "source_columns": list(source["columns"]) if source and differences else None,
        "source_unique": bool(source["unique"]) if source and differences else None,
        "reason": reason or None,
    }


def _collected_shape(index: Mapping[str, Any], engine: str = "") -> dict[str, Any]:
    """A collected index in the parts ``index_shape`` reads, for one the draft does not
    write: its name, key columns, uniqueness and access method (``btree`` for one the
    target has no spelling of), and on Aurora MySQL its key prefixes. The predicate is
    compared only through the draft, which renders it."""
    columns = [str(c) for c in index.get("columns") or []]
    shape: dict[str, Any] = {
        "name": str(index["index_name"]),
        "columns": columns,
        "unique": bool(index.get("is_unique")),
    }
    method = str(index.get("index_type") or "btree").lower()
    if engine == "aurora_mysql":
        prefixes = index.get("prefix_lengths")
        shape["prefixes"] = list(prefixes) if prefixes else [None] * len(columns)
        shape["method"] = method if method in ("fulltext", "spatial") else "btree"
    else:
        shape["method"] = method if method in INDEX_METHODS else "btree"
        shape["include"] = [str(c) for c in index.get("included_columns") or []]
    return shape


def _differences(
    design: Mapping[str, Any], reference: Mapping[str, Any], key_order_known: bool
) -> list[str]:
    out = []
    for part in INDEX_PARTS:
        if part not in reference:
            continue
        a, b = design.get(part), reference.get(part)
        if part == "columns":
            a, b = [_cleaned(c) for c in a or []], [_cleaned(c) for c in b or []]
            if not key_order_known:
                a, b = sorted(a), sorted(b)
        elif part == "include":
            a, b = sorted(_cleaned(c) for c in a or []), sorted(_cleaned(c) for c in b or [])
        if a != b:
            out.append(part)
    return out


def _indexes(
    engine: str,
    design: Mapping[str, Any],
    source: Mapping[str, Any],
    notes: object,
    label: str,
    draft: _DraftTable | None,
) -> list[dict[str, Any]]:
    primary_key = [_cleaned(c) for c in source.get("primary_key") or []]
    collected = [
        i
        for i in source.get("indexes") or []
        if isinstance(i, Mapping) and i.get("index_name") and not _is_primary(i, primary_key)
    ]
    renamed = _renamed_by_notes(notes, label)
    noted = _notes_by_object(notes, label)
    free = {str(i["index_name"]): i for i in collected}
    rows: list[dict[str, Any]] = []

    def compare(shape: Mapping[str, Any], src: Mapping[str, Any]) -> tuple[list[str], dict]:
        name = str(src["index_name"])
        collected_shape = _collected_shape(src, engine)
        known = src.get("key_order_known") is True
        drafted = draft.indexes.get(name) if draft is not None else None
        if drafted is None:
            # The draft does not write it: only what the collector states is compared.
            return _differences(shape, collected_shape, known), collected_shape
        if draft is not None and name in draft.notes:
            # The draft writes it with a note (a key prefix it adds, #1623, or a warning):
            # the parts the collector states are the source's, the rest the draft's.
            return _differences(shape, {**drafted, **collected_shape}, known), collected_shape
        # The draft writes the source index as collected: the reference for every part.
        return _differences(shape, drafted, known), collected_shape

    for statement in design.get("indexes") or []:
        shape = index_shape(engine, statement)
        if shape is None:
            continue
        name = shape["name"]
        old = next((o for o, new in renamed.items() if new == name and o in free), None)
        if name in free:
            diffs, collected_shape = compare(shape, free.pop(name))
            rows.append(
                _index_row(
                    shape,
                    CHANGED if diffs else SAME,
                    differences=diffs,
                    source=collected_shape,
                    reason=(draft.notes.get(name) if draft is not None and diffs else None)
                    or (noted.get(name) if diffs else None),
                )
            )
        elif old is not None:
            diffs, collected_shape = compare(shape, free.pop(old))
            rows.append(
                _index_row(
                    shape, RENAMED, source_name=old, differences=diffs, source=collected_shape
                )
            )
        else:
            rows.append(_index_row(shape, ADDED, reason=noted.get(name)))
    for name, src in free.items():
        shape = _collected_shape(src, engine)
        if draft is not None and name in draft.step1:
            rows.append(_index_row(shape, STEP1))
            continue
        reason = (draft.notes.get(name) if draft is not None else None) or noted.get(name)
        rows.append(_index_row(shape, SKIPPED, reason=reason))
    return rows


def design_source_changes(
    engine: str, schema: Mapping[str, Any], collector: Mapping[str, Any] | None
) -> list[dict[str, Any]] | None:
    """Per table definition of ``schema`` (a saved ``engine`` design), what it changes from
    the collected table: ``source_table`` (the collected table's id, None for a table the
    design adds) with its ``columns`` and ``indexes`` rows, or ``uncompared`` with the
    reason it is not compared. None when the collector read no tables, so nothing can be."""
    tables = ((collector or {}).get("database_schema") or {}).get("tables") or []
    tables = [t for t in tables if isinstance(t, Mapping)]
    if not tables or collector is None:
        return None
    source = (collector.get("metadata") or {}).get("source_database") or {}
    source_engine = str(source.get("engine") or "")
    source_db = str(source.get("database_name") or schema.get("source_database") or "")
    strategy = schema.get("migration_strategy")
    if strategy not in ("carry_over", "translate"):
        strategy = migration_strategy(source_engine, engine)  # type: ignore[arg-type]
    translate = strategy == "translate"
    family = classify_type_family(source_engine)
    clauses = column_clauses(schema, engine)
    notes = schema.get("app_layer_notes")
    definitions = schema.get("table_definitions") or []
    qualified = any(isinstance(d, Mapping) and table_schema(d) for d in definitions)
    design_types = {
        (
            f"{table_schema(d)}.{d['table_name']}" if table_schema(d) else str(d["table_name"])
        ).lower(): {
            str(c.get("name") or "").lower(): str(c.get("aurora_type") or "")
            for c in d.get("columns") or []
            if isinstance(c, Mapping) and c.get("aurora_type")
        }
        for d in definitions
        if isinstance(d, Mapping) and str(d.get("table_name") or "").strip()
    }
    drafts = _draft(engine, collector, source_engine, qualified, design_types)
    out: list[dict[str, Any]] = []
    for i, design in enumerate(definitions):
        if not isinstance(design, Mapping) or not str(design.get("table_name") or "").strip():
            out.append({"uncompared": NO_NAME})
            continue
        found = _source_table(design, tables, source_db)
        if found is False:
            out.append({"uncompared": AMBIGUOUS_SOURCE})
            continue
        table_clauses = clauses[i] if clauses is not None and i < len(clauses) else None
        schema_name = table_schema(design)
        label = f"{schema_name}.{design['table_name']}" if schema_name else design["table_name"]
        draft = drafts.get(str(label).lower()) if drafts is not None else None
        src: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
        if translate and src and draft is None:
            # A translated type is the draft's translation, or a change: with no draft to
            # check against, the table is not compared rather than every type a change.
            out.append({"uncompared": NO_DRAFT})
            continue
        out.append(
            {
                "source_table": (
                    str(
                        src.get("table_id")
                        or ".".join(p for p in (src.get("schema_name"), src["table_name"]) if p)
                    )
                    if src
                    else None
                ),
                "columns": _columns(
                    design, src, table_clauses, family, engine, translate, draft if src else None
                ),
                "indexes": _indexes(engine, design, src, notes, str(label), draft if src else None),
            }
        )
    return out
