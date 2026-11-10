# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's DMS stack by CloudFormation status, its first failed resource and the fix hints
for common create failures (#2063)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.tools.data_movement import stack_states as st
from tests.unit.tools.aws.aws_stubs import make_client

STACK = "dm-job1-step-1"
STACK_STATUSES = make_client("cloudformation").meta.service_model.shape_for("StackStatus").enum
# What each status CloudFormation defines lets step 1 do, written out here so the module's
# table is checked against an independent copy.
KIND = {
    "CREATE_IN_PROGRESS": "changing", "CREATE_FAILED": "create_failed",
    "CREATE_COMPLETE": "deployed", "ROLLBACK_IN_PROGRESS": "changing",
    "ROLLBACK_FAILED": "create_failed", "ROLLBACK_COMPLETE": "create_failed",
    "DELETE_IN_PROGRESS": "changing", "DELETE_FAILED": "stuck", "DELETE_COMPLETE": "gone",
    "UPDATE_IN_PROGRESS": "changing", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS": "changing",
    "UPDATE_COMPLETE": "deployed", "UPDATE_FAILED": "stuck",
    "UPDATE_ROLLBACK_IN_PROGRESS": "changing", "UPDATE_ROLLBACK_FAILED": "stuck",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS": "changing",
    "UPDATE_ROLLBACK_COMPLETE": "deployed", "REVIEW_IN_PROGRESS": "not_created",
    "IMPORT_IN_PROGRESS": "changing", "IMPORT_COMPLETE": "deployed",
    "IMPORT_ROLLBACK_IN_PROGRESS": "changing", "IMPORT_ROLLBACK_FAILED": "stuck",
    "IMPORT_ROLLBACK_COMPLETE": "deployed",
}  # fmt: skip


def test_the_table_covers_every_status_cloudformation_defines():
    assert set(KIND) == set(STACK_STATUSES)


@pytest.mark.parametrize("status", STACK_STATUSES)
def test_every_status_has_its_kind(status):
    assert st.stack_kind(status) == KIND[status]


def test_no_stack_is_gone_and_an_unknown_status_is_stuck_never_deployed():
    assert st.stack_kind(None) == "gone"
    assert st.stack_kind("SOME_NEW_STATE") == "stuck"
    assert st.stack_kind("SOME_NEW_STATE_IN_PROGRESS") == "changing"


@pytest.mark.parametrize("status", STACK_STATUSES)
def test_every_status_that_is_not_plainly_deployed_or_changing_says_what_to_do(status):
    action = st.stack_action(status, STACK, protected=False)
    if KIND[status] in ("changing", "gone") or status in st.HEALTHY:
        assert action is None
    else:
        assert action and STACK in action


@pytest.mark.parametrize("status", sorted(s for s, k in KIND.items() if k == "create_failed"))
def test_a_failed_create_says_delete_it_first_and_that_move_can_end_it(status):
    action = st.stack_action(status, STACK, protected=False)
    assert "delete the failed stack first (`/move step-1` can end it)" in action


def test_an_update_that_rolled_back_keeps_the_earlier_settings():
    action = st.stack_action("UPDATE_ROLLBACK_COMPLETE", STACK, protected=False)
    assert "earlier settings" in action and "deploy again" in action


def test_an_update_rollback_that_failed_names_the_customers_way_out_and_no_write_of_ours():
    action = st.stack_action("UPDATE_ROLLBACK_FAILED", STACK, protected=False)
    assert "continue-update-rollback" in action and "UPDATE_ROLLBACK_COMPLETE" in action
    assert "sends it no update" in action


def test_termination_protection_is_named_and_never_turned_off_for_the_customer():
    action = st.stack_action("ROLLBACK_COMPLETE", STACK, protected=True)
    assert "termination protection" in action and "never turns it off" in action


T0 = datetime(2026, 11, 9, tzinfo=UTC)


def _event(logical, status, reason=None, seconds=0, kind="AWS::DMS::ReplicationInstance"):
    if logical == STACK:
        kind = "AWS::CloudFormation::Stack"
    return {"StackId": "id", "EventId": f"{logical}-{status}-{seconds}", "StackName": STACK,
            "LogicalResourceId": logical, "ResourceType": kind, "ResourceStatus": status,
            "Timestamp": T0 + timedelta(seconds=seconds),
            **({"ResourceStatusReason": reason} if reason else {})}  # fmt: skip


CAPACITY = ("Resource handler returned message: \"There are not enough resources available to "
            "create a replication instance of class dms.r6i.large in us-east-1a "
            "(Service: DatabaseMigration, Status Code: 400, Request ID: x)\" "
            "(HandlerErrorCode: InsufficientResourceCapacityFault)")  # fmt: skip


def test_the_first_failed_resource_of_the_latest_operation_is_named_with_its_hint():
    events = [  # newest first, as DescribeStackEvents returns them
        _event(STACK, "ROLLBACK_COMPLETE", seconds=50),
        _event("SubnetGroup", "DELETE_COMPLETE", seconds=40),
        _event(
            STACK,
            "ROLLBACK_IN_PROGRESS",
            "The following resource(s) failed to create: "
            "[ReplicationInstance, SecretsAccessRole].",
            seconds=30,
        ),
        _event("SecretsAccessRole", "CREATE_FAILED", "Resource creation cancelled", seconds=21),
        _event("ReplicationInstance", "CREATE_FAILED", CAPACITY, seconds=20),
        _event("ReplicationInstance", "CREATE_IN_PROGRESS", seconds=10),
        _event(STACK, "CREATE_IN_PROGRESS", "User Initiated", seconds=0),
        _event("ReplicationInstance", "CREATE_FAILED", "an earlier create", seconds=-100),
    ]
    found = st.first_failure(events)
    assert found == {"logical_id": "ReplicationInstance",
                     "resource_type": "AWS::DMS::ReplicationInstance",
                     "status": "CREATE_FAILED", "reason": CAPACITY,
                     "hint": st.stack_hint(CAPACITY)}  # fmt: skip
    assert "instance_class" in found["hint"] and "Availability Zones" in found["hint"]


def test_a_failed_update_is_named_not_the_older_create():
    events = [
        _event(STACK, "UPDATE_ROLLBACK_COMPLETE", seconds=90),
        _event("ReplicationInstance", "UPDATE_COMPLETE", seconds=80),
        _event(STACK, "UPDATE_ROLLBACK_IN_PROGRESS", "failed to update", seconds=70),
        _event("ReplicationInstance", "UPDATE_FAILED", CAPACITY, seconds=60),
        _event(STACK, "UPDATE_IN_PROGRESS", "User Initiated", seconds=50),
        _event(STACK, "CREATE_COMPLETE", seconds=40),
        _event("SubnetGroup", "CREATE_FAILED", "old", seconds=1),
    ]
    found = st.first_failure(events)
    assert found is not None
    assert found["status"] == "UPDATE_FAILED" and found["reason"] == CAPACITY


def test_no_events_or_no_failed_resource_is_no_failure_never_a_guess():
    assert st.first_failure([]) is None
    assert st.first_failure([_event(STACK, "ROLLBACK_COMPLETE", seconds=5),
                             _event(STACK, "CREATE_IN_PROGRESS", seconds=0)]) is None  # fmt: skip


def test_the_events_are_read_only_up_to_the_operations_start():
    read = []

    def events():
        for e in (_event(STACK, "ROLLBACK_COMPLETE", seconds=9),
                  _event("SubnetGroup", "CREATE_FAILED", "InvalidSubnet: subnet-x", seconds=5),
                  _event(STACK, "CREATE_IN_PROGRESS", seconds=0)):  # fmt: skip
            read.append(e)
            yield e
        raise AssertionError("read past the start of the operation")

    assert st.first_failure(events())["logical_id"] == "SubnetGroup"
    assert len(read) == 3


@pytest.mark.parametrize("reason, needle", [
    (CAPACITY, "instance_class"),
    ("The IAM Role arn:aws:iam::111122223333:role/dms-vpc-role is not configured properly.",
     "dms-vpc-role"),
    ("ReplicationSubnetGroupDoesNotCoverEnoughAZs: the subnet group must cover at least 2 AZs",
     "two Availability Zones"),
    ("InvalidSubnet: The subnet subnet-0abc does not exist", "subnet_ids"),
    ("InvalidCertificateFault: certificate arn:aws:dms:us-east-1:1:cert:X not found",
     "certificate_arn"),
    ("KMSKeyNotAccessibleFault: the KMS key is disabled", "replication_kms_key_arn"),
])  # fmt: skip
def test_common_create_failures_carry_a_hint(reason, needle):
    hint = st.stack_hint(reason)
    assert hint is not None and needle in hint


@pytest.mark.parametrize("reason", [None, "", "Resource creation cancelled",
                                    "Internal Failure", "a table called capacity_planning"])  # fmt: skip
def test_any_other_reason_carries_no_hint(reason):
    assert st.stack_hint(reason) is None


@pytest.mark.parametrize("reason, needle", [
    ("Certificate arn:aws:dms:us-east-1:111122223333:cert:ABC123 not found", "certificate_arn"),
    ("Error: InsufficientInstanceCapacity for the replication instance", "instance_class"),
])  # fmt: skip
def test_the_anchored_phrasings_carry_their_hint(reason, needle):
    hint = st.stack_hint(reason)
    assert hint is not None and needle in hint


@pytest.mark.parametrize("reason", [
    # Names a certificate, but the missing resource is the secret.
    "Endpoint with certificate arn:aws:dms:us-east-1:111122223333:cert:ABC123: secret "
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:src-AbCdEf does not exist",
    "Secrets Manager can't find the specified secret. (ResourceNotFoundException)",
    # A generic capacity or quota message from another service.
    "There are not enough resources available to complete this request (Service: Lambda)",
    "LimitExceededException: insufficient capacity in the account's quota",
    "The role dms-vpc-role was created",
    "the subnet group must cover at least 2 AZs",
    "KMS key alias/x is pending deletion",
])  # fmt: skip
def test_near_misses_carry_no_hint(reason):
    assert st.stack_hint(reason) is None


def test_a_stack_whose_events_show_a_completed_create_completed_it():
    assert st.create_never_completed([_event(STACK, "DELETE_FAILED", seconds=9),
                                      _event(STACK, "CREATE_COMPLETE", seconds=1),
                                      _event(STACK, "CREATE_IN_PROGRESS")]) is False  # fmt: skip
    assert st.create_never_completed([_event(STACK, "DELETE_FAILED", seconds=9),
                                      _event(STACK, "ROLLBACK_FAILED", seconds=5),
                                      _event(STACK, "CREATE_IN_PROGRESS")]) is True  # fmt: skip
