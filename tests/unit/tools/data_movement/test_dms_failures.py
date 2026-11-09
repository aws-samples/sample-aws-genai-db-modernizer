# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Known DMS task failures and the fix hint each carries (#2061).

A hint is given only for a failure the catalog describes; any other message gets none, never a
near-miss hint."""

from __future__ import annotations

import pytest

from src.tools.data_movement import dms_failures as df

OOM = "Failed to run load command. Cannot allocate memory"
KNOWN = [
    ("mysql_replication_grants",
     "Error 1227 (42000): Access denied; you need (at least one of) the REPLICATION SLAVE "
     "privilege(s) for this operation", "REPLICATION SLAVE"),
    ("mysql_replication_grants",
     "Access denied; you need the replication client privilege", "REPLICATION CLIENT"),
    ("postgresql_replication_role",
     "permission denied to create replication slot: the user is not a member of rds_replication",
     "rds_replication"),
    ("postgresql_heartbeat_create",
     "ERROR: permission denied for table public.awsdms_heartbeat", "CREATE ON SCHEMA"),
    ("postgresql_heartbeat_create",
     "permission denied for relation awsdms_heartbeat", "write access"),
    ("mysql_binary_logging",
     "Binary logging must be enabled for MySQL server", "automated backups"),
    ("mysql_binary_logging",
     "CDC task failing: binary logging disabled on the source", "binlog_row_image=FULL"),
    ("out_of_memory",
     "Failed to run load command. Cannot allocate memory", "instance_class"),
    ("out_of_memory", "Stop Reason STOPPED_DUE_TO_LOW_MEMORY", "instance_class"),
    ("low_disk", "Stop Reason STOPPED_DUE_TO_LOW_DISK", "allocated_storage"),
]  # fmt: skip

UNKNOWN = [
    "Last Error Table error; Task error notification received",  # DMS's generic task error
    "Failed to get lob data, going to set to null",  # a LOB lookup failure, not a truncation
    "permission denied for table orders",  # a SELECT grant, not the heartbeat's CREATE
    "Could not find first log file name in binary log index file",  # purged, not turned off
    "Connection reset by peer",
    "memory usage is high",
    "REPLICATION",
    "",
    # Review counter-examples (#2074): a message that names a binlog variable or a table like
    # the heartbeat's is not the failure the hint fixes.
    "Failed to read binlog_format: Access denied for user dms",
    "Could not read @@binlog_row_image; Lost connection to MySQL server during query",
    "binlog_format is MIXED, the task needs ROW",  # no documented message: no hint
    "permission denied for table heartbeat_events",
    "permission denied for table awsdms_heartbeat_archive",
    "Access denied for user 'dms'@'10.0.0.1' (using password: YES)",
    "Lost connection to MySQL server during query",
    "LOB data was truncated for column 'body'",  # no entry: truncation does not stop the task
    # Re-review counter-examples (#2074).
    "Binary logging is enabled but account 'dms' is disabled",
    "permission denied for table orders; while writing public.awsdms_heartbeat",
    "permission denied for schema public; CREATE TABLE public.awsdms_heartbeat",  # the schema's
    "could not fork autovacuum worker process: Cannot allocate memory",  # the source's memory
    "Cannot allocate memory",
]


@pytest.mark.parametrize(("hint_id", "message", "words"), KNOWN,
                         ids=[f"{h}-{i}" for i, (h, *_) in enumerate(KNOWN)])  # fmt: skip
def test_a_known_failure_message_carries_its_fix_hint(hint_id, message, words):
    found = df.match_failure(message)
    assert found is not None and found.id == hint_id
    assert words in found.hint


@pytest.mark.parametrize("message", UNKNOWN)
def test_an_unknown_failure_message_carries_no_hint(message):
    assert df.match_failure(message) is None


def test_every_hint_names_its_documented_source():
    ids = [h.id for h in df.FAILURE_HINTS]
    assert len(ids) == len(set(ids)) and set(ids) == {k for k, *_ in KNOWN}
    for hint in df.FAILURE_HINTS:
        assert hint.source.startswith(
            ("AWS DMS User Guide", "AWS DMS API Reference", "step 1 rehearsal")
        ), hint.id


@pytest.mark.parametrize("status", ["failed", "failed-move"])
def test_a_failed_task_gets_the_hint_of_its_failure_message(status):
    task = {"Status": status, "LastFailureMessage": OOM, "StopReason": "FATAL_ERROR"}
    assert "instance_class" in str(df.failure_hint(task))


def test_a_stop_reason_alone_can_carry_the_hint():
    task = {
        "Status": "failed",
        "StopReason": "Stop Reason FATAL_ERROR: Failed to run load command. Cannot allocate memory",
    }
    assert "instance_class" in str(df.failure_hint(task))


@pytest.mark.parametrize("status", ["running", "starting", "stopping", "ready"])
def test_a_task_that_did_not_fail_gets_no_hint_from_an_old_failure_message(status):
    """DMS keeps the last failure message after a resume: a running task has no failure now."""
    assert df.failure_hint({"Status": status, "LastFailureMessage": OOM}) is None


def test_a_task_stopped_normally_gets_no_hint():
    task = {"Status": "stopped", "StopReason": "Stop Reason NORMAL",
            "LastFailureMessage": OOM}  # fmt: skip
    assert df.failure_hint(task) is None


@pytest.mark.parametrize("reason", ["STOPPED_DUE_TO_LOW_MEMORY", "STOPPED_DUE_TO_LOW_DISK"])
def test_a_task_dms_stopped_for_low_memory_or_disk_gets_its_hint(reason):
    hint = df.failure_hint({"Status": "stopped", "StopReason": f"Stop Reason {reason}"})
    assert (
        hint is not None
        and ("instance_class" if "MEMORY" in reason else "allocated_storage") in hint
    )


def test_a_task_stopped_by_an_error_gets_its_hint():
    task = {"Status": "stopped", "StopReason": "Stop Reason FATAL_ERROR",
            "LastFailureMessage": OOM}  # fmt: skip
    assert "instance_class" in str(df.failure_hint(task))


def test_no_task_or_no_message_gets_no_hint():
    assert df.failure_hint(None) is None
    assert df.failure_hint({"Status": "failed"}) is None
