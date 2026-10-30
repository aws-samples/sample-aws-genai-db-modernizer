# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Customer-facing wording for a model flag's reason category (#647, #629).

One copy, read by the routing code (``src.agents.referee.model_flags``) and the
deliverables (``src.report``), so a reason reads the same everywhere. Keys are
``src.contracts.model_flag.FlagReason`` values.
"""

from __future__ import annotations

FLAG_REASON_TEXT: dict[str, str] = {
    "full_scan": "it would need a full scan",
    "client_side_join": "it would need a client-side join",
    "missing_feature": "it needs a feature the engine lacks",
    "other": "a capability gap",
    # Not a model flag: a write re-routed after approval so its table's writes land
    # on one engine (#992, ``assignment_overrides.SOURCE_OF_TRUTH_REROUTE_CATEGORY``).
    "source_of_truth": "its table's writes were moved to one engine, its source of truth",
}


# Why a flag changed nothing, in its ``ignored`` ``flag_decisions`` record
# (``model_flags.ignored_flag_outcome``), the same words in both LLM modes (#746).
IGNORED_NOT_ON_ENGINE = "it is not on that engine"
IGNORED_NOT_MOVED_ONTO = "no consolidation moved it onto that engine"
IGNORED_NOT_UNDER_REVIEW = "it is not one of the queries under review"


def flag_reason_text(category: str | None) -> str:
    """The wording for ``category`` (a ``FlagReason`` value); the generic one otherwise."""
    return FLAG_REASON_TEXT.get(str(category or ""), FLAG_REASON_TEXT["other"])
