# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``scripts/run_routine_repair.py`` (#1940): one JSON status line per run, in each mode
the command and a headless session use."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own script with fixed arguments
import sys
from pathlib import Path

from tests.unit.agents.routine_repair import repair_fixtures as fx

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "run_routine_repair.py"


def _run(tmp_path: Path, *args: str) -> tuple[int, list[dict]]:
    proc = subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(SCRIPT), "--job-id", fx.JOB, "--db", fx.DB,
         "--artifact-root", str(tmp_path), *args],
        capture_output=True, text=True, cwd=REPO, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )  # fmt: skip
    lines = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")]
    return proc.returncode, lines


def _write(path: str, data: dict) -> None:
    Path(path).write_text(json.dumps(data))


def test_mode_none_prints_one_skipped_line(tmp_path):
    fx.job(tmp_path)
    code, lines = _run(tmp_path, "--llm-mode", "none")
    assert code == 0
    (line,) = lines
    assert line["phase"] == "routine_repair" and line["status"] == "skipped"
    assert line["counts"]["not_repaired"] == 1


def test_external_then_finalize(tmp_path):
    fx.job(tmp_path)
    code, (line,) = _run(tmp_path, "--llm-mode", "external")
    assert code == 0 and line["status"] == "awaiting_llm"
    # #2385 review: the paths are printed under the artifact root, ready to read and write.
    request = json.loads(Path(line["llm_request"]).read_text())
    assert fx.LITERAL_MARK not in json.dumps(request)
    _write(
        line["llm_response"],
        {
            "repairs": [
                {
                    "kind": "function",
                    "routine_id": fx.WAL,
                    "repaired_create": fx.WAL_REPAIRED,
                    "change_note": "Calls the function by its PostgreSQL 10 name.",
                }
            ]
        },
    )
    code, (done,) = _run(tmp_path, "--finalize")
    assert code == 0 and done["status"] == "complete"
    assert done["counts"]["repaired_pending_proof"] == 1


def test_finalize_with_an_answer_off_the_contract_is_an_error_naming_where(tmp_path):
    fx.job(tmp_path)
    _, (line,) = _run(tmp_path, "--llm-mode", "external")
    _write(line["llm_response"], {"repairs": [], "status": "proved"})
    code, (err,) = _run(tmp_path, "--finalize")
    assert code == 1 and err["status"] == "error"
    assert "RoutineRepairResponse" in err["message"]
    assert "proved" not in err["message"]


def _rule_matches(rule: str, tool: str, path: str) -> bool:
    """Whether an ``Edit(...)``/``Write(...)`` permission rule covers ``path``."""
    import fnmatch

    if not (rule.startswith(f"{tool}(") and rule.endswith(")")):
        return False
    return fnmatch.fnmatchcase(path, rule[len(tool) + 1 : -1])


def test_a_headless_round_writes_its_answer_where_the_ci_settings_allow(tmp_path, monkeypatch):
    """#2385 review: /modernize hands the round subagent the line's ``llm_response`` as
    printed. Run from the repo root with the default artifact root, it is a path under
    ``artifacts/`` that ``.claude/settings.ci.json`` lets the Write tool write, and the
    file the next ``--finalize`` reads."""
    from scripts import run_routine_repair

    fx.job(tmp_path / "artifacts")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)
    monkeypatch.delenv("ARTIFACT_DIR", raising=False)
    monkeypatch.setattr(run_routine_repair, "_JOB", {})
    argv = ["run_routine_repair.py", "--job-id", fx.JOB, "--db", fx.DB, "--llm-mode", "external"]
    monkeypatch.setattr(sys, "argv", argv)
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        run_routine_repair.main()
    (line,) = [json.loads(x) for x in out.getvalue().splitlines() if x.startswith("{")]
    response, request = line["llm_response"], line["llm_request"]
    assert response == f"artifacts/{fx.DB}/{fx.JOB}/routines/v1/repair/llm_response.json"
    assert (tmp_path / request).is_file()
    permissions = json.loads((REPO / ".claude" / "settings.ci.json").read_text())["permissions"]
    assert any(_rule_matches(r, "Write", response) for r in permissions["allow"])
    assert not any(_rule_matches(r, "Write", response) for r in permissions["deny"])
    # The answer written there is the one --finalize applies.
    (tmp_path / response).write_text(json.dumps({"repairs": []}))
    monkeypatch.setattr(sys, "argv", argv[:5] + ["--finalize"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        run_routine_repair.main()
    (done,) = [json.loads(x) for x in out.getvalue().splitlines() if x.startswith("{")]
    assert done["status"] == "complete" and done["counts"]["no_answer"] == 1
    assert done["artifact"].startswith(f"artifacts/{fx.DB}/{fx.JOB}/")
