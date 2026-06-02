"""
Validates that all test fixtures pass full Pydantic contract validation.

Run: pytest tests/fixtures/test_fixture_validation.py -v
"""

from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.collector_output import CollectorOutputContract
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.fixtures.gaming_collector_output import get_gaming_collector_output


class TestEcommerceFixtureValidation:
    """Ensure the e-commerce fixture is fully contract-compliant."""

    def test_collector_output_contract_validates(self):
        """The raw dict must pass CollectorOutputContract v3.0 validation."""
        data = get_ecommerce_collector_output()
        contract = CollectorOutputContract(**data)

        assert contract.contract_version == "3.0"
        assert contract.job_id == "job-ecommerce-001"
        assert contract.metadata.source_database.engine.value == "mysql"
        assert len(contract.database_schema.tables) == 8
        assert len(contract.queries.query_patterns) == 14

    def test_can_wrap_in_analysis_input(self):
        """The fixture must work as collector_output inside AnalysisInput."""
        data = get_ecommerce_collector_output()
        analysis_input = AnalysisInput(
            job_id="job-ecommerce-001",
            collector_output=data,
            target_database=TargetDatabase.elasticache,
            target_region="us-east-1",
        )

        assert analysis_input.target_database == TargetDatabase.elasticache
        assert analysis_input.collector_output["job_id"] == "job-ecommerce-001"

    def test_schema_completeness(self):
        """All tables should have columns, indexes, and primary keys."""
        data = get_ecommerce_collector_output()
        contract = CollectorOutputContract(**data)

        for table in contract.database_schema.tables:
            assert len(table.columns) >= 1, f"{table.table_id} has no columns"
            assert (
                table.indexes is not None and len(table.indexes) >= 1
            ), f"{table.table_id} has no indexes"
            assert (
                table.primary_key is not None and len(table.primary_key) >= 1
            ), f"{table.table_id} has no primary key"

    def test_query_patterns_cover_all_types(self):
        """Fixture should contain SELECT and INSERT query types."""
        data = get_ecommerce_collector_output()
        contract = CollectorOutputContract(**data)

        query_types = {q.query_type.value for q in contract.queries.query_patterns if q.query_type}
        assert "SELECT" in query_types
        assert "INSERT" in query_types

    def test_rds_metadata_present(self):
        """Fixture should include full RDS instance metadata."""
        data = get_ecommerce_collector_output()
        contract = CollectorOutputContract(**data)

        rds = contract.metadata.source_database.rds_instance_metadata
        assert rds is not None
        assert rds.instance_class == "db.r6g.xlarge"
        assert rds.multi_az is True
        assert rds.performance_insights_enabled is True

    def test_views_procedures_triggers_present(self):
        """Fixture should include views, procedures, and triggers."""
        data = get_ecommerce_collector_output()
        contract = CollectorOutputContract(**data)

        assert contract.database_schema.views is not None
        assert len(contract.database_schema.views) >= 1
        assert contract.database_schema.procedures is not None
        assert len(contract.database_schema.procedures) >= 1
        assert contract.database_schema.triggers is not None
        assert len(contract.database_schema.triggers) >= 1

    def test_cloudwatch_metrics_present(self):
        """Fixture should include RDS CloudWatch metrics."""
        data = get_ecommerce_collector_output()
        contract = CollectorOutputContract(**data)

        cw = contract.metrics.rds_cloudwatch_metrics
        assert cw is not None
        assert cw.cpu_utilization is not None
        assert cw.read_iops is not None


class TestEcommerceFixtureRedisPatterns:
    """Verify the fixture contains queries that trigger each Redis pattern."""

    def _get_queries(self):
        return get_ecommerce_collector_output()["queries"]["query_patterns"]

    def test_has_caching_candidates(self):
        """Should have high-frequency SELECT queries (calls_per_second > 1)."""
        caching = [
            q
            for q in self._get_queries()
            if q.get("query_type") == "SELECT" and q.get("calls_per_second", 0) > 1
        ]
        assert len(caching) >= 3, f"Expected >=3 caching candidates, got {len(caching)}"

    def test_has_session_store_candidates(self):
        """Should have queries referencing session/user_id."""
        session = [
            q
            for q in self._get_queries()
            if "session" in q.get("query_text", "").lower()
            or "user_id" in q.get("query_text", "").lower()
        ]
        assert len(session) >= 2, f"Expected >=2 session queries, got {len(session)}"

    def test_has_leaderboard_candidates(self):
        """Should have ORDER BY + LIMIT queries."""
        leaderboard = [
            q
            for q in self._get_queries()
            if "order by" in q.get("query_text", "").lower()
            and "limit" in q.get("query_text", "").lower()
        ]
        assert len(leaderboard) >= 2, f"Expected >=2 leaderboard queries, got {len(leaderboard)}"

    def test_has_time_series_candidates(self):
        """Should have timestamp/date + GROUP BY queries."""
        timeseries = [
            q
            for q in self._get_queries()
            if any(
                kw in q.get("query_text", "").lower() for kw in ["timestamp", "created_at", "date"]
            )
            and "group by" in q.get("query_text", "").lower()
        ]
        assert len(timeseries) >= 2, f"Expected >=2 time series queries, got {len(timeseries)}"

    def test_has_large_result_set_anti_pattern(self):
        """Should have queries returning >10k rows (anti-pattern)."""
        large = [q for q in self._get_queries() if q.get("rows_returned_avg", 0) > 10000]
        assert len(large) >= 1, f"Expected >=1 large result set queries, got {len(large)}"


# ============================================================================
# Gaming fixture validation
# ============================================================================


class TestGamingFixtureValidation:
    """Ensure the gaming fixture is fully contract-compliant."""

    def test_collector_output_contract_validates(self):
        """The raw dict must pass CollectorOutputContract v3.0 validation."""
        data = get_gaming_collector_output()
        contract = CollectorOutputContract(**data)

        assert contract.contract_version == "3.0"
        assert contract.job_id == "job-gaming-001"
        assert contract.metadata.source_database.engine.value == "postgresql"
        assert len(contract.database_schema.tables) == 6
        assert len(contract.queries.query_patterns) == 12

    def test_can_wrap_in_analysis_input(self):
        """The fixture must work as collector_output inside AnalysisInput."""
        data = get_gaming_collector_output()
        analysis_input = AnalysisInput(
            job_id="job-gaming-001",
            collector_output=data,
            target_database=TargetDatabase.elasticache,
            target_region="us-west-2",
        )

        assert analysis_input.target_database == TargetDatabase.elasticache
        assert analysis_input.collector_output["job_id"] == "job-gaming-001"

    def test_schema_completeness(self):
        """All tables should have columns, indexes, and primary keys."""
        data = get_gaming_collector_output()
        contract = CollectorOutputContract(**data)

        for table in contract.database_schema.tables:
            assert len(table.columns) >= 1, f"{table.table_id} has no columns"
            assert (
                table.indexes is not None and len(table.indexes) >= 1
            ), f"{table.table_id} has no indexes"
            assert (
                table.primary_key is not None and len(table.primary_key) >= 1
            ), f"{table.table_id} has no primary key"

    def test_rds_metadata_present(self):
        """Fixture should include full RDS instance metadata."""
        data = get_gaming_collector_output()
        contract = CollectorOutputContract(**data)

        rds = contract.metadata.source_database.rds_instance_metadata
        assert rds is not None
        assert rds.instance_class == "db.r7g.2xlarge"
        assert rds.multi_az is True
        assert rds.performance_insights_enabled is True

    def test_views_procedures_triggers_present(self):
        """Fixture should include views, procedures, and triggers."""
        data = get_gaming_collector_output()
        contract = CollectorOutputContract(**data)

        assert contract.database_schema.views is not None
        assert len(contract.database_schema.views) >= 1
        assert contract.database_schema.procedures is not None
        assert len(contract.database_schema.procedures) >= 1
        assert contract.database_schema.triggers is not None
        assert len(contract.database_schema.triggers) >= 1

    def test_cloudwatch_metrics_present(self):
        """Fixture should include RDS CloudWatch metrics."""
        data = get_gaming_collector_output()
        contract = CollectorOutputContract(**data)

        cw = contract.metrics.rds_cloudwatch_metrics
        assert cw is not None
        assert cw.cpu_utilization is not None
        assert cw.read_iops is not None


class TestGamingFixtureRedisPatterns:
    """Verify the gaming fixture contains queries that trigger each Redis pattern."""

    def _get_queries(self):
        return get_gaming_collector_output()["queries"]["query_patterns"]

    def test_has_caching_candidates(self):
        """Should have high-frequency SELECT queries (calls_per_second > 1)."""
        caching = [
            q
            for q in self._get_queries()
            if q.get("query_type") == "SELECT" and q.get("calls_per_second", 0) > 1
        ]
        assert len(caching) >= 2, f"Expected >=2 caching candidates, got {len(caching)}"

    def test_has_session_store_candidates(self):
        """Should have queries referencing session/player_id."""
        session = [
            q
            for q in self._get_queries()
            if "session" in q.get("query_text", "").lower()
            or "player_id" in q.get("query_text", "").lower()
        ]
        assert len(session) >= 2, f"Expected >=2 session queries, got {len(session)}"

    def test_has_leaderboard_candidates(self):
        """Should have ORDER BY + LIMIT queries."""
        leaderboard = [
            q
            for q in self._get_queries()
            if "order by" in q.get("query_text", "").lower()
            and "limit" in q.get("query_text", "").lower()
        ]
        assert len(leaderboard) >= 2, f"Expected >=2 leaderboard queries, got {len(leaderboard)}"

    def test_has_time_series_candidates(self):
        """Should have timestamp/date + GROUP BY queries."""
        timeseries = [
            q
            for q in self._get_queries()
            if any(
                kw in q.get("query_text", "").lower() for kw in ["timestamp", "created_at", "date"]
            )
            and "group by" in q.get("query_text", "").lower()
        ]
        assert len(timeseries) >= 2, f"Expected >=2 time series queries, got {len(timeseries)}"

    def test_has_geospatial_candidates(self):
        """Should have queries with ST_Distance or ST_Within."""
        geospatial = [
            q
            for q in self._get_queries()
            if any(
                kw in q.get("query_text", "").lower()
                for kw in ["st_distance", "st_within", "st_dwithin"]
            )
        ]
        assert len(geospatial) >= 1, f"Expected >=1 geospatial queries, got {len(geospatial)}"

    def test_has_large_result_set_anti_pattern(self):
        """Should have queries returning >10k rows (anti-pattern)."""
        large = [q for q in self._get_queries() if q.get("rows_returned_avg", 0) > 10000]
        assert len(large) >= 1, f"Expected >=1 large result set queries, got {len(large)}"
