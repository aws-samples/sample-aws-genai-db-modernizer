# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DMS Step Preflight — Contract Model

The read-only checks run before step 1's DMS stack deploys (#1799), written at
``<db>/<job>/data-movement/step-<k>/preflight.json`` by
``src.tools.data_movement.dms_preflight``: one row per check, always all of them and in a
fixed order, with the evidence read from AWS (never a secret's value). The run stops at the
first failure; the rows after it are ``not_run``.

Version History:
- 1.0 (2026-11-03, #1799): Initial version.
- 1.1 (2026-11-11, #2123): Added the lob_sizes check, first in CHECK_ORDER (a MySQL LOB
  table the collection did not measure stops step 1); a file with the eight earlier
  checks still loads.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CheckId = Literal[
    "lob_sizes",
    "aurora_cluster",
    "aurora_parameters",
    "secrets",
    "instance_class_offered",
    "dms_service_roles",
    "source_cdc",
    "dms_certificates",
    "secrets_manager_reachable",
]
CHECK_ORDER: tuple[str, ...] = (
    "lob_sizes",
    "aurora_cluster",
    "aurora_parameters",
    "secrets",
    "instance_class_offered",
    "dms_service_roles",
    "source_cdc",
    "dms_certificates",
    "secrets_manager_reachable",
)
# The checks of a preflight written before lob_sizes (#2123): such a file still loads.
LEGACY_CHECK_ORDER: tuple[str, ...] = CHECK_ORDER[1:]


class PreflightCheck(BaseModel):
    """One check and what it read."""

    id: CheckId = Field(..., description="Which check this row is")
    status: Literal["pass", "fail", "confirm", "not_run"] = Field(
        ...,
        description=(
            "pass, fail, confirm (the customer must confirm the listed items with "
            "--confirm <check id>) or not_run (an earlier check failed)"
        ),
    )
    summary: str = Field(..., min_length=1, description="One sentence: what was found")
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description="The values read from AWS, by name (never a secret's value)",
    )
    message: str | None = Field(None, description="What to do, on fail or confirm")
    checklist: list[str] = Field(
        default_factory=list, description="Items the API cannot read, for the customer to confirm"
    )


class DmsPreflight(BaseModel):
    """Step 1's preflight result (#1799)."""

    contract_version: str = Field(
        default="1.1", pattern=r"^\d+\.\d+$", description="Contract version of this file"
    )
    job_id: str = Field(..., description="The job the step belongs to")
    database_name: str = Field(..., description="The source database the job assesses")
    step: int = Field(1, ge=1, description="The migration step checked")
    region: str = Field(..., min_length=1, description="The AWS region the checks read")
    checked_at: datetime = Field(..., description="When the checks ran")
    contract_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the step's transformation contract file",
    )
    instance_class: str = Field(
        ...,
        description="The replication instance class checked (the plan's, or the caller's override)",
    )
    status: Literal["pass", "fail", "needs_confirmation"] = Field(
        ...,
        description="fail if any check failed, else needs_confirmation if any needs confirming, else pass",
    )
    checks: list[PreflightCheck] = Field(
        ...,
        description=(
            "One row per check, all present, in CHECK_ORDER (a file written before #2123 has "
            "LEGACY_CHECK_ORDER's eight)"
        ),
    )

    @model_validator(mode="after")
    def _consistent(self) -> "DmsPreflight":
        if tuple(c.id for c in self.checks) not in (CHECK_ORDER, LEGACY_CHECK_ORDER):
            raise ValueError(f"checks must be one row per check, in order: {CHECK_ORDER}")
        statuses = [c.status for c in self.checks]
        if "fail" in statuses:
            first = statuses.index("fail")
            if any(s != "not_run" for s in statuses[first + 1 :]):
                raise ValueError("every check after the first failure is not_run")
            expected = "fail"
        elif "not_run" in statuses:
            raise ValueError("a check is not_run only after a failure")
        else:
            expected = "needs_confirmation" if "confirm" in statuses else "pass"
        if self.status != expected:
            raise ValueError(f"status {self.status} disagrees with the rows ({expected})")
        return self
