# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_collect.py`` and ``run_assessment.py`` refuse a bad offline collection with
one clear status line, no traceback (#131).

The three inputs of the issue: a file that is not JSON, an entry missing a key the
transform reads, and valid JSON of another shape. Each ends in an ``error`` status
line naming the file; none reaches the transform. An absent optional section is not
an error: the collection loads and the status line names it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import run_assessment, run_collect
from src.storage.local_store import LocalArtifactStore

RAW_COLLECTION: dict[str, Any] = {
    "collection_version": "1.0",
    "metadata": {"database_name": "testdb", "version": "8.0.0"},
    "tables": [{"table_name": "t0", "row_count": 1}],
    "columns": [{"table_name": "t0", "column_name": "id", "data_type": "int"}],
    "indexes": [{"table_name": "t0", "index_name": "PRIMARY", "non_unique": 0}],
    "foreign_keys": [],
    "primary_keys": [{"table_name": "t0", "column_name": "id"}],
    "views": [],
    "procedures": [],
    "triggers": [],
    "queries": [{"query_text": "SELECT id FROM t0", "digest": "d0"}],
    "global_stats": {},
}


def _with(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(json.dumps(RAW_COLLECTION))
    for key, value in changes.items():
        if value is None:
            data.pop(key)
        else:
            data[key] = value
    return data


def _run_collect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    argv = ["run_collect.py", "--file", path, "--db", "testdb"]
    monkeypatch.setattr(sys, "argv", [*argv, "--artifact-root", str(tmp_path / "artifacts")])
    run_collect.main()


def _run_assessment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    run_assessment.phase_collect(path, "testdb", store)


RUNNERS: dict[str, Callable[[pytest.MonkeyPatch, Path, str], None]] = {
    "run_collect": _run_collect,
    "run_assessment": _run_assessment,
}


def _last_line(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    line: dict[str, Any] = json.loads(captured.out.strip().splitlines()[-1])
    return line


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "shop-collection.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_file_that_is_not_json_names_the_file_and_the_line(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "collection_output\n" + json.dumps(RAW_COLLECTION)[:-40])

    with pytest.raises(SystemExit):
        RUNNERS[runner](monkeypatch, tmp_path, path)

    out = _last_line(capsys)
    assert out["status"] == "error"
    assert path in out["message"]
    assert "not valid JSON" in out["message"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_missing_key_lists_the_entry_not_a_key_error(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _with(indexes=[{"index_name": "PRIMARY", "non_unique": 0}])
    path = _write(tmp_path, json.dumps(bad))

    with pytest.raises(SystemExit):
        RUNNERS[runner](monkeypatch, tmp_path, path)

    out = _last_line(capsys)
    assert out["status"] == "error"
    assert path in out["message"]
    assert "indexes[0]" in out["message"] and "table_name" in out["message"]
    assert not list((tmp_path / "artifacts").glob("**/collector/output.json"))


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize(
    "document",
    [{"name": "shop", "rows": []}, {"collection_version": "1.0", "index": []}],
    ids=["another-document", "misnamed-sections"],
)
def test_valid_json_of_another_shape_is_refused_not_read_as_empty(
    runner: str,
    document: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(tmp_path, json.dumps(document))

    with pytest.raises(SystemExit):
        RUNNERS[runner](monkeypatch, tmp_path, path)

    out = _last_line(capsys)
    assert out["status"] == "error"
    assert path in out["message"]
    assert "'tables'" in out["message"] and "'queries'" in out["message"]
    assert not list((tmp_path / "artifacts").glob("**/collector/output.json"))


@pytest.mark.parametrize("runner", RUNNERS)
def test_an_absent_optional_section_loads_and_is_on_the_status_line(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, json.dumps(_with(indexes=None)))

    RUNNERS[runner](monkeypatch, tmp_path, path)

    out = _last_line(capsys)
    assert out["status"] == "complete"
    assert out["absent_sections"] == ["indexes"]
    assert out["tables"] == 1


@pytest.mark.passes_on_main  # the negative case: a whole collection reports nothing
@pytest.mark.parametrize("runner", RUNNERS)
def test_a_whole_collection_has_no_absent_sections_on_the_status_line(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, json.dumps(RAW_COLLECTION))

    RUNNERS[runner](monkeypatch, tmp_path, path)

    out = _last_line(capsys)
    assert out["status"] == "complete"
    assert "absent_sections" not in out


@pytest.mark.parametrize("runner", RUNNERS)
def test_tables_without_queries_loads_in_both_scripts(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same check decides for both: run_assessment.py used to call this file an
    "Unrecognized file format." while run_collect.py loaded it (#1755 review)."""
    data = _with(queries=None)
    data.pop("collection_version")
    path = _write(tmp_path, json.dumps(data))

    RUNNERS[runner](monkeypatch, tmp_path, path)

    out = _last_line(capsys)
    assert out["status"] == "complete"
    assert out["absent_sections"] == ["queries"]
    assert out["tables"] == 1 and out["queries"] == 0


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_local_oracle_spool_loads(
    runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Oracle script's spool: a trailing ``{"_sentinel": true}`` in each list and
    an unescaped control character, which only the S3 path used to clean."""
    data = _with(metadata={"database_name": "testdb", "version": "Oracle Database 19c"})
    for section in ("tables", "columns", "queries", "views"):
        data[section].append({"_sentinel": True})
    text = json.dumps(data).replace("SELECT id", "SELECT\tid")
    path = _write(
        tmp_path, "collection_output\n" + text + "\n\nPL/SQL procedure successfully completed.\n"
    )

    RUNNERS[runner](monkeypatch, tmp_path, path)

    out = _last_line(capsys)
    assert out["status"] == "complete"
    assert out["tables"] == 1
    assert out["queries"] == 1  # the sentinel is not a statement
    assert "absent_sections" not in out
