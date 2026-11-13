# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each mapped table's owner, the engines that also read it and its cache (#212).

ADR-031 Amendment 9: a table has one writer, and that engine owns it. Every other
engine whose queries read the table reads a copy kept in sync from the owner via
CDC, and the cache layer fronts its hot reads. The engineering report's Migration
map, the executive report and the UI results page state one line per table and the
same counts. This module formats ``facts.table_owner_lines``
(``src.shared.report_facts.table_ownership_facts``, ADR-032); nothing here counts a
table.
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from .facts import report_facts
from .formatting import plural_noun

# The owner rule, as every deliverable states it (the executive report has no columns).
# Since #2166 a table no query writes stays on the plan's relational engine; a report
# saved before it gave such a table to the engine serving most of its queries. Each
# report prints the rule its own lines follow (``facts.table_owner_lines.
# unwritten_owner_rule``, :func:`owner_rule_short`), never a computation here.
_ONE_OWNER = "Each table has one owner, as in the wave plan: the engine that takes its writes; "
_NO_CACHE_OWNER = (
    " A cache never owns a table. Every other engine that reads a table reads a copy kept "
    "current from its owner via CDC."
)
OWNER_RULE_SHORT = (
    f"{_ONE_OWNER}a table no query writes stays on the plan's relational engine, or on the "
    f"source database when the plan has none.{_NO_CACHE_OWNER}"
)
OWNER_RULE_SHORT_MOST_QUERIES = (
    f"{_ONE_OWNER}a table no query writes is owned by the engine that serves most of its "
    f"queries.{_NO_CACHE_OWNER}"
)
_COLUMNS = (
    " Also read by names those engines; Cached by names the cache in front of the table's reads."
)
OWNER_RULE = f"{OWNER_RULE_SHORT}{_COLUMNS}"
OWNER_RULE_MOST_QUERIES = f"{OWNER_RULE_SHORT_MOST_QUERIES}{_COLUMNS}"


def _most_queries_rule(facts: dict[str, Any]) -> bool:
    return bool(facts.get("unwritten_owner_rule") == "most_queries")


def owner_rule_short(facts: dict[str, Any]) -> str:
    """The owner rule ``facts.table_owner_lines`` names (``unwritten_owner_rule``)."""
    return OWNER_RULE_SHORT_MOST_QUERIES if _most_queries_rule(facts) else OWNER_RULE_SHORT


def owner_rule(facts: dict[str, Any]) -> str:
    """:func:`owner_rule_short` with the Migration map's column note."""
    return OWNER_RULE_MOST_QUERIES if _most_queries_rule(facts) else OWNER_RULE


def ownership_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """``facts.table_owner_lines``, or None when the report maps no table."""
    facts = report_facts(report).get("table_owner_lines")
    if not isinstance(facts, dict) or not facts.get("table_count"):
        return None
    return facts


def _and_join(parts: list[str]) -> str:
    """``a, b and c``."""
    return ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else "".join(parts)


def _names(engines: list[Any]) -> str:
    return _and_join([display_engine(str(e)) for e in engines])


def ownership_summary_text(facts: dict[str, Any]) -> str:
    """``Of the 18 mapped tables, DynamoDB owns 15 and Aurora MySQL owns 3; 16 tables are
    also read by another engine through CDC replicas; 9 tables are cached by
    ElastiCache.``"""
    n = facts.get("table_count", 0)
    owned = [
        f"{display_engine(str(e))} owns {k}"
        for e, k in (facts.get("owned_table_counts") or {}).items()
    ]
    none = facts.get("no_owner_table_count") or 0
    if none:
        owned.append(f"{none} {'has' if none == 1 else 'have'} no owner engine")
    text = f"Of the {n} mapped {plural_noun(n, 'table')}, " + _and_join(owned)
    also = facts.get("also_read_table_count") or 0
    text += f"; {also} {_tables_are(also)} also read by another engine through CDC replicas"
    cached = facts.get("cached_table_count") or 0
    if cached:
        engines = _names(facts.get("cache_engines") or [])
        text += f"; {cached} {_tables_are(cached)} cached by {engines}"
    return text + "."


def _tables_are(n: Any) -> str:
    return "table is" if n == 1 else "tables are"


SPLIT_WRITES_LABEL = "no single writer"


def owner_cell(row: dict[str, Any]) -> str:
    """The owner's display name, a dash for a table with no owner engine, and "no
    single writer" for a table whose writes still sit on several engines (Sources of
    truth lists it so), next to the owner the wave plan names for it."""
    name = display_engine(str(row["owner"])) if row.get("owner") else "—"
    if row.get("owner_basis") == "split_writes":
        return f"{name} ({SPLIT_WRITES_LABEL})" if row.get("owner") else SPLIT_WRITES_LABEL
    return name


def also_read_by_cell(row: dict[str, Any]) -> str:
    """The other readers' display names, most queries first, or a dash."""
    engines = row.get("also_read_by") or []
    return ", ".join(display_engine(str(e)) for e in engines) if engines else "—"


def cached_by_cell(row: dict[str, Any]) -> str:
    return display_engine(str(row["cached_by"])) if row.get("cached_by") else "—"


def target_cell(row: dict[str, Any]) -> tuple[str, str | None]:
    """(target table, the design it comes from when that is not the owner's)."""
    engine = row.get("target_engine")
    other = display_engine(str(engine)) if engine and engine != row.get("owner") else None
    return str(row.get("target_table") or "-"), other
