# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The columns a collected unique index makes a key (#2381): one rule for every reader.

An index with an expression key (``has_expressions``) makes no key of its plain
``columns``: they are only part of its keys, so a unique ``(tenant_id, lower(name))`` does
not make ``tenant_id`` unique, nor give it one distinct value per row, nor make a foreign
key from it 1:1. Routing's key evidence (``access_pattern._table_keys``), the
partition-load facts (``partition_load.key_distinct_values``), the DynamoDB relationship
detection (``dynamodb_analysis_tools.detect_relationships``) and the redesign's
composite keys (``redesign_findings.RedesignModel``) all read it from here. ADR-031
Decision 14 Amendment 5. Lives in ``src.shared`` because the facts layer may not import
``src.agents`` (ADR-032 Decision 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def unique_key_columns(index: Any) -> tuple[str, ...]:
    """The lower-case columns, in key order, that ``index`` (a collector index mapping)
    makes a unique key; empty when it makes none: not unique and not the primary key, or
    with an expression key."""
    if not isinstance(index, Mapping):
        return ()
    if not (index.get("is_unique") or index.get("is_primary")):
        return ()
    if index.get("has_expressions"):
        return ()
    return tuple(str(c).lower() for c in index.get("columns") or [] if c)
