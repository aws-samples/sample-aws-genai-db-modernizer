# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cutover message shown wherever a step's gate evidence appears (#1800, #2062).

``DECOMMISSION_MESSAGE`` is its counterpart for the source database (#2265): wave 1's gate
says it, and no other text speaks of that step.

A fixed template, never model text: the gate's output quotes it by id (``gate.json``
carries ``cutover_message_id``). The modernizer gives no cutover advice: where the
cutover comes up, the text says only ``CUTOVER_MESSAGE``. A saved gate written with the
earlier id (``cutover-decision-v1``) is shown this same sentence: the earlier text spoke
of the copy being kept current, which step 1 does not claim (#2062).
"""

from __future__ import annotations

CUTOVER_MESSAGE_ID = "cutover-decision-v2"
LEGACY_CUTOVER_MESSAGE_IDS = ("cutover-decision-v1",)
CUTOVER_MESSAGE = "The cutover is your responsibility."
# The source database's counterpart (maintainer decision of 2026-11-14 on #2265): the same
# plain form, and never when, in what order or whether.
DECOMMISSION_MESSAGE = "Decommissioning the source database is your responsibility."
# The first line of every step 1 file that touches the cutover (maintainer decision of
# 2026-11-10 on #2062): the same sentence everywhere, before anything else.
CUTOVER_DISCLAIMER = f"{CUTOVER_MESSAGE} The modernizer does not run, schedule or advise on it."
CUTOVER_MESSAGES: dict[str, str] = {
    CUTOVER_MESSAGE_ID: CUTOVER_MESSAGE,
    **{legacy: CUTOVER_MESSAGE for legacy in LEGACY_CUTOVER_MESSAGE_IDS},
}


def cutover_message_text(message_id: str = CUTOVER_MESSAGE_ID) -> str:
    return CUTOVER_MESSAGES[message_id]
