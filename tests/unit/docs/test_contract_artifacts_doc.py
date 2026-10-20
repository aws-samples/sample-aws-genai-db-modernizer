"""``docs/contracts/artifacts.md`` lists exactly the declared artifacts (#1192).

The page mirrors the three lists of ``src/shared/contract_versioning.py``: the
registered contracts (with their model), model I/O, and the internal artifacts
(with their reason). An entry added, removed or reworded in the code must change
on the page too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.shared import contract_versioning as versioning

DOC = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "artifacts.md"
_ROW = re.compile(r"^\| `([^`]+)` \|(?: (.*) \|)?$")


def _sections() -> dict[str, dict[str, str]]:
    """Section heading -> {pattern: the row's second cell, or ""}."""
    sections: dict[str, dict[str, str]] = {}
    current = ""
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = {}
        elif (row := _ROW.match(line)) and current:
            sections[current][row.group(1)] = row.group(2) or ""
    return sections


def test_the_doc_lists_every_registered_and_internal_artifact() -> None:
    sections = _sections()
    contracts = {
        pattern: target.split(":")[1] for pattern, target in versioning._ARTIFACT_CONTRACTS
    }
    listed = sections["Registered contracts"]
    assert set(listed) == {*contracts, versioning._SCHEMA_OUTPUT}
    for pattern, name in contracts.items():
        assert listed[pattern].startswith(f"`{name}` "), pattern

    assert set(sections["Model I/O"]) == set(versioning.MODEL_IO_PATTERNS)
    assert sections["Internal artifacts"] == dict(versioning.INTERNAL_ARTIFACTS)


@pytest.mark.passes_on_main  # a check of this file's parser
def test_the_parser_reads_the_rows() -> None:
    assert _ROW.match("| `a\\.json` | why |").groups() == ("a\\.json", "why")
    assert _ROW.match("| `a\\.json` |").groups() == ("a\\.json", None)
    assert _ROW.match("|---|---|") is None
