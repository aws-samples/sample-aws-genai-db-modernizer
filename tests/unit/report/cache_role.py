# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What an older stored facts block reads back as (#399, #2162, #2321)."""

from __future__ import annotations

from typing import Any

from src.shared.cache_role import cache_role_sentence
from src.shared.report_facts import with_owner_line_risk


def with_cache_role_sentence(facts: dict[str, Any]) -> dict[str, Any]:
    """``facts`` as ``report_facts`` reads it back: a ``cache_layer`` block written
    before #399 gains its ``role_sentence``, its figures as stored."""
    cache = facts.get("cache_layer")
    if not isinstance(cache, dict) or cache.get("role_sentence"):
        return facts
    return {
        **facts,
        "cache_layer": {**cache, "role_sentence": cache_role_sentence(cache.get("engine"))},
    }


def as_read_back(facts: dict[str, Any]) -> dict[str, Any]:
    """``with_cache_role_sentence``, a ``table_sources`` block written before #2321 gains
    its ``shared_risk_sentence``, and a migration target's row written before #2162
    gains ``kept_in_sync_counted_under``: empty for a wave whose engines read no
    outside-owned table in common, its other figures as stored."""
    out = with_cache_role_sentence(facts)
    sources = out.get("table_sources")
    if isinstance(sources, dict) and not sources.get("shared_risk_sentence"):
        # A table_sources block written before #2321 gains its shared risk sentence.
        out = {**out, "table_sources": with_owner_line_risk(sources, out.get("table_owner_lines"))}
    engines = out.get("engines")
    if not isinstance(engines, list):
        return out
    return {
        **out,
        "engines": [
            (
                {**e, "kept_in_sync_counted_under": {}}
                if isinstance(e, dict)
                and isinstance(e.get("engine_source_tables"), int)
                and e.get("kept_in_sync_counted_under") is None
                else e
            )
            for e in engines
        ],
    }
