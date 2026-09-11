"""Customer assignment-review document: render, parse, and diff (ADR-028).

The review is a free-form markdown document the customer reads and hand-edits.
Inside it is exactly one marker-bounded, ``query_id``-anchored editable table:

    <!-- ATX-ASSIGNMENT-REVIEW:BEGIN (do not remove this line) -->
    | query_id | access pattern | current engine | new engine | in scope |
    | --- | --- | --- | --- | --- |
    | q1 | t.users | dynamodb | dynamodb | yes |
    <!-- ATX-ASSIGNMENT-REVIEW:END (do not remove this line) -->

Only the table between the markers is parsed; prose around it is ignored, so the
customer can reflow the document freely. The customer edits the ``new engine`` and
``in scope`` columns; ``query_id`` is the stable anchor and ``current engine`` is
read-only context (the authoritative "current" is the Assignment, not a possibly
hand-edited cell). Rendering an unedited assignment and parsing it back yields
zero deltas, so a customer who only reads and approves changes nothing.

The parse is strict and fail-loud: a missing marker, wrong columns, unknown
engine, non-yes/no scope, duplicate query_id, or malformed row raises
``ReviewParseError`` with a message the caller can show the customer, rather than
silently dropping an edit.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from src.agents.referee.assignment_overrides import QueryOverrideInput
from src.agents.referee.triage import ANALYSIS_AGENTS
from src.contracts.assignment_models import Assignment

# --- Editable-surface catalog (single source of truth) ----------------------
# The renderer, the parser's validation, and the customer guidance all read
# these, so what we show, what we accept, and what we document cannot drift.

REVIEW_BEGIN_MARKER = "<!-- ATX-ASSIGNMENT-REVIEW:BEGIN (do not remove this line) -->"
REVIEW_END_MARKER = "<!-- ATX-ASSIGNMENT-REVIEW:END (do not remove this line) -->"

# Fixed column order of the editable table. The parser validates the header
# against this exactly (case-insensitive).
REVIEW_TABLE_COLUMNS: tuple[str, ...] = (
    "query_id",
    "access pattern",
    "current engine",
    "new engine",
    "in scope",
)


def valid_target_engines() -> frozenset[str]:
    """Engines a customer may reassign a query to (syntactic guard).

    Sourced from triage's ANALYSIS_AGENTS so the review's accepted set cannot
    drift from the engines the pipeline actually analyzes and assigns. Reassigning
    to an engine that was not analyzed for this specific job is caught later by
    ``apply_assignment_overrides`` (validation); this set rejects typos and
    engines the pipeline does not support at all.
    """
    return ANALYSIS_AGENTS


class ReviewParseError(ValueError):
    """A hand-edited review document could not be parsed or failed validation."""


@dataclass(frozen=True)
class ReviewRow:
    """One parsed row of desired state (not a delta)."""

    query_id: str
    new_engine: str
    in_scope: bool


# --- Render ------------------------------------------------------------------


def _sanitize_cell(text: str) -> str:
    """Make a value safe for a markdown table cell (no pipes or newlines)."""
    return re.sub(r"\s+", " ", text.replace("|", "/")).strip()


def _bool_to_scope(in_scope: bool) -> str:
    return "yes" if in_scope else "no"


def render_assignment_review(assignment: Assignment) -> str:
    """Render the effective assignment as a customer-editable review document.

    Groups a short read-only summary by engine (with the "why") for context, then
    emits the single marker-bounded editable table. Rendering then parsing an
    unedited document produces no changes.
    """
    engines_in_use = sorted({qa.assigned_engine for qa in assignment.query_assignments})
    in_scope_total = sum(1 for qa in assignment.query_assignments if qa.in_scope)

    lines: list[str] = []
    lines.append("# Review query-to-engine routing")
    lines.append("")
    lines.append(
        f"Assignment version {assignment.version}: "
        f"{len(assignment.query_assignments)} queries "
        f"({in_scope_total} in scope) across {len(engines_in_use)} engine(s). "
        "Review where each access pattern is routed and why, then approve or edit."
    )
    lines.append("")
    lines.append("## How to edit")
    lines.append("")
    lines.append(
        "- To move a query to a different engine, change its **new engine** cell. "
        "To drop a query from this iteration, set its **in scope** cell to `no`."
    )
    lines.append(
        "- You only need to send back the rows you changed. Keep the two marker "
        "comments and the header row, then include just the edited rows — any query "
        "you leave out is kept exactly as it is now. (You can also send the whole "
        "table back if you prefer.)"
    )
    lines.append(
        "- Do not change the **query_id** column (it anchors each row) or remove the "
        "marker comments around the table."
    )
    lines.append(f"- Valid engines: {', '.join(sorted(valid_target_engines()))}.")
    lines.append("- Approve as-is by leaving the table unchanged.")
    lines.append("")

    # Per-engine "why" context (read-only prose).
    lines.append("## Current routing and rationale")
    lines.append("")
    for engine in engines_in_use:
        group = [qa for qa in assignment.query_assignments if qa.assigned_engine == engine]
        in_scope_n = sum(1 for qa in group if qa.in_scope)
        lines.append(f"### {engine} ({in_scope_n} in scope / {len(group)} total)")
        lines.append("")
        for qa in group:
            scope = "" if qa.in_scope else " _(out of scope)_"
            reason = _sanitize_cell(qa.assignment_reason) or "no reason recorded"
            lines.append(f"- `{qa.query_id}`{scope}: {reason}")
        lines.append("")

    # The one editable, machine-parseable table.
    lines.append("## Editable routing table")
    lines.append("")
    lines.append(REVIEW_BEGIN_MARKER)
    lines.append("| " + " | ".join(REVIEW_TABLE_COLUMNS) + " |")
    lines.append("| " + " | ".join(["---"] * len(REVIEW_TABLE_COLUMNS)) + " |")
    for qa in assignment.query_assignments:
        access = _sanitize_cell(", ".join(qa.source_tables)) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    _sanitize_cell(qa.query_id),
                    access,
                    qa.assigned_engine,
                    qa.assigned_engine,  # "new" defaults to current -> no change
                    _bool_to_scope(qa.in_scope),
                ]
            )
            + " |"
        )
    lines.append(REVIEW_END_MARKER)
    lines.append("")

    return "\n".join(lines)


# --- Parse -------------------------------------------------------------------


def _split_row(line: str) -> list[str]:
    """Split a markdown table row on unescaped pipes, dropping the outer borders."""
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(c) <= {"-", ":"} and c for c in cells)


def parse_assignment_review(markdown: str) -> list[ReviewRow]:
    """Parse the editable table from a (possibly hand-edited) review document.

    Returns one ReviewRow per data row (desired state, not deltas). Raises
    ReviewParseError on any structural or value problem so a bad edit surfaces
    with a clear message instead of being silently dropped.
    """
    if REVIEW_BEGIN_MARKER not in markdown or REVIEW_END_MARKER not in markdown:
        raise ReviewParseError(
            "Could not find the review table markers. Do not remove the "
            f"'{REVIEW_BEGIN_MARKER}' / '{REVIEW_END_MARKER}' comment lines."
        )

    body = markdown.split(REVIEW_BEGIN_MARKER, 1)[1].split(REVIEW_END_MARKER, 1)[0]
    table_lines = [ln for ln in body.splitlines() if ln.strip().startswith("|")]
    if len(table_lines) < 2:
        raise ReviewParseError(
            "The review table is missing its header or has no rows. Keep the "
            "header row and the '| --- |' separator."
        )

    header = [c.lower() for c in _split_row(table_lines[0])]
    if tuple(header) != REVIEW_TABLE_COLUMNS:
        raise ReviewParseError(
            f"Unexpected table columns {header}. Expected exactly: "
            f"{list(REVIEW_TABLE_COLUMNS)}. Do not add, remove, or rename columns."
        )

    valid_engines = valid_target_engines()
    rows: list[ReviewRow] = []
    seen: set[str] = set()

    for line in table_lines[1:]:
        cells = _split_row(line)
        if _is_separator_row(cells):
            continue
        if len(cells) != len(REVIEW_TABLE_COLUMNS):
            raise ReviewParseError(
                f"Row has {len(cells)} columns, expected {len(REVIEW_TABLE_COLUMNS)}: "
                f"{line.strip()!r}"
            )

        query_id, _access, _current, new_engine, in_scope_raw = cells
        if not query_id:
            raise ReviewParseError(f"Row is missing a query_id: {line.strip()!r}")
        if query_id in seen:
            raise ReviewParseError(f"Duplicate query_id in the table: {query_id!r}")
        seen.add(query_id)

        if new_engine not in valid_engines:
            raise ReviewParseError(
                f"Unknown engine {new_engine!r} for query {query_id!r}. "
                f"Valid engines: {sorted(valid_engines)}."
            )

        normalized_scope = in_scope_raw.strip().lower()
        if normalized_scope in ("yes", "y", "true", "in scope"):
            in_scope = True
        elif normalized_scope in ("no", "n", "false", "out of scope"):
            in_scope = False
        else:
            raise ReviewParseError(
                f"Invalid 'in scope' value {in_scope_raw!r} for query {query_id!r}. "
                "Use 'yes' or 'no'."
            )

        rows.append(ReviewRow(query_id=query_id, new_engine=new_engine, in_scope=in_scope))

    if not rows:
        raise ReviewParseError("The review table has no data rows.")
    return rows


# --- Diff --------------------------------------------------------------------


def diff_review_rows(current: Assignment, rows: list[ReviewRow]) -> list[QueryOverrideInput]:
    """Compute the minimal overrides between the current assignment and edited rows.

    Only fields that actually changed are set on each returned override, and only
    for queries that changed at all. Queries absent from ``rows`` are treated as
    unchanged (a deleted row is not a deletion of the query). Raises
    ReviewParseError if a row names a query_id not in the current assignment.
    """
    current_by_id = {qa.query_id: qa for qa in current.query_assignments}
    overrides: list[QueryOverrideInput] = []

    for row in rows:
        qa = current_by_id.get(row.query_id)
        if qa is None:
            raise ReviewParseError(
                f"Row references query {row.query_id!r} which is not in the current "
                "assignment. Do not add or rename query_id rows."
            )
        engine_changed = row.new_engine != qa.assigned_engine
        scope_changed = row.in_scope != qa.in_scope
        if not engine_changed and not scope_changed:
            continue
        overrides.append(
            QueryOverrideInput(
                query_id=row.query_id,
                assigned_engine=row.new_engine if engine_changed else None,
                in_scope=row.in_scope if scope_changed else None,
            )
        )

    return overrides


# --- Structured (HITL) surface ----------------------------------------------
# The platform HITL transport (ADR-028 amendment) does not exchange markdown: it
# renders an editable ``TableComponent`` and hands the edited rows back as JSON.
# These helpers are the structured analogues of render/parse/diff above. The
# markdown functions remain for the chat fallback (when HITL is unavailable).

# Item field keys for the structured routing table. The editable ones
# (``new_engine``, ``in_scope``) mirror the two customer-editable markdown columns;
# ``rationale`` is a read-only "why this engine" column added for the HITL table.
REVIEW_ITEM_FIELDS: tuple[str, ...] = (
    "query_id",
    "access_pattern",
    "current_engine",
    "new_engine",
    "in_scope",
    "rationale",
)


def _parse_scope(value: Any, query_id: str) -> bool:
    """Parse an 'in scope' cell (yes/no and friends) to a bool, or raise.

    Shared accepted-token set with the markdown parser so the two transports
    agree on what counts as in/out of scope.
    """
    normalized = str(value if value is not None else "").strip().lower()
    if normalized in ("yes", "y", "true", "in scope"):
        return True
    if normalized in ("no", "n", "false", "out of scope"):
        return False
    raise ReviewParseError(
        f"Invalid 'in scope' value {value!r} for query {query_id!r}. Use 'yes' or 'no'."
    )


def _top_reasons(group: list, limit: int = 3) -> str:
    """Join the most common per-query rationales in an engine group.

    The per-query ``assignment_reason`` is the authoritative "why"; for an
    engine-level summary we surface the few most frequent distinct reasons so the
    recommendation reads as a rationale rather than a wall of identical strings.
    """
    counts = Counter(_sanitize_cell(qa.assignment_reason) for qa in group if qa.assignment_reason)
    top = [reason for reason, _ in counts.most_common(limit) if reason]
    return "; ".join(top) if top else "no reason recorded"


def render_assignment_summary(assignment: Assignment) -> str:
    """Render the engine-level routing recommendation (no per-query table).

    This is the first step of the two-step gate: the customer sees where the
    workload is routed and why, per engine, and decides whether to accept the
    recommendation or open the full per-query table for detailed review. It is
    intentionally small (one row per engine) so it is safe to show in chat even
    when the assignment has thousands of queries.
    """
    engines_in_use = sorted({qa.assigned_engine for qa in assignment.query_assignments})
    in_scope_total = sum(1 for qa in assignment.query_assignments if qa.in_scope)

    lines: list[str] = []
    lines.append("# Query-to-engine routing \u2014 recommendation")
    lines.append("")
    lines.append(
        f"Assignment version {assignment.version}: "
        f"{len(assignment.query_assignments)} queries "
        f"({in_scope_total} in scope) across {len(engines_in_use)} engine(s)."
    )
    lines.append("")
    lines.append("| engine | queries (in scope / total) | main rationale |")
    lines.append("| --- | --- | --- |")
    for engine in engines_in_use:
        group = [qa for qa in assignment.query_assignments if qa.assigned_engine == engine]
        in_scope_n = sum(1 for qa in group if qa.in_scope)
        lines.append(f"| {engine} | {in_scope_n} / {len(group)} | {_top_reasons(group)} |")
    lines.append("")
    return "\n".join(lines)


def build_review_table(assignment: Assignment) -> tuple[list[dict], list[dict]]:
    """Build the editable HITL ``TableComponent`` payload for the assignment.

    Returns ``(column_definitions, items)``. ``new engine`` and ``in scope`` are
    the editable columns (each carries an ``editConfig`` with a validation regex
    the WebApp enforces inline); ``query_id``, ``access pattern``, ``current
    engine`` and ``rationale`` are read-only context. One item per query; ``id``
    is the stable row anchor (the query id). Rendering then diffing an unedited
    table yields no overrides.
    """
    engines = sorted(valid_target_engines())
    engine_regex = "^(" + "|".join(re.escape(e) for e in engines) + ")$"

    column_definitions: list[dict] = [
        {"header": "query_id", "field": "query_id", "type": "text"},
        {"header": "access pattern", "field": "access_pattern", "type": "text"},
        {"header": "current engine", "field": "current_engine", "type": "text"},
        {
            "header": "new engine",
            "field": "new_engine",
            "type": "text",
            "editConfig": {"editingCell": True, "validation": engine_regex},
        },
        {
            "header": "in scope",
            "field": "in_scope",
            "type": "text",
            "editConfig": {"editingCell": True, "validation": "^(yes|no)$"},
        },
        {"header": "rationale", "field": "rationale", "type": "text"},
    ]

    items: list[dict] = []
    for qa in assignment.query_assignments:
        access = _sanitize_cell(", ".join(qa.source_tables)) or "-"
        items.append(
            {
                "id": qa.query_id,
                "query_id": qa.query_id,
                "access_pattern": access,
                "current_engine": qa.assigned_engine,
                "new_engine": qa.assigned_engine,  # defaults to current -> no change
                "in_scope": _bool_to_scope(qa.in_scope),
                "rationale": _sanitize_cell(qa.assignment_reason) or "no reason recorded",
            }
        )
    return column_definitions, items


def diff_review_items(current: Assignment, items: list[dict]) -> list[QueryOverrideInput]:
    """Compute minimal overrides from edited HITL table rows (structured analogue
    of :func:`diff_review_rows`).

    Reads ``query_id`` (falling back to ``id``), ``new_engine`` and ``in_scope``
    from each submitted row, compares against the current assignment, and returns
    only the queries that changed, with only the fields that changed set. A blank
    ``new_engine`` is treated as unchanged (keeps the current engine). Raises
    ``ReviewParseError`` on a duplicate row, an unknown engine, an unparseable
    scope, or a row anchored to a query not in the current assignment.
    """
    current_by_id = {qa.query_id: qa for qa in current.query_assignments}
    valid_engines = valid_target_engines()
    overrides: list[QueryOverrideInput] = []
    seen: set[str] = set()

    for row in items:
        query_id = str(row.get("query_id") or row.get("id") or "").strip()
        if not query_id:
            # A row without an anchor cannot be applied; skip rather than fail the
            # whole submission (the platform should never emit one).
            continue
        if query_id in seen:
            raise ReviewParseError(f"Duplicate query_id in the submitted table: {query_id!r}")
        seen.add(query_id)

        qa = current_by_id.get(query_id)
        if qa is None:
            raise ReviewParseError(
                f"Submitted row references query {query_id!r} which is not in the current "
                "assignment."
            )

        new_engine = str(row.get("new_engine") or "").strip() or qa.assigned_engine
        if new_engine not in valid_engines:
            raise ReviewParseError(
                f"Unknown engine {new_engine!r} for query {query_id!r}. "
                f"Valid engines: {sorted(valid_engines)}."
            )
        in_scope = _parse_scope(row.get("in_scope"), query_id)

        engine_changed = new_engine != qa.assigned_engine
        scope_changed = in_scope != qa.in_scope
        if not engine_changed and not scope_changed:
            continue
        overrides.append(
            QueryOverrideInput(
                query_id=query_id,
                assigned_engine=new_engine if engine_changed else None,
                in_scope=in_scope if scope_changed else None,
            )
        )

    return overrides
