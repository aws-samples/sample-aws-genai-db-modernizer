# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Null-safe reads of schema design fields for synthesis (#757, #1094).

Synthesis reads ``schema_output.json`` as a raw dict, without validating it against
the engine's contract (``synthesis_data._read_artifact``). ``dict.get(key, [])``
returns ``None`` when the key is present with a null value, so every read of a
design's list or mapping field goes through these helpers: a missing key and an
explicit null both read as the empty default.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def field_list(obj: Mapping[str, Any] | None, key: str) -> list:
    """``obj[key]``, or ``[]`` when ``obj`` or the key is missing or null."""
    value = (obj or {}).get(key)
    return [] if value is None else value


def field_dict(obj: Mapping[str, Any] | None, key: str) -> dict:
    """``obj[key]``, or ``{}`` when ``obj`` or the key is missing or null."""
    value = (obj or {}).get(key)
    return {} if value is None else value
