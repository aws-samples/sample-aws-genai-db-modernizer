"""The UI states the owner rule in the engineering report's words (#212).

``src/ui/src/locales/en.json`` carries the results page's copy of
``src.report.table_owners.OWNER_RULE``, and its per-table counts follow
``ownership_summary_text``'s wording, so the two deliverables cannot drift apart
silently (the same way ``test_workload_fit_labels_js_sync.py`` pins its labels).
"""

from __future__ import annotations

import json
from pathlib import Path

from src.report.table_owners import OWNER_RULE, ownership_summary_text

REPO = Path(__file__).resolve().parents[3]
EN = json.loads((REPO / "src/ui/src/locales/en.json").read_text())


def test_the_ui_states_the_owner_rule_word_for_word() -> None:
    assert EN["analysis-results.table-owners.rule"] == OWNER_RULE


def test_the_ui_counts_use_the_reports_wording() -> None:
    facts = {
        "table_count": 3,
        "owned_table_counts": {"dynamodb": 2, "aurora_mysql": 1},
        "no_owner_table_count": 0,
        "also_read_table_count": 2,
        "cached_table_count": 2,
        "cache_engines": ["elasticache"],
    }
    text = ownership_summary_text(facts)
    for key, values in (
        ("owns", {"engine": "DynamoDB", "count": 2}),
        ("also-read_other", {"count": 2}),
        ("cached_other", {"count": 2, "engines": "ElastiCache"}),
    ):
        phrase = EN[f"analysis-results.table-owners.{key}"]
        for name, value in values.items():
            phrase = phrase.replace("{{" + name + "}}", str(value))
        assert phrase in text, (key, phrase, text)
