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


# #1410: a statement this long is said once in the whole report.
REPORT_MIN_WORDS = 15
# The per-query routing record: each query's own trace, one entry per query, where
# two queries that took the same step read the same by design (like table rows).
PER_QUERY_SECTIONS = ("Appendix: per-query routing decisions",)


def repeated_statements(
    md: str, min_words: int = REPORT_MIN_WORDS, skip: tuple[str, ...] = PER_QUERY_SECTIONS
) -> list[tuple[str, int]]:
    """Each sentence of at least ``min_words`` words the whole report states more than
    once, with how often (#1410). Unlike ``repeated_sentences`` the text is compared
    as written (numbers and code spans count), so two rows that differ in their table
    or figure are not a repeat, but a list printed in two sections is: the same
    sentence as a risk's detail and as a trade-off's bold title counts twice. Table
    rows, code blocks, headings and the ``skip`` sections are not compared."""
    seen: Counter[str] = Counter()
    for line in _prose_lines(md, skip):
        # "- **Title.**: impact" ends a sentence at ".:" too.
        for raw in re.split(r"(?<=[.!?])[:;]?\s+", line.replace("**", "")):
            sentence = re.sub(r"\s+", " ", raw).strip(" -*:,.;")
            if len(sentence.split()) >= min_words:
                seen[sentence] += 1
    return [(s, n) for s, n in seen.items() if n > 1]


def repeated_paragraphs(
    md: str, min_words: int = REPORT_MIN_WORDS, skip: tuple[str, ...] = PER_QUERY_SECTIONS
) -> list[tuple[str, int]]:
    """Each paragraph or list item of at least ``min_words`` words the whole report
    prints more than once, word for word, with how often (#1410). Table rows, code
    blocks, headings and the ``skip`` sections are not compared."""
    seen: Counter[str] = Counter()
    for line in _prose_lines(md, skip):
        text = re.sub(r"\s+", " ", line).strip(" -*")
        if len(text.split()) >= min_words:
            seen[text] += 1
    return [(s, n) for s, n in seen.items() if n > 1]


def _prose_lines(md: str, skip: tuple[str, ...]) -> list[str]:
    body = re.sub(r"\A---\n.*?\n---\n", "", md, flags=re.S)
    parts = re.split(r"^(?=## )", body, flags=re.M)
    text = "\n".join(p for p in parts if not any(p.startswith(f"## {h}") for h in skip))
    return [
        line.strip()
        for line in _FENCE.sub("", text).splitlines()
        if line.strip() and not line.strip().startswith(("|", "#"))
    ]


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


# #1220: what one section lists and another lists again.
_REGISTER_RISK = re.compile(r"^- \*\*(RISK-\d+)\*\*")
_REGISTER_MITIGATION = re.compile(r"^\s+- Mitigation: (.*)$")
_SEVERE_STRATEGY = re.compile(r"^- (RISK-\d+) \\\(.*?\\\): (.*)$")
_CLOSE_CALL_PAIR = re.compile(r"(?:keeps|takes) [^;.]*?close calls?[^;.]*? over ")


def cross_section_repeats(md: str) -> dict[str, int]:
    """How often the engineering report lists an item a second time in another section
    (#1220), by kind:

    - ``severe_mitigation``: a Mitigation strategies line that repeats the mitigation its
      risk's Risk register entry prints;
    - ``close_call_pairs``: an engine pair the register's close-call risk lists while the
      Close calls table has a row for each;
    - ``open_tie_lines``: an appendix runner-up or unresolved-tie line that restates an open
      tie Pending engine choices states;
    - ``consolidation_reason``: an appendix step restating the reality check's reason the
      appendix points to Migration trade-offs for;
    - ``aurora_consolidation_scores``: a Queries on Aurora by table row with the reality
      check's per-query scores, which the appendix gives per query;
    - ``raw_utility_reason``: a row with the utility pin's raw label.
    """
    register = section(md, "Risk register")
    appendix = section(md, "Appendix: per-query routing decisions")
    aurora = section(md, "Queries on Aurora by table")
    mitigations: dict[str, str] = {}
    risk = None
    for line in register.splitlines():
        if m := _REGISTER_RISK.match(line):
            risk = m.group(1)
        elif (m := _REGISTER_MITIGATION.match(line)) and risk:
            mitigations[risk] = m.group(1).strip().rstrip(".")
    severe = 0
    for line in section(md, "Mitigation strategies").splitlines():
        m = _SEVERE_STRATEGY.match(line)
        if m and mitigations.get(m.group(1)) and mitigations[m.group(1)] in m.group(2):
            severe += 1
    ties = 0
    if section(md, "Pending engine choices"):
        for line in appendix.splitlines():
            ties += line.startswith("- Runner-up:") and "until the customer chooses" in line
            ties += line.startswith("- Unresolved tie:") and "Pending engine choices" not in line
    pointer = 'under "Migration trade-offs" (Reality check)' in appendix
    return {
        "severe_mitigation": severe,
        "close_call_pairs": (
            len(_CLOSE_CALL_PAIR.findall(register)) if section(md, "Close calls") else 0
        ),
        "open_tie_lines": ties,
        "consolidation_reason": (
            appendix.count("which adds too little over the engines kept") if pointer else 0
        ),
        "aurora_consolidation_scores": aurora.count("(no unique value:"),
        "raw_utility_reason": aurora.count("utility/metadata statement"),
    }
