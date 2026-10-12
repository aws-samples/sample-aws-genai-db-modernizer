"""check_decision's artifact root default must match what the scripts write (./artifacts)."""

from __future__ import annotations

from scripts import check_decision


def test_artifact_root_default_is_artifacts_dir() -> None:
    parser = check_decision.build_parser()
    args = parser.parse_args(
        ["--job-id", "job1", "--db", "dynamodb", "--decision", "triage_approval"]
    )

    assert args.artifact_root == "./artifacts"


def test_rejects_invalid_db_outside_sandbox(monkeypatch, capsys) -> None:
    """check_decision.py takes --db too, so it must validate it the same way
    as every other scripts/run_*.py entry point (#420 review), not only
    under MODERNIZER_CI_SANDBOX=1."""
    import json
    import sys

    import pytest

    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_decision.py", "--job-id", "job1", "--db", "../escape", "--decision", "x"],
    )
    with pytest.raises(SystemExit) as exc:
        check_decision.main()
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "database_name" in out["message"]


def test_sandbox_rejects_decision_name_traversal(monkeypatch, capsys) -> None:
    import json
    import sys

    import pytest

    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_decision.py", "--job-id", "job1", "--db", "wordpress", "--decision", "../../x"],
    )
    with pytest.raises(SystemExit) as exc:
        check_decision.main()
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "--decision" in out["message"]
