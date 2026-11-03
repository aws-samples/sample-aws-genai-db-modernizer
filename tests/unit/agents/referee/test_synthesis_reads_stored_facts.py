# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis reads the collection's stored query facts before the ranking (#384).

Every other step calls ``remember_collected_facts`` when it loads the collection; the
synthesis called it only for the query-pattern groups, after the ranking, so a
standalone ``run_synthesis.py`` parsed every statement again for the ranking (most of
its time at 50,000 statements), and read them with the default string settings
instead of the source's.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.agents.referee import synthesis_handler
from src.agents.referee.synthesis_data import SynthesisData
from src.shared.sql_spans import DEFAULT_SPAN_SETTINGS, active_span_settings, use_span_settings
from src.tools.sql import query_facts as qf


class _Stop(Exception):
    pass


def test_the_ranking_runs_with_the_stored_facts_and_the_source_settings():
    text = "SELECT a FROM t WHERE id = 1"
    collector = {
        "metadata": {
            "source_database": {
                "engine": "mysql",
                "settings": {"sql_mode": "NO_BACKSLASH_ESCAPES"},
            }
        },
        "queries": {"query_patterns": [{"query_id": "q1", "query_text": text}]},
    }
    data = SynthesisData(job_id="j", database_name="d", collector=collector)
    seen: dict = {}

    def ranking(_data):
        seen["settings"] = active_span_settings()
        raise _Stop

    use_span_settings(DEFAULT_SPAN_SETTINGS)
    try:
        with (
            patch.object(synthesis_handler, "load_synthesis_data", return_value=data),
            patch.object(synthesis_handler, "build_ranking", side_effect=ranking),
            patch.object(qf, "remember_collected_facts", wraps=qf.remember_collected_facts) as rem,
            patch("src.storage.assignment_versioning.read_reality_check_output", return_value=None),
        ):
            with pytest.raises(_Stop):
                synthesis_handler.run_synthesis_deterministic(MagicMock(), "j", "d", 0)
        rem.assert_called_once_with(collector)
        assert seen["settings"].no_backslash_escapes is True
    finally:
        use_span_settings(DEFAULT_SPAN_SETTINGS)
