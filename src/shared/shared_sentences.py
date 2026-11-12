# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sibling design entries that share a sentence, grouped once in ``report.json`` (#2165).

A designer can give two entries of one list the same long sentence: two ElastiCache
unsupported patterns that each end "A stale answer makes the application insert a
duplicate row or update a missing one.", or an Aurora app-layer note that restates the
sentence its column-default notes carry. The engineering report printed one bullet per
entry, so the sentence was printed twice. Synthesis groups them here, so ``report.json``
holds the grouped form and every deliverable reads it (ADR-032):

- ``unsupported_patterns`` (and ``served_by_waiver``) and ``migration_notes`` entries of
  one design that share a sentence of at least ``ONCE_MIN_WORDS`` words (directly or
  through another entry), and are of the same kind (pattern type, object type), become
  one entry: every query id (or object name) of the group, the shared sentence stated
  once and each entry's own sentences kept. The entry lists the entries it groups, as
  they were, in the optional ``grouped_from``. A ``cache.unsupported`` risk's details
  list the grouped entry once, as the report lists it.
- ``app_layer_notes``: a column-default note stands for one column, and the report
  lists the columns whose note reads the same on one line, so column-default notes that
  share a sentence get one text (each keeps its column); another note that restates a
  sentence a column-default note states drops it (the column-default line says it, with
  its columns), and other notes that share a sentence become one entry as above.

Entries that share no long sentence are left as they are (the same objects). The pass
is idempotent, and the deliverables and the results payload read a report through it
(``design_text.design_prose_report``), so a report synthesized before it reads the same.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from src.shared.design_text import ONCE_MIN_WORDS, unsupported_pattern_detail
from src.shared.engine_names import canonical_engine_id

# Where a sentence ends, as the report guard cuts sentences (``tests/report_concision``).
_SENTENCE_END = re.compile(r"(?<=[.!?])[:;]?\s+")
GROUPED_FROM = "grouped_from"
_COLUMN_DEFAULT = "column_default"
_UNSUPPORTED_TEXT = ("reason", "description", "recommendation", "workaround")
_UNSUPPORTED_IDS = ("query_ids", "source_query_ids")


def _sentences(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def _key(sentence: str) -> str:
    return " ".join(sentence.replace("**", "").split()).strip(" -*:,.;").casefold()


def _long(key: str) -> bool:
    return len(key.split()) >= ONCE_MIN_WORDS


def _long_keys(entry: dict[str, Any], fields: Sequence[str]) -> set[str]:
    return {k for f in fields for s in _sentences(entry.get(f)) if _long(k := _key(s))}


def merged_text(texts: Sequence[str]) -> str:
    """``texts`` as one text: each sentence once. A sentence two or more texts state is
    shared: one the first text opens with stays at the front, every other shared one
    follows the texts' own sentences, so no entry's own sentence reads as if the shared
    one were only about the entry before it."""
    split = [_sentences(t) for t in texts if isinstance(t, str) and t.strip()]
    counts: dict[str, int] = {}
    for sentences in split:
        for k in {_key(s) for s in sentences}:
            counts[k] = counts.get(k, 0) + 1
    shared = {k for k, n in counts.items() if n > 1}
    out: list[str] = []
    said: set[str] = set()

    def say(sentence: str) -> None:
        k = _key(sentence)
        if k not in said:
            said.add(k)
            out.append(sentence)

    for s in split[0] if split else []:
        if _key(s) not in shared:
            break
        say(s)
    for sentences in split:
        for s in sentences:
            if _key(s) not in shared:
                say(s)
    for sentences in split:
        for s in sentences:
            say(s)
    return " ".join(s if s.endswith((".", "!", "?")) else f"{s}." for s in out)


def _groups(
    entries: Sequence[Any], fields: Sequence[str], kind: Callable[[dict[str, Any]], str]
) -> list[list[int]]:
    """The positions of the entries that share a long sentence, directly or through
    another entry of the same kind, one list per group of two or more, in order."""
    parent = list(range(len(entries)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[tuple[str, str], int] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        for k in sorted(_long_keys(e, fields)):
            j = first.setdefault((kind(e), k), i)
            parent[find(i)] = find(j)
    by_root: dict[int, list[int]] = {}
    for i, e in enumerate(entries):
        if isinstance(e, dict):
            by_root.setdefault(find(i), []).append(i)
    return [g for g in by_root.values() if len(g) > 1]


def _unique(values: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _originals(members: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries as the design wrote them; a member grouped before brings its own."""
    out: list[dict[str, Any]] = []
    for m in members:
        if isinstance(m.get(GROUPED_FROM), list) and m[GROUPED_FROM]:
            out += [o for o in m[GROUPED_FROM] if isinstance(o, dict)]
        else:
            out.append(m)
    return out


def _merged_entry(
    members: Sequence[dict[str, Any]],
    text_fields: Sequence[str],
    list_fields: Sequence[str] = (),
    name_fields: Sequence[str] = (),
) -> dict[str, Any]:
    entry = dict(members[0])
    for f in list_fields:
        if any(isinstance(m.get(f), list) for m in members):
            entry[f] = _unique(
                [v for m in members if isinstance(m.get(f), list) for v in m[f] if v]
            )
    for f in name_fields:
        names = _unique([str(m[f]).strip() for m in members if str(m.get(f) or "").strip()])
        if names:
            entry[f] = ", ".join(names)
    for f in text_fields:
        texts = [str(m[f]) for m in members if isinstance(m.get(f), str) and m[f].strip()]
        if texts:
            entry[f] = merged_text(texts)
    entry[GROUPED_FROM] = _originals(members)
    return entry


def group_entries(
    entries: Any,
    text_fields: Sequence[str],
    *,
    list_fields: Sequence[str] = (),
    name_fields: Sequence[str] = (),
    kind: Callable[[dict[str, Any]], str] = lambda e: "",
) -> Any:
    """``entries`` with each group that shares a long sentence as one entry, at the
    first member's place. ``entries`` comes back as it is (the same object) when no
    two entries share one, or when it is not a list."""
    if not isinstance(entries, list):
        return entries
    groups = _groups(entries, text_fields, kind)
    if not groups:
        return entries
    lead = {g[0]: g for g in groups}
    folded = {i for g in groups for i in g[1:]}
    out: list[Any] = []
    for i, e in enumerate(entries):
        if i in lead:
            members = [entries[j] for j in lead[i]]
            out.append(_merged_entry(members, text_fields, list_fields, name_fields))
        elif i not in folded:
            out.append(e)
    return out


def _pattern_kind(entry: dict[str, Any]) -> str:
    return str(entry.get("pattern_type") or "").strip().strip("*").casefold()


def _object_kind(entry: dict[str, Any]) -> str:
    return str(entry.get("object_type") or "").strip().casefold()


def group_unsupported_patterns(entries: Any) -> Any:
    return group_entries(
        entries, _UNSUPPORTED_TEXT, list_fields=_UNSUPPORTED_IDS, kind=_pattern_kind
    )


def group_migration_notes(entries: Any) -> Any:
    return group_entries(
        entries,
        ("application_logic_required",),
        name_fields=("object_name", "source_table"),
        kind=_object_kind,
    )


def group_app_layer_notes(notes: Any) -> Any:
    """An Aurora design's app-layer notes with each shared sentence stated once (see the
    module docstring): column-default notes keep one entry per column."""
    if not isinstance(notes, list):
        return notes
    field = ("recommendation",)
    out: list[Any] = list(notes)
    changed = False
    for g in _groups(out, field, _default_kind):
        if out[g[0]].get("feature") != _COLUMN_DEFAULT:
            continue
        texts = _unique([str(out[i].get("recommendation") or "") for i in g])
        if len(texts) > 1:
            one = merged_text(texts)
            for i in g:
                out[i] = {**out[i], "recommendation": one}
            changed = True
    stated = {k for n in out if _is_default(n) for k in _long_keys(n, field)}
    for i, n in enumerate(out):
        if not isinstance(n, dict) or _is_default(n):
            continue
        sentences = _sentences(n.get("recommendation"))
        kept = [s for s in sentences if _key(s) not in stated]
        if len(kept) < len(sentences):
            # Nothing left of its own: the column-default line states it, with its columns.
            out[i] = {**n, "recommendation": " ".join(kept)} if kept else None
            changed = True
    kept_notes = [n for n in out if n is not None]
    grouped = group_entries(
        kept_notes,
        field,
        name_fields=("source_object",),
        kind=lambda e: f"default {id(e)}" if _is_default(e) else "",
    )
    return grouped if changed or grouped is not kept_notes else notes


def _is_default(note: Any) -> bool:
    return isinstance(note, dict) and note.get("feature") == _COLUMN_DEFAULT


def _default_kind(note: dict[str, Any]) -> str:
    return _COLUMN_DEFAULT if _is_default(note) else ""


def _grouped_design(summary: dict[str, Any]) -> dict[str, Any]:
    out = summary
    for key, group in (
        ("unsupported_patterns", group_unsupported_patterns),
        ("served_by_waiver", group_unsupported_patterns),
        ("migration_notes", group_migration_notes),
        ("app_layer_notes", group_app_layer_notes),
    ):
        value = summary.get(key)
        grouped = group(value)
        if grouped is not value:
            out = {**out, key: grouped}
    return out


def _detail_renames(before: Any, after: Any) -> dict[str, str]:
    """Each original unsupported pattern's risk detail -> its grouped entry's detail."""
    renames: dict[str, str] = {}
    if before is after or not isinstance(after, list):
        return renames
    for entry in after:
        if isinstance(entry, dict) and isinstance(entry.get(GROUPED_FROM), list):
            detail = unsupported_pattern_detail(entry)
            for original in entry[GROUPED_FROM]:
                if isinstance(original, dict):
                    renames[unsupported_pattern_detail(original)] = detail
    return renames


def _risk_engine(risk: dict[str, Any]) -> str:
    """The engine a risk's ``[engine]`` description prefix names (risks carry no
    engine field), or ""."""
    m = re.match(r"\s*\[([^\]]+)\]", str(risk.get("description") or ""))
    return canonical_engine_id(m.group(1).strip()) if m else ""


def grouped_report(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with each design's sibling entries that share a sentence grouped, and
    each ``cache.unsupported`` risk's details listing a grouped entry once. A report
    with nothing to group comes back unchanged (the same object)."""
    designs = report.get("schema_designs")
    if not isinstance(designs, dict):
        return report
    new_designs: dict[str, Any] = {}
    renames: dict[str, dict[str, str]] = {}
    for engine, summary in designs.items():
        if not isinstance(summary, dict):
            new_designs[engine] = summary
            continue
        new_designs[engine] = grouped = _grouped_design(summary)
        renames[canonical_engine_id(str(engine))] = _detail_renames(
            summary.get("unsupported_patterns"), grouped.get("unsupported_patterns")
        )
    if all(new_designs[e] is designs[e] for e in designs):
        return report
    out = {**report, "schema_designs": new_designs}
    assessment = report.get("risk_assessment")
    if isinstance(assessment, dict) and isinstance(assessment.get("risks"), list):
        risks = []
        for r in assessment["risks"]:
            rename = renames.get(_risk_engine(r)) if isinstance(r, dict) else None
            if rename and r.get("rule") == "cache.unsupported" and r.get("details"):
                r = {**r, "details": _unique([rename.get(d, d) for d in r["details"]])}
            risks.append(r)
        out["risk_assessment"] = {**assessment, "risks": risks}
    return out
