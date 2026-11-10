# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's DMS stack by CloudFormation status (#2063).

``scripts/run_dms_step.py`` reads every stack status through ``stack_kind``:

- ``deployed``: the stack holds its resources and takes an update (``deploy`` sends an UPDATE
  change set, ``status`` judges the task). A stack whose last update rolled back
  (``UPDATE_ROLLBACK_COMPLETE``) runs with its earlier settings and is still deployed.
- ``changing``: CloudFormation is working on it; everything waits.
- ``not_created``: ``REVIEW_IN_PROGRESS``, a change set was created and never executed, so the
  stack holds no resource; ``deploy`` sends a CREATE change set again.
- ``create_failed``: the create failed (``ROLLBACK_COMPLETE``, ``ROLLBACK_FAILED``, or
  ``CREATE_FAILED`` with rollback turned off). CloudFormation updates no such stack: it must be
  deleted first, and ``teardown`` may end this job's own one.
- ``stuck``: any other status (``UPDATE_ROLLBACK_FAILED``, ``DELETE_FAILED``, ...): the
  modernizer sends it no update, and ``stack_action`` names the customer's way on (ending the
  step may still delete it, after the customer's approval).
- ``gone``: no stack, or one deleted.

``first_failure`` names the first resource that failed in the stack's latest operation, from
``DescribeStackEvents`` (read-only), with the fix hint of ``STACK_HINTS`` for a reason
CloudFormation or DMS describes in a known way (None for any other: never a near-miss guess).
Each pattern is anchored on a DMS fault name from the AWS DMS API Reference, or on the one
phrase DMS uses for that fault; the reason strings in the tests are written from those names,
not captured from a live failed create.

``create_never_completed`` tells, from the same events, whether a stack (for example one in
``DELETE_FAILED`` after a failed create) ever finished its create.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

HEALTHY = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"})
DEPLOYED = HEALTHY | {"UPDATE_ROLLBACK_COMPLETE", "IMPORT_ROLLBACK_COMPLETE"}
CREATE_FAILED = frozenset({"CREATE_FAILED", "ROLLBACK_FAILED", "ROLLBACK_COMPLETE"})
NOT_CREATED = frozenset({"REVIEW_IN_PROGRESS"})
NEVER_CREATED = CREATE_FAILED | NOT_CREATED  # a stack whose create never completed
STACK_TYPE = "AWS::CloudFormation::Stack"
# The stack's own event that starts an operation: the failure is looked for after it.
OPERATION_STARTS = frozenset(
    {"CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "DELETE_IN_PROGRESS", "IMPORT_IN_PROGRESS"}
)


def stack_kind(status: str | None) -> str:
    """``deployed``, ``changing``, ``not_created``, ``create_failed``, ``stuck`` or ``gone``."""
    if status is None or status == "DELETE_COMPLETE":
        return "gone"
    if status in NOT_CREATED:
        return "not_created"
    if status.endswith("_IN_PROGRESS"):
        return "changing"
    if status in DEPLOYED:
        return "deployed"
    if status in CREATE_FAILED:
        return "create_failed"
    return "stuck"  # and any status CloudFormation adds later: never taken for deployed


def reads_failure(status: str) -> bool:
    """Whether the status follows a failed operation, so its first failure is worth reading."""
    return stack_kind(status) in ("create_failed", "stuck") or (
        stack_kind(status) == "deployed" and status not in HEALTHY
    )


_PROTECTED = ("Stack {name} has termination protection on, and the modernizer never turns it "
              "off: if you mean to delete the stack, turn it off yourself (CloudFormation console, "
              "or aws cloudformation update-termination-protection "
              "--no-enable-termination-protection --stack-name {name}), then end the step again.")  # fmt: skip
NOT_UPDATABLE = "delete the failed stack first (`/move step-1` can end it)"


def protection_message(name: str) -> str:
    return _PROTECTED.format(name=name)


def stack_action(status: str, name: str, *, protected: bool) -> str | None:
    """What the customer can do with a stack in ``status``; None for a stack that is plainly
    deployed, changing or gone."""
    kind = stack_kind(status)
    if kind in ("changing", "gone") or status in HEALTHY:
        return None
    if kind == "not_created":
        text = (f"A change set was created for stack {name} but never executed, so the stack holds "
                "no resource: run /move step-1 to create and execute a change set, or end the step "
                "to delete the empty stack.")  # fmt: skip
    elif kind == "create_failed":
        left = (" CloudFormation could not delete every resource of the failed create; deleting "
                "the stack deletes them." if status == "ROLLBACK_FAILED" else "")  # fmt: skip
        text = (f"The create of stack {name} failed ({status}), and CloudFormation cannot update "
                f"it: fix the cause (stack_failure), {NOT_UPDATABLE}, then run /move step-1 "
                f"again to create it anew.{left}")  # fmt: skip
    elif kind == "deployed":
        text = (f"The last update of stack {name} rolled back ({status}): the stack runs with its "
                "earlier settings and its task is judged as it is. Fix the cause (stack_failure) "
                "and deploy again to apply the change.")  # fmt: skip
    elif status == "UPDATE_ROLLBACK_FAILED":
        text = (f"CloudFormation could not roll back the last update of stack {name}, and the "
                "modernizer sends it no update: continue the rollback "
                "yourself (CloudFormation console, or aws cloudformation continue-update-rollback "
                f"--stack-name {name}, skipping with --resources-to-skip only resources you have "
                "fixed by hand) until it reads UPDATE_ROLLBACK_COMPLETE, then run /move step-1 "
                "again; or end the step to delete the stack once its task is not running.")  # fmt: skip
    elif status == "UPDATE_FAILED":
        text = (f"The last update of stack {name} failed and was not rolled back (rollback was "
                "turned off), and the modernizer sends it no update: roll it "
                f"back yourself (CloudFormation console, or aws cloudformation rollback-stack "
                f"--stack-name {name}), then run /move step-1 again.")  # fmt: skip
    elif status == "DELETE_FAILED":
        text = (f"A delete of stack {name} failed (stack_failure says which resource): fix the "
                "cause, then end the step again to delete it.")  # fmt: skip
    else:
        text = (f"Stack {name} is {status}, a state step 1 never puts it in, and the modernizer "
                "sends it no update: check it in the CloudFormation console, or end the step "
                "to delete it once its task is not running.")  # fmt: skip
    return f"{text} {protection_message(name)}" if protected else text


@dataclass(frozen=True)
class StackHint:
    id: str
    pattern: re.Pattern[str]
    hint: str
    source: str


def _hint(hint_id: str, pattern: str, hint: str, source: str) -> StackHint:
    return StackHint(hint_id, re.compile(pattern, re.IGNORECASE | re.DOTALL), hint, source)


STACK_HINTS: tuple[StackHint, ...] = (
    _hint("insufficient_capacity",
          r"\bInsufficientResourceCapacity(?:Fault)?\b|\bInsufficientInstanceCapacity\b",
          "AWS has no capacity for this replication instance class in the subnets' Availability "
          "Zones right now: set another instance_class in the inputs file, or subnet_ids in other "
          "Availability Zones.",
          "AWS DMS API Reference, InsufficientResourceCapacityFault"),
    _hint("dms_vpc_role", r"\brole/dms-vpc-role\s+is\s+not\s+configured\s+properly\b",
          "The account lacks the DMS role dms-vpc-role, or it is not set up as DMS needs: create "
          "it as the README's \"Before you deploy the DMS stack\" says.",
          "AWS DMS User Guide, Creating the IAM roles to use with AWS DMS"),
    _hint("subnet_group_zones",
          r"\bReplicationSubnetGroupDoesNotCoverEnoughAZs\b",
          "The replication subnet group must cover at least two Availability Zones: set "
          "subnet_ids to private subnets in two zones of the same VPC.",
          "AWS DMS API Reference, ReplicationSubnetGroupDoesNotCoverEnoughAZs"),
    _hint("invalid_subnet", r"\bInvalidSubnet\b",
          "A subnet in subnet_ids does not exist in this region or is not in the same VPC as the "
          "others: correct subnet_ids.",
          "AWS DMS API Reference, InvalidSubnet"),
    # The fault, or the certificate's own ARN directly followed by "not found": a message that
    # names a certificate and then another missing resource (a secret) never matches.
    _hint("certificate", r"\bInvalidCertificateFault\b|\bcertificate\s+"
          r"arn:aws[a-z-]*:dms:[a-z0-9-]+:\d{12}:cert:[A-Za-z0-9-]+\s+(?:was\s+)?"
          r"(?:not\s+found|does\s+not\s+exist)\b",
          "A certificate ARN in the inputs file is wrong, or from another region or account: "
          "check source_certificate_arn and target_certificate_arn against the certificates "
          "imported into DMS in this region.",
          "AWS DMS API Reference, InvalidCertificateFault"),
    _hint("kms_key", r"\bKMS(?:KeyNotAccessible|AccessDenied|Disabled|NotFound|InvalidState)Fault\b",
          "DMS cannot use a KMS key the inputs file names: check replication_kms_key_arn (and the "
          "secrets' key ARNs); each key must exist in this region, be enabled, and let your "
          "deploy role and DMS use it in its key policy.",
          "AWS DMS API Reference, KMSKeyNotAccessibleFault"),
)  # fmt: skip


def stack_hint(reason: str | None) -> str | None:
    """The hint of the first ``STACK_HINTS`` entry whose pattern matches ``reason``; else None."""
    if not reason:
        return None
    return next((h.hint for h in STACK_HINTS if h.pattern.search(reason)), None)


def create_never_completed(events: Iterable[dict[str, Any]]) -> bool:
    """True when every event of the stack has been read (``events`` must be all of them) and
    none of the stack's own says it reached a deployed status: its create never completed."""
    return not any(e.get("ResourceType") == STACK_TYPE and e.get("ResourceStatus") in DEPLOYED
                   for e in events)  # fmt: skip


def first_failure(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The first resource that failed in the stack's latest operation, or None.

    ``events`` come newest first, as ``DescribeStackEvents`` returns them; they are read only
    back to the stack's own event that started the latest operation, so an older create's
    failure is never taken for a failed update's. The failure is the oldest resource event in
    that window whose status ends ``_FAILED`` (the later ones are mostly cancellations)."""
    found: dict[str, Any] | None = None
    for event in events:
        if event.get("ResourceType") == STACK_TYPE:
            if event.get("ResourceStatus") in OPERATION_STARTS:
                break
            continue
        if str(event.get("ResourceStatus") or "").endswith("_FAILED"):
            found = event
    if found is None:
        return None
    reason = found.get("ResourceStatusReason")
    return {"logical_id": found.get("LogicalResourceId"), "resource_type": found.get("ResourceType"),
            "status": found.get("ResourceStatus"), "reason": reason, "hint": stack_hint(reason)}  # fmt: skip
