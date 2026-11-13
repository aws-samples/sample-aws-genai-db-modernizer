# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep a SQL Server ``(max)`` type in the column's type (#2219).

``sys.columns.max_length`` is -1 for ``varchar(max)``, ``nvarchar(max)`` and
``varbinary(max)``, and the collector contract has no -1 (``max_length`` is ``ge=0``,
absent means unbounded), so the collectors turn it into an absent length. An absent
length is also what a trimmed or hand-built collection has, and the Aurora draft must
not read those as ``(max)`` (ADR-028 Decision 1: never a silent guess). So where the
collector still knows the -1 it writes the type as ``nvarchar(max)``:

* live (``SQLServerRemoteCollector.collect_columns``): ``max_length`` is -1;
* offline (``scripts/collect-sqlserver.sql``): the script writes NULL for -1, and
  ``FOR JSON PATH`` leaves a NULL out, so the row has no ``max_length``.
  ``sys.columns.max_length`` is never NULL, so an absent or null value means -1 there.

The bare name stays in ``data_type`` for the normalizers; the suffixed type goes to
``column_type``, which the offline builder (``mysql_collector._native_type``) promotes to
the contract's ``data_type``, and which the live builder reads the same way. Every
consumer that reads the type name strips the parentheses first (the gap rules'
``normalized_data_type``, DMS sizing's ``_base_type``, ``validation_key``), so they see
``nvarchar`` as before. A collection saved before this change has no suffix, and its
(max) columns take the flagged no-length path in the draft DDL.
"""

from __future__ import annotations

from typing import Any

#: The SQL Server types that take ``(max)``.
MAX_TYPES = frozenset({"varchar", "nvarchar", "varbinary"})


def max_type(data_type: Any, max_length: Any, *, absent_is_max: bool) -> str | None:
    """``nvarchar(max)`` when the row says the column is (max), else None.

    ``max_length`` -1 always means (max); with ``absent_is_max`` (an offline row from
    collect-sqlserver.sql) an absent or null length does too."""
    base = str(data_type or "").strip().lower()
    if base not in MAX_TYPES:
        return None
    if max_length is None:
        return f"{base}(max)" if absent_is_max else None
    try:
        return f"{base}(max)" if int(max_length) == -1 else None
    except (TypeError, ValueError):
        return None


def sqlserver_max_column(row: dict) -> dict:
    """An offline collect-sqlserver.sql column row, with ``column_type`` set to the
    ``(max)`` type when its length is (max); any other row is returned unchanged."""
    if row.get("column_type"):
        return row
    typed = max_type(row.get("data_type"), row.get("max_length"), absent_is_max=True)
    return {**row, "column_type": typed} if typed else row
