# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every deliverable calls one run an assessment, as the UI does (#2505, after #2504).

The executive report cover, the folder index and both chat summary headings name the
run's id "assessment id", the UI header's word. No heading of a deliverable, no line
of the folder index and nothing on the cover says "job"; machine names (``job_id``
fields, ``<job>`` paths) stay. Model text in a report body may still speak of a
"background job", so the body is not checked.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from pypdf import PdfReader

from src.report import deliverables as dl
from src.report.printed_summary import render_final_summary, render_gate_block
from src.storage.local_store import LocalArtifactStore

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.json"))
JOB_ID = "8dd35fce"
JOB_WORD = re.compile(r"\bjob('s)?\b", re.IGNORECASE)


def _render(fixture: Path, tmp_path: Path) -> dict[str, bytes]:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = f"wordpress/{JOB_ID}/synthesis/v1/report.json"
    report = json.loads(fixture.read_text())
    report["job_id"] = JOB_ID
    store.write_json(key, report)
    rendered = dl.render_deliverables(store, JOB_ID, "wordpress", key)
    files = {entry.filename: item.content for entry, item in rendered.folder_files()}
    files[dl.INDEX_FILENAME] = dl.render_folder_index(
        "wordpress", JOB_ID, 1, [e for e, _ in rendered.folder_files()]
    ).encode("utf-8")
    return files


def _pdf(content: bytes) -> tuple[str, list[str]]:
    reader = PdfReader(io.BytesIO(content))
    subject = str(reader.metadata.get("/Subject", "") if reader.metadata else "")
    return subject, [" ".join((p.extract_text() or "").split()) for p in reader.pages]


def _readable(text: str) -> str:
    """``text`` without its code spans (paths and file names are machine names)."""
    return re.sub(r"`[^`]*`", "", text)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.stem for f in FIXTURES])
def test_the_cover_and_the_index_name_the_assessment_id(fixture: Path, tmp_path: Path) -> None:
    files = _render(fixture, tmp_path)
    subject, pages = _pdf(files["executive-report.pdf"])
    assert f"Assessment id {JOB_ID}" in pages[0]
    assert f"assessment id {JOB_ID}" in subject
    index = files[dl.INDEX_FILENAME].decode("utf-8")
    assert f"\nAssessment id: {JOB_ID}\n" in index


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.stem for f in FIXTURES])
def test_no_deliverable_heading_says_job(fixture: Path, tmp_path: Path) -> None:
    files = _render(fixture, tmp_path)
    found = [
        f"cover: {m.group(0)}" for m in JOB_WORD.finditer(_pdf(files["executive-report.pdf"])[1][0])
    ]
    for name, content in files.items():
        if not name.endswith(".md"):
            continue
        text = content.decode("utf-8")
        lines = text.splitlines()
        if name != dl.INDEX_FILENAME:  # the index is all pipeline text: every line counts
            lines = [line for line in lines if line.startswith("#")]
        found += [f"{name}: {line}" for line in lines if JOB_WORD.search(_readable(line))]
    assert found == []


def test_both_chat_summary_headings_name_the_assessment_id() -> None:
    gate = render_gate_block(
        {"before_distribution": {"dynamodb": 3}, "after_distribution": {"dynamodb": 3}},
        database_name="wordpress",
        job_id=JOB_ID,
    )
    report = json.loads(FIXTURES[0].read_text())
    final = render_final_summary(
        report,
        database_name="wordpress",
        job_id=JOB_ID,
        deliverables_dir=f"artifacts/wordpress/{JOB_ID}/deliverables/",
        deliverables=dl.FOLDER_ENTRIES,
    )
    assert gate.startswith(f"## Assignment for approval: wordpress (assessment id {JOB_ID})\n")
    assert final.startswith(f"## Assessment: wordpress (assessment id {JOB_ID})\n")
    headings = [
        line for text in (gate, final) for line in text.splitlines() if line.startswith("#")
    ]
    assert headings and not [h for h in headings if JOB_WORD.search(h)]
