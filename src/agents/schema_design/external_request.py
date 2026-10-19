"""The single-pass external schema design request, written so Read can page it (#1103).

Grouped inputs (``input_group_<G>.json``) are capped at ``MAX_GROUP_INPUT_CHARS``
and split until they fit. DocumentDB, OpenSearch and ElastiCache have no grouped
external path (only ``/design-schema-dynamodb`` runs ``--split``), so their one
request file cannot be split. Instead it is written the way a group input is,
one record per line, and the exact Read pages (``offset``/``limit``) that cover
it are returned for the ``--llm-mode external`` status line. Nothing is dropped:
a request over the group budget is simply read in more pages, and the status
line says so (``oversized``).
"""

from __future__ import annotations

import json
from typing import Any

from src.agents.schema_design.group_input import (
    LINE_WIDTH,
    _split_text,
    read_pages,
    render_group_input,
)
from src.agents.schema_design.group_splitter import MAX_GROUP_INPUT_CHARS, MAX_GROUP_INPUT_PAGES


def _with_text_lines(value: Any) -> Any:
    """``value`` with ``<field>_lines`` next to every string field too long for one line.

    Read cuts a line over 2000 characters, so a long text (a query text in
    ``queries.query_patterns``, ``cache_invalidation_context.write_queries`` or
    anywhere else in any engine's request) is also given as short lines, as in
    a group input. The original field is kept. Walks dicts and lists; the value
    given is not modified.
    """
    if isinstance(value, list):
        return [_with_text_lines(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        out[key] = _with_text_lines(item)
        lines_key = f"{key}_lines"
        if (
            isinstance(item, str)
            and not key.endswith("_lines")
            and lines_key not in value
            and len(json.dumps(item, ensure_ascii=False)) > LINE_WIDTH
        ):
            out[lines_key] = _split_text(item)
    return out


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
