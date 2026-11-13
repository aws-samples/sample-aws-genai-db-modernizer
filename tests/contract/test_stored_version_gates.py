# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every MAJOR.MINOR literal in ``src/`` outside ``src/contracts/`` is reviewed (#2176).

Since #2176 a pending MINOR waits on main until the release, so files written between
releases carry the last released number (ADR-008 Decision 8). A reader that branches on a
stored MINOR (``trade_off_topics.TOPICS_SINCE``, read through ``predates_topics``) would
read such a file as older if it named the unreleased number. A stored-version gate may
therefore name only a released number, added after the release resolve that assigned
it. This test lists every version-shaped string literal outside docstrings so a new one,
a gate or not, is reviewed against that rule: add it to ``REVIEWED`` with what it is.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERSION = re.compile(r"^\d+\.\d+$")

#: path -> (number of literals, what they are). ``gate`` marks a stored-version gate.
REVIEWED: dict[str, tuple[int, str]] = {
    "src/shared/trade_off_topics.py": (
        7,
        "gate: TOPICS_SINCE, the first released version of each design output and of the "
        "synthesis report that carries topic and note_kind; released numbers only",
    ),
    "src/assessment/models.py": (
        11,
        "the public assess() contract, versioned on its own outside the pending-bump "
        "mechanism (hand-versioned, #2192): CONTRACT_VERSION, the ContractVersion and "
        "SUPPORTED_CONTRACT_VERSIONS values 1.3 and 1.4, the retired 1.0, 1.1 and 1.2, and "
        "two gates, both on the released 1.3 (ADR-008 Decision 8 as #2183 amended it): "
        "_refuse_fields_of_1_4 (a 1.3 request may not set routine_sources) and "
        "writes_routine_sources (a 1.3 run writes none, #2022)",
    ),
    "src/agents/analysis/elasticache_analysis_agent.py": (1, "a default trace version"),
    "src/agents/load_test/elasticache/provisioner.py": (1, "an ElastiCache engine version"),
    "src/agents/schema_design/group_merger.py": (1, "a version fallback for sorting"),
    "src/tools/data_movement/dms_step.py": (1, "a source database engine version"),
    "src/tools/routines/proof_inputs.py": (1, "a numeric test value, not a version"),
    "src/tools/schema/aurora_common/delta_merge.py": (1, "a delta version fallback"),
}


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.add(id(value))
    return found


def version_literals(root: Path = REPO) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("src/contracts/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        n = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and VERSION.match(node.value)
            and id(node) not in skip
        )
        if n:
            counts[rel] = n
    return counts


@pytest.mark.passes_on_main  # a review guard of the tree as it is
def test_every_version_literal_outside_the_contracts_is_reviewed() -> None:
    found = version_literals()
    reviewed = {path: count for path, (count, _) in REVIEWED.items()}
    assert found == reviewed, (
        "a version-shaped literal was added or removed outside src/contracts/. A reader that "
        "branches on a stored MINOR may name only a released number (ADR-008 Decision 8, "
        "#2176); review it and update REVIEWED"
    )


@pytest.mark.passes_on_main  # a review guard of the tree as it is
def test_the_topics_gate_names_released_numbers_only() -> None:
    """Every TOPICS_SINCE number is at or below its contract's version on this tree, which
    is the last release's while its bump is pending."""
    from scripts.contract_value_snapshot import module_version
    from src.shared.trade_off_topics import TOPICS_SINCE

    def key(v: str) -> tuple[int, int]:
        a, b = v.split(".")
        return int(a), int(b)

    for module, since in TOPICS_SINCE.items():
        current = module_version(module)
        assert current is not None and key(since) <= key(current), module
