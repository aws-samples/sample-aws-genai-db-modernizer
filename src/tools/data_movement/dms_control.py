# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where step 1's DMS task keeps its control tables on Aurora (#2133).

The task settings name ``ControlTablesSettings.ControlSchema`` explicitly, on both target
engines, and every failure query step 1 prints reads that schema: this module is the one
place its name is written. ``ControlSchema`` is "the database schema name for the control
tables for the AWS DMS target"; left empty, they go to "the default location", ``public`` on
PostgreSQL and ``awsdms_control`` on MySQL (AWS DMS User Guide, "Control table task
settings"). With it empty, the DMS 3.6.1 validator on an Aurora MySQL target looked for its
failure table under the target database and could not create it (``Cannot create
ValidationFailure table``), so the failures it counted were recorded nowhere.

``awsdms_control`` is MySQL's default name, so a MySQL target keeps the database the DMS guide's
target prerequisites already grant (``GRANT ALL PRIVILEGES ON awsdms_control.*``), and a
PostgreSQL target gets a schema of that name in the target database. The guide does not say
whether DMS creates that schema or database itself, so step 1's DDL creates it (``IF NOT
EXISTS``) and the DMS user's grants cover it, which works either way.

The control tables kept there: the apply exceptions (``awsdms_apply_exceptions``, always on a
database target), and for a full load and CDC task the replication status
(``awsdms_status``), the suspended tables (``awsdms_suspended_tables``), the replication
history (``awsdms_history``) and the validation failures (``awsdms_validation_failures_v1``;
on a PostgreSQL target ``awsdms_validation_failures_v2`` from DMS 3.6.1, AWS DMS User Guide,
"AWS DMS data validation"). The heartbeat table is on the source, not here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

CONTROL_SCHEMA = "awsdms_control"


def control_tables_settings() -> dict[str, str]:
    """The task settings' ``ControlTablesSettings``."""
    return {"ControlSchema": CONTROL_SCHEMA}


def failure_table(name: str) -> str:
    """A validation failure table, qualified by the control schema."""
    return f"{CONTROL_SCHEMA}.{name}"


def task_control_schema(task: Mapping[str, Any]) -> str | None:
    """The live task's ``ControlSchema``: ``""`` when its settings set none (a stack rendered
    before #2133), None when DMS gives no settings to read."""
    try:
        settings = json.loads(task.get("ReplicationTaskSettings") or "null")
    except ValueError:
        return None
    if not isinstance(settings, dict):
        return None
    control = settings.get("ControlTablesSettings") or {}
    value = control.get("ControlSchema") if isinstance(control, dict) else None
    return value if isinstance(value, str) else ""


def rerun_note(found: str) -> str:
    """For a task whose settings name another control schema than step 1 sets now."""
    named = f"names {found}" if found else "sets no DMS control schema"
    return (f"This task {named} (step 1 now sets {CONTROL_SCHEMA}), so DMS may not have recorded "
            "the records it counted as failed: the first query below shows where, if anywhere, "
            "DMS made its failure table. To have them recorded, run /move step-1 again: it "
            f"renders the DMS stack with ControlSchema {CONTROL_SCHEMA} and deploys that change "
            "after your approval.")  # fmt: skip
