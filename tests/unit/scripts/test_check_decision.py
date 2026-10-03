"""check_decision's artifact root default must match what the scripts write (./artifacts)."""

from __future__ import annotations

from scripts import check_decision


def test_artifact_root_default_is_artifacts_dir() -> None:
    parser = check_decision.build_parser()
    args = parser.parse_args(
        ["--job-id", "job1", "--db", "dynamodb", "--decision", "triage_approval"]
    )

    assert args.artifact_root == "./artifacts"
