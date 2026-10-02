"""Centralized output escaping for customer-facing deliverables.

Threat model finding R3 (``docs/security/atx-threat-model.md``): the HTML, SVG and
Markdown deliverables interpolate customer-derived and LLM-derived strings — table
and column names, raw SQL ``query_text``, risk prose, trade-off text, LLM summaries.
When escaping is applied ad hoc per field, a single missed interpolation point is an
injection vector into a document a human downloads and opens. Both customer input
and LLM output are untrusted here (R1/E2): the LLM sees customer SQL, so its output
must be rendered as escaped document content, never as trusted markup.

This module is the one place escaping lives, so a new field is safe by default: the
renderers call a context-appropriate helper rather than reaching for ``html.escape``
with whatever arguments each call site happened to pass. The context matters — the
correct neutralization differs between an HTML text node, an HTML/SVG attribute
value, a Markdown table cell, a Markdown code span, and a Mermaid node label — and
getting it wrong (e.g. ``html.escape`` with the default ``quote=False`` inside an
attribute) reintroduces the very gap R3 describes.

None of these helpers emit markup; they only neutralize the input so the *caller's*
surrounding markup is the only markup in the output.
"""

from __future__ import annotations

import html as _html
from typing import Any

# ---------------------------------------------------------------------------
# HTML / SVG
#
# HTML and SVG share the same character-reference escaping. They are kept as
# separate names so call sites read in terms of the context they render into and
# so the two could diverge later without touching callers.
# ---------------------------------------------------------------------------


def html_text(value: Any) -> str:
    """Escape a value for an HTML/SVG **text node** (between tags).

    Neutralizes ``& < >``. Quotes are left alone — they are not special in text
    content — which keeps prose readable. Use :func:`html_attr` for anything that
    lands inside a quoted attribute value.
    """
    return _html.escape(str(value), quote=False)


def html_attr(value: Any) -> str:
    """Escape a value for a **quoted HTML/SVG attribute** value.

    Neutralizes ``& < >`` and both quote characters, so the value cannot close the
    attribute it sits in regardless of whether the attribute is single- or
    double-quoted. This is the correct helper for ``<meta content="...">``,
    ``data-*`` attributes, ``<span style="...">`` fills and the like — the default
    ``html.escape(quote=False)`` used at some call sites did *not* escape quotes and
    so could break out of an attribute (R3).

    Raw CR/LF are collapsed to a space so the value stays on one line: a newline in an
    attribute value is legal but splits the tag across physical lines, which is both
    ugly and a hazard for any line-oriented consumer of the document.
    """
    s = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _html.escape(s, quote=True)


def html_comment(value: Any) -> str:
    """Escape a value embedded inside an ``<!-- ... -->`` HTML comment.

    A comment is not parsed as markup, so an embedded ``<script>`` is inert; the one
    sequence that matters is ``--``, which can terminate the comment early (and ``-->``
    outright). Double dashes are collapsed to a single dash. As defense-in-depth the
    angle brackets are also neutralized, so a value that leaks out of a malformed
    comment cannot become live markup and the comment reads cleanly in a viewer.
    """
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    while "--" in s:
        s = s.replace("--", "-")
    return s


# SVG uses the same rules as HTML for text and attribute contexts.
svg_text = html_text
svg_attr = html_attr


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
    """
    s = str(value)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
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
