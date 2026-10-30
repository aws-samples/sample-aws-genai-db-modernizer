# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Skipped placeholders: what is stored when there is nothing to design or synthesize.

A schema design for an engine with nothing assigned to it, a merge whose every
query touches no source table, and a synthesis run with no analysis output each
write a placeholder to the contract file path the real output would use
(``schema-<engine>/v<N>/schema_output.json``, ``synthesis/v<N>/report.json``).
The placeholder is not that contract; it is one of these models, recognised by
its ``status: "skipped"``. ``src/shared/contract_versioning.py`` reads the
``status`` first and checks a placeholder's ``contract_version`` against the model
here, not against the design's or the report's (#1023).

Version History:
- 1.0 (2026-10-17, #1023): Initial version. A placeholder written before has no
  ``contract_version`` (and the empty synthesis report said ``status:
  "completed"``); both still load.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .schema_design_input import ExcludedQuery


def _is_none(value: object) -> bool:
    return value is None


class SkippedSchemaOutput(BaseModel):
    """A schema design that was skipped: nothing assigned, or nothing to design against."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    target_type: str = Field(..., description="The engine the design would be for")
    status: Literal["skipped"] = "skipped"
    reason: str = Field(..., description="Why there is no design")
    assignment_version: int | None = Field(
        default=None, exclude_if=_is_none, description="The assignment version it was skipped on"
    )
    excluded_queries: list[ExcludedQuery] | None = Field(
        default=None,
        exclude_if=_is_none,
        description="Queries --split left out of every group (the merge's placeholder)",
    )


class SkippedSynthesisReport(BaseModel):
    """A synthesis that had no analysis output to synthesize."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    job_id: str
    database_name: str
    agent_type: str = "referee-synthesis"
    status: Literal["skipped"] = "skipped"
    ranking: list[dict[str, object]] = Field(default_factory=list)
    needs_deeper_analysis: bool = False
    summary: str = "No analysis outputs were available for synthesis."
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
