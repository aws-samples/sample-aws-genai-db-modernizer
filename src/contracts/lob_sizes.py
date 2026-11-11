# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Step 1 LOB Sizes — Contract Model

The LOB sizes the customer measured, or chose not to measure, for step 1's moved tables
that the collection did not measure (#2123), written at
``<db>/<job>/data-movement/step-1/lob-sizes.json`` by ``scripts/run_dms_step.py lob-sizes``.
The MySQL source endpoint truncates a LOB above the task's limit with no task error, so a
MySQL table is never given a silent size: preflight's ``lob_sizes`` check stops until each
such table is measured by the collection or by the read-only ``lob-sizes.sql``, whose output
this file records, or until the customer explicitly skips measuring (``"lob_sizes": "skip"``
in step 1's inputs.json). The step 1 plan writer reads it: a measured column's longest value
sizes its table as the collection's ``max_value_bytes`` would. Lengths in bytes only, never a
value. ``contract_sha256`` and ``measured_at`` record which script run measured them and are
not checked when the file is read: the lengths describe the source's data, not a contract
(measure again if the data grew). The choice to skip is recorded here, but each plan and
preflight decides it from step 1's inputs.json at the time: taking ``"lob_sizes": "skip"`` out
of it makes the unmeasured tables unmeasured again.

Version History:
- 1.0 (2026-11-11, #2123): Initial version.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LobSizeColumn(BaseModel):
    """One LOB column's longest stored value, as ``lob-sizes.sql`` measured it."""

    table: str = Field(..., min_length=1, description="Source table id (schema.table)")
    column: str = Field(..., min_length=1, description="The LOB column, as the collection names it")
    max_value_bytes: int = Field(
        ..., ge=0, description="MAX(LENGTH(column)) on the source, in bytes (0 for no value)"
    )
    values: int = Field(..., ge=0, description="The column's non-NULL values when it was measured")


class LobSizes(BaseModel):
    """What step 1 knows of the LOB sizes the collection did not measure (#2123)."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version of this file"
    )
    job_id: str = Field(..., min_length=1, description="The job")
    database_name: str = Field(..., min_length=1, description="The job's database name")
    step: int = Field(1, ge=1, description="The migration step")
    choice: Literal["measured", "skipped"] = Field(
        ...,
        description=(
            "measured: the customer ran lob-sizes.sql and its output was read; skipped: the "
            "customer chose not to measure (inputs.json lob_sizes skip), so each unmeasured "
            "table runs at the catalog's lob.skipped_max_kb"
        ),
    )
    recorded_at: datetime = Field(..., description="When this file was written")
    contract_sha256: str | None = Field(
        None,
        pattern=r"^[0-9a-f]{64}$",
        description="measured: the SHA-256 of the step contract lob-sizes.sql was written for",
    )
    measured_at: str | None = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}$",
        description="measured: the source server's UTC time when the script's run started",
    )
    columns: list[LobSizeColumn] = Field(
        default_factory=list,
        description="The measured columns, sorted by table and column (kept when skipped later)",
    )

    @model_validator(mode="after")
    def _consistent(self) -> "LobSizes":
        if self.choice == "measured" and (self.contract_sha256 is None or self.measured_at is None):
            raise ValueError("a measured file names its contract and when the run started")
        keys = [(c.table, c.column) for c in self.columns]
        if keys != sorted(set(keys)):
            raise ValueError("columns are sorted by table and column, each once")
        return self
