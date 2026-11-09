# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The source's change-log retention while step 1 proves the move (#2060).

CloudWatch goes to a botocore Stubber: nothing is sent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from botocore.stub import ANY

from src.tools.data_movement import source_retention as sr
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
GIB = sr.GIB


def _data(cred, values: dict[str, list[float]], expected=None):
    """One GetMetricData page: each key's values, newest last."""
    results = []
    for i, (key, points) in enumerate(values.items()):
        stamps = [NOW - timedelta(minutes=len(points) - n) for n in range(len(points))]
        results.append({"Id": f"m{i}", "Label": key, "StatusCode": "Complete",
                        "Timestamps": stamps, "Values": points})  # fmt: skip
    cred.stub("cloudwatch").add_response(
        "get_metric_data", {"MetricDataResults": results}, expected
    )


def _read(cred, kind="rds_instance", family="postgresql", source_id="shop-db"):
    return sr.read_source_retention(cred.client("cloudwatch"), source_kind=kind,
                                    source_id=source_id, family=family, now=NOW)  # fmt: skip


def _queried(kind, family, source_id="shop-db"):
    return {"MetricDataQueries": sr._queries(kind, source_id, family), "StartTime": ANY,
            "EndTime": ANY}  # fmt: skip


def test_a_healthy_rds_postgresql_source_reads_ok_from_the_latest_datapoints():
    cred = StubbedCredMgr()
    _data(cred, {"slot_lag_bytes": [9e9, 2e6], "slot_disk_bytes": [4e6],
                 "transaction_logs_bytes": [3e8], "free_storage_bytes": [50 * GIB]},
          _queried("rds_instance", "postgresql"))  # fmt: skip
    out = _read(cred)
    assert out["level"] == "ok" and out["warnings"] == [] and out["message"] is None
    assert out["slot_lag_bytes"] == 2e6  # the newest datapoint, not the largest
    assert out["free_storage_bytes"] == 50 * GIB and out["not_measured"] == {}
    assert out["slot_disk_bytes"] == 4e6 and out["replication_slot"] == "present"
    cred.stub("cloudwatch").assert_no_pending_responses()


def test_the_rds_queries_name_the_instance_and_the_metrics_of_its_family():
    pg = sr._queries("rds_instance", "pg", "postgresql")
    assert [q["MetricStat"]["Metric"]["MetricName"] for q in pg] == [
        "OldestReplicationSlotLag", "ReplicationSlotDiskUsage", "TransactionLogsDiskUsage",
        "FreeStorageSpace"]  # fmt: skip
    assert all(q["MetricStat"]["Metric"]["Dimensions"]
               == [{"Name": "DBInstanceIdentifier", "Value": "pg"}] for q in pg)  # fmt: skip
    my = sr._queries("rds_instance", "my", "mysql")
    assert [q["MetricStat"]["Metric"]["MetricName"] for q in my] == [
        "BinLogDiskUsage", "FreeStorageSpace"]  # fmt: skip


def test_an_aurora_postgresql_source_reads_the_writer_and_no_free_storage():
    queries = sr._queries("aurora_cluster", "src-cluster", "postgresql")
    metrics = {q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Metric"]["Dimensions"]
               for q in queries}  # fmt: skip
    writer = [{"Name": "DBClusterIdentifier", "Value": "src-cluster"},
              {"Name": "Role", "Value": "WRITER"}]  # fmt: skip
    assert metrics == {"OldestReplicationSlotLag": writer, "ReplicationSlotDiskUsage": writer,
                       "TransactionLogsDiskUsage": writer}  # fmt: skip
    # Aurora storage grows by itself, and Aurora PostgreSQL publishes no AuroraVolumeBytesLeftTotal.


def test_an_aurora_source_with_a_lagging_slot_warns():
    cred = StubbedCredMgr()
    _data(cred, {"slot_lag_bytes": [3 * GIB], "slot_disk_bytes": [3 * GIB],
                 "transaction_logs_bytes": [3 * GIB]})  # fmt: skip
    out = _read(cred, kind="aurora_cluster", source_id="src-cluster")
    assert out["level"] == "warning" and "3.0 GiB behind" in out["message"]
    assert "replication slot" in out["message"] and "free_storage_bytes" not in out


@pytest.mark.parametrize(("kept", "level"), [
    (sr.AURORA_LOG_WARN_BYTES - 1, "ok"), (sr.AURORA_LOG_WARN_BYTES, "warning"),
    (sr.AURORA_LOG_CRITICAL_BYTES - 1, "warning"), (sr.AURORA_LOG_CRITICAL_BYTES, "critical")])  # fmt: skip
def test_an_aurora_source_is_judged_on_the_absolute_change_log_it_keeps(kept, level):
    out = sr.judge({"slot_lag_bytes": 1e6, "slot_disk_bytes": float(kept),
                    "transaction_logs_bytes": 500.0 * GIB}, source_kind="aurora_cluster",
                   source_id="c", family="postgresql")  # fmt: skip
    assert out["level"] == level
    if level != "ok":
        assert "ReplicationSlotDiskUsage" in out["message"]


def test_aurora_volume_space_left_is_reported_and_never_judged():
    out = sr.judge({"binlog_bytes": 1e6, "volume_left_bytes": 0.0}, source_kind="aurora_cluster",
                   source_id="c", family="mysql")  # fmt: skip
    assert out["level"] == "ok" and out["volume_left_bytes"] == 0.0


def test_a_metric_without_a_datapoint_is_not_measured_and_never_guessed():
    cred = StubbedCredMgr()
    _data(cred, {"slot_lag_bytes": [], "slot_disk_bytes": [0.0], "transaction_logs_bytes": [1e8],
                 "free_storage_bytes": [40 * GIB]})  # fmt: skip
    out = _read(cred)
    assert out["level"] == "ok" and out["slot_lag_bytes"] is None
    assert out["not_measured"] == {"slot_lag_bytes": "not measured: no OldestReplicationSlotLag "
                                   "datapoint for shop-db in the last 15 minutes"}  # fmt: skip


def test_no_datapoint_at_all_is_not_measured():
    cred = StubbedCredMgr()
    _data(cred, {"binlog_bytes": [], "free_storage_bytes": []})
    out = _read(cred, family="mysql")
    assert out["level"] == "not_measured" and out["warnings"] == []
    assert "no datapoint" in out["reason"]


def test_a_cloudwatch_that_refuses_to_read_is_not_measured():
    cred = StubbedCredMgr()
    cred.stub("cloudwatch").add_client_error("get_metric_data", "AccessDenied", "not allowed")
    out = _read(cred)
    assert out["level"] == "not_measured" and "AccessDenied" in out["reason"]


@pytest.mark.parametrize("family", ["postgresql", "mysql"])
def test_a_self_managed_source_is_not_measured_and_names_the_check_to_run_on_it(family):
    cred = StubbedCredMgr()
    out = _read(cred, kind="self_managed", family=family, source_id=None)
    assert out["level"] == "not_measured" and "self-managed" in out["reason"]
    assert out["check_on_source"] == sr.SELF_MANAGED_CHECK[family]
    cred.stub("cloudwatch").assert_no_pending_responses()  # no CloudWatch call at all


def test_an_unknown_source_engine_is_not_measured_without_a_call():
    out = _read(StubbedCredMgr(), family=None)
    assert out["level"] == "not_measured" and "MySQL or PostgreSQL" in out["reason"]


def _judge(**values):
    return sr.judge(values, source_kind="rds_instance", source_id="db", family="postgresql")


def test_zero_free_storage_is_critical():
    out = _judge(slot_lag_bytes=0.0, transaction_logs_bytes=0.0, free_storage_bytes=0.0)
    assert out["level"] == "critical" and "0.0 GiB of storage free" in out["message"]


@pytest.mark.parametrize(("free", "level"), [
    (sr.FREE_STORAGE_WARN_BYTES + 1, "ok"), (sr.FREE_STORAGE_WARN_BYTES, "warning"),
    (sr.FREE_STORAGE_CRITICAL_BYTES + 1, "warning"), (sr.FREE_STORAGE_CRITICAL_BYTES, "critical")])  # fmt: skip
def test_free_storage_thresholds_are_inclusive(free, level):
    assert _judge(free_storage_bytes=float(free))["level"] == level


@pytest.mark.parametrize(("kept", "level"), [
    (0.24, "ok"), (0.25, "warning"), (0.49, "warning"), (0.5, "critical")])  # fmt: skip
def test_the_change_log_share_of_free_storage_warns_then_turns_critical(kept, level):
    free = 400 * GIB
    out = _judge(slot_lag_bytes=0.0, slot_disk_bytes=kept * free, free_storage_bytes=free)
    assert out["level"] == level
    if level != "ok":
        assert "ReplicationSlotDiskUsage" in out["message"]


def test_total_wal_alone_is_the_sources_baseline_and_never_judged():
    """Live, a source with no slot kept 2.28 GB of WAL against 18 GB free: that is normal."""
    out = _judge(slot_lag_bytes=-1.0, slot_disk_bytes=0.0, transaction_logs_bytes=0.6 * 18 * GIB,
                 free_storage_bytes=18.0 * GIB)  # fmt: skip
    assert out["level"] == "ok" and out["transaction_logs_bytes"] == 0.6 * 18 * GIB


def test_a_negative_slot_lag_means_no_replication_slot_never_a_lag():
    """RDS reports OldestReplicationSlotLag -1 once the source has no slot (seen after teardown)."""
    cred = StubbedCredMgr()
    _data(cred, {"slot_lag_bytes": [-1.0], "slot_disk_bytes": [0.0], "transaction_logs_bytes": [],
                 "free_storage_bytes": [40 * GIB]})  # fmt: skip
    out = _read(cred)
    assert out["replication_slot"] == "none" and out["slot_lag_bytes"] is None
    assert out["level"] == "ok" and out["warnings"] == []
    assert "slot_lag_bytes" not in out["not_measured"]


def test_no_slot_alone_with_nothing_else_measured_is_not_measured():
    cred = StubbedCredMgr()
    _data(cred, {"slot_lag_bytes": [-1.0], "slot_disk_bytes": [], "transaction_logs_bytes": [],
                 "free_storage_bytes": []})  # fmt: skip
    out = _read(cred)
    assert out["level"] == "not_measured" and out["replication_slot"] == "none"
    assert set(out["not_measured"]) == {"slot_disk_bytes", "transaction_logs_bytes",
                                        "free_storage_bytes"}  # fmt: skip


def test_an_aurora_mysql_source_reads_sum_binary_log_size_on_the_cluster():
    queries = sr._queries("aurora_cluster", "src-cluster", "mysql")
    metrics = {q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Metric"]["Dimensions"]
               for q in queries}  # fmt: skip
    cluster = [{"Name": "DBClusterIdentifier", "Value": "src-cluster"}]
    assert metrics == {"SumBinaryLogSize": cluster, "AuroraVolumeBytesLeftTotal": cluster}
    cred = StubbedCredMgr()
    _data(cred, {"binlog_bytes": [30.0 * GIB], "volume_left_bytes": [100.0 * GIB]},
          _queried("aurora_cluster", "mysql", "src-cluster"))  # fmt: skip
    out = _read(cred, kind="aurora_cluster", family="mysql", source_id="src-cluster")
    assert out["level"] == "warning" and "SumBinaryLogSize" in out["message"]
    assert "replication_slot" not in out and out["volume_left_bytes"] == 100.0 * GIB


def test_a_slot_lag_at_the_threshold_warns_and_below_it_does_not():
    free = 1000 * GIB
    assert _judge(slot_lag_bytes=float(sr.SLOT_LAG_WARN_BYTES - 1),
                  free_storage_bytes=free)["level"] == "ok"  # fmt: skip
    assert _judge(slot_lag_bytes=float(sr.SLOT_LAG_WARN_BYTES),
                  free_storage_bytes=free)["level"] == "warning"  # fmt: skip


def test_a_lagging_slot_without_a_free_storage_figure_still_warns():
    out = _judge(slot_lag_bytes=5.0 * GIB, free_storage_bytes=None)
    assert out["level"] == "warning"


def test_every_threshold_crossed_is_one_warning_and_the_worst_sets_the_level():
    out = _judge(slot_lag_bytes=6.0 * GIB, slot_disk_bytes=6.0 * GIB, free_storage_bytes=8.0 * GIB)
    assert out["level"] == "critical" and len(out["warnings"]) == 3


def test_the_thresholds_are_the_documented_values():
    assert (sr.SLOT_LAG_WARN_BYTES, sr.FREE_STORAGE_WARN_BYTES, sr.FREE_STORAGE_CRITICAL_BYTES) == (
        GIB, 10 * GIB, 2 * GIB)  # fmt: skip
    assert (sr.LOG_SHARE_WARN, sr.LOG_SHARE_CRITICAL) == (0.25, 0.5)
    assert (sr.AURORA_LOG_WARN_BYTES, sr.AURORA_LOG_CRITICAL_BYTES) == (10 * GIB, 100 * GIB)


def test_teardown_followups_name_the_slot_query_and_drop_on_postgresql():
    pg = sr.source_followups("postgresql")
    assert "replication slot" in pg["source_reminder"]
    text = " ".join(pg["source_followups"])
    assert "FROM pg_replication_slots WHERE NOT active" in text
    assert "pg_drop_replication_slot('<slot_name>')" in text
    my = sr.source_followups("mysql")
    assert "binary log retention" in my["source_reminder"] and "slot" not in my["source_reminder"]
    unknown = sr.source_followups(None)
    assert unknown["source_reminder"].startswith("on a PostgreSQL source")
    assert all(i.startswith("On a PostgreSQL source: ") for i in unknown["source_followups"])
