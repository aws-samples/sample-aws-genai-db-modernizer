# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the job's routines migrate, in every deliverable (#1939, ADR-034 Decision 7).

Synthesis computes ``facts.routines`` once (``src.shared.routine_facts``, #1938): the
headline counts, the to-do and the words for each routine behind a routine risk. This
module only formats them (ADR-032):

- :func:`routines_md`: the engineering report's Routines section, the headline, the
  to-do with the routines to migrate yourself, each listed routine (to migrate yourself,
  that would behave differently, with no code collected) by id, kind and status with why
  and the routines it names, then each rewrite and check that could not run once with its
  routines (the LOW roll-up). The routine risks' "And N more" points here;
- :func:`routine_summary_lines`: the headline and the to-do, counts only, which the
  executive report and the final chat summary print.

Never a routine's code: the facts hold ids, kinds and words only. A report with no
``facts.routines`` gets nothing.
"""

from __future__ import annotations

from typing import Any

from . import escaping
from .facts import report_facts


def routine_facts_of(report: dict[str, Any]) -> dict[str, Any] | None:
    """The report's ``facts.routines``, or None (no routines, or a report before them)."""
    routines = report_facts(report).get("routines")
    return routines if isinstance(routines, dict) else None


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def to_do_text(routines: dict[str, Any]) -> str | None:
    """ "We could not translate N routines automatically; migrate them yourself before
    wave N.": the facts' to-do as a sentence, or None when no routine needs it."""
    line = routines.get("migrate_yourself_line")
    return f"{_cap(str(line))}." if line else None


def routine_summary_lines(report: dict[str, Any], *, to_do: bool = True) -> list[tuple[str, str]]:
    """``(label, text)`` for the executive report and the chat summary: the headline
    counts, then the to-do when ``to_do`` (a caller that already prints it in the wave's
    line passes False). Counts only: no routine or table name."""
    routines = routine_facts_of(report)
    if routines is None or not routines.get("line"):
        return []
    out = [("Routines", str(routines["line"]))]
    sentence = to_do_text(routines)
    if sentence and to_do:
        out.append(("To do", sentence))
    return out


def lists_routines(report: dict[str, Any]) -> bool:
    """Whether the engineering report lists any routine by id (a listed row or a note)."""
    routines = routine_facts_of(report) or {}
    return any(
        isinstance(r, dict) and r.get("listed") for r in routines.get("routines") or []
    ) or bool(routines.get("check_notes"))


def routine_wave(report: dict[str, Any]) -> int | None:
    """The wave whose lines carry the routines' to-do (``facts.routines.wave``), or None."""
    wave = (routine_facts_of(report) or {}).get("wave")
    return wave if isinstance(wave, int) and not isinstance(wave, bool) else None


def _code_cell(value: Any) -> str:
    """An id as a code span inside a table cell: a code span shows ``<`` as it is, so
    only the pipe, which splits the row even inside a span, is escaped."""
    return "`" + escaping.md_code(value).replace("|", "\\|") + "`"


def _ref(ref: dict[str, Any]) -> str:
    return f"{escaping.md_text(ref.get('kind'))} `{escaping.md_code(ref.get('routine_id'))}`"


def _refs_cell(refs: list[Any]) -> str:
    return ", ".join(
        f"{escaping.md_cell(r.get('kind'))} {_code_cell(r.get('routine_id'))}"
        for r in refs
        if isinstance(r, dict)
    )


def _note_md(note: dict[str, Any]) -> str:
    ids = ", ".join(
        f"`{escaping.md_code(r.get('routine_id'))}`"
        for r in note.get("routines") or []
        if isinstance(r, dict)
    )
    more = note.get("more_count")
    if isinstance(more, int) and more > 0:
        ids += f", … and {more} more"
    text = str(note.get("text") or "").strip()
    return f"- {escaping.md_text(note.get('label'))} ({note.get('note_count')}): {ids}." + (
        f" {escaping.md_text(text)}" if text else ""
    )


def routines_md(report: dict[str, Any]) -> list[str]:
    """The engineering report's Routines section, from ``facts.routines``; nothing when
    the report has none."""
    routines = routine_facts_of(report)
    if routines is None:
        return []
    rows = [r for r in routines.get("routines") or [] if isinstance(r, dict) and r.get("listed")]
    notes = [n for n in routines.get("check_notes") or [] if isinstance(n, dict)]
    others = routines.get("others_line")
    out = ["## Routines", "", escaping.md_text(routines.get("line") or "")]
    if others and not rows and not notes:
        out[-1] += " " + escaping.md_text(others)
    out.append("")
    to_do = [r for r in routines.get("to_do_routines") or [] if isinstance(r, dict)]
    sentence = to_do_text(routines)
    if sentence and to_do:
        out += ["### To do", "", escaping.md_text(sentence), ""]
        out += [f"- {_ref(r)}" for r in to_do]
        out.append("")
        step = routines.get("migrate_yourself_step")
        if step:
            out += [f"{escaping.md_text(_cap(str(step)))}.", ""]
    if rows:
        out += [
            "### Each routine",
            "",
            "Each routine to migrate yourself, that would behave differently or that has no "
            "code collected, in the order it is created on the target.",
            "",
            "| Routine | Kind | Status | Why | Names, not translated | Tables it names |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            tables = ", ".join(_code_cell(t) for t in r.get("tables") or [])
            out.append(
                f"| {_code_cell(r.get('routine_id'))} "
                f"| {escaping.md_cell(r.get('kind'))} "
                f"| {escaping.md_cell(r.get('status_text') or '-')} "
                f"| {escaping.md_cell(' '.join(str(w) for w in r.get('why') or []) or '-')} "
                f"| {_refs_cell(r.get('blocked_by') or []) or '-'} "
                f"| {tables or '-'} |"
            )
        out.append("")
    if notes:
        out += ["### Rewrites and checks that could not run", ""]
        out += [_note_md(n) for n in notes]
        out.append("")
        if others:
            out += [escaping.md_text(others), ""]
    elif others and rows:
        out += [escaping.md_text(others), ""]
    return out
