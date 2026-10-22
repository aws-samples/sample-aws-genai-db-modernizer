"""Sources of truth and replicas (#992, ADR-031 Amendment 9).

A table's writes land on one engine, its source of truth; another engine that
serves the table's reads reads a copy kept in sync from it via CDC, so those
reads are eventually consistent. The engineering report states it per table,
after its wave plan: the source of truth, the replicas, the CDC direction and the
reads a replica serves, then the risk once for all of them (#1170). It formats ``facts.table_sources`` (``src.shared.report_facts.table_source_facts``,
ADR-032); nothing here counts a table or a query.
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.report_facts import shared_replica_risk_text

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


def _risks_md(rows: list[dict[str, Any]]) -> list[str]:
    """The risk of the replicated tables, stated once (#1170).

    A row whose facts say ``shares_replica_risk`` (its stored risk is the standard
    sentence, which differs from the next only in what the table above gives for
    each row) is covered by one sentence naming the sources of truth. Any other
    row's stored risk (a report written with older wording) is printed as stored,
    for its own table. The facts decide which is which (#1232), as the UI reads.
    """
    sources: list[str] = []
    own: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("source_of_truth", ""))
        if row.get("shares_replica_risk") is True:
            if source not in sources:
                sources.append(source)
        else:
            own.append(row)
    out: list[str] = []
    if sources:
        lead = (
            "Every other table above has this risk." if own else "Every table above has this risk."
        )
        out.append(f"{lead} {escaping.md_text(shared_replica_risk_text(sources))}")
    if own:
        out += ["", "The risk of each table below:", ""] if sources else ["The risk of each:", ""]
        for row in own:
            out.append(
                f"- `{escaping.md_cell(escaping.md_code(row.get('table', '?')))}`: "
                f"{escaping.md_text(row.get('risk', ''))}"
            )
    return out


def sources_of_truth_md(report: dict[str, Any]) -> list[str]:
    """The "Sources of truth and replicas" section (engineering report)."""
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
        out += ["", *_risks_md(rows), ""]
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
    kept = facts.get("source_database_table_count") or 0
    if kept:
        # #1329: their writes have no engine in the customer's engine scope.
        out += [source_database_text(kept), ""]
    split = facts.get("split_write_table_count") or 0
    if split:
        out += [
            f"{split} {plural_noun(split, 'table')} {plural_verb(split, 'has', 'have')} writes "
            "on more than one engine and no single source of truth; route each table's writes "
            "to one engine in the assignment review.",
            "",
        ]
    return out


def source_database_text(n: int) -> str:
    """The sentence for the tables the source database keeps writing (#1329); the
    same sentence as the UI (analysis-results.sources-of-truth.source-database)."""
    return (
        f"The source database stays the writer of {n} {plural_noun(n, 'table')}: no engine "
        f"in the customer's engine scope can serve {plural_noun(n, 'its', 'their')} writes. "
        f"Each engine that reads {plural_noun(n, 'it', 'one')} holds a replica kept in sync "
        "from the source database."
    )
