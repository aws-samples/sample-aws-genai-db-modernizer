# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validators no production path called are gone, not left to look like guards (#2154).

Each duplicated a check the pipeline already runs elsewhere, so it read as a rule
being enforced while only its own tests ran it:

- ``engine_exclusions.validate_customer_overrides``: a customer override onto an
  excluded engine is rejected by ``assignment_overrides.customer_gate_failure``
  (through the shared scorer, #548 and #801).
- ``engine_exclusions.check_all_exclusions``: every caller gates one engine at a time
  with ``check_exclusions``.
- ``scope.validate_schema_scope``: ``handler.check_schema_scope`` runs
  ``assess_schema_scope`` itself.
"""

from __future__ import annotations

import pytest

from src.agents.referee import engine_exclusions
from src.agents.schema_design import scope


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (engine_exclusions, "validate_customer_overrides"),
        (engine_exclusions, "check_all_exclusions"),
        (scope, "validate_schema_scope"),
    ],
)
def test_the_uncalled_validator_is_removed(module, name: str) -> None:
    assert not hasattr(module, name)
