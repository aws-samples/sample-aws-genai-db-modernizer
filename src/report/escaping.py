# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Centralized output escaping for customer-facing deliverables.

Threat model finding R3 (``docs/security/atx-threat-model.md``): the Markdown
deliverable (and the executive report PDF, which escapes its own paragraph markup) interpolates
customer-derived and LLM-derived strings — table
and column names, raw SQL ``query_text``, risk prose, trade-off text, LLM summaries.
When escaping is applied ad hoc per field, a single missed interpolation point is an
injection vector into a document a human downloads and opens. Both customer input
and LLM output are untrusted here (R1/E2): the LLM sees customer SQL, so its output
must be rendered as escaped document content, never as trusted markup.

This module is the one place escaping lives, so a new field is safe by default: the
renderers call a context-appropriate helper rather than reaching for ``html.escape``
with whatever arguments each call site happened to pass. The context matters — the
correct neutralization differs between a Markdown table cell, flowing text, a code
span, a front-matter value and a Mermaid node label — and getting it wrong
reintroduces the very gap R3 describes.

None of these helpers emit markup; they only neutralize the input so the *caller's*
surrounding markup is the only markup in the output.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

# ---------------------------------------------------------------------------
# Markdown
#
# The engineering report interpolates untrusted strings into a GitHub-flavored
# Markdown document with tables, inline code spans and Mermaid fences. Markdown has
# no single escape function; each of these contexts needs its own neutralization.
# ---------------------------------------------------------------------------


def md_cell(value: Any) -> str:
    """Escape a value for a **Markdown table cell**.

    A raw ``|`` starts a new cell and a newline ends the row, so an unescaped table
    name or query fragment silently corrupts the table structure. Pipes are
    backslash-escaped (the GFM-defined escape inside a cell) and CR/LF are collapsed
    to a space so the value stays within its one cell. Also neutralizes inline HTML
    (``< >``) — most Markdown renderers pass raw HTML through, so a ``<script>`` in a
    table name would otherwise be live markup in the rendered document.
    """
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("\\", "\\\\").replace("|", "\\|")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    return s


def md_text(value: Any) -> str:
    """Escape a value for **Markdown flowing text** (list items, prose lines).

    Not inside a table, so pipes are fine, but newlines still have to be tamed so a
    single logical value cannot inject extra list items or break the block, and
    inline HTML is neutralized for the same reason as :func:`md_cell`.

    Link/image syntax and emphasis markers (``[ ] ( ) ! * _``) are backslash-escaped
    (#211): without this, untrusted text containing ``![](http://evil/x.png)`` or
    ``[click](http://evil)`` renders as a live remote image fetch or a clickable
    link, and ``*``/``_`` turn plain words into emphasis. The backslash itself is
    escaped first, so a value already containing one is not mistaken for part of
    one of these escapes once the special characters are escaped too. Backticks and
    pipes are untouched here -- not special in flowing text, and :func:`md_code` /
    :func:`md_cell` are the right helpers for the contexts where they are.
    """
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("\\", "\\\\")
    for ch in "[]()!*_":
        s = s.replace(ch, "\\" + ch)
    return s


def md_code(value: Any) -> str:
    """Escape a value rendered inside an **inline code span** (`` `...` ``).

    A backtick inside the value would close the span early and let the remainder
    render as live Markdown. Backticks and the backslash are stripped rather than
    escaped: backslash escaping does not apply inside a code span, so the only safe
    move is to remove the characters that can terminate it. Newlines are collapsed
    so the span stays on one line. The renderer still supplies the surrounding
    backticks; this only sanitizes the content between them.
    """
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("`", "").replace("\\", "")
    return s


# A word as the report's id checks read one (#1078 review): letters, digits, "_" and "-".
_ID_WORD = re.compile(r"[0-9A-Za-z_-]+")
_TICKS = re.compile(r"`+")


def _code_spans(text: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of each code span in ``text``: a run of n backticks opens
    one the next run of exactly n closes, as in Markdown; an unpaired run is text."""
    spans: list[tuple[int, int]] = []
    runs = list(_TICKS.finditer(text))
    i = 0
    while i < len(runs):
        width = len(runs[i].group(0))
        close = next((j for j in range(i + 1, len(runs)) if len(runs[j].group(0)) == width), None)
        if close is None:
            i += 1
            continue
        spans.append((runs[i].start(), runs[close].end()))
        i = close + 1
    return spans


def md_text_with_ids(value: Any, query_ids: Collection[str]) -> str:
    """:func:`md_text`, with each of the report's query ids (full, or its 8-character
    prefix, as the report's tables name one) that ``value`` holds as a whole word set
    as a code span, as the report prints every other query id. Only the ids the
    report itself holds count, so a hash or UUID in model prose stays prose; an id
    already in a code span (the schema-design gate writes every cited id as one, #1321)
    is left as written, never quoted twice (#1981); every other part of ``value`` is
    escaped exactly as :func:`md_text` escapes it."""
    text = str(value)
    known = set(query_ids) | {q[:8] for q in query_ids if len(q) >= 8}
    if not known:
        return md_text(text)
    spans = _code_spans(text)
    out: list[str] = []
    last = 0
    for match in _ID_WORD.finditer(text):
        quoted = any(start < match.start() and match.end() < end for start, end in spans)
        if match.group(0) in known and not quoted:
            out += [md_text(text[last : match.start()]), f"`{md_code(match.group(0))}`"]
            last = match.end()
    out.append(md_text(text[last:]))
    return "".join(out)


def md_yaml_value(value: Any) -> str:
    """Escape a value as a **double-quoted YAML scalar** for Markdown front matter.

    Front-matter values such as the database name are customer-derived. Unquoted,
    a value containing ``:``, ``#`` or a leading indicator character can be misread
    as YAML structure; the caller wraps this in double quotes and this collapses
    newlines and escapes any embedded double quote and backslash so the value cannot
    terminate its own quoting. Does not include the surrounding quotes.
    """
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return s


def mermaid_label(value: Any) -> str:
    """Escape a value used as a **Mermaid node label** inside quotes.

    Mermaid node text such as ``T0["<value>"]`` breaks if the value contains a double
    quote (closes the label), a square bracket (ends the node shape) or a newline
    (ends the statement) — any of which turns customer text into Mermaid syntax.
    Mermaid also renders label text as HTML by default (``htmlLabels``), so a ``<``
    is an injection vector on its own. Angle brackets and quotes become the HTML
    entities Mermaid renders literally, brackets become parentheses, and newlines
    collapse to a space.
    """
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("&", "&amp;")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace('"', "&quot;")
    s = s.replace("[", "(").replace("]", ")")
    return s
