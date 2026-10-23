"""#1348: a table's profile counts the writes its write group counts, on every sample.

ADR-031 Amendment 9: a table's writes are its targets, decided once, by
``write_groups.written_tables``. The table profile (``build_table_profiles``) used to
count only statements typed as writes, so discourse's data-modifying CTEs that end
in ``SELECT`` (collected as ``OTHER``) were writes in the write group and not in the
profile.

The wordpress and trailgear cases of the two parametrized tests pass on main by design:
those samples have no write that is not typed as DML, so they guard that their counts
stay unchanged. Only the discourse cases fail on main.
"""

from __future__ import annotations

import json

from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.write_groups import collector_write_groups, written_tables
from src.contracts.analysis_output import WorkloadAnalysis
from src.tools.analysis.scoring import build_table_profiles
from src.tools.sql.query_facts import source_engine_of
from tests.e2e.pipeline import PipelineResult

_DML_TYPES = {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"}


def _collector(run: PipelineResult) -> dict:
    out: dict = json.loads(
        (run.job_dir() / "collector" / "output.json").read_text(encoding="utf-8")
    )
    return out


def _write_counts(collector: dict) -> dict[str, int]:
    profiles = build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))
    return {t: p.write_query_count for t, p in profiles.items() if p.write_query_count}


def test_every_profile_write_count_equals_its_write_group(run: PipelineResult) -> None:
    collector = _collector(run)
    groups = collector_write_groups(collector)
    assert _write_counts(collector) == {t: len(m) for t, m in groups.items()}, run.db


def test_only_writes_not_typed_as_dml_change_a_profile(run: PipelineResult) -> None:
    """The profile differs from the old typed-DML count only by the writes not typed as
    DML; a sample with none (wordpress, trailgear) keeps every count."""
    collector = _collector(run)
    engine = source_engine_of(collector)
    scope = SourceScope.from_collector(collector)
    typed: dict[str, int] = {}
    untyped: dict[str, int] = {}
    for q in collector["queries"]["query_patterns"]:
        counts = typed if str(q.get("query_type") or "").upper() in _DML_TYPES else untyped
        for t in written_tables(q, engine, scope):
            counts[t] = counts.get(t, 0) + 1
    expected = dict(typed)
    for t, n in untyped.items():
        expected[t] = expected.get(t, 0) + n
    assert _write_counts(collector) == expected, run.db
    if run.db in {"wordpress", "trailgear"}:
        assert untyped == {}, run.db


def test_discourse_user_badges_counts_its_cte_writes(discourse_run: PipelineResult) -> None:
    """The #1348 repro: 39 of user_badges' writes are ``WITH w AS (INSERT ...) SELECT``."""
    collector = _collector(discourse_run)
    groups = collector_write_groups(collector)
    counts = _write_counts(collector)
    assert counts["discourse.user_badges"] == len(groups["discourse.user_badges"])
    assert counts["discourse.topic_links"] == len(groups["discourse.topic_links"])
