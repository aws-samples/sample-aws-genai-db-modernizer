# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Canonical bytes and SHA-256 of a step contract: what a later step pins (#1762)."""

from __future__ import annotations

import hashlib
import json

from src.contracts.transformation_contract import StepContract


def canonical_json(contract: StepContract) -> bytes:
    """Sorted keys, no whitespace, UTF-8: the same contract always gives the same bytes.

    These are the bytes a writer writes to ``step-<k>.json``, never a re-indented dump,
    so the file's own SHA-256 equals the pin (``contract_sha256``)."""
    return json.dumps(
        contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def contract_sha256(contract: StepContract) -> str:
    """SHA-256 (hex) over ``canonical_json(contract)``: what ``DependsOn`` pins. It is
    also the SHA-256 of the written file, because the file holds exactly those bytes."""
    return hashlib.sha256(canonical_json(contract)).hexdigest()
