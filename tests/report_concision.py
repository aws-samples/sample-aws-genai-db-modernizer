"""Concision checks on the engineering report (#1170).

A rubric judge scored the engineering report's concision 2 of 5 because the
deterministic text repeated itself: the same risk sentence once per table, a
wave gate repeating a table list its own section had just given, raw labels
such as "**unsupported pattern** (`f31a66f5`)" and "(was tied)". These helpers
find each kind, so the unit tests (on a saved model run) and the e2e tests (on
every sample) keep them fixed.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# A sentence at least this many words long is a statement worth saying once.
MIN_WORDS = 10

_FENCE = re.compile(r"^(```|~~~).*?^\1\s*$", re.M | re.S)
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_NUMBER = re.compile(r"\d[\d,.]*")


def md_sections(md: str, level: int) -> list[tuple[str, str]]:
    """``(heading, body)`` for each heading of exactly ``level`` hashes; a body
    runs to the next heading of that level or higher."""
    marks = list(re.finditer(rf"^{'#' * level} (.+)$", md, re.M))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(md)
        nxt = re.search(rf"^#{{1,{level}}} ", md[m.end() : end], re.M)
        out.append((m.group(1).strip(), md[m.end() : m.end() + nxt.start() if nxt else end]))
    return out


def section(md: str, prefix: str, level: int = 2) -> str:
    """The body of the first heading that starts with ``prefix``, or ""."""
    return next((body for h, body in md_sections(md, level) if h.startswith(prefix)), "")


def repeated_sentences(text: str, min_words: int = MIN_WORDS) -> list[tuple[str, int]]:
    """Each sentence of at least ``min_words`` words that ``text`` states more than
    once, with how often. Code spans (table names, ids) and numbers are dropped
    before comparing, so "- `wp_posts`: 28 reads ..." and "- `wp_users`: 1 read
    ..." are the same sentence; table rows and code blocks are not prose."""
    prose = "\n".join(
        line for line in _FENCE.sub("", text).splitlines() if not line.startswith("|")
    )
    seen: Counter[str] = Counter()
    for raw in re.split(r"(?<=[.!?])\s+|\n", prose):
        sentence = _NUMBER.sub("#", _CODE_SPAN.sub("", raw))
        sentence = re.sub(r"\s+", " ", sentence).strip(" -*:,")
        if sum(1 for w in sentence.split() if re.search(r"[A-Za-z]", w)) >= min_words:
            seen[sentence] += 1
    return [(s, n) for s, n in seen.items() if n > 1]


def known_query_ids(report: Any) -> set[str]:
    """Every query id ``report.json`` holds (``query_id``, ``query_ids``,
    ``source_query_ids``, ``representative_id`` at any depth)."""
    found: set[str] = set()
    if isinstance(report, dict):
        for key, value in report.items():
            if key in ("query_id", "representative_id") and isinstance(value, str):
                found.add(value)
            elif key in ("query_ids", "source_query_ids") and isinstance(value, list):
                found |= {v for v in value if isinstance(v, str)}
            else:
                found |= known_query_ids(value)
    elif isinstance(report, list):
        for value in report:
            found |= known_query_ids(value)
    return found


def bare_ids(md: str, query_ids: set[str]) -> list[str]:
    """The report's query ids (or their 8-character prefixes) that appear outside
    code spans and code blocks. Only known query ids count, so a hash or UUID a
    designer writes in its own prose is not one."""
    prose = _CODE_SPAN.sub("", _FENCE.sub("", md))
    words = set(re.findall(r"[0-9A-Za-z_-]+", prose))
    prefixes = {q[:8] for q in query_ids if len(q) >= 8}
    return sorted(w for w in words if w in query_ids or w in prefixes)


def raw_labels(md: str) -> list[str]:
    """Labels that read as machine output: the generic "unsupported pattern"
    label in bold, and an appendix heading's bare "(moved)", "(was tied)" or
    "(unresolved tie)" with its engine path after a colon."""
    found = re.findall(r"\*\*unsupported pattern\*\*", md)
    found += re.findall(
        r"^### .*\((?:moved|was tied|unresolved tie|\d+ unresolved ties?)[^)]*\): .*$", md, re.M
    )
    found += re.findall(r"^### .*\): on \S.*$", md, re.M)
    return found
