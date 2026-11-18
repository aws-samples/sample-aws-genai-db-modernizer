# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The words a deliverable uses for a risk severity (#2442).

``report.json`` keeps the stored ids (``CRITICAL``, ``HIGH``, ``MEDIUM``, ``LOW``).
Every Python renderer shows them through this module, so the engineering report,
the executive report PDF and the printed summary say what the results page says.
The UI keeps its own words in ``en.json`` under
``analysis-results.risk-assessment.severity.*``; the two lists match word for word.
"""

from __future__ import annotations

from typing import Any

SEVERITY_LABELS: dict[str, str] = {
    "CRITICAL": "Critical",
    "HIGH": "High",
    "MEDIUM": "Medium",
    "LOW": "Low",
}
UNRATED_LABEL = "Unrated"


def severity_label(level: Any) -> str:
    """``"HIGH"`` -> ``"High"``; any other value is ``"Unrated"``. Case-insensitive."""
    return SEVERITY_LABELS.get(str(level or "").strip().upper(), UNRATED_LABEL)


def severity_word(level: Any) -> str:
    """The label in running text: ``"HIGH"`` -> ``"high"``."""
    return severity_label(level).lower()
