# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The results page shows the first characters of a close call's query id, as the routing table does.

On discourse (337 close calls) 5 characters gave 2 visible prefix collisions; 8 give none.
The width is read from the UI source (formatShortId), so a change there is checked against the sample.
"""

from __future__ import annotations

import json
import re

from tests.e2e.pipeline import REPO, PipelineResult

FORMAT = REPO / "src" / "ui" / "src" / "utils" / "format.js"


def _ui_width() -> int:
    match = re.search(
        r"export function formatShortId\(id\) \{[^}]*?\.slice\(0, (\d+)\)", FORMAT.read_text()
    )
    assert match, "formatShortId not found in format.js"
    return int(match.group(1))


def _listed_ids(run: PipelineResult) -> list[str]:
    """Every close call (and settled tie) the report lists, not only the few ids the banner names."""
    digest = json.loads(run.report_json().read_text())["query_traces"]
    return [str(row["query_id"]) for row in digest["close_calls"]]


def test_the_results_page_shows_eight_characters() -> None:
    """The close calls table shows ids as the query routing table does (#2318)."""
    assert _ui_width() == 8


def test_discourse_close_call_short_ids_are_unique(discourse_run: PipelineResult) -> None:
    ids = sorted(set(_listed_ids(discourse_run)))
    width = _ui_width()
    assert len(ids) > 300, "the discourse sample should list about 337 close-call queries"
    shorts: dict[str, str] = {}
    for qid in ids:
        short = qid[:width]
        assert short not in shorts, f"{qid} and {shorts[short]} both show as {short!r}"
        shorts[short] = qid


def test_five_characters_would_collide_on_discourse(discourse_run: PipelineResult) -> None:
    """The negative case: the width is not arbitrary, 5 does collide on this sample."""
    ids = sorted(set(_listed_ids(discourse_run)))
    assert len({qid[:5] for qid in ids}) < len(ids)
