"""Per-engine schema-design artifact shapes.

Each schema designer names its output after its own target's vocabulary, so no
single field means "a design exists": DynamoDB and both Aurora engines emit
``table_definitions``, DocumentDB ``collections``, ElastiCache ``key_designs``,
OpenSearch ``index_designs`` plus ``data_stream_designs``.

Synthesis used to rescue three of those four inline and had no branch for
ElastiCache, so a completed Redis design read as absent: ``key_designs``
appeared nowhere in synthesis_report.py, ``schema_design_available`` was always
False for the cache, and the cache then dropped out of ``architecture_type`` and
out of ``recommended_architecture.databases`` altogether. One map consulted by
every caller is what stops the next engine from repeating that.

``src/atx_orchestrator/core.py`` keeps its own ``_DESIGN_SHAPE``, pairing the
same field names with customer-facing unit labels. It stays independent on
purpose: that module takes no module-level ``src.*`` imports, so it cannot read
this one at import time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple


class DesignShape(NamedTuple):
    """One list of design records inside a schema-design artifact.

    ``field`` is the artifact key holding the list. ``name_key`` is the record
    key holding the target object's own name — a table, a collection, an index,
    a Redis key pattern. ``aggregate_pattern`` is the label to report for
    records that do not carry an ``aggregate_pattern`` of their own.
    """

    field: str
    name_key: str
    aggregate_pattern: str


# The relational shape, shared by DynamoDB and both Aurora targets, and the
# fallback for an engine with no entry below (a future target's designer is more
# likely to be table-shaped than anything else, and the fallback only has to be
# empty-or-present, not exhaustive).
TABLE_SHAPE = DesignShape("table_definitions", "table_name", "separate")

ENGINE_DESIGN_SHAPES: dict[str, tuple[DesignShape, ...]] = {
    "dynamodb": (TABLE_SHAPE,),
    "aurora_postgresql": (TABLE_SHAPE,),
    "aurora_mysql": (TABLE_SHAPE,),
    "documentdb": (DesignShape("collections", "collection_name", "document_collection"),),
    "elasticache": (DesignShape("key_designs", "key_pattern", "cache_key"),),
    "opensearch": (
        DesignShape("index_designs", "index_name", "search_index"),
        DesignShape("data_stream_designs", "data_stream_name", "data_stream"),
    ),
}

# A cache fronts a primary store rather than replacing one, so it is never a
# source table's migration destination even when it scores highest for that
# table — see build_table_mappings. Membership, not a substring test on the
# engine name: "cache" in "elasticache" happens to hold but says nothing about
# memorydb.
CACHE_ENGINES = frozenset({"elasticache", "memorydb"})


def design_shapes(engine: str) -> tuple[DesignShape, ...]:
    """Shapes *engine*'s designer can emit, falling back to the relational one."""
    return ENGINE_DESIGN_SHAPES.get(engine, (TABLE_SHAPE,))


def design_records(engine: str, schema: Mapping[str, Any]) -> list[tuple[DesignShape, dict]]:
    """Every design record in *schema*, paired with the shape it came from.

    ``TABLE_SHAPE`` is read for every engine, not just the relational ones, so a
    target that writes ``table_definitions`` unexpectedly still counts as
    designed. The contracts give each engine exactly one design field, so in
    practice only one shape ever contributes.
    """
    shapes = design_shapes(engine)
    if TABLE_SHAPE not in shapes:
        shapes = (TABLE_SHAPE, *shapes)

    records: list[tuple[DesignShape, dict]] = []
    for shape in shapes:
        for record in schema.get(shape.field) or []:
            if isinstance(record, dict):
                records.append((shape, record))
    return records


def design_count(engine: str, schema: Mapping[str, Any]) -> int:
    """How many target objects *engine*'s design defines (0 when none exist)."""
    return len(design_records(engine, schema))


def design_table_defs(engine: str, schema: Mapping[str, Any]) -> list[dict]:
    """Design records normalised to the common table-mapping vocabulary.

    Every record becomes ``{table_name, source_tables, aggregate_pattern}``
    regardless of what the engine called its target objects, so callers mapping
    source tables to targets need no per-engine branches.
    """
    table_defs = []
    for shape, record in design_records(engine, schema):
        source_tables = [t for t in (record.get("source_tables") or []) if isinstance(t, str)]
        table_defs.append(
            {
                "table_name": record.get(shape.name_key, ""),
                "source_tables": source_tables,
                "aggregate_pattern": record.get("aggregate_pattern") or shape.aggregate_pattern,
            }
        )
    return table_defs


def is_cache_engine(engine: str) -> bool:
    """Whether *engine* is a cache fronting a primary store."""
    name = engine.strip().lower()
    return name in CACHE_ENGINES or "cache" in name
