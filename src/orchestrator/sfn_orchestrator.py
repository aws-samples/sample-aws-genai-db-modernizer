"""Step Functions orchestrator — cloud implementation.

Delegates job lifecycle to AWS Step Functions and stores metadata in DynamoDB.

Requirements: 1.2, 1.3
"""

from __future__ import annotations

import json
from typing import Any

from src.contracts.phase_models import Phase, PhaseProgression, PhaseRecord, PhaseStatus
from src.orchestrator.base import (
    Orchestrator,
    PhasePrerequisiteError,
    PhaseScope,
    TaskTokenNotFoundError,
)


class StepFunctionsOrchestrator(Orchestrator):
    """Cloud orchestrator backed by Step Functions + DynamoDB.

    - ``start_job`` calls ``sfn.start_execution``.
    - ``resume`` reads the task token from DynamoDB and calls ``sfn.send_task_success``.
    - ``get_progression`` reads ``phase_progression`` from DynamoDB.
    """

    def __init__(
        self,
        sfn_client: Any,
        dynamodb_table_name: str,
        state_machine_arn: str,
        dynamodb_resource: Any | None = None,
    ) -> None:
        self.sfn = sfn_client
        self.table_name = dynamodb_table_name
        self.state_machine_arn = state_machine_arn
        # Allow injecting a DynamoDB Table resource for easier testing
        self._ddb_resource = dynamodb_resource

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_item(self, job_id: str) -> dict:  # type: ignore[type-arg]
        """Read the DynamoDB item for *job_id*."""
        key = {"jobId": job_id, "timestamp": 0}
        if self._ddb_resource is not None:
            resp = self._ddb_resource.get_item(Key=key)
        else:
            import boto3

            table = boto3.resource("dynamodb").Table(self.table_name)
            resp = table.get_item(Key=key)
        return resp.get("Item", {})  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Orchestrator interface
    # ------------------------------------------------------------------

    def start_job(self, job_id: str, config: dict) -> None:
        """Start a Step Functions execution for the job."""
        self.sfn.start_execution(
            stateMachineArn=self.state_machine_arn,
            name=job_id,
            input=json.dumps(config),
        )

    def resume(self, job_id: str, phase: Phase, scope: PhaseScope | None = None) -> None:
        """Resume the paused state machine by sending task success with the stored token.

        Raises:
            TaskTokenNotFoundError: If no task token is stored (gate not reached yet).
            PhasePrerequisiteError: If the requested phase doesn't match the active gate.
        """
        item = self._get_item(job_id)
        token = item.get("task_token")
        if not token:
            raise TaskTokenNotFoundError(
                f"No task token found for job {job_id}. "
                "The execution has not reached the approval gate yet, "
                "or the token has already been consumed."
            )

        # Validate the requested phase matches the active gate stored by the Lambda
        raw_progression = item.get("phase_progression", {})
        active_gate: str | None = None
        for phase_name, status in raw_progression.items():
            if isinstance(status, str) and status.upper() in ("AWAITING_REVIEW", "AWAITING_INPUT"):
                active_gate = phase_name
                break

        if active_gate and active_gate != phase.value:
            raise PhasePrerequisiteError(
                f"Cannot resume phase '{phase.value}': "
                f"the active gate is '{active_gate}'. "
                f"Use phase='{active_gate}' to release the current gate."
            )

        output: dict[str, Any] = {"phase": phase.value}
        if scope is not None:
            output["scope"] = {"engines": scope.engines}

        self.sfn.send_task_success(
            taskToken=token,
            output=json.dumps(output),
        )

    def get_progression(self, job_id: str) -> PhaseProgression:
        """Read phase progression from DynamoDB.

        Handles two storage formats:
        1. Full PhaseProgression dict (written by the orchestrator itself)
        2. Flat {phase_name: status_string} dict (written by the StoreTaskToken Lambda)
        """
        item = self._get_item(job_id)
        raw = item.get("phase_progression")

        if raw:
            # Detect the flat Lambda format: {"assignment": "AWAITING_REVIEW"}
            # vs the full format which always has "job_id", "current_phase", "phases"
            if "job_id" not in raw:
                # Convert flat format to full PhaseProgression
                phases: dict[Phase, PhaseRecord] = {}
                current_phase = Phase.COLLECT_TRIAGE
                for p in Phase:
                    status_str = raw.get(p.value)
                    if status_str:
                        try:
                            status = PhaseStatus(status_str.lower())
                        except ValueError:
                            status = PhaseStatus.IN_PROGRESS
                        phases[p] = PhaseRecord(phase=p, status=status)
                        current_phase = p
                    else:
                        phases[p] = PhaseRecord(phase=p, status=PhaseStatus.NOT_STARTED)
                return PhaseProgression(
                    job_id=job_id,
                    current_phase=current_phase,
                    phases=phases,
                )
            return PhaseProgression.model_validate(raw)

        # No phase_progression stored yet — return default empty progression
        return PhaseProgression(
            job_id=job_id,
            current_phase=Phase.COLLECT_TRIAGE,
            phases={p: PhaseRecord(phase=p, status=PhaseStatus.NOT_STARTED) for p in Phase},
        )
