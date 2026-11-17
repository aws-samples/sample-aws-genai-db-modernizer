# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The headless e2e-llm run's collection carries the sample's routine set (#2385), the same
one the deterministic e2e adds, so a headless ``/modernize`` run reaches a routine repair
round (Phase 6f); ``tests/e2e/test_sample_routines.py`` checks each sample's set has a
routine to repair."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own script with fixed arguments
import sys
import zipfile
from pathlib import Path

import pytest

from ci.llm.sample_routines import add_sample_routines, main

REPO = Path(__file__).resolve().parents[3]
E2E_LLM = REPO / "ci" / "e2e-llm.sh"


def _extract(name: str, dest: Path) -> Path:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        z.extractall(dest)
    return dest / f"{name}-collection.json"


@pytest.mark.parametrize("name", ["wordpress", "discourse"])
def test_the_sample_set_is_added_to_the_collection(tmp_path, name):
    collection = _extract(name, tmp_path)
    from src.tools.database.collection_text import extract_json_object

    before = json.loads(extract_json_object(collection.read_text()))
    routines = json.loads((REPO / "docs" / "examples" / name / f"{name}-routines.json").read_text())
    added = add_sample_routines(collection, name)
    after = json.loads(collection.read_text())
    assert added == sum(len(routines.get(k) or []) for k in ("procedures", "triggers", "events"))
    assert added > 0
    for key in ("procedures", "triggers", "events", "routine_parameters"):
        assert len(after.get(key) or []) == len(before.get(key) or []) + len(
            routines.get(key) or []
        )
    assert after["metadata"]["routine_capture"] == routines["metadata"]["routine_capture"]
    assert {k: v for k, v in after.items() if k not in routines} == {
        k: v for k, v in before.items() if k not in routines
    }


def test_a_sample_without_a_set_is_left_as_it_is(tmp_path):
    collection = tmp_path / "x-collection.json"
    collection.write_text('{"tables": []}')
    assert add_sample_routines(collection, "no-such-sample") == 0
    assert collection.read_text() == '{"tables": []}'


def test_the_cli_prints_one_line_and_refuses_a_missing_file(tmp_path, capsys):
    collection = _extract("wordpress", tmp_path)
    assert main([str(collection), "wordpress"]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["routines_added"] > 0
    assert main([str(tmp_path / "missing.json"), "wordpress"]) == 2
    proc = subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(REPO / "ci" / "llm" / "sample_routines.py")],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 2


def test_e2e_llm_adds_the_set_after_extracting_and_before_the_run():
    text = E2E_LLM.read_text()
    unzip = text.index('unzip -o -q "$REPO_ROOT/docs/examples/$FIXTURE/$FIXTURE.zip" -d "$WORK"')
    add = text.index(
        'uv run python ci/llm/sample_routines.py "$WORK/$FIXTURE-collection.json" "$FIXTURE"'
    )
    prompt = text.index('PROMPT="/modernize $WORK/$FIXTURE-collection.json')
    assert unzip < add < prompt
