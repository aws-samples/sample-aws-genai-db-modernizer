"""Routing-quality gate: fail when a reviewed case's accuracy drops (#952).

``benchmarks/cases/<stage>/gate.json`` holds the floors::

    {
      "min_aggregate_acceptable_accuracy": 1.0,
      "min_case_acceptable_accuracy": {"kv-lookup-clean": 1.0, "wordpress-trimmed": 1.0}
    }

``check_gate`` returns one failure line per broken floor: an incomplete run
(a throttled or errored case), the aggregate below its floor, and any gated
case below its floor or not scored at all (removed, unreviewed, errored), so
a case can never pass the gate by dropping out of the report. A case with no
floor is reported but not gated. Both keys are required in gate.json. The assignment stage is deterministic, so a
drop is a code change, not noise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GATE_FILE = "gate.json"


class GateError(Exception):
    """gate.json is missing or malformed."""


def _floor(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise GateError(f"{where} must be a number from 0 to 1, got {value!r}")
    return float(value)


def load_gate(stage_dir: Path) -> dict[str, Any]:
    path = Path(stage_dir) / GATE_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError(f"{path} is not a JSON object")
    # Both keys are required and the case floors non-empty: a missing key
    # would leave the aggregate (or every case) silently ungated.
    for key in ("min_aggregate_acceptable_accuracy", "min_case_acceptable_accuracy"):
        if key not in data:
            raise GateError(f"{path}: missing {key}")
    cases = data["min_case_acceptable_accuracy"]
    if not isinstance(cases, dict) or not cases:
        raise GateError(f"{path}: min_case_acceptable_accuracy must be a non-empty object")
    gate: dict[str, Any] = {
        "min_case_acceptable_accuracy": {
            str(cid): _floor(v, f"{path}: floor for {cid}") for cid, v in cases.items()
        }
    }
    gate["min_aggregate_acceptable_accuracy"] = _floor(
        data["min_aggregate_acceptable_accuracy"], f"{path}: aggregate floor"
    )
    return gate


def check_gate(report: dict[str, Any], gate: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not report.get("complete", False):
        failures.append("incomplete run: a case was throttled or errored; re-run before gating")
    floor = gate.get("min_aggregate_acceptable_accuracy")
    agg = report.get("aggregate_acceptable_accuracy")
    if floor is not None and (agg is None or agg < floor):
        failures.append(f"aggregate acceptable-accuracy {agg} is below the floor {floor}")
    by_case = {c["case_id"]: c for c in report.get("per_case") or []}
    for cid, case_floor in sorted(gate.get("min_case_acceptable_accuracy", {}).items()):
        case = by_case.get(cid)
        if case is None:
            failures.append(f"{cid}: gated case missing from the run")
        elif case.get("outcome") != "scored" or case.get("acceptable_accuracy") is None:
            failures.append(f"{cid}: gated case was not scored (outcome {case.get('outcome')})")
        elif case["acceptable_accuracy"] < case_floor:
            failures.append(
                f"{cid}: acceptable-accuracy {case['acceptable_accuracy']} is below "
                f"the floor {case_floor}"
            )
    return failures
