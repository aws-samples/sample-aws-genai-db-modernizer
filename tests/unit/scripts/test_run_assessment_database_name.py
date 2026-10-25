"""The name a script run gives the database never changes its routing (#1572).

``--db`` and the name ``--db-from-file-name`` (``/modernize``) derives from the
collection file name used to key the collected tables, while each statement's
``tables_accessed`` kept the collection's own database name: with any other name
no statement matched a table, and every one went to Aurora on the tie rule. A
name that is ambiguous for the collection stops the collect phase with an error.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.storage.local_store import LocalArtifactStore
from tests.unit.assessment.conftest import sample_collection


def _route(
    tmp: Path, raw: dict[str, Any], file_name: str, db: str | None, *, from_file_name: bool = False
) -> dict[str, Any]:
    import scripts.run_assessment as run_assessment

    tmp.mkdir()
    collection = tmp / file_name
    collection.write_text(json.dumps(raw), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp / "artifacts"))
    mp = pytest.MonkeyPatch()
    mp.chdir(tmp)  # phase_collect writes .modernizer-state.json here
    try:
        job_id, name = run_assessment.phase_collect(
            str(collection), db, store, from_file_name=from_file_name
        )
        selected = run_assessment.phase_triage(store, job_id, name)
        run_assessment.phase_analysis(store, job_id, name, selected)
        run_assessment.phase_assignment(store, job_id, name)
    finally:
        mp.undo()
    base = f"{name}/{job_id}"
    assignment = store.read_json(f"{base}/assignment/v1/assignment.json")
    return {
        "name": name,
        "engines": {
            qa["query_id"]: (qa["assigned_engine"], qa.get("tied_engines"), qa.get("reason_code"))
            for qa in assignment["query_assignments"]
        },
        "tables": store.read_json(f"{base}/collector/output.json")["database_schema"]["tables"],
    }


@pytest.mark.parametrize("sample", ["wordpress", "trailgear"])
def test_db_flag_with_another_name_routes_as_the_collected_name(
    tmp_path: Path, sample: str
) -> None:
    raw = sample_collection(sample)
    matching = _route(tmp_path / "matching", raw, f"{sample}-collection.json", sample)
    other = _route(tmp_path / "other", raw, f"{sample}-collection.json", "shop")
    assert other["name"] == "shop"
    assert matching["engines"]
    assert other["engines"] == matching["engines"]
    assert other["tables"] == matching["tables"]


def test_a_name_from_the_file_name_routes_as_the_collected_name(tmp_path: Path) -> None:
    raw = sample_collection("wordpress")
    matching = _route(tmp_path / "matching", raw, "wordpress-collection.json", "wordpress")
    derived = _route(tmp_path / "derived", raw, "my-collection.json", None, from_file_name=True)
    assert derived["name"] == "my"
    assert derived["engines"] == matching["engines"]
    distribution: dict[str, int] = {}
    for engine, _, _ in derived["engines"].values():
        distribution[engine] = distribution.get(engine, 0) + 1
    assert set(distribution) != {"aurora_mysql"}


def test_a_collection_with_no_database_name_routes_by_its_file_name(tmp_path: Path) -> None:
    """No metadata name: the file name names the run and keys the tables."""
    raw = sample_collection("wordpress")
    unnamed = copy.deepcopy(raw)
    unnamed["metadata"].pop("database_name")
    matching = _route(tmp_path / "matching", raw, "wordpress-collection.json", "wordpress")
    derived = _route(tmp_path / "derived", unnamed, "shop-collection.json", None)
    assert derived["name"] == "shop"
    assert derived["engines"] == matching["engines"]


def test_the_collect_line_names_the_collected_database_when_they_differ(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_assessment as run_assessment

    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    collection = tmp_path / "wordpress-collection.json"
    collection.write_text(json.dumps(sample_collection("wordpress")), encoding="utf-8")
    lines = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path)
        for db in ("wordpress", "shop"):
            run_assessment.phase_collect(str(collection), db, store)
            line = capsys.readouterr().out.strip().splitlines()[-1]
            lines[db] = json.loads(line)
    assert "collected_database_name" not in lines["wordpress"]
    assert lines["shop"]["database_name"] == "shop"
    assert lines["shop"]["collected_database_name"] == "wordpress"


def _several_databases() -> dict[str, Any]:
    raw = sample_collection("trailgear")
    raw["metadata"].pop("database_name")
    for i, row in enumerate(raw["queries"]):
        row["database_name"] = ("sales", "hr")[i % 2]
    return raw


def test_a_run_named_after_a_sibling_database_routes_as_the_stated_one(tmp_path: Path) -> None:
    """Server-wide statements name a sibling database: the stated name wins."""
    raw = sample_collection("trailgear")
    for i, row in enumerate(raw["queries"]):
        row["database_name"] = ("trailgear", "reporting")[i % 2]
    matching = _route(tmp_path / "matching", raw, "trailgear-collection.json", "trailgear")
    sibling = _route(tmp_path / "sibling", raw, "trailgear-collection.json", "reporting")
    assert matching["engines"]
    assert sibling["engines"] == matching["engines"]


def test_an_ambiguous_name_stops_the_collect_phase(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No stated name, and statements of several databases, none of them the run's."""
    import scripts.run_assessment as run_assessment

    collection = tmp_path / "trailgear-collection.json"
    collection.write_text(json.dumps(_several_databases()), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            run_assessment.phase_collect(str(collection), "shop", store)
    assert exc.value.code == 1
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["phase"] == "collect" and line["status"] == "error"
    assert "'sales'" in line["message"] and "'hr'" in line["message"]


def _run_collect(
    tmp: Path, raw: dict[str, Any], db: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any], Path]:
    import scripts.run_collect as run_collect

    collection = tmp / "trailgear-collection.json"
    collection.write_text(json.dumps(raw), encoding="utf-8")
    argv = ["run_collect.py", "--file", str(collection), "--db", db]
    argv += ["--job-id", "job1", "--artifact-root", str(tmp / "artifacts")]
    code = 0
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp)
        mp.setattr("sys.argv", argv)
        try:
            run_collect.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return code, line, tmp / "artifacts" / db / "job1" / "collector" / "output.json"


def test_run_collect_keys_tables_by_the_collected_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, line, output = _run_collect(tmp_path, sample_collection("trailgear"), "shop", capsys)
    assert code == 0 and line["status"] == "complete"
    assert line["collected_database_name"] == "trailgear"
    tables = json.loads(output.read_text())["database_schema"]["tables"]
    assert all(t["table_id"].startswith("trailgear.") for t in tables)


def test_run_collect_refuses_an_ambiguous_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, line, output = _run_collect(tmp_path, _several_databases(), "shop", capsys)
    assert code == 1 and line["status"] == "error"
    assert "'sales'" in line["message"] and "'hr'" in line["message"]
    assert not output.exists()


def test_test_local_phased_refuses_an_ambiguous_name_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.test_local_phased as test_local_phased

    collection = tmp_path / "trailgear-collection.json"
    collection.write_text(json.dumps(_several_databases()), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path)
        mp.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
        mp.setattr("sys.argv", ["test_local_phased.py", str(collection), "--db", "shop", "-y"])
        with pytest.raises(SystemExit) as exc:
            test_local_phased.main()
    assert exc.value.code == 1
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("ERROR: ") and "'sales'" in last and "'hr'" in last
