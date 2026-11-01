# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The docs-impact map (``ci/docs_impact_map.toml``, #1162) stays honest.

``ci/pre_merge.sh`` reads the map to tell a branch which pages to review. A map
that names a page that was renamed, or a path that no longer matches a file,
silently stops warning, so these tests keep every entry real.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- fixed argv
from pathlib import Path

import pytest

from ci.pre_merge import DOCS_IMPACT_MAP, docs_glob_matches, load_docs_impact_map

REPO = Path(__file__).resolve().parents[3]
ENTRIES = load_docs_impact_map()


def _tracked() -> list[str]:
    out = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.splitlines() if (REPO / p).exists()]


def test_the_map_is_small_and_every_entry_has_a_name_paths_and_pages():
    assert 1 <= len(ENTRIES) <= 15, "keep the map small; map only where docs have drifted"
    names = [e["name"] for e in ENTRIES]
    assert len(names) == len(set(names))
    for entry in ENTRIES:
        assert entry["paths"] and entry["docs"], entry["name"]
        assert entry.get("why"), f"{entry['name']}: say what drifted or what the pages describe"


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["name"])
def test_every_mapped_page_exists(entry):
    for page in entry["docs"]:
        assert (REPO / page).is_file(), f"{entry['name']}: {page} does not exist"


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["name"])
def test_every_mapped_path_matches_a_tracked_file(entry):
    tracked = _tracked()
    for pattern in entry["paths"]:
        assert any(
            docs_glob_matches(pattern, p) for p in tracked
        ), f"{entry['name']}: {pattern} matches no tracked file; update the map"


def test_the_map_file_is_where_the_docs_say():
    assert DOCS_IMPACT_MAP == REPO / "ci" / "docs_impact_map.toml"
    for doc in ("ci/README.md", "CONTRIBUTING.md"):
        assert "ci/docs_impact_map.toml" in (REPO / doc).read_text(encoding="utf-8"), doc
