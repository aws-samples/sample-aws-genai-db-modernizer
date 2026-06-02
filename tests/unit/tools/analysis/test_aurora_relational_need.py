"""Tests for graduated relational need baseline scoring."""


from src.tools.analysis.aurora_common_analysis_tools import compute_relational_need_score


class TestRelationalNeedScore:
    """Relational need score replaces flat BASELINE_PATTERN_MATCH_FLOOR."""

    def test_zero_relational_signals_gives_low_score(self):
        score = compute_relational_need_score(
            fk_outbound_count=0,
            fk_inbound_count=0,
            join_query_count=0,
            multi_table_write_count=0,
            total_query_count=5,
        )
        assert score <= 25

    def test_high_fk_density_gives_high_score(self):
        score = compute_relational_need_score(
            fk_outbound_count=4,
            fk_inbound_count=0,
            join_query_count=0,
            multi_table_write_count=0,
            total_query_count=5,
        )
        assert score >= 50

    def test_inbound_fks_signal_relational_need(self):
        score = compute_relational_need_score(
            fk_outbound_count=0,
            fk_inbound_count=3,
            join_query_count=0,
            multi_table_write_count=0,
            total_query_count=5,
        )
        assert score >= 45

    def test_join_participation_increases_score(self):
        score = compute_relational_need_score(
            fk_outbound_count=0,
            fk_inbound_count=0,
            join_query_count=5,
            multi_table_write_count=0,
            total_query_count=10,
        )
        assert score >= 40

    def test_multi_table_writes_increase_score(self):
        score = compute_relational_need_score(
            fk_outbound_count=1,
            fk_inbound_count=0,
            join_query_count=2,
            multi_table_write_count=3,
            total_query_count=10,
        )
        assert score >= 50

    def test_fully_relational_table_scores_high(self):
        score = compute_relational_need_score(
            fk_outbound_count=3,
            fk_inbound_count=2,
            join_query_count=8,
            multi_table_write_count=4,
            total_query_count=15,
        )
        assert score >= 60

    def test_score_clamped_to_range(self):
        score = compute_relational_need_score(
            fk_outbound_count=10,
            fk_inbound_count=10,
            join_query_count=50,
            multi_table_write_count=20,
            total_query_count=100,
        )
        assert 0 <= score <= 65

    def test_no_queries_gives_minimum(self):
        score = compute_relational_need_score(
            fk_outbound_count=2,
            fk_inbound_count=1,
            join_query_count=0,
            multi_table_write_count=0,
            total_query_count=0,
        )
        assert 20 <= score <= 40
