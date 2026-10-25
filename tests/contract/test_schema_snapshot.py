"""Contract schema snapshot guard (#574).

``scripts/validate_contracts.py`` and the rest of ``tests/contract/`` check
that a contract *instance* validates; neither one catches a contract's
*shape* changing without its ``contract_version`` moving -- the actual bug
that motivated this file: a field added twice without a version bump, once
with the default left alone and once with only the model's own "Version
history" docstring updated.

``test_schema_snapshot_file_matches_a_fresh_rebuild`` recomputes
``tests/contract/schema_snapshot.json`` from the current ``src/contracts/``
and compares it, field by field, against the committed file via
``scripts.contract_schema_snapshot.evaluate_snapshot`` -- the same
comparison ``--write`` prints and
``tests/unit/scripts/test_contract_schema_snapshot.py`` exercises against
synthetic models, one case at a time. See that module's docstring for the
full bump-classification rules.

``test_unversioned_contract_models_are_allowlisted`` and
``test_allowlist_has_no_stale_entries`` guard the three models whose
``contract_version`` is required with no default
(``scripts.contract_schema_snapshot.EXCLUDED_NO_DEFAULT``): any other model
added later with a required, no-default ``contract_version`` fails until
it's either given a literal starting version or added to that allowlist
with a reason.

No network, no AWS, no model calls: this only imports ``src/contracts/``
modules and recomputes JSON Schema, so it is fast and fully deterministic
(``scripts/contract_schema_snapshot.py``'s canonicalization is written to
be stable across Python and pydantic patch versions -- see its docstring).
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from scripts.contract_bumps import load_pending, pending_kinds
from scripts.contract_schema_snapshot import (
    EXCLUDED_NO_DEFAULT,
    REGENERATE_COMMAND,
    SNAPSHOT_PATH,
    build_snapshot,
    evaluate_snapshot,
    stale_allowlist_entries,
    unallowlisted_unversioned_contracts,
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
def test_schema_snapshot_file_matches_a_fresh_rebuild() -> None:
    """The committed snapshot must be exactly what rebuilding it now produces.

    Any divergence means ``tests/contract/schema_snapshot.json`` is stale
    relative to ``src/contracts/`` -- the fix is always to regenerate and
    commit it; ``evaluate_snapshot()``'s job is to say *why* it's stale and
    *which* bump CONTRIBUTING.md's rule requires, at the deepest path the
    change happened.
    """
    committed = _load_committed()
    actual = build_snapshot()
    # A bump a pending file declares is accepted until it is resolved (ADR-008
    # Decision 8); the merge queue refuses a pending file, so main never holds one.
    problems = evaluate_snapshot(committed, actual, pending_kinds(load_pending()))
    assert not problems, "\n".join(problems) + _ALL_STALE_HINT


def test_unversioned_contract_models_are_allowlisted() -> None:
    """Any model with a required ``contract_version`` (no literal default)
    must be one of the three reviewed exclusions, each with its own reason
    -- a new one means a contract was added without picking a starting
    version."""
    missing = unallowlisted_unversioned_contracts()
    assert not missing, (
        "contract_version is required with no default, and not in "
        "EXCLUDED_NO_DEFAULT, for: " + ", ".join(missing) + ". Either give it a literal "
        "default and let it join the snapshot, or add it to EXCLUDED_NO_DEFAULT in "
        "scripts/contract_schema_snapshot.py with a reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Every ``EXCLUDED_NO_DEFAULT`` key must still describe a real,
    required, no-default ``contract_version`` model -- otherwise the
    allowlist is quietly out of date (the model was given a default,
    renamed, or removed)."""
    stale = stale_allowlist_entries()
    assert not stale, (
        "EXCLUDED_NO_DEFAULT entries no longer match a required, no-default "
        "contract_version model (remove or update them): " + ", ".join(stale)
    )


def test_allowlist_entries_have_non_empty_reasons() -> None:
    empty = [key for key, reason in EXCLUDED_NO_DEFAULT.items() if not reason.strip()]
    assert not empty, f"EXCLUDED_NO_DEFAULT entries missing a reason: {empty}"
