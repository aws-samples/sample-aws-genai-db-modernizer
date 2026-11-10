# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A PE review's verdict agrees with its change requests (#2081).

One rule for every engine's review: ``APPROVED`` carries no ``blocker`` change request,
and ``CHANGES_REQUESTED`` carries at least one change request. DynamoDB's contract
enforces it when it validates; the headless review step holds every other engine's new
answer to it (``pe_review_external.apply_pe_review_llm_output``), so the other
contracts, and the reviews already saved with them, load as before.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

APPROVED_WITH_BLOCKER = "Cannot approve with blocker-severity change requests."
CHANGES_WITHOUT_REQUESTS = "Must provide change_requests when verdict=CHANGES_REQUESTED."


def _value(field: Any) -> str:
    return str(getattr(field, "value", field) or "")


def _severity(change_request: Any) -> str:
    raw = (
        change_request.get("severity")
        if isinstance(change_request, dict)
        else getattr(change_request, "severity", None)
    )
    return _value(raw).lower()


def verdict_errors(verdict: Any, change_requests: Iterable[Any] | None) -> list[str]:
    """The rule's violations (fixed messages, never the review's own text); empty when
    the verdict and the change requests agree."""
    requests = list(change_requests or [])
    errors = []
    if _value(verdict) == "APPROVED" and any(_severity(cr) == "blocker" for cr in requests):
        errors.append(APPROVED_WITH_BLOCKER)
    if _value(verdict) == "CHANGES_REQUESTED" and not requests:
        errors.append(CHANGES_WITHOUT_REQUESTS)
    return errors
