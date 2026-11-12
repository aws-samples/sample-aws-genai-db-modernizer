# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What an older stored facts block reads back as (#399, #2162)."""

from __future__ import annotations

from typing import Any

from src.shared.cache_role import cache_role_sentence


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
    """``with_cache_role_sentence``, and a migration target's row written before #2162
    gains ``kept_in_sync_counted_under``: empty for a wave whose engines read no
    outside-owned table in common, its other figures as stored."""
    out = with_cache_role_sentence(facts)
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
