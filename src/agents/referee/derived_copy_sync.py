"""The stream sync of DynamoDB derived copies, listed and priced (#1527, ADR-032).

When the DynamoDB group merge cannot give a source table one home, it names an
authoritative home and keeps the other tables as derived copies, kept in sync from the
home's DynamoDB Streams (#333, ``dynamodb_merge.independent_homes``). The sync is
something to build and to pay for, so synthesis computes it once into
``facts.derived_copy_sync`` and the deliverables print it next to the overlap risk.

One entry per overlap (``dynamodb_merge.merge_overlaps``) that has an open risk:

- ``replayed_writes_per_second``: the design's write access patterns on any home of the
  overlap's source rows (``design_rps``). A write on a copy is pointed at the home, so
  every one of them lands on the home's stream and is replayed to every copy.
- **Stream reads**: the home's stream is read once, however many copies there are. A
  Streams read request unit is one GetRecords call, which returns up to 1 MB
  (``streams_read_unit_bytes`` of the engine file); the replayed bytes a month
  (``item_size_bytes`` of each write) are billed in whole units, as full batches: a
  lower bound, since a consumer that polls more often reads more calls.
- **Copy writes**: each copy applies every replayed item as a standard write, rounded up
  to whole 1 KB write request units one item at a time (the DynamoDB estimate's rule,
  ``write_request_units``, #1491), at the on-demand write price.

Prices are the price table's (``src/catalog/prices.toml``); a missing one raises
(``catalog.required_price``, #1460). The consumer's own compute, the free tier and the
copies' storage and GSIs are not priced.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.schema_design.dynamodb_merge import (
    WRITE_OPERATIONS,
    merge_overlaps,
    overlap_home,
)
from src.agents.schema_design.scope import normalize_table_name
from src.shared import catalog
from src.shared.catalog.prices import SECONDS_PER_MONTH
from src.tools.analysis.dynamodb_analysis_tools import STANDARD, write_request_units

DATA_CONSISTENCY = "DATA_CONSISTENCY"


def _number(value: Any) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    return float(value) if ok and math.isfinite(value) else 0.0


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _count(x: float) -> str:
    """A rate or unit count: thousands separators, at most two places, no trailing zeros."""
    if 0 < x < 0.01:
        return "<0.01"
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _lines(sync: Mapping[str, Any]) -> dict[str, str]:
    """The sync's sentences, written once for every deliverable (ADR-032): the path, the
    cost, and the table-free summary the executive report prints."""
    home = sync["authoritative_home"]
    sources = ", ".join(sync["source_tables"])
    derived = sync["derived"]
    n = len(derived)
    copies = ", ".join(c["table"] for c in derived)
    rate = _count(sync["replayed_writes_per_second"])
    path = (
        f"Sync path: writes to {sources} go to {home}; {copies} "
        f"{_plural(n, 'is a derived copy', 'are derived copies')}, kept in sync from the "
        f"DynamoDB Streams of {home} by a consumer to build, replaying {rate} writes per second."
    )
    writes = "; ".join(
        f"{c['table']} {_usd(c['cost_usd'])} ({_count(c['write_units_per_month'])} write "
        "request units)"
        for c in derived
    )
    cost = (
        f"Sync cost: {_usd(sync['monthly_cost_usd'])}/mo at on-demand prices: "
        f"{_usd(sync['stream_read_cost_usd'])} for {_count(sync['stream_read_units_per_month'])} "
        f"DynamoDB Streams read request units, and {_usd(sync['copy_write_cost_usd'])} for the "
        f"{_plural(n, 'copy', 'copies')}' writes ({writes})."
    )
    summary = (
        f"{n} derived {_plural(n, 'copy is', 'copies are')} kept in sync from DynamoDB "
        f"Streams, replaying {rate} writes per second, for about "
        f"{_usd(sync['monthly_cost_usd'])} a month at on-demand prices."
    )
    return {"path_line": path, "cost_line": cost, "summary_line": summary}


def _linked_risk(overlap: Mapping, risks: Iterable[Mapping]) -> str | None:
    """The id of the open DynamoDB design-conflict risk of ``overlap``'s source tables."""
    sources = {normalize_table_name(s) for s in overlap.get("source_tables") or [] if s}
    for risk in risks:
        if risk.get("risk_type") != DATA_CONSISTENCY:
            continue
        if not str(risk.get("description") or "").startswith(f"[{overlap.get('engine')}]"):
            continue
        affected = {normalize_table_name(str(t)) for t in risk.get("affected_tables") or []}
        if affected == sources and risk.get("risk_id"):
            return str(risk["risk_id"])
    return None


def derived_copy_sync_facts(
    schema: Mapping[str, Any] | None, risks: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """``facts.derived_copy_sync`` for the DynamoDB design ``schema``; None with no sync.

    ``risks`` is the report's open risks: only an overlap that has its risk is listed,
    so every entry has the place the deliverables print it (``risk_id``).
    """
    if not isinstance(schema, Mapping):
        return None
    overlaps = merge_overlaps(schema)
    if not overlaps:
        return None
    risks = list(risks)
    patterns = [ap for ap in schema.get("access_patterns") or [] if isinstance(ap, dict)]

    # The engine the merge's review trade-offs name (``independent_homes``).
    engine = str(overlaps[0].get("engine") or "")
    prices = catalog.pricing(engine)
    write_price = catalog.required_price(
        engine, "write_unit_price_per_million", prices.write_unit_price_per_million
    )
    streams_price = catalog.required_price(
        engine,
        "streams_read_unit_price_per_million",
        prices.streams_read_unit_price_per_million,
    )
    write_bytes = int(catalog.required_price(engine, "write_unit_bytes", prices.write_unit_bytes))
    streams_bytes = int(
        catalog.required_price(engine, "streams_read_unit_bytes", prices.streams_read_unit_bytes)
    )

    syncs: list[dict[str, Any]] = []
    for overlap in overlaps:
        risk_id = _linked_risk(overlap, risks)
        if risk_id is None:
            continue
        homes = [t for t in overlap.get("target_tables") or [] if isinstance(t, str)]
        sources = {normalize_table_name(s) for s in overlap.get("source_tables") or [] if s}
        chosen = overlap_home(overlap, patterns)
        if chosen is None:
            continue
        home, _ = chosen

        writes = 0.0  # replayed writes a second
        write_units = 0.0  # write request units a second one copy bills
        stream_bytes = 0.0  # bytes a second the home's stream carries
        for ap in patterns:
            if ap.get("operation") not in WRITE_OPERATIONS or ap.get("table_name") not in homes:
                continue
            named = {normalize_table_name(s) for s in ap.get("source_tables") or [] if s}
            if named and not named & sources:
                continue
            rps, size = _number(ap.get("design_rps")), _number(ap.get("item_size_bytes"))
            writes += rps
            stream_bytes += rps * size
            write_units += rps * write_request_units(size, STANDARD, write_bytes)

        stream_units = math.ceil(stream_bytes * SECONDS_PER_MONTH / streams_bytes - 1e-9)
        stream_cost = round(stream_units / 1_000_000 * streams_price, 2)
        copies: list[dict[str, Any]] = []
        for table in homes:
            if table == home:
                continue
            units = write_units * SECONDS_PER_MONTH
            copies.append(
                {
                    "table": table,
                    "write_units_per_month": units,
                    "cost_usd": round(units / 1_000_000 * write_price, 2),
                }
            )
        copy_cost = round(sum(c["cost_usd"] for c in copies), 2)
        row = {
            "risk_id": risk_id,
            "source_tables": sorted(str(s) for s in overlap.get("source_tables") or []),
            "authoritative_home": home,
            "derived": copies,
            "replayed_writes_per_second": round(writes, 3),
            "stream_read_units_per_month": stream_units,
            "stream_read_cost_usd": stream_cost,
            "copy_write_cost_usd": copy_cost,
            "monthly_cost_usd": round(stream_cost + copy_cost, 2),
        }
        syncs.append({**row, **_lines(row)})
    if not syncs:
        return None
    write_kb, stream_mb = write_bytes // 1024, streams_bytes // (1024 * 1024)
    return {
        "syncs": syncs,
        "total_monthly_cost_usd": round(sum(s["monthly_cost_usd"] for s in syncs), 2),
        "prices": {
            "streams_read_unit_price_per_million": streams_price,
            "write_unit_price_per_million": write_price,
        },
        "assumptions": [
            "Every write to the source rows is replayed from the authoritative home's "
            "DynamoDB stream to each derived copy, at the design's write rate.",
            f"Stream reads are billed as {stream_mb} MB GetRecords calls (one Streams read "
            "request unit each) over the bytes replayed, as full batches: a lower bound.",
            f"Each copy applies a replayed item as a standard write, rounded up to {write_kb} KB "
            "write request units per item, at the on-demand write price.",
            "The consumer's compute, the free tier and the copies' storage and indexes are "
            "not included.",
        ],
    }
