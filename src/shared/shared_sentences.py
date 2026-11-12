# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A sentence sibling design entries share, stated once (#2165).

A designer can give two entries of one list the same long sentence: two ElastiCache
unsupported patterns that each end with the same sentence about stale answers, or two
Aurora column-default notes (a DATE and a DATETIME zero date) that each end with the
same sentence about migrating zero dates. The engineering report printed one bullet per
entry, so the sentence was printed twice.

Synthesis records each such sentence once (``grouped_report``), next to the entries,
which stay as the design wrote them: a design summary's optional
``shared_sentence_groups`` holds one record per group, ``{"list", "sentences",
"indexes"}``: the list the entries are in, the sentences they all state, and their
positions in it. Only entries of one kind are grouped: unsupported patterns of one
pattern type, migration notes of one object type, app-layer notes of one feature. A
sentence counts when it has at least ``ONCE_MIN_WORDS`` words and reads the same once
case, ``**`` and the closing punctuation are folded; a different number, id or table
name is a different sentence. The renderers print each group as a lead line stating
its sentences once, then each entry with its own text (``shared_layout``), so no
entry's text covers another one, and every list keeps its entries and their count.

A column-default note stands for one column, and the report already lists the columns
whose note reads the same on one line, so for app-layer notes a sentence is shared when
two different note texts state it. Another app-layer note that restates, word for word,
a sentence a column-default note states drops it (the column-default line says it,
with its columns), unless nothing of its own would be left.

The deliverables and the results payload read a report through this too
(``design_text.design_prose_report``), so a report synthesized before it reads the same.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from src.shared.design_text import ONCE_MIN_WORDS

GROUPS_KEY = "shared_sentence_groups"
# Where a sentence ends, as the report guard cuts sentences (``tests/report_concision``).
_SENTENCE_END = re.compile(r"(?<=[.!?])[:;]?\s+")
_COLUMN_DEFAULT = "column_default"
UNSUPPORTED_TEXT = ("reason", "description", "recommendation", "workaround")
MIGRATION_NOTE_TEXT = ("application_logic_required",)
APP_LAYER_TEXT = ("recommendation",)


def _label(field: str) -> Callable[[dict[str, Any]], str]:
    return lambda e: str(e.get(field) or "").strip().strip("*").casefold()


# Each list: its text fields, what makes two entries the same kind, and whether a
# sentence is shared by entries (False) or by different texts (True).
_LISTS: dict[str, tuple[tuple[str, ...], Callable[[dict[str, Any]], str], bool]] = {
    "unsupported_patterns": (UNSUPPORTED_TEXT, _label("pattern_type"), False),
    "served_by_waiver": (UNSUPPORTED_TEXT, _label("pattern_type"), False),
    "migration_notes": (MIGRATION_NOTE_TEXT, _label("object_type"), False),
    "app_layer_notes": (APP_LAYER_TEXT, _label("feature"), True),
}


def _sentences(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def sentence_key(sentence: str) -> str:
    """A sentence as it is compared: one line, ``**``, edge punctuation and case folded."""
    return " ".join(sentence.replace("**", "").split()).strip(" -*:,.;").casefold()


def is_long(sentence: str) -> bool:
    return len(sentence_key(sentence).split()) >= ONCE_MIN_WORDS


def without_sentences(text: Any, sentences: Sequence[str]) -> Any:
    """``text`` without the given sentences (compared as ``sentence_key`` does)."""
    if not isinstance(text, str) or not sentences:
        return text
    drop = {sentence_key(s) for s in sentences}
    return " ".join(s for s in _sentences(text) if sentence_key(s) not in drop)


def _text_of(entry: dict[str, Any], fields: Sequence[str]) -> str:
    return "\n".join(str(entry.get(f) or "") for f in fields)


def sentence_groups(
    entries: Any,
    fields: Sequence[str],
    kind: Callable[[dict[str, Any]], str],
    by_text: bool = False,
) -> list[dict[str, Any]]:
    """The groups of ``entries`` that share a long sentence: ``{"sentences", "indexes"}``.

    Sentences are taken in the order the entries first state them. A sentence the same
    entries share is added to their group; an entry is in one group only, so a sentence
    that some of a group's entries share with another entry is left in their own text.
    With ``by_text`` a sentence is shared when two different texts state it, and every
    entry whose text states it is in the group."""
    if not isinstance(entries, list):
        return []
    seen: dict[tuple[str, str], tuple[str, list[int]]] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        for f in fields:
            for s in _sentences(e.get(f)):
                if is_long(s):
                    first, members = seen.setdefault((kind(e), sentence_key(s)), (s, []))
                    if i not in members:
                        members.append(i)
    groups: list[dict[str, Any]] = []
    placed: dict[int, dict[str, Any]] = {}
    for sentence, members in seen.values():
        texts = {_text_of(entries[i], fields) for i in members}
        if len(texts if by_text else members) < 2:
            continue
        group = placed.get(members[0])
        if group is not None and group["indexes"] == members:
            group["sentences"].append(sentence)
        elif not any(i in placed for i in members):
            group = {"sentences": [sentence], "indexes": list(members)}
            groups.append(group)
            placed.update({i: group for i in members})
    return groups


def shared_layout(
    entries: Sequence[Any], groups: Any, list_name: str
) -> list[tuple[list[str], list[Any]]]:
    """How a list prints with its shared sentences: blocks of ``(sentences, entries)``.
    A group's block stands where its first entry did, its sentences stated once, then
    each of its entries with its own text (the shared sentences taken out); every other
    entry is a block of its own with no sentences. Entries are never merged."""
    fields = _LISTS.get(list_name, ((), _label(""), False))[0]
    records = [
        g
        for g in groups or []
        if isinstance(g, dict)
        and g.get("list") == list_name
        and isinstance(g.get("indexes"), list)
        and all(isinstance(i, int) and 0 <= i < len(entries) for i in g["indexes"])
    ]
    by_first = {g["indexes"][0]: g for g in records if g["indexes"]}
    later = {i for g in records for i in g["indexes"][1:]}
    blocks: list[tuple[list[str], list[Any]]] = []
    for i, entry in enumerate(entries):
        if i in by_first:
            group = by_first[i]
            sentences = [str(s) for s in group.get("sentences") or []]
            members = []
            for j in group["indexes"]:
                member = entries[j]
                if isinstance(member, dict):
                    member = {
                        **member,
                        **{
                            f: without_sentences(member[f], sentences)
                            for f in fields
                            if isinstance(member.get(f), str)
                        },
                    }
                members.append(member)
            blocks.append((sentences, members))
        elif i not in later:
            blocks.append(([], [entry]))
    return blocks


def _restated_dropped(notes: list[Any]) -> list[Any]:
    """App-layer notes with each sentence a column-default note states dropped from the
    other notes, word for word, unless nothing of a note's own would be left."""
    stated = {
        sentence_key(s)
        for n in notes
        if isinstance(n, dict) and n.get("feature") == _COLUMN_DEFAULT
        for s in _sentences(n.get("recommendation"))
        if is_long(s)
    }
    out = notes
    for i, n in enumerate(notes):
        if not isinstance(n, dict) or n.get("feature") == _COLUMN_DEFAULT:
            continue
        sentences = _sentences(n.get("recommendation"))
        kept = [s for s in sentences if sentence_key(s) not in stated]
        if kept and len(kept) < len(sentences):
            out = list(out) if out is notes else out
            out[i] = {**n, "recommendation": " ".join(kept)}
    return out


def _grouped_design(summary: dict[str, Any]) -> dict[str, Any]:
    if GROUPS_KEY in summary:
        return summary
    out = summary
    notes = summary.get("app_layer_notes")
    if isinstance(notes, list):
        dropped = _restated_dropped(notes)
        if dropped is not notes:
            out = {**out, "app_layer_notes": dropped}
    records: list[dict[str, Any]] = []
    for name, (fields, kind, by_text) in _LISTS.items():
        for group in sentence_groups(out.get(name), fields, kind, by_text):
            records.append({"list": name, **group})
    if records:
        out = {**out, GROUPS_KEY: records}
    return out


def grouped_report(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with each design summary's shared sentences recorded once
    (``shared_sentence_groups``); the entries themselves are left as they are. A report
    with nothing to record comes back unchanged (the same object)."""
    designs = report.get("schema_designs")
    if not isinstance(designs, dict):
        return report
    new = {e: _grouped_design(s) if isinstance(s, dict) else s for e, s in designs.items()}
    if all(new[e] is designs[e] for e in designs):
        return report
    return {**report, "schema_designs": new}
