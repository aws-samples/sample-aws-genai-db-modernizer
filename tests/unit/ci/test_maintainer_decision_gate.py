"""The Maintainer Decision gate workflow blocks merging a labelled PR (#388)."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/maintainer-decision-gate.yml"
LABEL = "Maintainer Decision"
# The internal validation mirror keeps only part of .github/ and no workflows
# (see tests/unit/test_agents_md.py). .github/workflows/ marks the GitHub
# source tree; without it the workflow checks have nothing to check.
GITHUB_SOURCE = WORKFLOW.parent.is_dir()


def _workflow() -> dict:
    loaded: dict = yaml.safe_load(WORKFLOW.read_text())
    return loaded


def _triggers(wf: dict) -> dict:
    # PyYAML reads the bare `on:` key as the boolean True.
    triggers: dict = wf.get("on") or wf[True]
    return triggers


def test_runs_when_labels_change_and_on_every_push_to_the_pr():
    if not GITHUB_SOURCE:
        assert not WORKFLOW.exists()
        return
    types = set(_triggers(_workflow())["pull_request"]["types"])
    assert {"opened", "reopened", "synchronize", "labeled", "unlabeled"} <= types


def test_only_the_newest_run_per_pr_counts():
    if not GITHUB_SOURCE:
        assert not WORKFLOW.exists()
        return
    concurrency = _workflow()["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


def test_needs_no_token_permissions():
    if not GITHUB_SOURCE:
        assert not WORKFLOW.exists()
        return
    assert _workflow()["permissions"] == {}


def test_the_required_check_has_a_stable_job_name():
    if not GITHUB_SOURCE:
        assert not WORKFLOW.exists()
        return
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["maintainer-decision-gate"]
    assert "steps" in jobs["maintainer-decision-gate"]


def test_fails_on_the_exact_label_name():
    if not GITHUB_SOURCE:
        assert not WORKFLOW.exists()
        return
    text = WORKFLOW.read_text()
    assert f"'{LABEL}'" in text
    assert "exit 1" in text


def test_agents_md_forbids_agents_removing_the_label_on_their_own():
    agents_md = (WORKFLOW.parents[2] / "AGENTS.md").read_text()
    assert f'"{LABEL}" label' in agents_md
    assert "agents never\n  remove it unless a human explicitly says to" in agents_md
