"""Unit tests for StepFunctionsService.get_execution_history.

Regression guard for the "assessments never end" UI bug: a full multi-engine
run emits more execution-history events than a single API page holds. The final
state (RunRefereeSynthesis) enters on the first page but exits on a later page.
Without pagination the exit event is never read, so the stage is reported as
"in-progress" with completed_at=null forever, even though the execution and its
ECS task both succeeded.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from src.api.services.step_functions import StepFunctionsService

_TS = datetime(2026, 7, 31, 15, 25, 0, tzinfo=UTC)


def _entered(state: str) -> dict:
    return {
        "type": "TaskStateEntered",
        "timestamp": _TS,
        "stateEnteredEventDetails": {"name": state},
    }


def _exited(state: str) -> dict:
    return {
        "type": "TaskStateExited",
        "timestamp": _TS,
        "stateExitedEventDetails": {"name": state},
    }


def _service_with_pages(pages: list[dict]) -> StepFunctionsService:
    """Build a service whose boto client returns the given pages in order."""
    svc = StepFunctionsService.__new__(StepFunctionsService)
    svc.state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:modernizer-dev"
    client = MagicMock()
    client.get_execution_history.side_effect = pages
    client.exceptions.ExecutionDoesNotExist = type("ExecutionDoesNotExist", (Exception,), {})
    svc.client = client
    return svc


def test_history_spanning_multiple_pages_marks_final_stage_completed():
    """The final stage's exit event on a later page must be read via nextToken."""
    # Page 1: synthesis has entered but not yet exited.
    page1 = {
        "events": [
            _entered("RunCollector"),
            _exited("RunCollector"),
            _entered("RunRefereeSynthesis"),
        ],
        "nextToken": "page-2",
    }
    # Page 2: synthesis exits here — only reachable by following nextToken.
    page2 = {"events": [_exited("RunRefereeSynthesis")]}

    svc = _service_with_pages([page1, page2])
    stages = {s["name"]: s for s in svc.get_execution_history("job-1")}

    synthesis = stages["RunRefereeSynthesis"]
    assert synthesis["status"] == "completed"
    assert synthesis["completed_at"] is not None
