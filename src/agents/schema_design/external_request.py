# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The single-pass external schema design request, written so Read can page it (#1103).

Grouped inputs (``input_group_<G>.json``) are capped at ``MAX_GROUP_INPUT_CHARS``
and split until they fit. DocumentDB, OpenSearch and ElastiCache have no grouped
external path (only ``/design-schema-dynamodb`` runs ``--split``), so their one
request file cannot be split. Instead it is written the way a group input is,
one record per line, and the exact Read pages (``offset``/``limit``) that cover
it are returned for the ``--llm-mode external`` status line. Nothing is dropped:
a request over the group budget is simply read in more pages, and the status
line says so (``oversized``). A text field gets a ``<field>_lines`` copy only
when its own line is too long for Read to return whole (#2463).
"""

from __future__ import annotations

import json
from typing import Any

from src.agents.schema_design.group_input import (
    _split_text,
    read_pages,
    render_group_input,
)
from src.agents.schema_design.group_splitter import MAX_GROUP_INPUT_CHARS, MAX_GROUP_INPUT_PAGES

# Read cuts a line longer than this many characters.
READ_LINE_CHARS = 2000


def _with_text_lines(value: Any, depth: int = 1) -> Any:
    """``value`` with ``<field>_lines`` next to every string field Read would cut.

    Read cuts a line over ``READ_LINE_CHARS`` characters, so a text whose line in the
    rendered file is that long (a query text in ``queries.query_patterns``,
    ``cache_invalidation_context.write_queries`` or anywhere else in any engine's
    request) is also given as short lines, as in a group input. The original field is
    kept. A text whose line Read returns whole gets no copy (#2463): the model reads
    the original. ``depth`` is how many containers enclose the fields of ``value``, so
    the line's indentation (two spaces per level, as ``render_group_input`` writes it)
    is counted. Walks dicts and lists; the value given is not modified.
    """
    if isinstance(value, list):
        return [_with_text_lines(v, depth + 1) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        out[key] = _with_text_lines(item, depth + 1)
        lines_key = f"{key}_lines"
        if (
            isinstance(item, str)
            and not key.endswith("_lines")
            and lines_key not in value
            and _field_line_chars(key, item, depth) >= READ_LINE_CHARS
        ):
            out[lines_key] = _split_text(item)
    return out


def _field_line_chars(key: str, item: str, depth: int) -> int:
    """The length of the ``"key": "item",`` line at ``depth``, in UTF-16 code units: a
    character outside the Basic Multilingual Plane is one code point but two units, and
    the larger count is the safe one whichever Read counts."""
    rendered = json.dumps(key, ensure_ascii=False) + ": " + json.dumps(item, ensure_ascii=False)
    return 2 * depth + len(rendered.encode("utf-16-le", "surrogatepass")) // 2 + 1


def render_external_request(request: dict) -> tuple[str, dict]:
    """The request file text and its paging summary.

    Returns ``(text, summary)``; ``summary`` has ``input_chars``, ``input_pages``
    (the Read pages covering every line of ``text``), ``input_page_budget``
    (``MAX_GROUP_INPUT_PAGES``) and ``oversized`` (more pages than a group input
    may have, or more characters than ``MAX_GROUP_INPUT_CHARS``).
    """
    text = render_group_input(_with_text_lines(request))
    pages = read_pages(text)
    return text, {
        "input_chars": len(text),
        "input_pages": pages,
        "input_page_budget": MAX_GROUP_INPUT_PAGES,
        "oversized": len(text) > MAX_GROUP_INPUT_CHARS or len(pages) > MAX_GROUP_INPUT_PAGES,
    }
