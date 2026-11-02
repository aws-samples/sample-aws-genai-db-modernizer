# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1536: a statement that names no table reads "No source table" in every deliverable.

The stored values stay as they are (``tables_accessed: ["unknown"]``, and the group name
a report saved before this change carries), so a saved report renders with the current
wording: the renderers map, they do not read a new field.
"""

from __future__ import annotations

import copy
import io
import json
import re
import subprocess  # nosec B404 - git ls-files only
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from src.report.executive_report import render_executive_report_pdf
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import (
    BLAME_GROUP_LABELS,
    UNRESOLVED_TABLE_GROUP_LABEL,
    display_group_name,
    query_group_facts,
)

LEGACY = "Table not identified by the collector"
FIXTURES = Path(__file__).parent / "fixtures"
SAVED = [
    "wordpress_sample_run_0_3_report.json",
    "trailgear_1_41_report.json",
    "wordpress_1_41_flagged_report.json",
]


def _saved(name: str) -> dict[str, Any]:
    saved: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return saved


def _with_group(report: dict[str, Any], label: str) -> dict[str, Any]:
    out = copy.deepcopy(report)
    for g in out["query_groups"]:
        if g["group_name"] in (LEGACY, UNRESOLVED_TABLE_GROUP_LABEL):
            g["group_name"] = label
    return out


def test_the_current_label_says_no_source_table() -> None:
    assert UNRESOLVED_TABLE_GROUP_LABEL == "No source table"
    assert {LEGACY, UNRESOLVED_TABLE_GROUP_LABEL} <= BLAME_GROUP_LABELS


def test_display_group_name_maps_only_the_legacy_label() -> None:
    assert display_group_name(LEGACY) == "No source table"
    assert display_group_name("No source table") == "No source table"
    assert display_group_name("wp.wp_posts") == "wp.wp_posts"
    assert display_group_name("Utility and session statements") == (
        "Utility and session statements"
    )


@pytest.mark.parametrize("name", SAVED)
@pytest.mark.parametrize("label", [LEGACY, "No source table"])
def test_engineering_report_says_no_source_table(name: str, label: str) -> None:
    saved = _saved(name)
    assert any(g["group_name"] == LEGACY for g in saved["query_groups"])
    md = render_engineering_report_md(_with_group(saved, label))
    assert LEGACY not in md
    assert "collector" not in " ".join(
        ln for ln in md.splitlines() if ln.startswith("| No source table")
    )
    assert re.search(r"^\| No source table \| \d+ \|", md, re.MULTILINE)


@pytest.mark.parametrize("label", [LEGACY, "No source table"])
def test_both_labels_sort_last_in_the_facts(label: str) -> None:
    report = _with_group(_saved("wordpress_sample_run_0_3_report.json"), label)
    facts = query_group_facts(report)
    assert facts is not None
    names = [
        report["query_groups"][r["group_index"]]["group_name"] for r in facts["relational_rows"]
    ]
    assert names.index(label) > max(i for i, n in enumerate(names) if n not in BLAME_GROUP_LABELS)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", SAVED)
def test_executive_report_never_blames_the_collector_for_a_tableless_statement(
    name: str,
) -> None:
    pdf = render_executive_report_pdf(_saved(name))
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert LEGACY not in text
    assert not re.search(r"\bunknown table\b", text, re.IGNORECASE)


_SOURCE_SUFFIXES = (".py", ".js", ".md")
_NOT_SOURCES = {"node_modules", "__tests__", "build"}


def _source_files(root: Path) -> list[Path]:
    """The tracked source files under ``root/src`` (``git ls-files``), so a gitignored
    build output such as the ``src/ui/build`` bundle a local e2e run leaves is never
    read; the build and dependency folders are left out even when tracked."""
    listed = subprocess.run(  # nosec B603 B607 - a fixed git command, no shell
        ["git", "ls-files", "-z", "--", "src"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [
        root / f
        for f in listed.split("\0")
        if f and Path(f).suffix in _SOURCE_SUFFIXES and not _NOT_SOURCES & set(Path(f).parts)
    ]


@pytest.mark.passes_on_main  # the scan lives in this file: the fix is the test itself
def test_the_old_label_is_written_nowhere_else_in_the_sources() -> None:
    allowed = {
        Path("src/shared/report_facts.py"),
        Path("src/agents/referee/synthesis_grounding.py"),
        Path("src/contracts/synthesis_output.py"),
        Path("src/ui/src/utils/tableNames.js"),
    }
    root = Path(__file__).resolve().parents[3]
    hits = [
        p.relative_to(root) for p in _source_files(root) if LEGACY in p.read_text(errors="ignore")
    ]
    assert set(hits) <= allowed, sorted(set(hits) - allowed)


@pytest.mark.passes_on_main  # the scan lives in this file: the fix is the test itself
def test_the_scan_skips_a_build_output(tmp_path: Path) -> None:
    """A gitignored ``src/ui/build`` bundle (left by a local e2e run) is not a source,
    nor a tracked one under a build folder."""
    (tmp_path / "src/app").mkdir(parents=True)
    (tmp_path / "src/app/main.py").write_text("x = 1\n")
    (tmp_path / "src/ui/build/static/js").mkdir(parents=True)
    (tmp_path / "src/ui/build/static/js/main.123.js").write_text(LEGACY)
    (tmp_path / "src/lib/build").mkdir(parents=True)
    (tmp_path / "src/lib/build/out.js").write_text(LEGACY)
    (tmp_path / ".gitignore").write_text("src/ui/build/\n")
    for cmd in (["init", "-q"], ["add", "."]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)  # nosec B603 B607

    assert _source_files(tmp_path) == [tmp_path / "src/app/main.py"]
