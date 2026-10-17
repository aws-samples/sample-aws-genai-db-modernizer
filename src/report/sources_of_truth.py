"""Sources of truth and replicas (#992, ADR-031 Amendment 9).

A table's writes land on one engine, its source of truth; another engine that
serves the table's reads reads a copy kept in sync from it via CDC, so those
reads are eventually consistent. The engineering report and the PDF report (in
its wave plan) state it per table: the source of truth, the replicas, the CDC
direction and the risk, naming the reads a replica serves. Both format
``facts.table_sources`` (``src.shared.report_facts.table_source_facts``,
ADR-032); nothing here counts a table or a query.
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from . import escaping
from .facts import report_facts
from .formatting import plural_noun, plural_verb

SOURCES_OF_TRUTH_INTRO = (
    "Each table's writes land on one engine, its source of truth. An engine that serves "
    "the table's reads keeps a copy in sync from it via CDC, so those reads are "
    "eventually consistent."
)


def _facts(report: dict[str, Any]) -> dict[str, Any] | None:
    facts = report_facts(report).get("table_sources")
    if not isinstance(facts, dict) or not facts.get("written_table_count"):
        return None
    return facts


def cdc_direction(row: dict[str, Any]) -> str:
    """``DynamoDB → Aurora PostgreSQL, DocumentDB``: CDC runs from the source of truth."""
    replicas = ", ".join(display_engine(str(e)) for e in row.get("replica_engines") or [])
    return f"{display_engine(str(row.get('source_of_truth', '')))} → {replicas}"


def sources_of_truth_md(report: dict[str, Any]) -> list[str]:
    """The "Sources of truth and replicas" section (engineering report and PDF)."""
    facts = _facts(report)
    if facts is None:
        return []
    n = facts.get("written_table_count", 0)
    out = [
        f"## Sources of truth and replicas ({n} written {plural_noun(n, 'table')})",
        "",
        SOURCES_OF_TRUTH_INTRO,
        "",
    ]
    rows = facts.get("replicated_tables") or []
    if rows:
        out += [
            "| Table | Source of truth | CDC to replicas | Reads on a replica |",
            "|---|---|---|---|",
        ]
        for row in rows:
            out.append(
                f"| `{escaping.md_cell(escaping.md_code(row.get('table', '?')))}` "
                f"| {escaping.md_cell(display_engine(str(row.get('source_of_truth', ''))))} "
                f"| {escaping.md_cell(cdc_direction(row))} "
                f"| {escaping.md_cell(row.get('replica_read_count', 0))} |"
            )
        # The risks name the replica-served reads, whose ids can be long: a list
        # wraps where a table cell would not.
        out += ["", "The risk of each:", ""]
        for row in rows:
            out.append(
                f"- `{escaping.md_cell(escaping.md_code(row.get('table', '?')))}`: "
                f"{escaping.md_text(row.get('risk', ''))}"
            )
        out.append("")
    more = facts.get("more_replicated_count") or 0
    if more:
        out += [
            f"{more} more replicated {plural_noun(more, 'table')} "
            f"{plural_verb(more, 'is', 'are')} not shown here. The Assessment Data (JSON) "
            "lists every written table under `table_sources`.",
            "",
        ]
    single = facts.get("single_engine_tables") or []
    if single:
        k = facts.get("single_engine_table_count", 0)
        out += [
            f"{k} written {plural_noun(k, 'table')} {plural_verb(k, 'runs', 'run')} every "
            "query on the source of truth, with no replica: "
            + escaping.md_text(", ".join(single))
            + ".",
            "",
        ]
    split = facts.get("split_write_table_count") or 0
    if split:
        out += [
            f"{split} {plural_noun(split, 'table')} {plural_verb(split, 'has', 'have')} writes "
            "on more than one engine and no single source of truth; route each table's writes "
            "to one engine in the assignment review.",
            "",
        ]
    return out
