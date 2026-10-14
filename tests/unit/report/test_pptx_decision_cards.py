"""The decisions slide keeps every decision card on the slide (#713 review round 3).

The #629 "moved after approval" card can be the fifth; at the original 1.30"
step it ended at 8.3" on a 7.5" slide.
"""

from __future__ import annotations

import pytest

from src.report.pptx_report import (
    DECISION_BOTTOM_MARGIN,
    DECISION_CARDS_AT_FULL_SIZE,
    SLIDE_BOTTOM,
    decision_card_layout,
)


@pytest.mark.parametrize("n", range(1, 8))
def test_every_card_ends_above_the_bottom_margin(n: int) -> None:
    layout = decision_card_layout(n)
    assert len(layout) == n
    top, height = layout[-1]
    assert top + height <= SLIDE_BOTTOM - DECISION_BOTTOM_MARGIN + 1e-9
    for (y1, h1), (y2, _h2) in zip(layout, layout[1:], strict=False):
        assert y1 + h1 <= y2  # no overlap


def test_four_cards_keep_the_original_size() -> None:
    assert all(h == 1.18 for _y, h in decision_card_layout(DECISION_CARDS_AT_FULL_SIZE))
