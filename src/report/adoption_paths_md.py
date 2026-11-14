# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engineering report's "Adoption paths" section and its waves' stages (#1131): the
Markdown layout only.

"Adoption paths" comes before "Migration roadmap": the capped path table, a short block
per path, the paths not offered and the one-step form. "Migration roadmap" then gives each
wave its stages, read from ``facts.slice_plan``'s steps through ``wave_phases``. Every
label, fixed sentence and rule about what may be printed is ``src.report.adoption``'s,
shared with the executive report's "Ways to adopt" page (#1133), so the two reports say
the same thing in the same words; this module adds the engineering report's own detail
(the tables read through a copy, the units, the table classes) and its layout.
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from . import escaping
from .adoption import (
    DEFINITIONS,
    DIFFERENCE_HEADER,
    NO_FIGURE,
    ONE_STEP_CHECK_HEADER,
    RISKS_HEADER,
    STAGE_KIND_LABELS,
    adoption_block,
    cap_text,
    cost_difference,
    cost_note,
    cost_stated,
    cost_text,
    difference_phrase,
    intro_text,
    not_offered_texts,
    one_step_block,
    one_step_check_rows,
    one_step_lines,
    one_step_verdict,
    path_label,
    per_path_priced,
    plan_step,
    queries_moved_header,
    relational_cost_stated,
    slice_plan_block,
    summary_paths,
    wave_phase,
)
from .formatting import _SaidOnce, fmt_num, plural_noun, plural_verb

#: What the roadmap's "one suggested adoption path" sentence points at once this section
#: is rendered (#1131): the menu of end states, the one-step form included.
ADOPTION_POINTER = 'the full target in "Adoption paths" above'

# How many of a list's names are shown before "+N more".
_REPLICATED_SHOWN = 25
_UNITS_SHOWN = 10
_UNIT_TABLES_SHOWN = 10


def _code_list(names: list[Any], cap: int) -> str:
    """The first ``cap`` names as code spans, then "(+N more)"."""
    shown = ", ".join(f"`{escaping.md_code(str(n))}`" for n in names[:cap])
    if not names[cap:]:
        return shown
    return f"{shown} (+{len(names) - cap} more)"


def _steps_phrase(path: dict[str, Any], plan: dict[str, Any]) -> str:
    """ "Steps 1 to 5, the last on DynamoDB in wave 3; 10 stages, 10 gates": the path's
    own counts, and its last step's engine and wave (review of #2241: a reader of
    "Through step 5" could not tell which engine or wave step 5 is)."""
    stop = path.get("stops_after_step") or 0
    steps = path.get("step_count") or 0
    stages = path.get("stage_count") or 0
    gates = path.get("gate_count") or 0
    span = "Step 1" if steps == 1 else f"Steps 1 to {stop}"
    last = plan_step(plan, stop)
    where = ""
    if last.get("engine"):
        wave = f" in wave {last['phase_wave']}" if last.get("phase_wave") else ""
        where = f", the last on {display_engine(str(last['engine']))}{wave}"
    return (
        f"{span}{where}; {fmt_num(stages)} {plural_noun(stages, 'stage')}, "
        f"{fmt_num(gates)} {plural_noun(gates, 'gate')}"
    )


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
    label = path_label(str(path.get("path_id") or ""), default_path_id)
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
    if cost_stated(path, per_path):
        cost = f"- Monthly cost: {escaping.md_text(cost_text(path, per_path))}"
        if relational_stated:
            cost += escaping.md_text(difference_phrase(path))
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


def _one_step_md(facts: dict[str, Any], pointer: str) -> list[str]:
    """ "One step": the full target as one step instead of staged (#473)."""
    one = one_step_block(facts)
    if not one:
        return []
    out = ["### One step", "", *(f"- {escaping.md_text(line)}" for line in one_step_lines(one))]
    verdict = one_step_verdict(one)
    if verdict:
        out += [
            "",
            escaping.md_text(verdict),
            "",
            *_table_md(list(ONE_STEP_CHECK_HEADER), one_step_check_rows(one)),
        ]
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
    adoption = adoption_block(facts)
    paths = [p for p in adoption.get("paths") or [] if isinstance(p, dict)]
    if not paths:
        return []
    total = adoption.get("path_count") or 0
    default_path_id = str(adoption.get("default_path_id") or "")
    relational_stated = relational_cost_stated(facts)
    per_path = per_path_priced(facts)
    plan = slice_plan_block(facts)
    out = [
        f"## Adoption paths ({total})",
        "",
        escaping.md_text(DEFINITIONS),
        "",
        f"*{escaping.md_text(intro_text(facts))}*",
        "",
    ]
    note = cost_note(facts)
    if note:
        out += [f"*{escaping.md_text(note)}*", ""]
    cap = cap_text(facts, "The table and the blocks below")
    if cap:
        out += [escaping.md_text(cap), ""]
    columns = ["Path", queries_moved_header(facts), "Monthly cost"]
    if relational_stated:
        columns.append(DIFFERENCE_HEADER)
    columns.append(RISKS_HEADER)
    out += ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    shown = summary_paths(facts)
    for path in shown:
        cells = [
            escaping.md_cell(path_label(str(path.get("path_id") or ""), default_path_id)),
            escaping.md_cell(fmt_num(path.get("queries_moved"))),
            escaping.md_cell(cost_text(path, per_path)),
        ]
        if relational_stated:
            cells.append(escaping.md_cell(cost_difference(path) or NO_FIGURE))
        cells.append(escaping.md_cell(fmt_num(path.get("critical_high_risks"))))
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    for path in shown:
        out += _path_block(path, default_path_id, relational_stated, per_path, plan, gates)
    for text in not_offered_texts(facts):
        out += [f"*{escaping.md_text(text)}*", ""]
    out += _one_step_md(facts, pointer)
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
    kind = STAGE_KIND_LABELS.get(str(stage.get("kind")), "")
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
    # count, which trailgear's headline "tables migrate" disagrees with (#2246). Three
    # classes of four would let a reader subtract it, so a count-free line closes them.
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
    if classes.get("leaves"):
        out.append("  - The other tables move in a later step.")
    return out


def wave_stages_md(facts: dict[str, Any], wave: int, gates: _SaidOnce) -> list[str]:
    """One wave's steps and stages in "Migration roadmap" (#1131).

    The wave's stages are its steps' stages in run order, as ``facts.slice_plan``'s
    ``wave_phases`` rolls them up (#2156), so the renderer picks nothing: a table of the
    stages with their gates, a table of their first units (the pilot named), and, for the
    wave the relational step rolls up into, that step's table classes. A report that
    states no plan adds no line.
    """
    plan = slice_plan_block(facts)
    phase = wave_phase(facts, wave)
    if not phase:
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
                ["Stage", "Pilot", "Queries", "Owns", "Reads a copy of", "CDC copy back to"],
                unit_rows,
            ),
            "",
        ]
    out += _table_class_lines(plan, wave)
    return out
