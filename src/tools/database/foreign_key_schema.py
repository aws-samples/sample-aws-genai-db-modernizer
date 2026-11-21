# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The schema a foreign key's referenced table is in, as the collectors read it (#2531).

Every collection script and live collector reads it from its engine's catalog
(``KEY_COLUMN_USAGE.REFERENCED_TABLE_SCHEMA``, the namespace of ``pg_constraint.confrelid``,
``sys.schemas`` of ``sys.foreign_keys.referenced_object_id``, ``ALL_CONSTRAINTS.R_OWNER``)
as ``referenced_schema_name``. A collection written before #2531 has none, and the key
keeps the bare-name rule (``ddl_generator.referenced_table``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def referenced_schema(row: Mapping[str, Any], *, lower: bool = False) -> dict[str, str]:
    """``{"referenced_schema": <name>}`` from a foreign key row, or ``{}`` when the row has
    none (missing, blank, or the client's NULL). ``lower``: the name folded, as the Oracle
    collectors fold every name."""
    value = row.get("referenced_schema_name")
    if value is None:
        return {}
    text = str(value).strip()
    if not text or text.upper() == "NULL":
        return {}
    return {"referenced_schema": text.lower() if lower else text}
