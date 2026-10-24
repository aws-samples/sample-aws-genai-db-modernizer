"""``scripts/collect-oracle.sql`` emits one query row per statement shape (#412)."""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[3] / "scripts" / "collect-oracle.sql").read_text()


def _queries_section() -> str:
    start = SCRIPT.index('"queries": [')
    return SCRIPT[start : SCRIPT.index("-- 2. Metadata", start)]


def test_digest_is_the_force_matching_signature_with_sql_id_fallback() -> None:
    section = _queries_section()
    assert "FORCE_MATCHING_SIGNATURE" in section
    assert re.search(r"'digest' VALUE g\.query_key", section)
    assert "'digest' VALUE SQL_ID" not in section


def test_counters_are_summed_across_variants() -> None:
    section = _queries_section()
    for col in (
        "executions",
        "elapsed_time",
        "rows_processed",
        "buffer_gets",
        "cpu_time",
        "disk_reads",
        "application_wait_time",
    ):
        assert f"SUM(v.{col})" in section, col
    assert "MIN(v.first_load_time)" in section
    assert "MAX(v.last_active_time)" in section


def test_floor_and_cap_apply_after_grouping() -> None:
    section = _queries_section()
    assert section.index("GROUP BY v.query_key") < section.index("WHERE g.executions >= 10")
    assert section.index("WHERE g.executions >= 10") < section.index("FETCH FIRST 1000 ROWS ONLY")


def test_sample_text_is_the_costliest_variant_with_a_stable_tiebreak() -> None:
    section = _queries_section()
    assert "PARTITION BY query_key" in section
    assert "ORDER BY ELAPSED_TIME DESC, SQL_ID" in section


def test_total_elapsed_denominator_uses_the_same_grouping() -> None:
    declare = SCRIPT[SCRIPT.index("-- Pre-compute total elapsed") : SCRIPT.index('"queries": [')]
    assert "FORCE_MATCHING_SIGNATURE" in declare
    assert "HAVING SUM(EXECUTIONS) >= 10" in declare
