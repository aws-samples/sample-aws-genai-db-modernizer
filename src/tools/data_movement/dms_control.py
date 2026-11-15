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
import shlex
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


def rerun_note(found: str, task_arn: str, region: str) -> list[str]:
    """The customer's own steps for an Aurora MySQL task whose settings name another control
    schema than step 1 sets now (a stack rendered before #2133), where DMS 3.6.1 may not have
    made its failure table. Only theirs, with their credentials: DMS modifies a task only once
    it is stopped (AWS DMS API Reference, ModifyReplicationTask), and ``/move step-1`` on a
    running or stopped task goes back to Prove, never to a deploy. The modify sends the task's
    whole settings with the control schema added, never a partial document. On Aurora
    PostgreSQL an older task writes its failures to ``public``, the documented default, so it
    gets no note."""
    named = f"names {found}" if found else "sets no DMS control schema"
    arn, where = shlex.quote(task_arn), shlex.quote(region)
    setting = json.dumps({"ControlTablesSettings": {"ControlSchema": CONTROL_SCHEMA}})
    return [
        f"This task {named} (step 1 now sets {CONTROL_SCHEMA}): on Aurora MySQL, DMS 3.6.1 may "
        "not have made its failure table, so the failures it counted may be recorded nowhere. "
        "Run the first query below: when it names an awsdms_validation_failures table, read "
        "the failures there. When it names none, the failures already counted will not appear "
        "later by themselves.",
        "To have DMS record them from then on, with your own credentials (while the task is "
        "stopped the source keeps its change log for it): stop the task: aws dms "
        f"stop-replication-task --replication-task-arn {arn} --region {where}",
        "Save its settings: aws dms describe-replication-tasks --filters "
        f"Name=replication-task-arn,Values={arn} --query "
        f"'ReplicationTasks[0].ReplicationTaskSettings' --output text --region {where} > "
        f"task-settings.json, then add {setting[1:-1]} to that JSON object and run: aws dms "
        f"modify-replication-task --replication-task-arn {arn} --replication-task-settings "
        f"file://task-settings.json --region {where}",
        f"Resume it: aws dms start-replication-task --replication-task-arn {arn} "
        f"--start-replication-task-type resume-processing --region {where}",
        "Then run /move step-1: it goes on at Prove, and when the gate names these tables it "
        "offers revalidate, which validates them again so DMS records what still differs.",
    ]
