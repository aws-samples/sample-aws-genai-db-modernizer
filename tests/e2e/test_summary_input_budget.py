# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every bundled sample's executive-summary request fits its token budget (#1064).

The per-list cap (``summary_input_budget``) can only shrink lists. A request
whose fixed part (everything a cut to zero items leaves) was itself near the
budget left no room for list detail, and a larger one stayed over the budget
with every list emptied. These checks run the real deterministic pipeline on
each bundled sample and build the request exactly as synthesis does
(``prepare_synthesis_llm_input``), with no model call.
"""

from __future__ import annotations

import pytest

from scripts.run_synthesis import _resolve_version
from src.agents.referee import summary_input_budget as budget
from src.agents.referee.synthesis_handler import (
    prepare_synthesis_llm_input,
    run_synthesis_deterministic,
)
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

# Lists cut to this many items must fit with headroom: 12 is the limit discourse
# (the one sample that needs a cut) was held to before #1064, so the request has
# to fit there with room to spare, whatever limit the cap itself ends up choosing.
REFERENCE_LIST_LIMIT = 12
HEADROOM = 0.85
# Share of the budget the request's fixed part may take, so a real workload with
# model-written designs (more trade-offs, mappings and risks) still has room.
MAX_FIXED_SHARE = 0.5


@pytest.fixture
def request_and_det(run: PipelineResult) -> tuple[dict, dict]:
    if run.external:
        pytest.skip("checks the bundled samples")
    store = LocalArtifactStore(base_dir=str(run.artifact_root))
    version = _resolve_version(store, run.job_id, run.db, None)
    det = run_synthesis_deterministic(run.job_id, run.db, store, version)
    return prepare_synthesis_llm_input(det), det


def test_the_request_fits_the_budget_with_its_list_detail_kept(
    request_and_det: tuple[dict, dict],
) -> None:
    payload, _ = request_and_det
    assert budget.estimate_tokens(payload) <= budget.SUMMARY_INPUT_TOKEN_BUDGET
    assert budget.headless_lines(payload) <= budget.SUMMARY_INPUT_LINE_BUDGET
    status = payload.get("input_budget")
    if status is not None:
        assert status["within_budget"] is True
    whole = {k: v for k, v in payload.items() if k != "input_budget"}
    weights = budget._group_weights(whole.get("query_groups") or [])
    cut, _ = budget._cap(whole, REFERENCE_LIST_LIMIT, weights)
    assert budget.estimate_tokens(cut) <= HEADROOM * budget.SUMMARY_INPUT_TOKEN_BUDGET


def test_every_wave_states_the_unit_of_its_share(request_and_det: tuple[dict, dict]) -> None:
    payload, _ = request_and_det
    waves = payload["migration_waves"]
    assert waves
    assert all(w["share_basis"] in {"queries", "calls"} for w in waves)


def test_the_fixed_part_leaves_room_for_the_lists(request_and_det: tuple[dict, dict]) -> None:
    payload, _ = request_and_det
    payload = {k: v for k, v in payload.items() if k != "input_budget"}
    weights = budget._group_weights(payload.get("query_groups") or [])
    emptied, _ = budget._cap(payload, 0, weights)
    fixed = budget.estimate_tokens(emptied)
    assert fixed <= MAX_FIXED_SHARE * budget.SUMMARY_INPUT_TOKEN_BUDGET, fixed
