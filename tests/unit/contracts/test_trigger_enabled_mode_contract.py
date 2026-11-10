# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A trigger's exact enable mode (#2071): PostgreSQL's ``tgenabled`` / ``evtenabled``
letter, kept next to ``is_enabled`` on ``Trigger`` and ``NonDmlTrigger``. Additive: a saved
collection loads with the mode unknown, and the engines with no such mode leave it unset."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agents.collector.mysql_collector import trigger_shape_fields
from src.agents.collector.routine_rows import trigger_enabled_mode
from src.agents.collector.routine_sources import non_dml_triggers
from src.contracts.collector_output import CollectorOutputContract, NonDmlTrigger, Trigger
from src.shared.contract_versioning import load_contract

REPO = Path(__file__).resolve().parents[3]
OLD_OUTPUT = REPO / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/collector/output.json"

_TRIGGER = {"trigger_id": "tg", "trigger_name": "tg", "table_id": "orders",
            "event_type": "INSERT", "timing": "AFTER", "is_enabled": True}  # fmt: skip
_NON_DML = {"trigger_id": "tg", "trigger_name": "tg", "scope": "table", "table_id": "orders",
            "events": ["TRUNCATE"], "is_enabled": False}  # fmt: skip


@pytest.mark.parametrize("model, row", [(Trigger, _TRIGGER), (NonDmlTrigger, _NON_DML)])
def test_the_mode_is_one_of_postgresql_s_four_letters(model, row):
    for letter in ("O", "D", "R", "A"):
        assert model.model_validate({**row, "enabled_mode": letter}).enabled_mode == letter
    for bad in ("o", "X", "ENABLED", "ALWAYS", 1, True):
        with pytest.raises(ValidationError):
            model.model_validate({**row, "enabled_mode": bad})
    assert model.model_validate(row).enabled_mode is None  # unknown, never guessed


def test_a_saved_collection_loads_with_the_mode_unknown():
    old = json.loads(OLD_OUTPUT.read_text())
    old["database_schema"]["triggers"] = [_TRIGGER]
    old["database_schema"]["non_dml_triggers"] = [_NON_DML]
    loaded = load_contract(CollectorOutputContract, old, artifact=str(OLD_OUTPUT))
    trigger, other = loaded.database_schema.triggers[0], loaded.database_schema.non_dml_triggers[0]
    assert (trigger.enabled_mode, trigger.is_enabled) == (None, True)
    assert (other.enabled_mode, other.is_enabled) == (None, False)


@pytest.mark.parametrize(
    ("raw", "mode"),
    [("O", "O"), ("D", "D"), ("R", "R"), ("A", "A"), (" A ", "A"),
     # MySQL / MariaDB report no state; SQL Server 0/1; Oracle ENABLED / DISABLED
     (None, None), ("", None), (0, None), (1, None), (True, None), ("ENABLED", None),
     ("DISABLED", None), ("YES", None), ("a", None)],
)  # fmt: skip
def test_only_a_postgresql_letter_gives_a_mode(raw, mode):
    assert trigger_enabled_mode(raw) == mode


@pytest.mark.parametrize("raw", [None, 0, 1, "ENABLED", "DISABLED"])
def test_the_other_engines_triggers_leave_the_mode_unset(raw):
    row = {"trigger_name": "tg", "table_name": "orders", "event_type": "TRUNCATE",
           "timing": "AFTER", "is_enabled": raw, "base_object_type": "TABLE"}  # fmt: skip
    assert trigger_shape_fields(row)["enabled_mode"] is None
    assert [t.enabled_mode for t in non_dml_triggers([row]) or []] == [None]


def test_is_enabled_keeps_its_meaning():
    """O and A fire for ordinary sessions, D and R do not, as before."""
    got = {letter: trigger_shape_fields({"is_enabled": letter}) for letter in ("O", "D", "R", "A")}
    assert {k: (v["is_enabled"], v["enabled_mode"]) for k, v in got.items()} == {
        "O": (True, "O"), "D": (False, "D"), "R": (False, "R"), "A": (True, "A"),
    }  # fmt: skip
