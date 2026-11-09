# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How much change log step 1 makes the source keep, read-only (#2060).

While step 1 proves the move, the source keeps its change log for DMS: on PostgreSQL the DMS
replication slot holds WAL until DMS reads it, so a task that is stopped, failed or far behind
lets WAL grow until the source's storage fills; on MySQL the binary logs DMS still needs are
purged once the retention period passes, and the task can then no longer resume.
``read_source_retention`` reads the source's Amazon RDS metrics from CloudWatch
(``GetMetricData`` only, the read ``status`` already makes for the DMS lag) and ``judge`` turns
them into ``ok``, ``warning`` or ``critical`` with these thresholds:

- ``SLOT_LAG_WARN_BYTES`` (1 GiB): ``OldestReplicationSlotLag`` at or above it is a warning. A
  slot DMS keeps reading stays at megabytes; a gigabyte means DMS is not reading the slot. A
  negative value is how RDS reports a source with no replication slot: ``replication_slot``
  ``none``, never a lag.
- ``FREE_STORAGE_WARN_BYTES`` (10 GiB) and ``FREE_STORAGE_CRITICAL_BYTES`` (2 GiB): an RDS
  source's free storage (``FreeStorageSpace``) at or below them is a warning or critical. Zero
  free storage is critical.
- ``LOG_SHARE_WARN`` (0.25) and ``LOG_SHARE_CRITICAL`` (0.5): the change log kept for DMS
  (``ReplicationSlotDiskUsage``, the WAL kept for slots, on PostgreSQL; ``BinLogDiskUsage``, on
  Aurora MySQL ``SumBinaryLogSize``, on MySQL) at or above this share of the free storage is a
  warning or critical. ``TransactionLogsDiskUsage`` (all WAL) is reported but never judged: a
  source with no slot at all keeps gigabytes of WAL as its normal baseline. RDS sources only.
- ``AURORA_LOG_WARN_BYTES`` (10 GiB) and ``AURORA_LOG_CRITICAL_BYTES`` (100 GiB): on an Aurora
  source, whose storage grows by itself (so free storage cannot fill), the change log kept for
  DMS (``ReplicationSlotDiskUsage`` on Aurora PostgreSQL, ``SumBinaryLogSize`` on Aurora MySQL)
  at or above them is a warning or critical, as an absolute size: it is billed storage and, on
  PostgreSQL, WAL the slot must still replay. Aurora MySQL's ``AuroraVolumeBytesLeftTotal`` is
  reported as ``volume_left_bytes`` and never judged; Aurora PostgreSQL does not publish it.

The metric names and dimensions were checked against live RDS for PostgreSQL, RDS for MySQL,
Aurora PostgreSQL and Aurora MySQL sources on 2026-11-09.

A self-managed source publishes no RDS metrics, and a metric with no datapoint in the last
``WINDOW_MINUTES`` is never guessed: both read "not measured", with the reason. Nothing here
writes, connects to a database or reads a secret; dropping a slot or changing a retention
setting stays the customer's (``source_followups`` says how).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

GIB = 1024**3
SLOT_LAG_WARN_BYTES = 1 * GIB
FREE_STORAGE_WARN_BYTES = 10 * GIB
FREE_STORAGE_CRITICAL_BYTES = 2 * GIB
LOG_SHARE_WARN = 0.25
LOG_SHARE_CRITICAL = 0.5
AURORA_LOG_WARN_BYTES = 10 * GIB
AURORA_LOG_CRITICAL_BYTES = 100 * GIB
WINDOW_MINUTES = 15
PERIOD_SECONDS = 60

# The plan's source engine -> the change-log family the retention rules follow.
FAMILY = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgresql"}
# (source kind, family) -> output key -> (AWS/RDS metric name, dimensions: the instance, the
# cluster alone, or the cluster's writer as CloudWatch aggregates an instance metric).
_PG_RDS = {"slot_lag_bytes": ("OldestReplicationSlotLag", "instance"),
           "slot_disk_bytes": ("ReplicationSlotDiskUsage", "instance"),
           "transaction_logs_bytes": ("TransactionLogsDiskUsage", "instance"),
           "free_storage_bytes": ("FreeStorageSpace", "instance")}  # fmt: skip
_SOURCES: dict[tuple[str, str], dict[str, tuple[str, str]]] = {
    ("rds_instance", "postgresql"): _PG_RDS,
    ("rds_instance", "mysql"): {"binlog_bytes": ("BinLogDiskUsage", "instance"),
                                "free_storage_bytes": ("FreeStorageSpace", "instance")},
    ("aurora_cluster", "postgresql"): {
        k: (m, "writer") for k, (m, _) in _PG_RDS.items() if k != "free_storage_bytes"},
    # Aurora MySQL publishes no BinLogDiskUsage: its binary log size is SumBinaryLogSize.
    ("aurora_cluster", "mysql"): {"binlog_bytes": ("SumBinaryLogSize", "cluster"),
                                  "volume_left_bytes": ("AuroraVolumeBytesLeftTotal", "cluster")},
}  # fmt: skip
_FAMILIES = ("postgresql", "mysql")
_LOG_KEYS = ("slot_disk_bytes", "binlog_bytes")  # the change log kept for DMS, judged
LEVELS = ("ok", "warning", "critical")
# What a stopped or failed task costs the source, per family: a task_stopped line names it.
STOPPED_TASK_RISK = {
    "postgresql": (
        "While the task is not running, its replication slot keeps WAL on the source and the "
        "source's storage use grows until DMS reads the slot again: fix and resume the task, or "
        "end the step (/move step-1) and drop the slot"
    ),
    "mysql": (
        "While the task is not running, the source purges the binary logs DMS still needs once "
        "its binlog retention passes, and the task can then no longer resume (only a full "
        "reload helps): resume the task within the retention period, or end the step"
    ),
}
SELF_MANAGED_CHECK = {
    "postgresql": (
        "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), "
        "restart_lsn)) AS retained_wal FROM pg_replication_slots;"
    ),
    "mysql": "SHOW BINARY LOGS;",
}


def family_of(engine: str | None) -> str | None:
    return FAMILY.get(engine or "")


def gib(value: float) -> str:
    return f"{value / GIB:.1f} GiB"


def metric_names(source_kind: str, family: str) -> dict[str, str]:
    """Output key -> the CloudWatch metric read for it on this kind of source."""
    return {key: name for key, (name, _) in _SOURCES[(source_kind, family)].items()}


def _queries(source_kind: str, source_id: str, family: str) -> list[dict[str, Any]]:
    queries = []
    for i, (key, (name, scope)) in enumerate(_SOURCES[(source_kind, family)].items()):
        if scope == "instance":
            dims = [{"Name": "DBInstanceIdentifier", "Value": source_id}]
        elif scope == "cluster":
            dims = [{"Name": "DBClusterIdentifier", "Value": source_id}]
        else:
            dims = [{"Name": "DBClusterIdentifier", "Value": source_id},
                    {"Name": "Role", "Value": "WRITER"}]  # fmt: skip
        queries.append({"Id": f"m{i}", "Label": key, "ReturnData": True,
                        "MetricStat": {"Metric": {"Namespace": "AWS/RDS", "MetricName": name,
                                                  "Dimensions": dims},
                                       "Period": PERIOD_SECONDS, "Stat": "Maximum"}})  # fmt: skip
    return queries


def not_measured(source_kind: str, source_id: str | None, family: str | None,
                 reason: str) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {"level": "not_measured", "source": source_id,
                           "source_kind": source_kind, "engine_family": family,
                           "reason": reason, "warnings": [], "message": None}  # fmt: skip
    if family in SELF_MANAGED_CHECK and source_kind == "self_managed":
        out["check_on_source"] = SELF_MANAGED_CHECK[family]
    return out


def read_source_retention(cloudwatch: Any, *, source_kind: str, source_id: str | None,
                          family: str | None, now: datetime) -> dict[str, Any]:  # fmt: skip
    """The source's latest change-log and storage figures, judged; read-only."""
    if source_kind == "self_managed":
        return not_measured(source_kind, source_id, family,
                            "a self-managed source publishes no RDS metrics: watch its change "
                            "log and free storage on the source itself")  # fmt: skip
    if family not in _FAMILIES:
        return not_measured(source_kind, source_id, family,
                            "the step plan names no MySQL or PostgreSQL source engine")  # fmt: skip
    if not source_id:
        return not_measured(source_kind, source_id, family, "the inputs file names no source_id")
    from botocore.exceptions import BotoCoreError, ClientError

    latest: dict[str, tuple[datetime, float]] = {}
    try:
        pages = cloudwatch.get_paginator("get_metric_data").paginate(
            MetricDataQueries=_queries(source_kind, source_id, family),
            StartTime=now - timedelta(minutes=WINDOW_MINUTES), EndTime=now,
        )  # fmt: skip
        for page in pages:
            for result in page.get("MetricDataResults") or []:
                for when, value in zip(
                    result.get("Timestamps") or [], result.get("Values") or [], strict=False
                ):
                    key = str(result.get("Label"))
                    if key not in latest or when > latest[key][0]:
                        latest[key] = (when, float(value))
    except (BotoCoreError, ClientError) as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") or type(exc).__name__
        return not_measured(source_kind, source_id, family,
                            f"CloudWatch GetMetricData could not be read ({code})")  # fmt: skip
    values = {
        key: latest[key][1] if key in latest else None for key in _SOURCES[(source_kind, family)]
    }
    names = metric_names(source_kind, family)
    missing = {key: f"not measured: no {names[key]} datapoint for "
                    f"{source_id} in the last {WINDOW_MINUTES} minutes"
               for key, v in values.items() if v is None}  # fmt: skip
    return judge(values, source_kind=source_kind, source_id=source_id, family=family,
                 missing=missing, names=names)  # fmt: skip


def judge(values: Mapping[str, float | None], *, source_kind: str, source_id: str | None,
          family: str, missing: Mapping[str, str] | None = None,
          names: Mapping[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
    """``ok``, ``warning`` or ``critical`` from the measured figures, or ``not_measured`` when
    none of them has a datapoint; every threshold crossed is one warning sentence. A negative
    ``slot_lag_bytes`` is RDS's "no replication slot", never a lag."""
    names = names or metric_names(source_kind, family)
    found: list[tuple[str, str]] = []
    values = dict(values)
    no_slot = (values.get("slot_lag_bytes") or 0) < 0
    if no_slot:
        values["slot_lag_bytes"] = None
    free = values.get("free_storage_bytes")
    slot = values.get("slot_lag_bytes")
    logs = [(k, v) for k, v in values.items() if k in _LOG_KEYS and v is not None]
    if source_kind == "aurora_cluster" and logs:  # storage grows by itself: an absolute size
        key, kept = max(logs, key=lambda kv: kv[1])
        what = f"the source keeps {gib(kept)} of change log for DMS ({names[key]})"
        if kept >= AURORA_LOG_CRITICAL_BYTES:
            found.append(("critical", f"{what} (critical at {gib(AURORA_LOG_CRITICAL_BYTES)})"))
        elif kept >= AURORA_LOG_WARN_BYTES:
            found.append(("warning", f"{what} (warns at {gib(AURORA_LOG_WARN_BYTES)})"))
    if free is not None and free <= FREE_STORAGE_CRITICAL_BYTES:
        found.append(("critical", f"the source has {gib(free)} of storage free (critical at "
                                  f"{gib(FREE_STORAGE_CRITICAL_BYTES)} or less)"))  # fmt: skip
    elif free is not None and free <= FREE_STORAGE_WARN_BYTES:
        found.append(("warning", f"the source has {gib(free)} of storage free (warns at "
                                 f"{gib(FREE_STORAGE_WARN_BYTES)} or less)"))  # fmt: skip
    if logs and free is not None:
        key, kept = max(logs, key=lambda kv: kv[1])
        share = kept / free if free > 0 else (float("inf") if kept > 0 else 0.0)
        what = f"the source keeps {gib(kept)} of change log for DMS ({names[key]})"
        if share >= LOG_SHARE_CRITICAL:
            found.append(("critical", f"{what}, at least {LOG_SHARE_CRITICAL:.0%} of its "
                                      f"{gib(free)} free storage"))  # fmt: skip
        elif share >= LOG_SHARE_WARN:
            found.append(("warning", f"{what}, at least {LOG_SHARE_WARN:.0%} of its "
                                     f"{gib(free)} free storage"))  # fmt: skip
    if slot is not None and slot >= SLOT_LAG_WARN_BYTES:
        found.append(("warning", f"the oldest replication slot is {gib(slot)} behind (warns at "
                                 f"{gib(SLOT_LAG_WARN_BYTES)}): DMS is not reading its slot, and "
                                 "the source keeps that WAL until it does"))  # fmt: skip
    measured = any(v is not None for v in values.values())  # "no slot" alone measures nothing
    level = (max((lvl for lvl, _ in found), key=LEVELS.index) if found
             else "ok" if measured else "not_measured")  # fmt: skip
    warnings = [text for _, text in found]
    out: dict[str, Any] = {"level": level, "source": source_id, "source_kind": source_kind,
                           "engine_family": family, **values,
                           "not_measured": dict(missing or {}), "warnings": warnings,
                           "message": "; ".join(warnings) or None}  # fmt: skip
    if family == "postgresql" and (no_slot or slot is not None):
        out["replication_slot"] = "none" if no_slot else "present"
    if not measured:
        out["reason"] = "CloudWatch has no datapoint for any of the source's metrics yet"
    return out


def source_followups(family: str | None) -> dict[str, Any]:
    """What ``teardown`` reminds the customer to do on the source once the stack is gone."""
    pg = ["Find it (read-only): SELECT slot_name, plugin, active, pg_size_pretty("
          "pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal FROM "
          "pg_replication_slots WHERE NOT active;",
          "Drop the inactive slot the task left: SELECT pg_drop_replication_slot('<slot_name>');"]  # fmt: skip
    if family == "postgresql":
        return {"source_reminder": "drop the replication slot DMS left on the source, or the "
                                   "source keeps WAL for it and its storage grows",
                "source_followups": pg}  # fmt: skip
    if family == "mysql":
        return {"source_reminder": "if you raised the source's binary log retention for the "
                                   "move, set it back: the binary logs it keeps use the "
                                   "source's storage",
                "source_followups": ["See the retention (RDS and Aurora): CALL "
                                     "mysql.rds_show_configuration;",
                                     "Set it back: CALL mysql.rds_set_configuration('binlog "
                                     "retention hours', <hours>);"]}  # fmt: skip
    return {"source_reminder": "on a PostgreSQL source, drop the replication slot DMS left, or "
                               "the source keeps WAL for it and its storage grows",
            "source_followups": [f"On a PostgreSQL source: {item}" for item in pg]}  # fmt: skip
