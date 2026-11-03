# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cookbook's catalog section is generated from src/graph/catalog.py (#307)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import render_graph_catalog as render
from src.graph import catalog


def _point_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    doc = tmp_path / "docs" / "cookbook.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    monkeypatch.setattr(render, "COOKBOOK", doc)
    monkeypatch.setattr(render, "REPO_ROOT", tmp_path)
    return doc


def test_the_committed_cookbook_is_fresh():
    assert render.main(["--check"]) == 0, (
        "docs/guides/context-graph-query-cookbook.md is stale. "
        "Run: uv run python scripts/render_graph_catalog.py"
    )


def test_check_fails_on_drift_and_a_write_fixes_it(tmp_path, monkeypatch):
    stale = f"# Cookbook\n\nHand text.\n\n{render.BEGIN}\nold\n{render.END}\n\nAfter.\n"
    doc = _point_at(tmp_path, monkeypatch, stale)
    assert render.main(["--check"]) == 1
    assert doc.read_text(encoding="utf-8") == stale
    assert render.main([]) == 0
    assert render.main(["--check"]) == 0
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("# Cookbook\n\nHand text.\n\n")
    assert text.endswith(f"{render.END}\n\nAfter.\n")


def test_check_fails_on_a_hand_edit_inside_the_block(tmp_path, monkeypatch):
    fresh = render.splice(f"{render.BEGIN}\n{render.END}\n", render.render_block())
    edited = fresh.replace("at most 20 rows", "at most 25 rows")
    assert edited != fresh
    _point_at(tmp_path, monkeypatch, edited)
    assert render.main(["--check"]) == 1


@pytest.mark.parametrize(
    "text",
    [
        "# No markers\n",
        f"{render.BEGIN}\nonly a start\n",
        f"{render.END}\n{render.BEGIN}\n",
        f"{render.BEGIN}\n{render.END}\n{render.BEGIN}\n{render.END}\n",
    ],
)
def test_markers_must_be_one_ordered_pair(tmp_path, monkeypatch, text):
    doc = _point_at(tmp_path, monkeypatch, text)
    assert render.main(["--check"]) == 1
    assert render.main([]) == 1
    assert doc.read_text(encoding="utf-8") == text


def test_every_entry_is_rendered_with_a_postable_request():
    block = render.render_block()
    for entry in catalog.entries():
        assert f"### {entry.ref}. {entry.title}\n" in block
        assert f"Catalog id `{entry.id}`" in block
        body = {"cypher": entry.cypher}
        if entry.params:
            body["params"] = {p.name: p.example for p in entry.params}
        assert json.dumps(body, ensure_ascii=True) in block
        for param in entry.params:
            assert f"`{param.choices_cypher}`" in block


def test_the_empty_results_table_lists_each_need_with_its_entries():
    table = render.render_empty_results()
    assert "| `LoadTestRun` nodes | Load test, per engine | 9, F |" in table
    assert "| `AccessPattern` nodes | Schema design, per engine | 4, 9, I |" in table


@pytest.mark.parametrize("marker", ["<<<<<<< HEAD", "=======", ">>>>>>> gh/main"])
@pytest.mark.parametrize("where", ["above", "inside", "below"])
def test_a_conflict_marker_anywhere_is_refused(tmp_path, monkeypatch, capsys, marker, where):
    """A rebase leaves markers outside the block too: --check and a write both refuse."""
    fresh = render.splice(
        f"# Cookbook\n\n{render.BEGIN}\n{render.END}\n\nAfter.\n", render.render_block()
    )
    if where == "above":
        text = fresh.replace("# Cookbook\n", f"# Cookbook\n{marker}\n", 1)
    elif where == "inside":
        text = fresh.replace(render.END, f"{marker}\n{render.END}", 1)
    else:
        text = fresh + f"{marker}\n"
    doc = _point_at(tmp_path, monkeypatch, text)
    assert render.main(["--check"]) == 1
    assert "merge conflict marker" in capsys.readouterr().out
    assert render.main([]) == 1
    assert doc.read_text(encoding="utf-8") == text


def test_marker_like_text_inside_a_line_is_not_a_conflict():
    text = f"Use `=======` or <<<<<<<< in prose.\n{render.BEGIN}\n{render.END}\n"
    assert render.splice(text, "X\n") == "Use `=======` or <<<<<<<< in prose.\nX\n"
