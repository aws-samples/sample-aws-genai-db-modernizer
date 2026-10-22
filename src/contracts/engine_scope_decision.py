"""
Engine Scope Decision — Contract Model

The customer's engine scope for one job (#1328, ADR-031 Decision 2 Amendment 14):
the one ``target_databases`` setting, recorded at
``<db>/<job>/decisions/engine_scope.json`` when the job is started with it, through
the API, ``scripts/run_assessment.py --target-databases`` or the orchestrator's
``start_job``. Triage reads it when it is not handed a scope itself, so every entry
point routes the same way; a job with no such file has no scope (every engine).

Version History:
- 1.0 (2026-10-22, #1328): Initial version: the engines, who chose them, through
  which entry point, and when.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EngineScopeDecision(BaseModel):
    """The engines a customer chose to consider for one job."""

    contract_version: str = Field(
        default="1.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    job_id: str = Field(..., description="The job the scope applies to")
    database_name: str = Field(..., description="The job's database name")
    engines: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "The engine catalog ids in scope, active engines only, sorted, each once "
            "(src.shared.target_databases.normalize_target_databases)"
        ),
    )
    decided_by: Literal["customer"] = Field(
        "customer", description="Who chose the scope: the customer, never the pipeline"
    )
    via: Literal["api", "cli", "library"] = Field(
        ...,
        description=(
            "The entry point the customer gave it to: the Assessments API, "
            "scripts/run_assessment.py, or the orchestrator's start_job"
        ),
    )
    decided_at: datetime = Field(..., description="When the scope was recorded (UTC)")
