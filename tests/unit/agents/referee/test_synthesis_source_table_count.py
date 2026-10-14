"""#530 review (round 2): ``SynthesisOutputContract.source_table_count`` gives
``src.report.architecture._n_tables_mapped`` a structured figure to read instead
of parsing it back out of ``build_summary``'s "Analyzed N source tables ..."
opening sentence.

``synthesis_report.source_table_count`` is the one place that number is
computed; ``build_summary`` now calls it too, so the structured field and the
summary sentence can never disagree. This test proves that by running
``build_summary``'s real output text through the renderer's own
``_ANALYZED_SOURCE_TABLES`` regex and checking the parsed number against
``source_table_count``.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import SynthesisData
from src.agents.referee.synthesis_report import build_summary, source_table_count
from src.shared.report_facts import ANALYZED_SOURCE_TABLES as _ANALYZED_SOURCE_TABLES

RANKING = [
    {
        "target": "dynamodb",
        "confidence_score": 50,
        "weight": 1.0,
        "assigned_queries": 5,
        "workload_percent": 100.0,
    }
]
TCO = {"projected_monthly_cost": 1.0, "savings_percent": 0}
RISKS = {"risks": [], "overall_risk_level": "LOW"}


def _data(n_tables: int) -> SynthesisData:
    return SynthesisData(
        job_id="j",
        database_name="db",
        collector={
            "database_schema": {"tables": [{"table_id": f"t{i}"} for i in range(n_tables)]},
            "queries": {"query_patterns": [{"query_id": f"q{i}"} for i in range(5)]},
        },
    )


def test_matches_len_of_the_collected_source_tables() -> None:
    data = _data(42)
    assert source_table_count(data) == 42 == len(data.source_tables)


def test_matches_build_summarys_own_analyzed_line() -> None:
    data = _data(42)
    text = build_summary(data, RANKING, [], TCO, RISKS, [])
    assert text.startswith("Analyzed 42 source tables")
    m = _ANALYZED_SOURCE_TABLES.search(text)
    assert m is not None
    assert int(m.group(1)) == source_table_count(data)


def test_matches_across_a_second_table_count() -> None:
    # Not a coincidence of one particular number: re-run at a different
    # schema size, including the discourse-shaped scale from #530's review.
    for n in (0, 1, 311):
        data = _data(n)
        text = build_summary(data, RANKING, [], TCO, RISKS, [])
        m = _ANALYZED_SOURCE_TABLES.search(text)
        assert m is not None
        assert int(m.group(1)) == source_table_count(data) == n
