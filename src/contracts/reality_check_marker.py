"""Reality-check external-mode marker contract (``reality-check/awaiting_llm.json``).

In ``--llm-mode external`` the reality check writes a deterministic preview of its
output and this marker beside it, ``status = awaiting_llm``, until the model's answer
is finalized; the finalize step then rewrites it as ``finalized``. Code outside the
reality check reads its ``status`` across runs: the assignment lineage
(``src/storage/assignment_versioning.py``) decides from it whether the preview counts
as a run, and the local API (``src/api/services/local_execution.py``) infers a job's
status from it. It was a plain dict; this model types it as it is, and its
``contract_version`` is checked on read like every other stored contract (ADR-008
Decisions 1 and 4).

Version History:
- 1.0 (2026-10-21, #1267): Initial version, the two keys the marker already had
  (``status``, and ``input_key`` while awaiting) plus ``contract_version``. A marker
  without ``contract_version`` was written before this contract, and still loads.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class MarkerStatus(StrEnum):
    """Where an external-mode reality check stands."""

    AWAITING_LLM = "awaiting_llm"
    FINALIZED = "finalized"


class RealityCheckMarker(BaseModel):
    """``reality-check/awaiting_llm.json``: whether a preview still waits for the model."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    status: MarkerStatus
    # The request the model answers; only while awaiting, so never written as null.
    input_key: str | None = Field(default=None, description="Artifact key of llm_input.json")
