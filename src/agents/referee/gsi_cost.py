# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DynamoDB design's global secondary indexes, priced from the design (#805, ADR-032).

The analysis-phase DynamoDB estimate (``estimate_dynamodb_costs``) runs before schema
design and models no GSI. Once a design exists, synthesis prices its GSIs once, from the
design, and ``build_tco_analysis`` adds the total to the DynamoDB row, so the projected
cost, the ranking and every deliverable's cost table agree, with one assumption line
(:func:`tco_assumption`) that states the share and what it counts.

- **Storage**: each GSI's ``item_count`` times its ``item_size_bytes`` (the projected
  item size the design states) plus the engine file's ``index_item_overhead_bytes``
  (DynamoDB stores each index item with 100 bytes of overhead), at the storage price.
- **Writes**: a base-table write (``WRITE_OPERATIONS``) on the GSI's table whose item
  may set the GSI's keys writes the GSI, billed on the GSI's projected item size rounded
  up to whole 1 KB write request units, at the standard rate (a transaction's 2x rate
  applies to its base-table writes, which the estimate prices). The DynamoDB Developer
  Guide's rules ("Provisioned throughput considerations for Global Secondary Indexes",
  GSI.html): a put or a delete is one index write; an update that changes an index key
  attribute is two (the old entry deleted, the new one put); an update that changes
  only attributes the index projects is one; an update that changes neither is none.
  The item may set the keys when the entity the write names (its ``source_tables``)
  defines every key attribute of the GSI and its sparse attribute (a sparse index is
  over-counted: an item that defines the attribute may still leave it unset); a key
  attribute no entity of the table defines (an overloaded key built at write time), or a
  write whose entity is not known, is taken to set it. What an update changes is the
  source query's ``SET`` columns, read with the source dialect
  (``access_facts(...).assigned_columns``) and matched to the attributes by name or
  ``source_column``. An update whose statement does not say (an upsert, SQL the parser
  cannot read, or a ``SET`` list it reads only partly), or an index key no attribute
  definition maps, is counted as changing the index keys: two writes.
- **Rate**: the average load (``design_write_load``, the rule the derived-copy sync
  reads too, #1753): the collected ``calls_per_second`` of the write's queries times the
  rows each call changes, as collected (``rows_affected_avg``, else
  ``rows_returned_avg``, else one). A fraction above zero stays a fraction, so a call
  that usually changes no row is not counted as one. An average of exactly zero is taken
  as missing data, like no row statistics, and counts as one row: a write that changes
  nothing on every call is far likelier a gap in the collection than real zero work, and
  counting it as zero would hide its cost. Only
  for the queries the assignment gives the engine in scope. ``design_rps`` is not used:
  it carries the design's measured peak factor (#1990).

Reads are not added: a read through a GSI is one of its query's reads, which the
estimate already prices. A row sized to no query keeps its figure. With no schema design
(``--llm-mode none``) nothing changes. Routing never reads it: its cost dimension is its
own per-query model.

Prices are the price table's (``src/catalog/prices.toml``); a missing one raises
(``catalog.required_price``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.referee.design_write_load import (
    collected_queries,
    items_per_second,
    pattern_queries,
)
from src.agents.schema_design.dynamodb_merge import WRITE_OPERATIONS
from src.agents.schema_design.scope import normalize_table_name
from src.shared import catalog
from src.shared.catalog.prices import SECONDS_PER_MONTH
from src.shared.engine_names import display_engine
from src.tools.analysis.dynamodb_analysis_tools import STANDARD, write_request_units
from src.tools.sql.facade import access_facts
from src.tools.sql.query_facts import source_engine_of
from src.tools.sql.view_lineage import access_text

_GB = 1024**3


def _number(value: Any) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    return float(value) if ok and math.isfinite(value) else 0.0


def _dicts(value: Any) -> list[dict]:
    return [v for v in value or [] if isinstance(v, dict)] if isinstance(value, list) else []


def _key_names(keys: Any) -> set[str]:
    keys = keys if isinstance(keys, list) else [keys]
    return {
        str(k["attribute_name"]) for k in keys if isinstance(k, dict) and k.get("attribute_name")
    }


def _names(attributes: Any) -> set[str]:
    return {str(a["name"]) for a in _dicts(attributes) if a.get("name")}


def _defined(table: Mapping[str, Any]) -> set[str]:
    """Every attribute the table's attributes or entities define."""
    names = _names(table.get("attributes"))
    for entity in _dicts(table.get("entities")):
        names |= _names(entity.get("attributes"))
    return names


def _written(table: Mapping[str, Any], pattern: Mapping[str, Any]) -> set[str] | None:
    """The attributes the item ``pattern`` writes carries; None when not known."""
    keys = _key_names(table.get("partition_key")) | _key_names(table.get("sort_key"))
    entities = _dicts(table.get("entities"))
    if entities:
        sources = {normalize_table_name(str(s)) for s in pattern.get("source_tables") or [] if s}
        named = [
            e for e in entities if normalize_table_name(str(e.get("source_table") or "")) in sources
        ]
        if not named:
            return None
        return keys.union(*(_names(e.get("attributes")) for e in named))
    attributes = _names(table.get("attributes"))
    return keys | attributes if attributes else None


def _sets_keys(table: Mapping[str, Any], gsi: Mapping[str, Any], pattern: Mapping) -> bool:
    needed = _key_names(gsi.get("partition_key")) | _key_names(gsi.get("sort_key"))
    if gsi.get("sparse_attribute"):
        needed.add(str(gsi["sparse_attribute"]))
    written = _written(table, pattern)
    return written is None or (needed & _defined(table)) <= written


def _sources(attribute: Mapping[str, Any]) -> set[str]:
    """The lower-case names an attribute is known by: its own and its source columns."""
    column = attribute.get("source_column")
    columns = column if isinstance(column, list) else [column]
    names = {str(c).lower() for c in columns if isinstance(c, str) and c}
    return names | {str(attribute["name"]).lower()} if attribute.get("name") else names


def _held(table: Mapping[str, Any], gsi: Mapping[str, Any]) -> set[str] | None:
    """The attribute names ``gsi`` holds, lower-case; None when it holds every one."""
    if gsi.get("projection") not in ("KEYS_ONLY", "INCLUDE"):
        return None
    held = _key_names(gsi.get("partition_key")) | _key_names(gsi.get("sort_key"))
    if gsi.get("projection") == "INCLUDE":
        held |= {str(a) for a in gsi.get("projected_attributes") or [] if a}
    return {h.lower() for h in held}


def _update_writes(
    table: Mapping[str, Any], gsi: Mapping[str, Any], changed: tuple[str, ...] | None
) -> int:
    """The index writes one item of an update that changes the ``changed`` source columns
    takes on ``gsi``: 2 when it changes an index key (or may: ``changed`` None, the
    statement does not say), 1 when it changes only a projected attribute, else 0."""
    if not changed:
        return 2
    attributes = list(_dicts(table.get("attributes")))
    for entity in _dicts(table.get("entities")):
        attributes += _dicts(entity.get("attributes"))
    mapped = {str(a["name"]).lower(): _sources(a) for a in attributes if a.get("name")}
    columns = {c.lower() for c in changed}
    keys = {
        k.lower() for k in _key_names(gsi.get("partition_key")) | _key_names(gsi.get("sort_key"))
    }
    if keys - set(mapped) or any(mapped[k] & columns for k in keys):
        return 2  # a key changes, or one no definition maps may
    held = _held(table, gsi)
    if held is None:
        return 1  # ALL: any changed attribute is projected
    if held - set(mapped):
        return 1  # a projected attribute no definition maps: it may be what changes
    return 1 if any(mapped[name] & columns for name in held) else 0


def gsi_cost_facts(
    schema: Mapping[str, Any] | None,
    collector: Mapping[str, Any] | None,
    query_ids: Iterable[str] | None,
    engine: str,
) -> dict[str, Any] | None:
    """The monthly cost of ``engine``'s design's GSIs; None when the design has none.

    ``query_ids`` is the in-scope queries the engine is sized for (None: every
    collected query, a job with no assignment)."""
    if not isinstance(schema, Mapping):
        return None
    tables = _dicts(schema.get("table_definitions"))
    if not any(_dicts(t.get("gsis")) for t in tables):
        return None
    prices = catalog.pricing(engine)
    storage_price = catalog.required_price(
        engine, "storage_price_per_gb_month", prices.storage_price_per_gb_month
    )
    write_price = catalog.required_price(
        engine, "write_unit_price_per_million", prices.write_unit_price_per_million
    )
    write_bytes = int(catalog.required_price(engine, "write_unit_bytes", prices.write_unit_bytes))
    overhead = int(
        catalog.required_price(
            engine, "index_item_overhead_bytes", prices.index_item_overhead_bytes
        )
    )

    queries = collected_queries(collector, query_ids)
    writes = [
        ap
        for ap in _dicts(schema.get("access_patterns"))
        if ap.get("operation") in WRITE_OPERATIONS and ap.get("in_scope", True) is not False
    ]

    dialect = source_engine_of(collector)
    readings: dict[str, tuple[str, ...] | None] = {}

    def changed(qid: str) -> tuple[str, ...] | None:
        """The columns the query's ``UPDATE ... SET`` assigns, read in the source dialect;
        None when the statement does not say (no SET list, or one read only partly)."""
        if qid not in readings:
            sql = access_text(queries[qid])
            facts = access_facts(sql, dialect) if sql else None
            readings[qid] = (
                facts.assigned_columns
                if facts is not None and facts.assigned_columns and facts.assigned_columns_complete
                else None
            )
        return readings[qid]

    indexes: list[dict[str, Any]] = []
    for table in tables:
        for gsi in _dicts(table.get("gsis")):
            size = _number(gsi.get("item_size_bytes"))
            items = _number(gsi.get("item_count"))
            storage_gb = items * (size + overhead) / _GB
            per_item: dict[str, int] = {}  # query -> index writes per item it changes
            for ap in writes:
                if ap.get("table_name") != table.get("table_name"):
                    continue
                if not _sets_keys(table, gsi, ap):
                    continue
                for q in pattern_queries(ap, queries):
                    n = (
                        _update_writes(table, gsi, changed(q))
                        if ap.get("operation") == "UpdateItem"
                        else 1
                    )
                    if n:
                        per_item[q] = max(per_item.get(q, 0), n)
            # #1753: the rate the derived-copy sync reads too (``design_write_load``).
            units = sum(
                items_per_second(queries[q]) * n * write_request_units(size, STANDARD, write_bytes)
                for q, n in sorted(per_item.items())
            )
            units_per_month = units * SECONDS_PER_MONTH
            indexes.append(
                {
                    "table": str(table.get("table_name") or ""),
                    "gsi": str(gsi.get("gsi_name") or gsi.get("index_name") or ""),
                    "storage_gb": storage_gb,
                    "storage_cost_usd": storage_gb * storage_price,
                    "write_units_per_month": units_per_month,
                    "write_cost_usd": units_per_month / 1_000_000 * write_price,
                    "write_query_ids": sorted(per_item),
                    "writes_per_item": dict(sorted(per_item.items())),
                }
            )
    storage_cost = round(sum(i["storage_cost_usd"] for i in indexes), 2)
    write_cost = round(sum(i["write_cost_usd"] for i in indexes), 2)
    return {
        "engine": engine,
        "gsi_count": len(indexes),
        "storage_gb": round(sum(i["storage_gb"] for i in indexes), 4),
        "storage_cost_usd": storage_cost,
        "write_units_per_month": round(sum(i["write_units_per_month"] for i in indexes), 2),
        "write_cost_usd": write_cost,
        "total_monthly_cost_usd": round(storage_cost + write_cost, 2),
        "indexes": indexes,
    }


def gsi_costs_by_engine(
    designs: Mapping[str, Mapping[str, Any] | None],
    collector: Mapping[str, Any] | None,
    query_ids: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """The GSI cost of each design that has GSIs, by engine id: only an engine whose
    engine file prices index storage (``index_item_overhead_bytes``, DynamoDB's).

    ``query_ids`` is, by engine, the in-scope queries the TCO sizes it for (None: no
    assignment, every collected query)."""
    out: dict[str, dict[str, Any]] = {}
    for engine, schema in designs.items():
        if catalog.pricing(engine).index_item_overhead_bytes is None:
            continue
        qids = None if query_ids is None else list(query_ids.get(engine) or [])
        found = gsi_cost_facts(schema, collector, qids, engine)
        if found:
            out[engine] = found
    return out


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def tco_assumption(cost: Mapping[str, Any]) -> str:
    """The one TCO assumption line that states the GSI share and what it counts (#805),
    with no semicolon, since the reports join assumptions with one."""
    engine = str(cost["engine"])
    n = int(cost["gsi_count"])
    prices = catalog.pricing(engine)
    write_kb = int(catalog.required_price(engine, "write_unit_bytes", prices.write_unit_bytes))
    write_kb //= 1024
    overhead = int(
        catalog.required_price(
            engine, "index_item_overhead_bytes", prices.index_item_overhead_bytes
        )
    )
    indexes = "global secondary index" if n == 1 else "global secondary indexes"
    return (
        f"The {display_engine(engine)} figure includes {_usd(cost['total_monthly_cost_usd'])}/mo "
        f"for the design's {n} {indexes} at on-demand prices: "
        f"{_usd(cost['storage_cost_usd'])} of storage (each index's item count times its "
        f"projected item size plus {overhead} bytes of index overhead per item) and "
        f"{_usd(cost['write_cost_usd'])} of index writes (one for each put or delete whose "
        "item may set the index's keys, two for an update that changes an index key or "
        "does not say what it changes, one for an update that changes only a projected "
        f"attribute, billed at the standard rate per {write_kb} KB of the projected item, "
        "on average load with the collected rows per call, an average of exactly zero "
        "counted as one row). Index reads are not "
        "added, since each is one of its query's reads and already priced, and the free "
        "tier is left out"
    )
