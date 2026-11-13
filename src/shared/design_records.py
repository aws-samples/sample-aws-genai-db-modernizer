# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An engine's schema design record: a design, or a skip record saying why there is none.

A skip record is the ``SkippedSchemaOutput`` placeholder
(``src.contracts.skipped_placeholders``) at the design's own path, recognised by
``status: "skipped"``. Synthesis needs one or the other for every engine it designs
(#2154, maintainer decision): a skipped engine is reported as not designed with its
reason; an engine with neither is missing and fails synthesis.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.contracts.phase_models import SCHEMA_DESIGN_SKIP_REASON_NO_LLM

USER_SKIP_REASON = "Skipped at the user's request after its design failed"
"""The reason ``scripts/run_schema_design.py --skip`` records (#2200)."""

NOTHING_ASSIGNED_REASON = "No queries or tables assigned to this engine"
"""The reason the schema design handler records for an engine with nothing to design."""

# Why the design was skipped, as a clause after "the <Engine> design was skipped".
_SKIP_PHRASES = {
    SCHEMA_DESIGN_SKIP_REASON_NO_LLM: "because this run used no model, and every schema "
    "designer needs one",
    USER_SKIP_REASON: "at your request after it failed",
    NOTHING_ASSIGNED_REASON: "because nothing was assigned to it",
}


def skip_reason(design: Any) -> str | None:
    """The stored reason of a skip record, ``""`` for one with none, ``None`` for a design."""
    if isinstance(design, Mapping) and design.get("status") == "skipped":
        reason = design.get("reason")
        return reason if isinstance(reason, str) else ""
    return None


def skip_phrase(reason: str) -> str:
    """Why the design was skipped, as a clause the deliverables append to
    "the <Engine> design was skipped"; empty for a reason it does not know."""
    return _SKIP_PHRASES.get(reason, "")


def is_real_design_key(key: str, read: Any) -> bool:
    """Whether ``key`` is a ``schema_output.json`` holding a design, not a skip record.

    ``read`` reads the key's JSON; an unreadable file counts as a design (it is not a
    skip record), as a file's presence did before skip records were written."""
    if not key.endswith("/schema_output.json"):
        return False
    try:
        return skip_reason(read(key)) is None
    except (OSError, ValueError):
        return True
