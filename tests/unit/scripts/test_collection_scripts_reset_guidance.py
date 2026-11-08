# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every collection script asks not to reset the statement statistics first (#1991).

A collection taken right after a reset counts only the minutes since, often a load test,
and the assessment then rates that window's load as continuous traffic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = ["mysql", "postgresql", "sqlserver", "oracle"]


@pytest.mark.parametrize("engine", SCRIPTS)
def test_each_script_asks_not_to_reset_the_statistics_before_collecting(engine: str) -> None:
    lines = (REPO / "scripts" / f"collect-{engine}.sql").read_text().splitlines()
    header = []
    for line in lines:
        if line.strip() and not line.startswith("--"):
            break
        header.append(line.lstrip("- ").strip())
    text = " ".join(header)
    assert "Do not reset the statement statistics before collecting" in text
    assert "do not collect right after a restart" in text


def test_the_guide_asks_it_too() -> None:
    guide = (REPO / "docs" / "use-claude-code.md").read_text()
    assert "Don't reset the statement statistics before collecting" in guide


def test_collected_at_is_utc_in_every_script() -> None:
    """The check dates a window from ``collected_at`` and says "UTC" (#2045 review): the
    MySQL and PostgreSQL scripts print it in UTC, not the session's time zone."""
    texts = {e: (REPO / "scripts" / f"collect-{e}.sql").read_text() for e in SCRIPTS}
    collected = {
        e: [line for line in t.splitlines() if "collected_at" in line and "--" not in line[:4]]
        for e, t in texts.items()
    }
    assert any("UTC_TIMESTAMP()" in line for line in collected["mysql"]), collected["mysql"]
    assert all("NOW()" not in line for line in collected["mysql"])
    assert any("now() AT TIME ZONE 'UTC'" in line for line in collected["postgresql"])
    assert any("GETUTCDATE()" in line for line in collected["sqlserver"])
