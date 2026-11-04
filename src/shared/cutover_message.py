# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cutover message shown wherever a step's gate evidence appears (#1800).

A fixed template, never model text: the gate's output, the engineering report's step
section and the results page all quote it by id (``gate.json`` carries
``cutover_message_id``). When to cut over is the customer's decision; the gate reports
evidence and never recommends. A new wording gets a new id, so a saved gate keeps the
text it was shown with.
"""

from __future__ import annotations

CUTOVER_MESSAGE_ID = "cutover-decision-v1"
CUTOVER_HEADLINE = "When to cut over is your decision."
CUTOVER_BODY = (
    "The data movement for this step keeps your Aurora copy in sync with your source and reports the evidence "
    "below: replication lag, rows loaded, and validation per table (with the tables DMS could not validate, "
    "and why). These figures help you decide; they are not a recommendation to switch. Choosing the moment to "
    "cut over, and preparing your applications for it, remains entirely your responsibility."
)
CUTOVER_MESSAGES: dict[str, tuple[str, str]] = {
    CUTOVER_MESSAGE_ID: (CUTOVER_HEADLINE, CUTOVER_BODY)
}


def cutover_message_markdown(message_id: str = CUTOVER_MESSAGE_ID) -> str:
    headline, body = CUTOVER_MESSAGES[message_id]
    return f"**{headline}** {body}"


def cutover_message_text(message_id: str = CUTOVER_MESSAGE_ID) -> str:
    headline, body = CUTOVER_MESSAGES[message_id]
    return f"{headline} {body}"
