"""Decision record contract (``decisions/<name>.json``).

``decisions/`` is the job's audit trail of recorded approvals. Two records are
known today: ``assignment_review.json``, the assignment-review gate recorded as
passed by ``mark_assignment_reviewed`` (``src/shared/job_status.py``) when the job has
no ``.meta`` progression, and ``assignment_approval.json``, a human's approval
polled by ``scripts/check_decision.py``. Other phases and callers read them back
across runs (``is_assignment_reviewed``, the local API's job status, the check
script). They were plain dicts; this one model types them as they are, and the
``contract_version`` is checked on read like every other stored contract
(ADR-008 Decisions 1 and 4).

Only ``status`` is required: a record a person or another tool wrote by hand
carries whatever else it likes, so unknown keys are kept (``extra="allow"``).

Version History:
- 1.0 (2026-10-21, #1257): Initial version, the keys the records already had
  (``status``, ``recorded_at``, ``source``, ``inferred_from``) plus
  ``contract_version``. A record without ``contract_version`` was written before
  this contract, and still loads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DecisionRecord(BaseModel):
    """``decisions/<name>.json``: one recorded approval."""

    model_config = ConfigDict(extra="allow")

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    status: str = Field(
        min_length=1, description='What was decided, e.g. "completed" or "approved"'
    )
    # The next three are written by the inferred gate record; a hand-written approval
    # has none of them, so they are never written as null.
    recorded_at: str | None = Field(default=None, description="ISO 8601 time it was recorded")
    source: str | None = Field(default=None, description='"inferred" when no person clicked')
    inferred_from: str | None = Field(
        default=None, description="The phase whose start implied the gate was passed"
    )
