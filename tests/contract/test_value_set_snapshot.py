"""Published value-set snapshot guard (#975, ADR-008 Decision 3).

``test_schema_snapshot.py`` guards field shapes. This guards the *values*
contracts publish: enum and literal members (reason codes, flag reasons,
phases), engine ids, capability ids and the access-pattern id prefixes.
Removing or renaming one needs a MAJOR bump of the publishing contract's
version, adding one a MINOR bump. Rules and regenerate command:
``scripts/contract_value_snapshot.py``.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from scripts.contract_bumps import load_pending, pending_kinds, pending_module_kinds
from scripts.contract_value_snapshot import (
    REGENERATE_COMMAND,
    SNAPSHOT_PATH,
    build_snapshot,
    evaluate_snapshot,
    unversioned_value_sets,
)

_ALL_STALE_HINT = (
    "\nTo regenerate every stale generated baseline at once: "
    "uv run python scripts/regenerate_baselines.py"
)


def _load_committed() -> dict[str, Any]:
    assert (
        SNAPSHOT_PATH.exists()
    ), f"{SNAPSHOT_PATH} is missing -- generate it once with: {REGENERATE_COMMAND}"
    return cast(dict[str, Any], json.loads(SNAPSHOT_PATH.read_text()))


@pytest.mark.passes_on_main  # only the failure message changed
def test_value_set_snapshot_matches_a_fresh_rebuild() -> None:
    pending = pending_module_kinds(pending_kinds(load_pending()))
    problems = evaluate_snapshot(_load_committed(), build_snapshot(), pending)
    assert not problems, "\n".join(problems) + _ALL_STALE_HINT


def test_every_published_value_set_has_a_version_to_bump() -> None:
    missing = unversioned_value_sets(build_snapshot())
    assert not missing, (
        "These value sets belong to a contract module with no contract_version and no "
        "Version History, so a removal could never be recorded as a MAJOR bump: "
        + ", ".join(missing)
    )


def test_the_snapshot_covers_the_ids_the_issue_names() -> None:
    snapshot = build_snapshot()
    assert "move_ledger.ReasonCode" in snapshot
    assert "text_pattern_match" in snapshot["capability_ids"]["values"]
    assert "dynamodb" in snapshot["engine_ids"]["values"]
    assert snapshot["access_pattern_id_prefixes.dynamodb"]["values"] == ["DDB"]
    assert snapshot["access_pattern_id_prefixes.documentdb"]["values"] == ["DOC"]
    assert snapshot["access_pattern_id_prefixes.elasticache"]["values"] == ["EC"]
    assert snapshot["access_pattern_id_prefixes.opensearch"]["values"] == ["OS"]
