"""Sweep both sample fixtures through ``normalize_digest_text`` and check
the ``truncated`` flag against ground truth (PR #560 review round 1,
Important 2).

wordpress (MySQL) has exactly three queries whose digest text is genuinely
cut off by ``performance_schema_max_digest_length`` -- see
``test_normalize.py::TestTruncatedRealExamples`` for the reasoning behind
each one. discourse (PostgreSQL) has none, so the sweep must find zero
false positives there.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from src.tools.sql.normalize import normalize_digest_text

REPO = Path(__file__).resolve().parents[4]

# 0-based indices into wordpress-collection.json's "queries" list whose
# digest text is genuinely truncated.
_WORDPRESS_TRUNCATED_INDICES = frozenset({2, 50, 79})


def _load_query_texts(sample: str) -> list[str]:
    zip_path = REPO / "docs" / "examples" / sample / f"{sample}.zip"
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read(f"{sample}-collection.json").decode("utf-8")
    # The wordpress fixture's JSON file is prefixed with a non-JSON label line.
    text = raw.split("\n", 1)[1] if raw.startswith("collection_output") else raw
    data = json.loads(text)
    return [q["query_text"] for q in data["queries"]]


def test_wordpress_truncation_matches_the_three_known_cut_off_queries() -> None:
    queries = _load_query_texts("wordpress")
    flagged = {
        i for i, query in enumerate(queries) if normalize_digest_text(query, "mysql").truncated
    }
    assert flagged == _WORDPRESS_TRUNCATED_INDICES


def test_discourse_has_zero_truncation_false_positives() -> None:
    queries = _load_query_texts("discourse")
    flagged = [
        i for i, query in enumerate(queries) if normalize_digest_text(query, "postgresql").truncated
    ]
    assert flagged == []
