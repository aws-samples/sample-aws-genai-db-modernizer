# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What an older stored facts block reads back as (#399)."""

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
