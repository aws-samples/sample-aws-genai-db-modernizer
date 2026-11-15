# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one rule for a metric shown in a deliverable (#259, #2285).

Lives in ``src.shared`` so the facts layer's sentences and the report renderers
(``src.report.formatting.fmt_num``, re-exported) format a figure the same way; the results
page mirrors it in ``formatFigure`` (``src/ui/src/utils/reportFacts.js``).
"""

from __future__ import annotations

import math
from typing import Any


def fmt_num(value: Any, decimals: int = 2) -> str:
    """A metric for display: thousands separators, at most ``decimals`` places.

    Sums of float metrics carry binary noise (``17.460499999999996``); rounding
    here keeps it out of every deliverable (#259). Trailing zeros are dropped
    (``3.0`` -> ``3``); a value that rounds to zero is ``0`` (never ``-0``), and a
    small non-zero one is ``<0.01`` (``>-0.01`` if negative) rather than ``0``.
    NaN and infinities print as an en dash. Anything that is not a number is
    returned as ``str()``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if not math.isfinite(value):
        return "\u2013"
    step = 10.0**-decimals
    if value != 0 and abs(value) < step / 2:
        return f"<{step:.{decimals}f}" if value > 0 else f">-{step:.{decimals}f}"
    text = f"{value:,.{decimals}f}"
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text in ("-0", "") else text
