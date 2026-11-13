# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Adoption paths, their stages and the one-step option (#1131).

"Adoption paths" is the engineering report's menu of end states, before "Migration
roadmap": ``facts.adoption``'s offered paths (each one the slice plan stopped after a
step, ADR-033 Amendment 2), the paths not offered, and the one-step form of the full
target (#473). "Migration roadmap" then gives each wave its stages, read from
``facts.slice_plan``'s steps through ``wave_phases``.

Nothing here ranks the paths: the staged full target stays the default, and an earlier
stop is stated as a smaller end state, not a better one. Every figure is a ``facts``
field and the renderer counts nothing (ADR-032); only what a capped list *shows* is
counted here ("+N more", "5 of the 8 paths"), and the sign of a stored difference picks
"less" or "more". Those stay presentation, as the renderers-only-format guard allows: no
total, difference or share is computed.

Three figures are deliberately left out:

- the tables a path moves off the relational engine (``paths[].tables_migrate``), and
  wave 1's tables a later step owns (``table_classes.leaves_count``, the same count): they
  and the headline ``facts.tables_migrate`` state the same end state's moving tables
  differently until #2237 lands, and a deliverable prints one answer per figure
  (ADR-032 Decision 1, the prerequisite recorded on #1131). Even then a cross-engine
  source's headline counts the move to Aurora too, so the figure may only ever be printed
  as "tables moved off <relational engine>";
- an earlier path's cost when the report was not priced path by path
  (``cost_basis`` other than ``per_path_sizing``): its figure is the full target's
  estimates summed, not a cost of that end state (ADR-033 Amendment 2, the 2026-11-12
  decision on #2170). The full target's cost, the headline, is always printed;
- a difference from the relational move only when ``relational_monthly_cost_usd`` is
  None: with no relational step there is no relational move to measure from, and
  ``cost_delta_vs_relational_usd`` still holds a number for contract compatibility
  (#2214).
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from . import escaping
from .formatting import _fmt_usd, _SaidOnce, fmt_num, plural_noun, plural_verb

#: What the roadmap's "one suggested adoption path" sentence points at once this section
#: is rendered (#1131): the menu of end states, the one-step form included.
ADOPTION_POINTER = 'the full target in "Adoption paths" above'

#: What a step, a stage, a unit and the pilot are, said once under the section heading
#: (review of #2241): the path labels, the path blocks and the roadmap's stages use them.
DEFINITIONS = (
    "A step moves one group of queries to one engine, and each wave of the roadmap holds "
    "one or more steps. A step runs in stages, each with its own gate. A stage moves "
    "units: a unit is the smallest set of queries and tables that moves on its own, and "
    "the pilot is the first unit to move."
)

#: The one-step block's verdict, shared with the executive report's page (#2240), so
#: both deliverables say the same thing about the one-step form.
ONE_STEP_SUITS = (
    "These checks describe the shape of this workload. Every check holds, so it suits a "
    "one-step move."
)
ONE_STEP_DOES_NOT_SUIT = (
    "These checks describe the shape of this workload. Not every check holds, so it does "
    "not suit a one-step move."
)

#: Said in place of the cost basis note when only the full target's cost is printed.
FULL_TARGET_COST_ONLY = (
    "Only the full target's cost is stated: this report was written before each path was "
    "estimated for itself."
)

# The names a path is shown by: never its raw ``path_id``, which is snake_case.
_PATH_LABELS = {"relational_only": "Relational move only", "full_target": "Full target"}
#: How the default path says so, with no parentheses: a Markdown heading escapes them.
_DEFAULT_SUFFIX = ", the default"

# What each stage kind does, in the reader's words (#1129's a, b, c, d).
_STAGE_KINDS = {
    "whole_database": "the whole database at once",
    "cache_reads": "the reads the cache fronts",
    "replica_reads": "reads on a CDC copy",
    "self_contained": "ownership with no CDC copy either way",
    "with_reverse_replica": "ownership with a CDC copy back to an earlier engine",
    "risk_held": "held by an open risk",
}

# What each "suits one step" rule checks (#473), in the reader's words.
_ONE_STEP_RULES = {
    "no_open_critical_high_risk": "No open critical or high risk",
    "at_most_one_purpose_built_target": "At most one purpose-built target",
    "no_reverse_replica_stage": "No stage that needs a CDC copy back to an earlier engine",
}

# How many of a list's names are shown before "+N more".
_REPLICATED_SHOWN = 25
_UNITS_SHOWN = 10
_UNIT_TABLES_SHOWN = 10


def _adoption(facts: dict[str, Any]) -> dict[str, Any]:
    block = facts.get("adoption")
    return block if isinstance(block, dict) else {}


def _plan(facts: dict[str, Any]) -> dict[str, Any]:
    block = facts.get("slice_plan")
    return block if isinstance(block, dict) else {}


def adoption_paths_shown(facts: dict[str, Any]) -> bool:
    """Whether "Adoption paths" is rendered: a report whose facts offer a path (#1131).

    A report written before them, or one rebuilt without the assignment, states no path
    and reads exactly as it did before this section existed.
    """
    return bool(_adoption(facts).get("paths"))


def _path_label(path_id: str, default_path_id: str = "") -> str:
    """ "Relational move only", "Through step 5", "Full target, the default"."""
    label = _PATH_LABELS.get(path_id)
    if label is None:
        label = f"Through step {path_id.rsplit('_', 1)[-1]}"
    return f"{label}{_DEFAULT_SUFFIX}" if path_id == default_path_id else label


def _code_list(names: list[Any], cap: int) -> str:
    """The first ``cap`` names as code spans, then "(+N more)"."""
    shown = ", ".join(f"`{escaping.md_code(str(n))}`" for n in names[:cap])
    if not names[cap:]:
        return shown
    return f"{shown} (+{len(names) - cap} more)"


def _relational_engine(plan: dict[str, Any]) -> str:
    """The engine of the plan's relational step, or "" when it has none."""
    for step in plan.get("plan_steps") or []:
        if isinstance(step, dict) and step.get("kind") == "relational_step":
            return str(step.get("engine") or "")
    return ""


def _step(plan: dict[str, Any], number: Any) -> dict[str, Any]:
    """The plan step numbered ``number``, or {} when the plan states none."""
    for step in plan.get("plan_steps") or []:
        if isinstance(step, dict) and step.get("step_number") == number:
            return step
    return {}


def _intro(facts: dict[str, Any]) -> str:
    engine = _relational_engine(_plan(facts))
    stays = (
        f", and the queries of every later step stay on {display_engine(engine)}" if engine else ""
    )
    return (
        "Each path is this plan stopped after one of its steps: it takes steps 1 to that "
        f"step{stays}. The staged full target is the default; an earlier stop is a "
        "smaller end state."
    )


def _steps_phrase(path: dict[str, Any], plan: dict[str, Any]) -> str:
    """ "Steps 1 to 5, the last on DynamoDB in wave 3; 10 stages, 10 gates": the path's
    own counts, and its last step's engine and wave (review of #2241: a reader of
    "Through step 5" could not tell which engine or wave step 5 is)."""
    stop = path.get("stops_after_step") or 0
    steps = path.get("step_count") or 0
    stages = path.get("stage_count") or 0
    gates = path.get("gate_count") or 0
    span = "Step 1" if steps == 1 else f"Steps 1 to {stop}"
    last = _step(plan, stop)
    where = ""
    if last.get("engine"):
        wave = f" in wave {last['phase_wave']}" if last.get("phase_wave") else ""
        where = f", the last on {display_engine(str(last['engine']))}{wave}"
    return (
        f"{span}{where}; {fmt_num(stages)} {plural_noun(stages, 'stage')}, "
        f"{fmt_num(gates)} {plural_noun(gates, 'gate')}"
    )


def _cost_stated(path: dict[str, Any], per_path: bool) -> bool:
    """Whether a path's cost may be printed: always the full target's (the headline), an
    earlier path's only when every path was priced for itself (ADR-033 Amendment 2)."""
    return per_path or path.get("path_id") == "full_target"


def _difference(path: dict[str, Any]) -> str:
    """ "$15.33 less" / "$186.94 more" / "the same", from the path's stated difference;
    "" for the relational move only itself, which the difference is measured from."""
    if path.get("path_id") == "relational_only":
        return ""
    delta = path.get("cost_delta_vs_relational_usd")
    if not isinstance(delta, (int, float)) or isinstance(delta, bool):
        return ""
    if delta == 0:
        return "the same"
    # abs() picks the word for the sign; the figure itself is the stated one.
    return f"{_fmt_usd(abs(delta))} {'less' if delta < 0 else 'more'}"


def _difference_phrase(path: dict[str, Any]) -> str:
    """The difference as a path block says it: ", $15.33 less than the relational move
    only" or ", the same as the relational move only"; "" when there is none to state."""
    difference = _difference(path)
    if not difference:
        return ""
    joiner = "as" if difference == "the same" else "than"
    return f", {difference} {joiner} the relational move only"


def _path_block(
    path: dict[str, Any],
    default_path_id: str,
    relational_stated: bool,
    per_path: bool,
    plan: dict[str, Any],
    gates: _SaidOnce,
) -> list[str]:
    """One path's short block: its engines, steps, queries, CDC copies, cost and first
    gate."""
    label = _path_label(str(path.get("path_id") or ""), default_path_id)
    engines = [str(e) for e in path.get("engines") or []]
    out = [
        f"### {escaping.md_text(label)}",
        "",
        "- Engines: " + ", ".join(escaping.md_text(display_engine(e)) for e in engines),
        f"- {escaping.md_text(_steps_phrase(path, plan))}",
    ]
    by_engine = path.get("queries_by_engine") or {}
    served = [
        f"{fmt_num(by_engine[e])} on {escaping.md_text(display_engine(e))}"
        for e in engines
        if e in by_engine
    ]
    if served:
        out.append("- Queries: " + ", ".join(served))
    copies = [str(t) for t in path.get("replicated_tables") or []]
    if copies:
        n = path.get("replicated_table_count") or 0
        out.append(
            f"- {fmt_num(n)} {plural_noun(n, 'table')} read through a CDC copy, in one direction or "
            f"the other: {_code_list(copies, _REPLICATED_SHOWN)}"
        )
    streams = path.get("temporary_stream_count") or 0
    if streams:
        out.append(
            f"- {fmt_num(streams)} CDC {plural_noun(streams, 'copy', 'copies')} back to an earlier "
            "engine, for reads that stay there"
        )
    if _cost_stated(path, per_path):
        cost = f"- Monthly cost: {escaping.md_text(_fmt_usd(path.get('monthly_cost_usd')))}"
        if relational_stated:
            cost += escaping.md_text(_difference_phrase(path))
        out.append(cost)
    gate = str(path.get("first_gate") or "")
    if gate:
        # Every path takes step 1 first, so they share one first gate: the ledger states
        # it under the first block and the others name that block (#1170). The roadmap's
        # Stage 1 points there too, through the same ledger.
        where = f'under "{label}" above'
        out.append(f"- First gate: {escaping.md_text(gates.line(gate, where))}")
    out.append("")
    return out


def _not_offered_md(adoption: dict[str, Any], default_path_id: str) -> list[str]:
    """One line per path not offered, and the closure offered in its place (#1130)."""
    out: list[str] = []
    for entry in adoption.get("not_offered") or []:
        if not isinstance(entry, dict):
            continue
        label = _path_label(str(entry.get("path_id") or ""), default_path_id)
        if entry.get("reason") == "no_relational_engine":
            text = (
                f"{label} is not offered: no step of this plan keeps the whole database on "
                "one relational engine."
            )
        else:
            n = entry.get("failing_query_count") or 0
            engines = [display_engine(str(e)) for e in entry.get("serving_engines") or []]
            text = (
                f"{label} is not offered: it would leave {fmt_num(n)} "
                f"{plural_noun(n, 'query', 'queries')} on the relational engine that the "
                "assignment review moved off it"
            )
            serve = plural_verb(len(engines), "serves", "serve")
            text += f" ({', '.join(engines)} {serve} them)." if engines else "."
        instead = entry.get("offered_instead")
        if instead:
            # The closure is named without the default's suffix: "Full target is offered".
            text += f" {_path_label(str(instead))} is offered in its place."
        out += [f"*{escaping.md_text(text)}*", ""]
    return out


def _one_step_md(adoption: dict[str, Any], pointer: str) -> list[str]:
    """ "One step": the full target as one step instead of staged (#473)."""
    one = adoption.get("one_step")
    if not isinstance(one, dict) or not one:
        return []
    verdict = ONE_STEP_SUITS if one.get("suits_one_step") else ONE_STEP_DOES_NOT_SUIT
    steps = one.get("staged_step_count") or 0
    stages = one.get("staged_stage_count") or 0
    gate_count = one.get("gate_count") or 0
    streams = one.get("staged_temporary_stream_count") or 0
    out = [
        "### One step",
        "",
        f"- The same end state in one step, in place of {fmt_num(steps)} "
        f"{plural_noun(steps, 'step')} and {fmt_num(stages)} {plural_noun(stages, 'stage')}.",
        f"- Its one gate holds all {fmt_num(gate_count)} stage "
        f"{plural_noun(gate_count, 'gate')}.",
    ]
    if streams:
        out.append(
            f"- It needs none of the {fmt_num(streams)} CDC "
            f"{plural_noun(streams, 'copy', 'copies')} "
            "back to an earlier engine that the staged form carries."
        )
    checks = [c for c in one.get("one_step_checks") or [] if isinstance(c, dict)]
    if checks:
        out += [
            "",
            escaping.md_text(verdict),
            "",
            "| Check | Observed | At most | Holds |",
            "|---|---|---|---|",
        ]
        for check in checks:
            rule = str(check.get("rule") or "")
            out.append(
                f"| {escaping.md_cell(_ONE_STEP_RULES.get(rule, rule))} "
                f"| {escaping.md_cell(fmt_num(check.get('observed')))} "
                f"| {escaping.md_cell(fmt_num(check.get('limit')))} "
                f"| {'yes' if check.get('passed') else 'no'} |"
            )
    out += [
        "",
        f"To take the full target in one step, adopt {escaping.md_text(pointer)} directly.",
        "",
    ]
    return out


def adoption_md(facts: dict[str, Any], pointer: str, gates: _SaidOnce) -> list[str]:
    """Engineering Report "Adoption paths" section (#1131), before "Migration roadmap".

    ``pointer`` names the one-step alternative already shown in this deliverable (the
    target schemas, or the Target engines table); ``gates`` is the ledger that keeps a
    gate stated once in the report, shared with the waves' stages.
    """
    adoption = _adoption(facts)
    paths = [p for p in adoption.get("paths") or [] if isinstance(p, dict)]
    if not paths:
        return []
    total = adoption.get("path_count") or 0
    shown_ids = [str(i) for i in adoption.get("summary_path_ids") or []]
    default_path_id = str(adoption.get("default_path_id") or "")
    relational_stated = adoption.get("relational_monthly_cost_usd") is not None
    per_path = adoption.get("cost_basis") == "per_path_sizing"
    plan = _plan(facts)
    relational = _relational_engine(plan)
    out = [
        f"## Adoption paths ({total})",
        "",
        escaping.md_text(DEFINITIONS),
        "",
        f"*{escaping.md_text(_intro(facts))}*",
        "",
    ]
    note = adoption.get("cost_basis_note") if per_path else FULL_TARGET_COST_ONLY
    if note:
        out += [f"*{escaping.md_text(str(note))}*", ""]
    if len(shown_ids) < total:
        # #2172: the capped view shows the paths ``summary_path_ids`` picks, so no
        # renderer chooses which ones.
        out += [
            escaping.md_text(
                f"The table and the blocks below show {len(shown_ids)} of the {total} paths: "
                "the first, the last, and evenly spaced steps between them."
            ),
            "",
        ]
    # The count is of the queries a path takes off the relational engine: "moved" alone
    # read as nothing moving on the relational move only, which moves every query.
    moved = f"Queries off {display_engine(relational)}" if relational else "Queries moved"
    columns = ["Path", moved, "Monthly cost"]
    if relational_stated:
        columns.append("Against the relational move only")
    columns.append("Open critical and high risks")
    out += ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    shown = [p for p in paths if str(p.get("path_id")) in shown_ids]
    for path in shown:
        cells = [
            escaping.md_cell(_path_label(str(path.get("path_id") or ""), default_path_id)),
            escaping.md_cell(fmt_num(path.get("queries_moved"))),
            escaping.md_cell(
                _fmt_usd(path.get("monthly_cost_usd")) if _cost_stated(path, per_path) else "-"
            ),
        ]
        if relational_stated:
            cells.append(escaping.md_cell(_difference(path) or "-"))
        cells.append(escaping.md_cell(fmt_num(path.get("critical_high_risks"))))
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    for path in shown:
        out += _path_block(path, default_path_id, relational_stated, per_path, plan, gates)
    out += _not_offered_md(adoption, default_path_id)
    out += _one_step_md(adoption, pointer)
    return out


def _stages_by_label(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(stage.get("label")): stage
        for step in plan.get("plan_steps") or []
        if isinstance(step, dict)
        for stage in step.get("step_stages") or []
        if isinstance(stage, dict)
    }


def _unit_row(label: str, unit: dict[str, Any]) -> list[str] | None:
    """One move unit as a row of its stage's unit table: the tables it owns (the step
    takes over their writes), the copies it reads, where a CDC copy of its tables goes
    back to, and its query count. The pilot is named; a unit is never shown by its key,
    which holds an engine id. None for a unit with no table either way: the stage's own
    counts state it (the cache's one unit)."""
    owned = [str(t) for t in unit.get("owned_tables") or []]
    reads = [str(r.get("table")) for r in unit.get("replicated_in") or [] if isinstance(r, dict)]
    back = [
        (str(r.get("engine")), r.get("read_count") or 0)
        for r in unit.get("reverse_replicas") or []
        if isinstance(r, dict)
    ]
    if not (owned or reads or back):
        return None
    # The tables a reverse replica copies are the unit's own, already in "Owns": what
    # this adds is where the copy goes and how many reads stay there.
    back_cell = ", ".join(
        f"{display_engine(engine)} ({fmt_num(n)} {plural_noun(n, 'read')})" for engine, n in back
    )
    return [
        label,
        "Pilot" if unit.get("pilot") else "-",
        fmt_num(unit.get("query_count") or 0),
        _code_list(owned, _UNIT_TABLES_SHOWN) or "-",
        _code_list(reads, _UNIT_TABLES_SHOWN) or "-",
        back_cell or "-",
    ]


def _stage_row(stage: dict[str, Any], gates: _SaidOnce) -> list[str]:
    """One stage as a row of its wave's stage table, with its gate. Two stages of
    different steps can have the same filled gate (the same reads on the same pair of
    engines): the ledger states it once and the later row names the stage that holds it."""
    label = str(stage.get("label"))
    kind = _STAGE_KINDS.get(str(stage.get("kind")), "")
    held = [str(r) for r in stage.get("holding_risk_ids") or []]
    if held:
        kind = f"{kind} ({', '.join(held)})" if kind else ", ".join(held)
    gate = str(stage.get("gate") or "")
    return [
        label,
        kind or "-",
        fmt_num(stage.get("unit_count") or 0),
        fmt_num(stage.get("query_count") or 0),
        gates.line(gate, f"under Stage {label}") if gate else "-",
    ]


def _table_md(header: list[str], rows: list[list[str]]) -> list[str]:
    """A Markdown table, each cell escaped (code-span cells are escaped too, #466)."""
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(escaping.md_cell(c) for c in row) + " |" for row in rows),
    ]


def _table_class_lines(plan: dict[str, Any], wave: int) -> list[str]:
    """The relational step's table classes (#1129): which of the tables it carries stay,
    are read as a copy, or no query reads (the ones a later step owns are left out)."""
    step = next(
        (
            s
            for s in plan.get("plan_steps") or []
            if isinstance(s, dict)
            and s.get("kind") == "relational_step"
            and int(s.get("phase_wave") or 0) == wave
        ),
        None,
    )
    classes = (step or {}).get("table_classes")
    if not isinstance(classes, dict) or not classes:
        return []
    engine = escaping.md_text(display_engine(str((step or {}).get("engine") or "")))
    # Not ``leaves_count`` (the tables a later step owns): it is the paths' moving-table
    # count, which disagrees with the headline "tables migrate" until #2237 lands.
    stays = classes.get("stays_count") or 0
    copied, unread = classes.get("replicated_out_count") or 0, classes.get("unread_count") or 0
    rows = [
        (stays, f"{plural_verb(stays, 'stays', 'stay')} on {engine}, read by no other engine"),
        (
            copied,
            f"{plural_verb(copied, 'stays', 'stay')} on {engine} and "
            f"{plural_verb(copied, 'is', 'are')} read as a CDC copy by another engine",
        ),
        (unread, f"{plural_verb(unread, 'is', 'are')} read by no collected query"),
    ]
    out = ["- Table classes:"]
    for count, text in rows:
        out.append(f"  - {fmt_num(count)} {plural_noun(count, 'table')} {text}")
    return out


def wave_stages_md(facts: dict[str, Any], wave: int, gates: _SaidOnce) -> list[str]:
    """One wave's steps and stages in "Migration roadmap" (#1131).

    The wave's stages are its steps' stages in run order, as ``facts.slice_plan``'s
    ``wave_phases`` rolls them up (#2156), so the renderer picks nothing: a table of the
    stages with their gates, a table of their first units (the pilot named), and, for the
    wave the relational step rolls up into, that step's table classes. A report that
    states no plan adds no line.
    """
    plan = _plan(facts)
    phase = next(
        (
            p
            for p in plan.get("wave_phases") or []
            if isinstance(p, dict) and int(p.get("phase_wave") or 0) == wave
        ),
        None,
    )
    if phase is None:
        return []
    labels = [str(label) for label in phase.get("stage_labels") or []]
    if not labels:
        return []
    stages = _stages_by_label(plan)
    units = {str(u.get("key")): u for u in plan.get("move_units") or [] if isinstance(u, dict)}
    count = phase.get("stage_count") or 0
    numbers = [n for n in phase.get("step_numbers") or [] if isinstance(n, int)]
    # The wave's steps, in order: they follow the waves, so they run from the first to
    # the last (the contract numbers steps 1, 2, ... in wave order).
    span = (
        f"Step {numbers[0]}"
        if len(numbers) == 1
        else f"Steps {numbers[0]} to {numbers[-1]}" if numbers else "Stages"
    )
    shown = [stages[label] for label in labels if label in stages]
    # Stages and units are records, so they are tables: a wave of 17 stages reads as
    # rows, and the gates of one template differ only in their counts (#1170).
    out = [
        f"- {span} ({fmt_num(count)} {plural_noun(count, 'stage')}):",
        "",
        *_table_md(
            ["Stage", "What it moves", "Units", "Queries", "Gate"],
            [_stage_row(stage, gates) for stage in shown],
        ),
        "",
    ]
    unit_rows: list[list[str]] = []
    for stage in shown:
        if stage.get("kind") == "whole_database":
            # One unit, the whole collected schema: the wave's own table list states it.
            continue
        label = str(stage.get("label"))
        keys = [str(k) for k in stage.get("unit_keys") or []]
        for key in keys[:_UNITS_SHOWN]:
            row = _unit_row(label, units.get(key) or {})
            if row is not None:
                unit_rows.append(row)
        if keys[_UNITS_SHOWN:]:
            more = len(keys) - _UNITS_SHOWN
            unit_rows.append([label, f"+{more} more {plural_noun(more, 'unit')}", "", "", "", ""])
    if unit_rows:
        out += [
            f"- Units of each stage, the first {_UNITS_SHOWN}:",
            "",
            *_table_md(
                ["Stage", "Unit", "Queries", "Owns", "Reads a copy of", "CDC copy back to"],
                unit_rows,
            ),
            "",
        ]
    out += _table_class_lines(plan, wave)
    return out
