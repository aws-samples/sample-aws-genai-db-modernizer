# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_dms_step.py verify-scripts`` and ``verify`` (#1998): no AWS call, files the customer
runs, their outputs read strictly, a stale result never left behind, the gate untouched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_dms_step as script
from src.contracts.verify_result import VerifyResult
from src.shared.contract_versioning import load_contract
from src.tools.data_movement.verify_scripts import OUTPUTS, verify_tables
from tests.unit.scripts.test_run_dms_step import _job, _run
from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle

REPO_ROOT = Path(__file__).resolve().parents[3]
SENTINEL = "s3cr3t-v4lue"


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    """Any AWS client the helper tried to build fails the test."""

    def refuse(*_a, **_k):
        raise AssertionError("verify made an AWS call")

    monkeypatch.setattr("src.tools.aws.credentials.AWSCredentialManager", refuse)


def _step(tmp_path: Path) -> Path:
    return tmp_path / "shop" / "job1" / "data-movement" / "step-1"


def _sha(capsys, tmp_path) -> str:
    code, line = _run(tmp_path, "verify-scripts", capsys=capsys)
    assert code == 0, line
    return Path(line["files"]["verify-source.sql"]).read_text().split("SHA-256: ")[1][:64]


def _output(tmp_path: Path, side: str, run: int, sha: str, lines: dict[int, tuple]) -> None:
    engine = "mysql" if side == "source" else "aurora_mysql"
    started = f"2026-11-09T1{run}:00:00.000000"
    body = [f"modernizer-verify\t1\t{side}\t{engine}\t{sha}\t{started}"]
    body += [f"table\t{n}\t{a}\t0\t{b}\t{c}" for n, (a, b, c) in lines.items()]
    body.append("modernizer-verify-end\t\t\t\t\t")
    (_step(tmp_path) / OUTPUTS[(side, run)]).write_text("\n".join(body) + "\n")  # type: ignore[index]


def test_verify_scripts_writes_the_two_scripts_and_instructions_with_no_aws_call(tmp_path, capsys):
    _job(tmp_path)
    code, line = _run(tmp_path, "verify-scripts", capsys=capsys)
    assert code == 0, line
    assert line["status"] == "ready" and line["tables"] == 3
    assert set(line["files"]) == {"verify-source.sql", "verify-target.sql", "verify.md"}
    assert line["gate"] == "verify is optional and does not change the gate"
    target = (_step(tmp_path) / "verify-target.sql").read_text()
    assert "FROM `shop`.`orders`" in target  # the plan's database: the default TargetDatabaseName


def test_verify_scripts_reads_the_inputs_target_database(tmp_path, capsys):
    _job(tmp_path, target_database_name="shop_new")
    _run(tmp_path, "verify-scripts", capsys=capsys)
    target = (_step(tmp_path) / "verify-target.sql").read_text()
    assert "FROM `shop_new`.`orders`" in target


def test_verify_compares_writes_verify_json_and_prints_no_checksum(tmp_path, capsys):
    _job(tmp_path)
    sha = _sha(capsys, tmp_path)
    tables = {t.table_id: t.n for t in verify_tables(make_bundle().contract)[0]}
    same = {n: (n, 987654321, 123456789) for n in tables.values()}
    _output(tmp_path, "source", 1, sha, same)
    _output(tmp_path, "target", 1, sha, {**same, tables["shop.orders"]: (3, 1, 1)})
    code, line = _run(tmp_path, "verify", capsys=capsys)
    assert code == 0, line
    assert (line["status"], line["match"], line["differ"], line["second_run"]) == (
        "differ",
        2,
        1,
        False,
    )
    assert line["not_matched"] == [
        {"table_id": "shop.orders", "status": "differ", "missing_on": [], "unhashed_on": []}
    ]
    raw = json.loads(Path(line["result"]).read_text())
    result = load_contract(VerifyResult, raw, artifact="verify.json")
    assert result.gate_effect == "none"
    for text in (json.dumps(line), json.dumps(raw)):
        assert "987654321" not in text and "123456789" not in text


def test_a_refused_file_or_none_yet_removes_an_earlier_result(tmp_path, capsys):
    _job(tmp_path)
    sha = _sha(capsys, tmp_path)
    same = {n: (1, 2, 3) for n in (1, 2, 3)}
    _output(tmp_path, "source", 1, sha, same)
    _output(tmp_path, "target", 1, sha, same)
    _, line = _run(tmp_path, "verify", capsys=capsys)
    result = Path(line["result"])
    assert line["status"] == "match" and result.is_file()

    (_step(tmp_path) / OUTPUTS[("target", 1)]).write_text(f"{SENTINEL}\n")
    code, line = _run(tmp_path, "verify", capsys=capsys)
    assert code == 1 and line["message"].startswith("refused: verify-target.tsv: ")
    assert SENTINEL not in line["message"] and not result.exists()

    _output(tmp_path, "target", 1, sha, same)
    _run(tmp_path, "verify", capsys=capsys)
    assert result.is_file()
    (_step(tmp_path) / OUTPUTS[("source", 1)]).unlink()
    code, line = _run(tmp_path, "verify", capsys=capsys)
    assert (code, line["status"]) == (0, "no_output") and not result.exists()


def test_new_scripts_remove_a_result_and_every_output_read_from_the_earlier_ones(tmp_path, capsys):
    _job(tmp_path)
    sha = _sha(capsys, tmp_path)
    same = {n: (1, 2, 3) for n in (1, 2, 3)}
    for side in ("source", "target"):
        for run in (1, 2):
            _output(tmp_path, side, run, sha, same)
    _, line = _run(tmp_path, "verify", capsys=capsys)
    assert line["second_run"] and Path(line["result"]).is_file()
    _run(tmp_path, "verify-scripts", capsys=capsys)
    assert not Path(line["result"]).exists()
    assert not [n for n in OUTPUTS.values() if (_step(tmp_path) / n).exists()]
    # A new first pair alone reads as one run: no old second pair decides it.
    _output(tmp_path, "source", 1, sha, same)
    _output(tmp_path, "target", 1, sha, same)
    _, line = _run(tmp_path, "verify", capsys=capsys)
    assert (line["status"], line["second_run"]) == ("match", False)


def test_an_output_of_another_contracts_scripts_is_refused(tmp_path, capsys):
    _job(tmp_path)
    _sha(capsys, tmp_path)
    same = {n: (1, 2, 3) for n in (1, 2, 3)}
    _output(tmp_path, "source", 1, "d" * 64, same)
    _output(tmp_path, "target", 1, "d" * 64, same)
    code, line = _run(tmp_path, "verify", capsys=capsys)
    assert (
        code == 1 and "another step contract" in line["message"] and "d" * 64 not in line["message"]
    )


@pytest.mark.passes_on_main
def test_the_gate_never_reads_verify():
    """Verify is optional and does not change the gate: neither gate module reads it."""
    for path in ("src/tools/data_movement/dms_gate.py", "scripts/check_dms_step_gate.py"):
        text = (REPO_ROOT / path).read_text()
        assert "verify" not in text.lower(), path


def test_verify_is_local_and_never_an_aws_write():
    assert {script.VERIFY_SCRIPTS, script.VERIFY} <= set(script.LOCAL)
    assert not {script.VERIFY_SCRIPTS, script.VERIFY} & set(script.WRITES)


def test_move_offers_verify_after_the_gate_optional_never_under_auto_and_never_a_value():
    text = (REPO_ROOT / ".claude" / "commands" / "move.md").read_text()
    prove = text.split("## Stage 4: Prove", 1)[1].split("\n## ", 1)[0]
    verify = " ".join(
        prove.split("**Verify (optional).**", 1)[1].split("Then tell the customer", 1)[0].split()
    )
    assert prove.index("**Gate.**") < prove.index("**Verify (optional).**")
    for phrase in ("it is optional and does not change the gate", "Under `--auto`, never offer it",
                   "never a value", "the modernizer never connects to a database",
                   "run_dms_step.py verify-scripts --job-id {job_id} --db {db}",
                   "run_dms_step.py verify --job-id {job_id} --db {db}"):  # fmt: skip
        assert phrase in verify, phrase
    assert "MOVE_RESULT" not in verify  # the run's result lines stay as they were
