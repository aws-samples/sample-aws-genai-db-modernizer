# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No migrating wave has more than two engines (#2162).

The facts layer attributes a migrating wave's in-wave moves and its shared kept-current
reads only for one- or two-engine waves (``report_facts.engine_served_split``), so each
Scope row adds up to the wave. The only two-engine wave is the merged DynamoDB/DocumentDB
one, DynamoDB first by construction. A builder change that put a third engine in a
migrating wave would leave its split unattributed: this pins that it does not, with
every engine routed, and on every saved report the tests carry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.migration_waves import build_migration_waves
from src.shared.report_facts import is_migrating_wave

ENGINES = ("aurora_mysql", "dynamodb", "documentdb", "opensearch", "elasticache")
REPO = Path(__file__).resolve().parents[4]


def _every_engine_routed() -> list[dict[str, Any]]:
    tables = {e: f"app.t_{e}" for e in ENGINES}
    waves = build_migration_waves(
        ranking=[
            {"target": e, "assigned_queries": 2, "workload_percent": 20.0}
            for e in ENGINES
            if e != "elasticache"
        ],
        table_assignments=[
            {"table_id": t, "primary_engine": e, "query_count": 2} for e, t in tables.items()
        ],
        query_assignments=[
            {
                "query_id": f"q_{e}_{i}",
                "assigned_engine": e,
                # Every engine also reads the shared table: the case #2162 counts once.
                "source_tables": [t, "app.shared"],
            }
            for e, t in tables.items()
            if e != "elasticache"
            for i in range(2)
        ],
        co_dependency_groups=[],
        cache_overlay={"engine": "elasticache", "query_count": 1, "query_ids": ["q_dynamodb_0"]},
        source_engine="mysql",
        source_version="8.0.45",
        known_tables={*tables.values(), "app.shared"},
    )
    assert waves
    return waves


@pytest.mark.passes_on_main(reason="pins the builders' wave shape the #2162 split relies on")
def test_no_migrating_wave_has_more_than_two_engines() -> None:
    waves = _every_engine_routed()
    migrating = [w for w in waves if is_migrating_wave(w)]
    assert migrating
    assert all(len(w["engines"]) <= 2 for w in migrating), [w["engines"] for w in migrating]
    pairs = [w["engines"] for w in migrating if len(w["engines"]) == 2]
    assert pairs == [["dynamodb", "documentdb"]]


def _saved_reports() -> list[Path]:
    roots = [REPO / "tests" / "fixtures", REPO / "tests" / "unit"]
    return sorted(p for root in roots for p in root.rglob("*report*.json"))


@pytest.mark.passes_on_main(reason="pins the builders' wave shape the #2162 split relies on")
def test_no_saved_report_has_a_migrating_wave_with_more_than_two_engines() -> None:
    checked = 0
    for path in _saved_reports():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        for w in report.get("migration_waves") or []:
            if isinstance(w, dict) and is_migrating_wave(w):
                checked += 1
                assert len(w.get("engines") or []) <= 2, (path, w.get("engines"))
    assert checked
