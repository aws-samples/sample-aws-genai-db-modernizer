"""DynamoDB hot-partition / capacity check, shared by Bedrock and external mode.

``src/skills/dynamodb-data-modeling.md`` only lets a design report
``validation_passed: true`` once this check has run successfully. It has two
callers that must agree exactly:

- Bedrock mode: the Strands tool ``compute_performances_and_costs`` in
  ``dynamodb_schema_agent.py`` is a thin wrapper around
  :func:`compute_performances_and_costs_entries`.
- External (Claude Code) mode: ``scripts/run_schema_design.py --check-costs
  <draft>`` calls :func:`check_draft_costs` on a group draft, which runs the
  same function per entry so it can report every failing entry at once.

Both also *compute* the arithmetic the skill no longer asks the model to do
(#626): ``partition_limit`` (3000 for reads, 1000 for writes),
``utilization_pct = min(100, rcu_or_wcu_per_second / partition_limit × 100)``
to one decimal, and ``at_risk = utilization_pct > 80``. The model supplies an
entry's table, operation, ``rcu_or_wcu_per_second``, contributing patterns
and mitigation; the returned ``results`` carry the computed three, which the
design copies. ``check_draft_costs`` reports a draft whose own values disagree
(or are missing), and also compares each access pattern's ``design_rps`` with
the group's ``design_metrics`` when the caller passes them.

A GSI read returns only what the index projects, so a read that returns an
attribute the projection leaves out fetches each item from the base table:
``avg_items_returned`` extra item reads per call (#1503). The designer lists what
each read returns in ``returned_attributes`` (#1509), and only a read the
projection does not cover is charged: ``KEYS_ONLY`` with any non-key attribute,
``INCLUDE`` missing one, never ``ALL``. A read without the field (designs before
#1509) keeps the #1503 rule: every ``KEYS_ONLY`` read is charged, ``INCLUDE`` is
not, and the result names those reads. :func:`keys_only_fetch_load` prices the
fetch (``design_rps × avg_items_returned`` reads per second, each item rounded
up to 4 KB and billed 0.5 read unit per 4 KB eventually consistent, 1 strongly
consistent), and :func:`keys_only_fetch_errors` rejects a design whose
base-table read entry is below that load, or whose ``returned_attributes`` name
an attribute its table does not define.

This module deliberately has no Strands/Bedrock dependency.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from pydantic import ValidationError

from src.contracts.design_metrics import DynamoDBPartitionLimits
from src.contracts.dynamodb_model_output import HotPartitionEntry
from src.tools.schema.design_metrics import (
    RPS_ABS_TOLERANCE,
    check_design_numbers,
    check_hot_partition_entry,
    expected_utilization_pct,
)

logger = logging.getLogger(__name__)

_PER_TABLE_FIELDS = (
    "table_name",
    "gsi_name",
    "operation",
    "rcu_or_wcu_per_second",
    "partition_limit",
    "utilization_pct",
    "at_risk",
)


#: Read unit size: an item is billed per 4 KB, rounded up.
READ_UNIT_BYTES = 4096
_READ_OPERATIONS = frozenset({"Query", "GetItem", "BatchGetItem", "TransactGetItems"})


def _key_names(keys: Any) -> set[str]:
    """Attribute names of a ``KeyDefinition`` or a list of them."""
    keys = keys if isinstance(keys, list) else [keys]
    return {
        str(k["attribute_name"]) for k in keys if isinstance(k, dict) and k.get("attribute_name")
    }


def _table_keys(table: dict) -> set[str]:
    return _key_names(table.get("partition_key")) | _key_names(table.get("sort_key"))


def _defined_attributes(table: dict) -> set[str] | None:
    """Every attribute name the table defines (keys, attributes, entity attributes,
    GSI keys); ``None`` when it lists no attributes, so nothing can be checked."""
    attrs: list[Any] = list(table.get("attributes") or [])
    for entity in table.get("entities") or []:
        if isinstance(entity, dict):
            attrs.extend(entity.get("attributes") or [])
    if not attrs:
        return None
    names = {str(a["name"]) for a in attrs if isinstance(a, dict) and a.get("name")}
    names |= _table_keys(table)
    for gsi in table.get("gsis") or []:
        if isinstance(gsi, dict):
            names |= _key_names(gsi.get("partition_key")) | _key_names(gsi.get("sort_key"))
    return names


def _uncovered(returned: list[Any], table: dict, gsi: dict) -> list[str]:
    """The returned attributes ``gsi``'s projection leaves out, sorted.

    Every projection carries the table key and the index key; ``INCLUDE`` adds its
    ``projected_attributes``; ``ALL`` carries everything.
    """
    projection = gsi.get("projection")
    if projection == "ALL":
        return []
    covered = _table_keys(table) | _key_names(gsi.get("partition_key"))
    covered |= _key_names(gsi.get("sort_key"))
    if projection == "INCLUDE":
        covered |= {str(a) for a in gsi.get("projected_attributes") or []}
    return sorted({str(a) for a in returned} - covered)


def keys_only_fetch_load(access_patterns: Any, table_definitions: Any) -> list[dict]:
    """The base-table fetch load of in-scope GSI reads the projection does not cover.

    A read fetches each returned item from the base table when its GSI does not
    project an attribute in its ``returned_attributes`` (#1509): ``KEYS_ONLY`` with any
    non-key attribute, ``INCLUDE`` missing one, never ``ALL``. A read with no
    ``returned_attributes`` (designs before #1509) keeps the #1503 rule: charged on a
    ``KEYS_ONLY`` index, not on ``INCLUDE``; its id goes in
    ``assumed_full_item_pattern_ids`` so the result says so.

    One row per ``(table_name, gsi_name)``: its ``projection``, the charged reads'
    ``pattern_ids``, ``assumed_full_item_pattern_ids``, ``unprojected_attributes`` (the
    returned attributes the index leaves out), ``extra_reads_per_second``
    (``Σ design_rps × avg_items_returned``, a null count being one item per call) and
    ``extra_read_units_per_second`` (each read costs ``ceil(table item_size_bytes /
    4096)`` units, times 0.5 eventually consistent or 1 when the pattern is
    ``strongly_consistent``). The name stays from #1503, when only ``KEYS_ONLY`` was charged.
    """
    if not isinstance(access_patterns, list) or not isinstance(table_definitions, list):
        return []
    tables = {t.get("table_name"): t for t in table_definitions if isinstance(t, dict)}
    rows: dict[tuple[str, str], dict] = {}
    for ap in access_patterns:
        if not isinstance(ap, dict) or not ap.get("gsi_name") or ap.get("in_scope") is False:
            continue
        if ap.get("operation") not in _READ_OPERATIONS:
            continue
        table = tables.get(ap.get("table_name"))
        gsi = next(
            (
                g
                for g in (table or {}).get("gsis") or []
                if isinstance(g, dict) and g.get("gsi_name") == ap["gsi_name"]
            ),
            None,
        )
        if gsi is None or table is None:
            continue
        returned = ap.get("returned_attributes")
        assumed = not isinstance(returned, list)
        missing = _uncovered(returned, table, gsi) if isinstance(returned, list) else []
        if (assumed and gsi.get("projection") != "KEYS_ONLY") or (not assumed and not missing):
            continue
        try:
            rps = float(ap.get("design_rps") or 0)
            items = ap.get("avg_items_returned")
            items = 1.0 if items is None else float(items)
            item_bytes = int(table.get("item_size_bytes") or 1)
        except (TypeError, ValueError):
            continue
        per_item = max(1, math.ceil(item_bytes / READ_UNIT_BYTES)) * (
            1.0 if ap.get("strongly_consistent") else 0.5
        )
        row = rows.setdefault(
            (str(ap["table_name"]), str(ap["gsi_name"])),
            {
                "table_name": str(ap["table_name"]),
                "gsi_name": str(ap["gsi_name"]),
                "projection": gsi.get("projection"),
                "pattern_ids": [],
                "assumed_full_item_pattern_ids": [],
                "unprojected_attributes": [],
                "extra_reads_per_second": 0.0,
                "extra_read_units_per_second": 0.0,
            },
        )
        row["pattern_ids"].append(str(ap.get("pattern_id")))
        if assumed:
            row["assumed_full_item_pattern_ids"].append(str(ap.get("pattern_id")))
        row["unprojected_attributes"] = sorted(set(row["unprojected_attributes"]) | set(missing))
        row["extra_reads_per_second"] += rps * items
        row["extra_read_units_per_second"] += rps * items * per_item
    for row in rows.values():
        row["extra_reads_per_second"] = round(row["extra_reads_per_second"], 4)
        row["extra_read_units_per_second"] = round(row["extra_read_units_per_second"], 4)
    return list(rows.values())


#: At most this many attribute names are quoted per message (design text is data).
_MAX_NAMES_SHOWN = 5


def _names(names: list[str]) -> str:
    shown = ", ".join(repr(n[:64]) for n in names[:_MAX_NAMES_SHOWN])
    more = len(names) - _MAX_NAMES_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def returned_attributes_errors(access_patterns: Any, table_definitions: Any) -> list[str]:
    """One message per in-scope read whose ``returned_attributes`` names an attribute
    its table does not define (keys, attributes, entity attributes or GSI keys)."""
    if not isinstance(access_patterns, list) or not isinstance(table_definitions, list):
        return []
    tables = {t.get("table_name"): t for t in table_definitions if isinstance(t, dict)}
    errors = []
    for ap in access_patterns:
        if not isinstance(ap, dict) or ap.get("in_scope") is False:
            continue
        returned = ap.get("returned_attributes")
        if ap.get("operation") not in _READ_OPERATIONS or not isinstance(returned, list):
            continue
        table = tables.get(ap.get("table_name"))
        defined = _defined_attributes(table) if isinstance(table, dict) else None
        if defined is None:
            continue
        unknown = sorted({str(a) for a in returned} - defined)
        if unknown:
            errors.append(
                f"access_patterns {str(ap.get('pattern_id'))[:64]!r}: returned_attributes "
                f"names {_names(unknown)}, not an attribute of table "
                f"{str(ap.get('table_name'))[:64]!r}; list only attributes the design defines"
            )
    return errors


def keys_only_fetch_errors(
    entries: list[Any], access_patterns: Any, table_definitions: Any
) -> list[str]:
    """One message per table whose base-table read entry leaves the fetch out, then
    one per read whose ``returned_attributes`` the design does not define.

    The fetch lands on the base table, so a base-table (no ``gsi_name``) read
    entry must carry at least its units; the entry may carry more (other reads).
    """
    totals: dict[str, float] = {}
    why: dict[str, list[str]] = {}
    for row in keys_only_fetch_load(access_patterns, table_definitions):
        table = row["table_name"]
        totals[table] = totals.get(table, 0.0) + row["extra_read_units_per_second"]
        reason = (
            f"{row['projection']} GSI {row['gsi_name']!r} does not project what "
            f"{', '.join(row['pattern_ids'])} return"
        )
        if row["unprojected_attributes"]:
            reason += f" (it leaves out {_names(row['unprojected_attributes'])})"
        if row["assumed_full_item_pattern_ids"]:
            reason += (
                f" ({', '.join(row['assumed_full_item_pattern_ids'])} carry no "
                "returned_attributes, so the check takes them to read the full item)"
            )
        why.setdefault(table, []).append(reason)
    errors = []
    for table, needed in totals.items():
        base = [
            float(e.get("rcu_or_wcu_per_second") or 0)
            for e in entries
            if isinstance(e, dict)
            and e.get("table_name") == table
            and e.get("operation") == "read"
            and not e.get("gsi_name")
        ]
        have = max(base, default=None)
        slack = RPS_ABS_TOLERANCE
        if have is None or have + slack < needed:
            shown = "no base-table read entry" if have is None else f"{have:g}"
            errors.append(
                f"hot_partition_analysis {table}: {'; '.join(why[table])}, so each read "
                f"fetches every returned item from the base table, adding {needed:g} read "
                f"units/s (design_rps × avg_items_returned item reads, each item rounded up "
                f"to 4 KB, 0.5 unit per 4 KB eventually consistent); the base-table read "
                f"entry has {shown}, so it must include them"
            )
    errors.extend(returned_attributes_errors(access_patterns, table_definitions))
    return errors


def computed_entry(raw: Any) -> dict:
    """``raw`` with ``partition_limit``, ``utilization_pct`` and ``at_risk`` computed.

    Raises ``pydantic.ValidationError`` when the result is not a valid
    ``HotPartitionEntry`` (e.g. ``at_risk`` without a ``mitigation``).
    """
    entry = dict(raw) if isinstance(raw, dict) else {}
    limits = DynamoDBPartitionLimits()
    try:
        units = float(entry.get("rcu_or_wcu_per_second"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        units = None
    if units is not None and entry.get("operation") in ("read", "write"):
        limit = (
            limits.read_units_per_second
            if entry["operation"] == "read"
            else limits.write_units_per_second
        )
        utilization = expected_utilization_pct(units, limit)
        entry.update(
            partition_limit=limit,
            utilization_pct=utilization,
            at_risk=utilization > limits.at_risk_threshold_pct,
        )
    return HotPartitionEntry.model_validate(entry).model_dump(mode="json")


def compute_performances_and_costs_entries(
    raw_entries: list[Any],
    access_patterns: Any = None,
    table_definitions: Any = None,
) -> list[dict]:
    """Compute and validate hot partition entries (see :func:`computed_entry`).

    Raises ``pydantic.ValidationError`` on the first invalid entry. Returns the
    entries as JSON-mode dicts, with the computed ``partition_limit``,
    ``utilization_pct`` and ``at_risk`` the design must use. With the design's
    ``access_patterns`` and ``table_definitions`` it also raises ``ValueError``
    when a base-table read entry leaves out the GSI fetch, or a read's
    ``returned_attributes`` name an attribute its table does not define.
    """
    computed = [computed_entry(e) for e in raw_entries]
    fetch_errors = keys_only_fetch_errors(computed, access_patterns, table_definitions)
    if fetch_errors:
        raise ValueError("; ".join(fetch_errors))
    logger.info("Computed %d hot partition entries", len(computed))
    return computed


def check_draft_costs(draft: dict, design_metrics: dict | None = None) -> dict:
    """Run :func:`compute_performances_and_costs_entries` over a draft's
    ``hot_partition_analysis`` and summarise the outcome.

    ``passed`` is true when every entry is valid once computed *and* the
    draft's own ``partition_limit`` / ``utilization_pct`` / ``at_risk`` match
    the computed ones (and, with ``design_metrics``, every ``design_rps``
    matches). The check is read-only: it never modifies ``draft``.

    Returns ``{"passed", "entry_count", "results", "per_table",
    "keys_only_fetch", "hot_partition_findings", "errors"}`` where ``results`` are the validated
    entries with the computed figures, ``per_table`` the capacity figures per table/GSI/operation,
    ``hot_partition_findings`` the at-risk entries with their mitigation, and
    ``errors`` one ``{"index", "error"}`` per rejected entry (``index`` is
    ``None`` when ``hot_partition_analysis`` itself is missing or not a list,
    or for an access pattern whose ``design_rps`` disagrees with
    ``design_metrics``).
    """
    entries = draft.get("hot_partition_analysis") if isinstance(draft, dict) else None
    if not isinstance(entries, list):
        return {
            "passed": False,
            "entry_count": 0,
            "results": [],
            "per_table": [],
            "keys_only_fetch": [],
            "hot_partition_findings": [],
            "errors": [{"index": None, "error": "hot_partition_analysis is missing or not a list"}],
        }

    results: list[dict] = []
    errors: list[dict] = []
    for index, entry in enumerate(entries):
        try:
            computed = computed_entry(entry)
        except ValidationError as exc:
            errors.append({"index": index, "error": str(exc)})
            continue
        results.append(computed)
        own = check_hot_partition_entry(entry, f"[{index}]") if isinstance(entry, dict) else []
        missing = [
            f
            for f in ("partition_limit", "utilization_pct", "at_risk")
            if not isinstance(entry, dict) or entry.get(f) is None
        ]
        if missing:
            own.append(
                f"hot_partition_analysis [{index}]: {', '.join(missing)} missing; "
                "copy them from this check's results"
            )
        if own:
            errors.append({"index": index, "error": "; ".join(own)})

    for message in keys_only_fetch_errors(
        results, draft.get("access_patterns"), draft.get("table_definitions")
    ):
        errors.append({"index": None, "error": message})

    if design_metrics:
        # Hot-partition messages were reported per entry above; keep the rest.
        for message in check_design_numbers("dynamodb", draft, design_metrics):
            if not message.startswith("hot_partition_analysis"):
                errors.append({"index": None, "error": message})

    return {
        "passed": not errors,
        "entry_count": len(entries),
        "results": results,
        "per_table": [{k: r.get(k) for k in _PER_TABLE_FIELDS} for r in results],
        "keys_only_fetch": keys_only_fetch_load(
            draft.get("access_patterns"), draft.get("table_definitions")
        ),
        "hot_partition_findings": [
            {
                "table_name": r["table_name"],
                "gsi_name": r["gsi_name"],
                "operation": r["operation"],
                "utilization_pct": r["utilization_pct"],
                "mitigation": r["mitigation"],
                "contributing_patterns": r["contributing_patterns"],
            }
            for r in results
            if r["at_risk"]
        ],
        "errors": errors,
    }
