# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Elapsed time between two ISO timestamps, or None when it is unknown (#2304)."""

from datetime import datetime


def duration_seconds(started_at: str | None, ended_at: str | None) -> int | None:
    """Whole seconds from start to end; None when either is missing or the end precedes the start.

    Local jobs take their start from a directory mtime when no start was recorded,
    which can land after the end. That is an unknown duration, not a negative one.
    """
    if not started_at or not ended_at:
        return None
    try:
        elapsed = (
            datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
        ).total_seconds()
    except (TypeError, ValueError):
        return None
    return int(elapsed) if elapsed >= 0 else None
