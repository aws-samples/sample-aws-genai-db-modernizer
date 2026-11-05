# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The id of the routine a collected statement ran in (#1891).

SQL Server's ``sys.dm_exec_sql_text(sql_handle).objectid`` names the procedure, function
or trigger a cached statement belongs to; both SQL Server collectors (the offline script's
``query_routines`` section and the live ``SQLServerRemoteCollector``) read it as a schema
and a name, and record it on ``QueryPattern.parent_routine_ids`` in one form.
"""

from __future__ import annotations


def routine_id(schema: object, name: object) -> str | None:
    """``<schema>.<name>`` (``dbo`` when the schema is missing), the form the SQL Server
    collector gives its procedures and triggers; None with no name."""
    name = str(name or "").strip()
    if not name:
        return None
    return f"{str(schema or '').strip() or 'dbo'}.{name}"
