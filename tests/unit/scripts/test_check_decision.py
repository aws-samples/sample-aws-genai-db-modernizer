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


def test_the_engine_scope_is_not_an_approval_to_poll(monkeypatch, capsys, tmp_path) -> None:
    """#1328: decisions/engine_scope.json has its own contract and no ``status``."""
    import json
    import sys

    import pytest

    from src.agents.referee.engine_scope import record_engine_scope
    from src.storage.local_store import LocalArtifactStore

    record_engine_scope(LocalArtifactStore(str(tmp_path)), "wordpress", "job1", ["dynamodb"], "cli")
    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_decision.py", "--job-id", "job1", "--db", "wordpress",
            "--decision", "engine_scope", "--artifact-root", str(tmp_path),
        ],
    )  # fmt: skip
    with pytest.raises(SystemExit) as exc:
        check_decision.main()
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert "not an approval" in out["message"]
