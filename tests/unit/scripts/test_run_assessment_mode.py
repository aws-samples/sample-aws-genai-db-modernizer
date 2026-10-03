"""``run_assessment.py --mode`` records the experience mode at creation (#246).

The state file is created by run_assessment.py, so /modernize used to edit
``experience_mode`` afterwards; in one headless run it tried to Write a
``.modernizer-state.json.mode-note`` sidecar instead. ``--mode`` sets it when
the state is created; without it the default stays ``"both"``.
"""

from __future__ import annotations

import json
import sys

import pytest

from scripts import run_assessment


class _StopAfterState(Exception):
    pass


def _run(monkeypatch, tmp_path, *extra: str) -> dict:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_assessment, "phase_collect", lambda f, db, store: ("job-1", "wordpress")
    )

    def stop(*args, **kwargs):
        raise _StopAfterState

    monkeypatch.setattr(run_assessment, "phase_triage", stop)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assessment.py",
            "--file",
            "collector.json",
            "--db",
            "wordpress",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            *extra,
        ],
    )
    with pytest.raises(_StopAfterState):
        run_assessment.main()
    state: dict = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    return state


@pytest.mark.parametrize("mode", ["chat", "ui", "both"])
def test_mode_sets_experience_mode_at_creation(monkeypatch, tmp_path, mode):
    assert _run(monkeypatch, tmp_path, "--mode", mode)["experience_mode"] == mode


def test_default_experience_mode_is_unchanged(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path)["experience_mode"] == "both"


def test_mode_rejects_unknown_values(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--file", "x.json", "--mode", "web"])
    with pytest.raises(SystemExit) as exc:
        run_assessment.main()
    assert exc.value.code == 2


def test_mode_is_recorded_on_an_existing_job_state(monkeypatch, tmp_path):
    # --job-id with an existing state file: an explicit --mode still wins.
    monkeypatch.chdir(tmp_path)
    (tmp_path / run_assessment.STATE_FILE).write_text(
        json.dumps({"job_id": "job-1", "experience_mode": "both", "phase_status": {}})
    )

    def stop(*args, **kwargs):
        raise _StopAfterState

    monkeypatch.setattr(run_assessment, "phase_triage", stop)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assessment.py",
            "--job-id",
            "job-1",
            "--db",
            "wordpress",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--mode",
            "chat",
        ],
    )
    with pytest.raises(_StopAfterState):
        run_assessment.main()
    state = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    assert state["experience_mode"] == "chat"
