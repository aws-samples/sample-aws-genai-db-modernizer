"""Step Functions service — reads execution state for pipeline progress."""

import json
from datetime import UTC, datetime

import boto3


class StepFunctionsService:
    def __init__(self, state_machine_arn: str):
        self.state_machine_arn = state_machine_arn
        self.client = boto3.client("stepfunctions")

    def start_execution(self, job_id: str, sfn_input: dict) -> dict:
        """Start a new Step Functions execution."""
        response = self.client.start_execution(
            stateMachineArn=self.state_machine_arn,
            name=job_id,
            input=json.dumps(sfn_input),
        )
        return {
            "execution_arn": response["executionArn"],
            "start_date": response["startDate"].isoformat(),
        }

    def describe_execution(self, job_id: str) -> dict | None:
        """Get execution status by job_id, including error details for failed executions."""
        execution_arn = self._execution_arn(job_id)
        try:
            response = self.client.describe_execution(executionArn=execution_arn)
        except self.client.exceptions.ExecutionDoesNotExist:
            return None

        result = {
            "status": response["status"],
            "started_at": response["startDate"].isoformat(),
            "stopped_at": response.get("stopDate", datetime.now(UTC)).isoformat()
            if response["status"] != "RUNNING"
            else None,
            "input": json.loads(response.get("input", "{}")),
        }

        # Surface error details for failed executions
        if response["status"] in ("FAILED", "TIMED_OUT", "ABORTED"):
            result["error"] = response.get("error")
            result["cause"] = response.get("cause")

        return result

    def get_execution_history(self, job_id: str) -> list[dict]:
        """Get per-state events from execution history.

        Returns a list of agent stages with status and timing derived
        from Step Functions execution history events.
        """
        execution_arn = self._execution_arn(job_id)
        try:
            response = self.client.get_execution_history(
                executionArn=execution_arn,
                maxResults=200,
                reverseOrder=False,
            )
        except self.client.exceptions.ExecutionDoesNotExist:
            return []

        stages: dict[str, dict] = {}
        for event in response.get("events", []):
            event_type = event.get("type", "")
            timestamp = event.get("timestamp", datetime.now(UTC)).isoformat()

            # TaskStateEntered = agent started
            if event_type == "TaskStateEntered":
                details = event.get("stateEnteredEventDetails", {})
                name = details.get("name", "")
                if name:
                    stages[name] = {
                        "name": name,
                        "status": "in-progress",
                        "started_at": timestamp,
                        "completed_at": None,
                    }

            # TaskStateExited = agent completed
            elif event_type == "TaskStateExited":
                details = event.get("stateExitedEventDetails", {})
                name = details.get("name", "")
                if name and name in stages:
                    stages[name]["status"] = "completed"
                    stages[name]["completed_at"] = timestamp

            # TaskFailed / ExecutionFailed
            elif "Failed" in event_type:
                details = event.get("stateEnteredEventDetails", {}) or event.get(
                    "executionFailedEventDetails", {}
                )
                name = details.get("name", "")
                if name and name in stages:
                    stages[name]["status"] = "failed"

        return list(stages.values())

    def get_full_execution_history(self, job_id: str) -> list[dict]:
        """Get complete execution history with all state types.

        Returns a hierarchical list: top-level states, with Map states
        containing MapIteration children, each containing their sub-steps.
        Uses previousEventId chains to correctly assign states to iterations
        even when iterations run in parallel.
        """
        execution_arn = self._execution_arn(job_id)
        try:
            events = []
            params = {
                "executionArn": execution_arn,
                "maxResults": 1000,
                "reverseOrder": False,
            }
            while True:
                response = self.client.get_execution_history(**params)
                events.extend(response.get("events", []))
                if "nextToken" in response:
                    params["nextToken"] = response["nextToken"]
                else:
                    break
        except self.client.exceptions.ExecutionDoesNotExist:
            return []

        exec_start = None

        # Phase 1: Index events and build previousEventId → iteration mapping
        events_by_id: dict[int, dict] = {}
        # Maps event_id → iteration_key (e.g. "RunEnginePipelines#0")
        event_to_iteration: dict[int, str] = {}
        # Track MapIterationStarted event IDs
        iteration_start_events: dict[int, str] = {}

        for event in events:
            eid = event.get("id", 0)
            events_by_id[eid] = event
            if event.get("type") == "ExecutionStarted":
                exec_start = event.get("timestamp")
            elif event.get("type") == "MapIterationStarted":
                details = event.get("mapIterationStartedEventDetails", {})
                name = details.get("name", "")
                index = details.get("index", 0)
                key = f"{name}#{index}"
                iteration_start_events[eid] = key
                event_to_iteration[eid] = key

        # For each non-iteration event inside the map, trace previousEventId
        # back to find which iteration it belongs to
        def _find_iteration(eid: int, visited: set | None = None) -> str | None:
            if visited is None:
                visited = set()
            if eid in event_to_iteration:
                return event_to_iteration[eid]
            if eid in visited or eid not in events_by_id:
                return None
            visited.add(eid)
            prev = events_by_id[eid].get("previousEventId", 0)
            result = _find_iteration(prev, visited)
            if result:
                event_to_iteration[eid] = result
            return result

        # Pre-compute iteration assignment for all events
        for event in events:
            eid = event.get("id", 0)
            if eid not in event_to_iteration:
                _find_iteration(eid)

        # Phase 2: Build state entries
        state_type_map = {
            "Task": "Task",
            "Map": "Map",
            "Parallel": "Parallel",
            "Pass": "Pass",
            "Wait": "Wait",
            "Choice": "Choice",
            "Succeed": "Succeed",
            "Fail": "Fail",
        }

        top_order: list[str] = []
        top_states: dict[str, dict] = {}
        # iteration_key → { state, children: [{ state, ... }] }
        iterations: dict[str, dict] = {}
        # Track child state keys per iteration to avoid duplicates
        iteration_child_keys: dict[str, dict[str, dict]] = {}

        for event in events:
            eid = event.get("id", 0)
            event_type = event.get("type", "")
            timestamp = event.get("timestamp", datetime.now(UTC))
            ts_iso = timestamp.isoformat()
            iteration_key = event_to_iteration.get(eid)

            # MapIteration lifecycle
            if event_type == "MapIterationStarted":
                details = event.get("mapIterationStartedEventDetails", {})
                name = details.get("name", "")
                index = details.get("index", 0)
                key = f"{name}#{index}"
                iterations[key] = {
                    "state": {
                        "name": f"#{index}",
                        "type": "MapIteration",
                        "status": "in-progress",
                        "started_at": ts_iso,
                        "completed_at": None,
                    },
                    "children": [],
                }
                iteration_child_keys[key] = {}
                continue

            if event_type in ("MapIterationSucceeded", "MapIterationFailed", "MapIterationAborted"):
                details = (
                    event.get("mapIterationSucceededEventDetails")
                    or event.get("mapIterationFailedEventDetails")
                    or event.get("mapIterationAbortedEventDetails")
                    or {}
                )
                name = details.get("name", "")
                index = details.get("index", 0)
                key = f"{name}#{index}"
                if key in iterations:
                    it = iterations[key]
                    if "Succeeded" in event_type:
                        it["state"]["status"] = "completed"
                    else:
                        it["state"]["status"] = "failed"
                    it["state"]["completed_at"] = ts_iso
                    # Resolve name from first child's agent_type
                    for child in it["children"]:
                        if child.get("_agent_type"):
                            it["state"]["name"] = child["_agent_type"]
                            break
                continue

            # Regular state events
            for prefix, stype in state_type_map.items():
                if event_type == f"{prefix}StateEntered":
                    details = event.get("stateEnteredEventDetails", {})
                    name = details.get("name", "")
                    if not name:
                        break

                    entry = {
                        "name": name,
                        "type": stype,
                        "status": "in-progress",
                        "started_at": ts_iso,
                        "completed_at": None,
                    }

                    # Parse agent_type from input
                    try:
                        inp_str = details.get("input", "{}")
                        inp = json.loads(inp_str) if isinstance(inp_str, str) else inp_str
                        if isinstance(inp, dict) and inp.get("agent_type"):
                            entry["_agent_type"] = inp["agent_type"]
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        pass

                    # Only assign to iteration if it's still active
                    active_iter = (
                        iteration_key
                        and iteration_key in iterations
                        and iterations[iteration_key]["state"]["status"] == "in-progress"
                    )
                    if active_iter and iteration_key is not None:
                        child_key = f"{name}-{len(iteration_child_keys[iteration_key])}"
                        iteration_child_keys[iteration_key][child_key] = entry
                        iterations[iteration_key]["children"].append(entry)
                    else:
                        if name not in top_states:
                            top_order.append(name)
                        top_states[name] = entry
                    break

                elif event_type == f"{prefix}StateExited":
                    details = event.get("stateExitedEventDetails", {})
                    name = details.get("name", "")
                    if not name:
                        break

                    active_iter_exit = (
                        iteration_key
                        and iteration_key in iterations
                        and iterations[iteration_key]["state"]["status"] == "in-progress"
                    )
                    if active_iter_exit and iteration_key is not None:
                        # Find the last in-progress child with this name
                        for child in reversed(iterations[iteration_key]["children"]):
                            if child["name"] == name and child["status"] == "in-progress":
                                child["status"] = "completed"
                                child["completed_at"] = ts_iso
                                break
                    elif name in top_states and top_states[name]["status"] == "in-progress":
                        top_states[name]["status"] = "completed"
                        top_states[name]["completed_at"] = ts_iso
                    break

        # Phase 3: Build result with timing
        def _fmt(state: dict) -> dict:
            duration = None
            started_after = None
            if state.get("started_at"):
                start_dt = datetime.fromisoformat(state["started_at"])
                if state.get("completed_at"):
                    end_dt = datetime.fromisoformat(state["completed_at"])
                    duration = round((end_dt - start_dt).total_seconds())
                if exec_start:
                    started_after = round((start_dt - exec_start).total_seconds())
            return {
                "name": state["name"],
                "type": state["type"],
                "status": state["status"],
                "duration_seconds": duration,
                "started_after_seconds": started_after,
                "started_at": state.get("started_at"),
                "completed_at": state.get("completed_at"),
                "children": [],
            }

        result = []
        for key in top_order:
            s = top_states[key]
            formatted = _fmt(s)

            if s["type"] == "Map":
                # Attach iterations as children, sorted by index
                map_name = s["name"]
                iter_keys = sorted(
                    [k for k in iterations if k.startswith(f"{map_name}#")],
                    key=lambda k: int(k.split("#")[1]),
                )
                for ik in iter_keys:
                    it = iterations[ik]
                    it_fmt = _fmt(it["state"])
                    it_fmt["children"] = [_fmt(c) for c in it["children"]]
                    formatted["children"].append(it_fmt)

            result.append(formatted)

        return result

    def stop_execution(self, job_id: str) -> bool:
        """Stop a running execution."""
        execution_arn = self._execution_arn(job_id)
        try:
            self.client.stop_execution(
                executionArn=execution_arn,
                cause="Cancelled by user via API",
            )
            return True
        except Exception:
            return False

    def list_executions(
        self, status_filter: str | None = None, max_results: int = 50
    ) -> list[dict]:
        """List recent executions."""
        params: dict = {
            "stateMachineArn": self.state_machine_arn,
            "maxResults": min(max_results, 100),
        }
        if status_filter:
            params["statusFilter"] = status_filter

        response = self.client.list_executions(**params)
        return [
            {
                "job_id": ex["name"],
                "status": ex["status"],
                "started_at": ex["startDate"].isoformat(),
                "stopped_at": ex["stopDate"].isoformat() if ex.get("stopDate") else None,
            }
            for ex in response.get("executions", [])
        ]

    def _execution_arn(self, job_id: str) -> str:
        """Build execution ARN from job_id."""
        return f"{self.state_machine_arn.replace('stateMachine', 'execution')}:{job_id}"
