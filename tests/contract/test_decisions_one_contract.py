"""Every ``decisions/`` file maps to exactly one contract (#1328, review of #1355).

``decisions/`` holds the job's recorded approvals, ``DecisionRecord`` (#1257), and the
customer's engine scope, ``EngineScopeDecision`` (#1328), which is not an approval:
it has no ``status`` and carries the engines, who chose them and when. The registry
resolves a path to its first match, so two patterns that both match a file would let
the order decide; each name must match one entry only.
"""

from __future__ import annotations

import pytest

from src.contracts.decision_record import DecisionRecord
from src.contracts.engine_scope_decision import EngineScopeDecision
from src.shared import contract_versioning as versioning

FILES = {
    "assignment_review.json": DecisionRecord,
    "assignment_approval.json": DecisionRecord,
    "triage_approval.json": DecisionRecord,
    "engine_scope_review.json": DecisionRecord,
    "my_engine_scope.json": DecisionRecord,
    "engine_scope.json": EngineScopeDecision,
}


@pytest.mark.parametrize(("name", "model"), sorted(FILES.items(), key=lambda kv: kv[0]))
def test_each_decisions_file_maps_to_exactly_one_contract(name, model) -> None:
    path = f"db/job/decisions/{name}"
    matches = [target for pattern, target in versioning._registry() if pattern.search(path)]
    assert len(matches) == 1, (path, matches)
    assert versioning.contract_for_artifact(path) is model


def test_the_engine_scope_decision_is_not_read_as_an_approval(tmp_path) -> None:
    from src.agents.referee.engine_scope import record_engine_scope, recorded_engine_scope
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(str(tmp_path))
    record_engine_scope(store, "db", "job", ["dynamodb"], via="cli")
    # Read back through the store, it is checked as EngineScopeDecision, not DecisionRecord.
    assert recorded_engine_scope(store, "db", "job") == ["dynamodb"]
