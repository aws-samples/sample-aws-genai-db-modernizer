# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The adoption paths in words, shared by every deliverable that shows them (#1131, #1133).

``facts.adoption`` holds the offered paths (each one the slice plan stopped after a step,
ADR-033 Amendment 2), the paths not offered, and the one-step form of the full target
(#473); ``facts.slice_plan`` holds the steps and stages behind them. Two deliverables show
them: the engineering report's "Adoption paths" section and its waves' stages
(``adoption_paths_md``), and the executive report's "Ways to adopt" page and its waves'
stage lines (``executive_report``). Where they show the same fact they say it in the same
words, so every label, fixed sentence and rule about what may be printed lives here, and
each renderer keeps only its own layout ("Deliverables agree", AGENTS.md).

Nothing here ranks the paths: the staged full target stays the default, and an earlier
stop is stated as a smaller end state, not a better one. Every figure is a ``facts`` field
and nothing here counts, totals or compares one (ADR-032); only what a capped list *shows*
is counted ("5 of the 8 paths"), and the sign of a stored difference picks "less" or
"more". Those stay presentation, as the renderers-only-format guard allows.

Three figures are deliberately left out of every deliverable:

- the tables a path moves off the relational engine (``paths[].tables_migrate``), and
  wave 1's tables a later step owns (``table_classes.leaves_count``, the same count): they
  and the headline ``facts.tables_migrate`` state the same end state's moving tables
  differently on a cross-engine source (trailgear: 18 against 30; #2246 checked after
  #2237), and a deliverable prints one answer per figure (ADR-032 Decision 1). Wave 1's
  table classes say only "The other tables move in a later step."; the figure may only
  ever be printed as "tables moved off <relational engine>";
- an earlier path's cost when the report was not priced path by path
  (``cost_basis`` other than ``per_path_sizing``): its figure is the full target's
  estimates summed, not a cost of that end state (ADR-033 Amendment 2, the 2026-11-12
  decision on #2170). The full target's cost, the headline, is always printed
  (``cost_stated``);
- a difference from the relational move only when ``relational_monthly_cost_usd`` is
  None: with no relational step there is no relational move to measure from, and
  ``cost_delta_vs_relational_usd`` still holds a number for contract compatibility
  (#2214). A deliverable then leaves the difference out altogether.

The cutover is never discussed here: the wave plans say ``CUTOVER_MESSAGE`` once, and the
one-step verdict (``ONE_STEP_SUITS``) describes the shape of the workload, not when or how
to switch (the 2026-11-12 ruling on #2156).
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from .formatting import _fmt_usd, fmt_num, plural_noun, plural_verb

#: What a step, a stage, a unit and the pilot are, said once under the engineering
#: report's section heading (review of #2241): the path labels, the path blocks and the
#: roadmap's stages use them.
DEFINITIONS = (
    "The relational step moves the whole database, a cache step fronts reads and moves no "
    "data, and every other step moves one group of queries to one engine; each wave of "
    "the roadmap holds one or more steps. A step runs in stages, each with its own gate. "
    "A stage moves units: a unit is the smallest set of queries and tables that moves on "
    "its own, and each step's pilot is its first unit to move."
)

#: The one-step verdict, the fixed "suits one step" sentence both reports print (#2240,
#: the 2026-11-12 ruling on #2156: it describes the workload's shape, not a cutover).
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

#: A table cell with no figure to state: an earlier path's cost with no per-path pricing,
#: or the relational move only's difference from itself.
NO_FIGURE = "-"

#: The names a path is shown by: never its raw ``path_id``, which is snake_case.
PATH_LABELS = {"relational_only": "Relational move only", "full_target": "Full target"}
#: How the default path says so, with no parentheses: a Markdown heading escapes them.
DEFAULT_SUFFIX = ", the default"
#: The column that holds each path's difference from the relational move only.
DIFFERENCE_HEADER = "Against the relational move only"
#: The column that holds each path's open critical and high risks.
RISKS_HEADER = "Open critical and high risks"

#: What each stage kind does, in the reader's words (#1129's a, b, c, d).
STAGE_KIND_LABELS = {
    "whole_database": "the whole database at once",
    "cache_reads": "the reads the cache fronts",
    "replica_reads": "reads on a CDC copy",
    "self_contained": "ownership with no CDC copy either way",
    "with_reverse_replica": "ownership with a CDC copy back to an earlier engine",
    "risk_held": "held by an open risk",
}

#: What each "suits one step" rule checks (#473), in the reader's words.
ONE_STEP_RULE_LABELS = {
    "no_open_critical_high_risk": "No open critical or high risk",
    "at_most_one_purpose_built_target": "At most one purpose-built target",
    "no_reverse_replica_stage": "No stage that needs a CDC copy back to an earlier engine",
}
#: The one-step checks table's columns, in both reports.
ONE_STEP_CHECK_HEADER = ("Check", "Observed", "At most", "Holds")


def adoption_block(facts: dict[str, Any]) -> dict[str, Any]:
    """``facts.adoption`` as stored, or {} (a report written before it)."""
    block = facts.get("adoption")
    return block if isinstance(block, dict) else {}


def slice_plan_block(facts: dict[str, Any]) -> dict[str, Any]:
    """``facts.slice_plan`` as stored, or {} (a report written before it)."""
    block = facts.get("slice_plan")
    return block if isinstance(block, dict) else {}


def adoption_paths_shown(facts: dict[str, Any]) -> bool:
    """Whether a deliverable shows the adoption paths: a report whose facts offer a path.

    A report written before them, or one rebuilt without the assignment, states no path
    and reads exactly as it did before (#1131, #1133).
    """
    return bool(adoption_block(facts).get("paths"))


def path_label(path_id: str, default_path_id: str = "") -> str:
    """ "Relational move only", "Through step 5", "Full target, the default"."""
    label = PATH_LABELS.get(path_id)
    if label is None:
        label = f"Through step {path_id.rsplit('_', 1)[-1]}"
    return f"{label}{DEFAULT_SUFFIX}" if path_id == default_path_id else label


def summary_paths(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """The paths a capped view shows, in plan order: the ones ``summary_path_ids`` names
    (#2172), so no renderer chooses which to drop."""
    adoption = adoption_block(facts)
    shown = {str(i) for i in adoption.get("summary_path_ids") or []}
    return [
        p
        for p in adoption.get("paths") or []
        if isinstance(p, dict) and str(p.get("path_id")) in shown
    ]


def cap_text(facts: dict[str, Any], subject: str) -> str:
    """ "<subject> show 5 of the 8 paths: the first, the last, and evenly spaced steps
    between them", or "" when every path is shown."""
    adoption = adoption_block(facts)
    total = adoption.get("path_count") or 0
    shown = len(adoption.get("summary_path_ids") or [])
    if shown >= total:
        return ""
    return (
        f"{subject} show {shown} of the {total} paths: the first, the last, and evenly "
        "spaced steps between them."
    )


def relational_engine(plan: dict[str, Any]) -> str:
    """The engine of the plan's relational step, or "" when it has none."""
    for step in plan.get("plan_steps") or []:
        if isinstance(step, dict) and step.get("kind") == "relational_step":
            return str(step.get("engine") or "")
    return ""


def plan_step(plan: dict[str, Any], number: Any) -> dict[str, Any]:
    """The plan step numbered ``number``, or {} when the plan states none."""
    for step in plan.get("plan_steps") or []:
        if isinstance(step, dict) and step.get("step_number") == number:
            return step
    return {}


def intro_text(facts: dict[str, Any]) -> str:
    """What every path is, and which one is the default: the first thing both reports say."""
    engine = relational_engine(slice_plan_block(facts))
    stays = (
        f", and the queries of every later step stay on {display_engine(engine)}" if engine else ""
    )
    return (
        "Each path is this plan stopped after one of its steps: it takes steps 1 to that "
        f"step{stays}. The staged full target is the default; an earlier stop is a "
        "smaller end state."
    )


def queries_moved_header(facts: dict[str, Any]) -> str:
    """The column of the queries each path takes off the relational engine: "moved" alone
    read as nothing moving on the relational move only, which moves every query."""
    relational = relational_engine(slice_plan_block(facts))
    return f"Queries off {display_engine(relational)}" if relational else "Queries moved"


def per_path_priced(facts: dict[str, Any]) -> bool:
    """Whether every path was priced for itself (``cost_basis`` is ``per_path_sizing``)."""
    return bool(adoption_block(facts).get("cost_basis") == "per_path_sizing")


def relational_cost_stated(facts: dict[str, Any]) -> bool:
    """Whether there is a relational move only to measure a difference from (#2214)."""
    return adoption_block(facts).get("relational_monthly_cost_usd") is not None


def cost_stated(path: dict[str, Any], per_path: bool) -> bool:
    """Whether a path's cost may be printed: always the full target's (the headline), an
    earlier path's only when every path was priced for itself (ADR-033 Amendment 2)."""
    return per_path or path.get("path_id") == "full_target"


def cost_text(path: dict[str, Any], per_path: bool) -> str:
    """A path's monthly cost as a table cell: the figure, or ``NO_FIGURE``."""
    return _fmt_usd(path.get("monthly_cost_usd")) if cost_stated(path, per_path) else NO_FIGURE


def cost_note(facts: dict[str, Any]) -> str:
    """What every printed cost rests on: the facts' own cost basis sentence, or, with no
    per-path pricing, that only the full target's cost is stated."""
    if not adoption_paths_shown(facts):
        return ""
    if per_path_priced(facts):
        return str(adoption_block(facts).get("cost_basis_note") or "")
    return FULL_TARGET_COST_ONLY


def cost_difference(path: dict[str, Any]) -> str:
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


def difference_phrase(path: dict[str, Any]) -> str:
    """The difference in a sentence: ", $15.33 less than the relational move only" or
    ", the same as the relational move only"; "" when there is none to state."""
    difference = cost_difference(path)
    if not difference:
        return ""
    joiner = "as" if difference == "the same" else "than"
    return f", {difference} {joiner} the relational move only"


def not_offered_texts(facts: dict[str, Any]) -> list[str]:
    """One sentence per path not offered, and the closure offered in its place (#1130).
    No query id and no table name."""
    adoption = adoption_block(facts)
    default_path_id = str(adoption.get("default_path_id") or "")
    out: list[str] = []
    for entry in adoption.get("not_offered") or []:
        if not isinstance(entry, dict):
            continue
        label = path_label(str(entry.get("path_id") or ""), default_path_id)
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
            text += f" {path_label(str(instead))} is offered in its place."
        out.append(text)
    return out


def one_step_block(facts: dict[str, Any]) -> dict[str, Any]:
    """``facts.adoption.one_step``, or {} when the facts state no one-step form."""
    one = adoption_block(facts).get("one_step")
    return one if isinstance(one, dict) else {}


def one_step_lines(one: dict[str, Any]) -> list[str]:
    """The one-step form against the staged one (#473): what it replaces, and the CDC
    copies back to an earlier engine it does not need."""
    if not one:
        return []
    steps = one.get("staged_step_count") or 0
    stages = one.get("staged_stage_count") or 0
    gate_count = one.get("gate_count") or 0
    streams = one.get("staged_temporary_stream_count") or 0
    out = [
        f"The same end state in one step, in place of {fmt_num(steps)} "
        f"{plural_noun(steps, 'step')} and {fmt_num(stages)} {plural_noun(stages, 'stage')}.",
        f"Its one gate holds all {fmt_num(gate_count)} stage {plural_noun(gate_count, 'gate')}.",
    ]
    if streams:
        out.append(
            f"It needs none of the {fmt_num(streams)} CDC "
            f"{plural_noun(streams, 'copy', 'copies')} "
            "back to an earlier engine that the staged form carries."
        )
    return out


def one_step_checks(one: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in one.get("one_step_checks") or [] if isinstance(c, dict)]


def one_step_verdict(one: dict[str, Any]) -> str:
    """The fixed "suits one step" sentence for this workload, or "" with no checks."""
    if not one_step_checks(one):
        return ""
    return ONE_STEP_SUITS if one.get("suits_one_step") else ONE_STEP_DOES_NOT_SUIT


def one_step_check_rows(one: dict[str, Any]) -> list[list[str]]:
    """Each check as a row of ``ONE_STEP_CHECK_HEADER``: what it checks, what the plan
    has, the most it allows, and whether it holds."""
    return [
        [
            ONE_STEP_RULE_LABELS.get(str(c.get("rule") or ""), str(c.get("rule") or "")),
            fmt_num(c.get("observed")),
            fmt_num(c.get("limit")),
            "yes" if c.get("passed") else "no",
        ]
        for c in one_step_checks(one)
    ]


def wave_phase(facts: dict[str, Any], wave: int) -> dict[str, Any]:
    """The engine phase migration wave ``wave`` rolls up into (``wave_phases``), or {}."""
    return next(
        (
            p
            for p in slice_plan_block(facts).get("wave_phases") or []
            if isinstance(p, dict) and int(p.get("phase_wave") or 0) == wave
        ),
        {},
    )


def stage_kinds_text(phase: dict[str, Any]) -> str:
    """A wave's stage kinds in run order, in the reader's words, joined: "reads on a CDC
    copy, ownership with a CDC copy back to an earlier engine"."""
    kinds = [STAGE_KIND_LABELS.get(str(k), "") for k in phase.get("stage_kinds") or []]
    return ", ".join(k for k in kinds if k)
