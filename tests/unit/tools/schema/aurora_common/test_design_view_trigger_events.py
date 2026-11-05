# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Aurora designers see every event a trigger fires on (#1651)."""

from __future__ import annotations

from src.agents.collector.mysql_collector import _build_triggers
from src.tools.schema.aurora_common.design_view import _source_features


def _features(triggers: list[dict]) -> list[dict]:
    collector = {"database_schema": {"triggers": triggers}}
    shown: list[dict] = _source_features(collector, {"orders"})["triggers"]
    return shown


def test_a_multi_event_trigger_shows_every_event():
    built = _build_triggers(
        [{"trigger_name": "tg", "table_name": "orders", "timing": "AFTER",
          "event_type": "INSERT OR UPDATE OR DELETE"}]  # fmt: skip
    )
    (t,) = _features([t.model_dump(mode="json") for t in built or []])
    assert t["events"] == ["INSERT", "UPDATE", "DELETE"]
    assert t["event_type"] == "INSERT"


def test_a_collector_output_without_events_shows_its_one_event():
    old = {"trigger_name": "tg", "table_id": "orders", "timing": "AFTER", "event_type": "DELETE"}
    (t,) = _features([old])
    assert t["events"] == ["DELETE"]
    (t,) = _features([{**old, "events": []}])
    assert t["events"] == ["DELETE"]
