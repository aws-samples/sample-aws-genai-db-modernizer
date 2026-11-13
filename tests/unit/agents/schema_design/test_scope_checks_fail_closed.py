# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A scope or coverage check that breaks fails the design, never passes it (#2154).

Before #2154 ``check_schema_scope`` and ``check_design_coverage`` caught any error,
logged a warning and returned no findings, so a design whose check could not run was
accepted as if it had been checked. Each now records one violation that names the error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.schema_design import design_coverage, handler
from src.agents.schema_design.handler import apply_schema_scope
from src.agents.schema_design.scope import COVERAGE_PREFIX, SCOPE_PREFIX
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.schema_design.test_design_coverage import (
    _documentdb_design,
    _job,
)
from tests.unit.agents.schema_design.test_design_per_access_pattern import _q


def _broken(*args, **kwargs):  # noqa: ANN002, ANN003
    raise RuntimeError("shape nobody anticipated")


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    s = LocalArtifactStore(str(tmp_path))
    _job(s, [_q("a", "SELECT * FROM products WHERE id = ?", 1.0)], engine="documentdb")
    return s


def test_a_scope_check_that_breaks_fails_the_design(store, monkeypatch) -> None:
    monkeypatch.setattr(handler, "assess_schema_scope", _broken)
    checked, report = apply_schema_scope(
        store, "ecommerce", "j1", "documentdb", _documentdb_design(["a"]), 1
    )
    assert report.violations == [
        f"{SCOPE_PREFIX}documentdb: the check could not run (RuntimeError), so the design "
        "is not accepted unchecked. Fix the job's artifacts and check the design again."
    ]
    assert checked["validation_passed"] is False
    assert checked["validation_failures"] == report.violations


def test_a_coverage_check_that_breaks_fails_the_design(store, monkeypatch) -> None:
    monkeypatch.setattr(design_coverage, "required_query_ids", _broken)
    checked, report = apply_schema_scope(
        store, "ecommerce", "j1", "documentdb", _documentdb_design(["a"]), 1
    )
    assert report.violations == [
        f"{COVERAGE_PREFIX}documentdb: the check could not run (RuntimeError), so the design "
        "is not accepted unchecked. Fix the job's artifacts and check the design again."
    ]
    assert checked["validation_passed"] is False


@pytest.mark.passes_on_main
def test_a_check_that_runs_again_replaces_the_failure(store, monkeypatch) -> None:
    """The failure is a check message: once the check runs, it is stripped and recomputed."""
    with monkeypatch.context() as m:
        m.setattr(handler, "assess_schema_scope", _broken)
        failed, _ = apply_schema_scope(
            store, "ecommerce", "j1", "documentdb", _documentdb_design(["a"]), 1
        )
    checked, report = apply_schema_scope(store, "ecommerce", "j1", "documentdb", failed, 1)
    assert report.violations == []
    assert checked["validation_passed"] is True
    assert checked["validation_failures"] == []


@pytest.mark.passes_on_main
def test_no_assignment_is_still_no_check(tmp_path: Path) -> None:
    """A job with no assignment to check against (legacy) is not failed."""
    store = LocalArtifactStore(str(tmp_path))
    checked, report = apply_schema_scope(
        store, "ecommerce", "j1", "documentdb", _documentdb_design(["a"]), 1
    )
    assert report.violations == []
    assert checked["validation_passed"] is True


def test_an_assignment_at_another_contract_major_raises_not_a_violation(tmp_path: Path) -> None:
    """#2154 review: a stored version this code cannot read is the version error the API
    turns into a 409, never a scope violation (or, as on main, a pass)."""
    import json

    from src.shared.contract_versioning import ContractVersionError

    store = LocalArtifactStore(str(tmp_path))
    _job(store, [_q("a", "SELECT * FROM products WHERE id = ?", 1.0)], engine="documentdb")
    path = tmp_path / "ecommerce/j1/assignment/v1/assignment.json"
    path.write_text(
        json.dumps({**json.loads(path.read_text()), "contract_version": "99.0"}),
        encoding="utf-8",
    )
    with pytest.raises(ContractVersionError):
        handler.check_schema_scope(
            store, "ecommerce", "j1", "documentdb", _documentdb_design(["a"]), 1
        )


def test_a_check_that_breaks_logs_only_the_error_type(store, monkeypatch, caplog) -> None:
    """#2154 review: an exception's text can carry stored content (a KeyError on a stored
    key); the warnings name its type only."""
    planted = "SELECT card_number FROM wp_secret_cards"

    def leaky(*args, **kwargs):  # noqa: ANN002, ANN003
        raise KeyError(planted)

    monkeypatch.setattr(handler, "assess_schema_scope", leaky)
    monkeypatch.setattr(design_coverage, "required_query_ids", leaky)
    with caplog.at_level("DEBUG"):
        _, report = apply_schema_scope(
            store, "ecommerce", "j1", "documentdb", _documentdb_design(["a"]), 1
        )
    assert len(report.violations) == 2
    assert "KeyError" in caplog.text
    assert "card_number" not in caplog.text
