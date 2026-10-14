"""The "moved after approval" wording is the same in every deliverable (#629).

The decision report, engineering report and deck read
``src.report.engine_choices``; the interactive analysis report and the UI
export embed the same sentence and reason wording in
``src/ui/src/utils/ExportReport.js`` (extracted to
``src/report/templates/analysis_report.js``). A wording change on one side
only fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.report.engine_choices import REROUTE_OVERRIDE_TEXT
from src.shared.flag_reasons import FLAG_REASON_TEXT

REPO = Path(__file__).resolve().parents[3]


def test_the_ui_export_carries_the_same_override_sentence_and_reasons() -> None:
    for path in (
        REPO / "src/ui/src/utils/ExportReport.js",
        REPO / "src/report/templates/analysis_report.js",
    ):
        text = path.read_text(encoding="utf-8")
        assert json.dumps(REROUTE_OVERRIDE_TEXT) in text.replace("\\'", "'"), path
        assert json.dumps(FLAG_REASON_TEXT) in text.replace("\\'", "'"), path
