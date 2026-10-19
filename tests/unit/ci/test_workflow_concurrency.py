"""Superseded pull request CI runs are cancelled; queue and main runs are not (#1174)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return data


def _cancels(concurrency: dict, event_name: str) -> bool:
    """Evaluate the cancel-in-progress expression for one event."""
    expr = str(concurrency["cancel-in-progress"]).strip()
    if expr in ("True", "true"):
        return True
    if expr in ("False", "false"):
        return False
    assert expr.startswith("${{") and expr.endswith("}}"), expr
    inner = expr[3:-2].strip()
    assert inner == "github.event_name == 'pull_request'", inner
    return event_name == "pull_request"


@pytest.mark.parametrize("name", ["ci.yml", "docs.yml"])
def test_workflow_cancels_only_pull_request_runs(name):
    conc = _load(name).get("concurrency")
    assert conc is not None, f"{name} has no top-level concurrency"
    assert "github.workflow" in conc["group"]
    group = conc["group"]
    # Keyed on the PR number, never the branch name: fork PRs from branches
    # with the same name (often `main`) must not share a group.
    assert "github.event.number" in group
    assert "head_ref" not in group
    assert "github.event.pull_request" not in group
    assert "github.run_id" in conc["group"]
    assert "github.ref" not in conc["group"]
    assert _cancels(conc, "pull_request") is True
    for event in ("merge_group", "push", "schedule", "workflow_dispatch"):
        assert _cancels(conc, event) is False, f"{name} cancels {event} runs"


def test_ci_required_aggregate_checks_still_report():
    jobs = _load("ci.yml")["jobs"]
    for job, shard in (("test", "test-shard"), ("e2e", "e2e-shard")):
        assert jobs[job]["if"] == "always()"
        assert jobs[job]["needs"] == shard


def test_docs_pages_deploy_group_is_not_cancelled():
    deploy = _load("docs.yml")["jobs"]["deploy"]["concurrency"]
    assert deploy["group"] == "pages"
    assert deploy["cancel-in-progress"] is False


@pytest.mark.parametrize("name", ["ci.yml", "docs.yml"])
def test_group_is_per_pull_request_and_per_run_otherwise(name):
    group = str(_load(name)["concurrency"]["group"])
    assert "github.event.number && format('pr-{0}', github.event.number) || github.run_id" in group

    def resolve(number: int | None, run_id: int) -> str:
        key = f"pr-{number}" if number else str(run_id)
        return f"{name}-{key}"

    # Two fork PRs from same-named branches get different groups.
    assert resolve(7, 1) != resolve(8, 2)
    # Non-PR events (no number) get a group per run, so two pushes to main
    # never share a group (a shared group drops pending runs).
    assert resolve(None, 100) != resolve(None, 101)
