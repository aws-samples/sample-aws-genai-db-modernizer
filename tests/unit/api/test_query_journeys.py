"""Unit tests for query journeys route."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import query_journeys

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_services():
    """Inject mock services into the query_journeys router."""
    sfn = MagicMock()
    store = MagicMock()

    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": "test_db"},
    }

    query_journeys.sfn_service = sfn
    query_journeys.artifact_store = store

    yield {"sfn": sfn, "store": store}

    query_journeys.sfn_service = None
    query_journeys.artifact_store = None


class TestListQueryJourneys:
    """Tests for GET /api/v1/assessments/{job_id}/query-journeys (list)."""

    def test_returns_paginated_journeys(self, mock_services):
        """Returns first page of journeys with pagination metadata."""
        mock_services["store"].list_prefix.return_value = [
            "test_db/job-1/query-journeys/q_001.json",
            "test_db/job-1/query-journeys/q_002.json",
            "test_db/job-1/query-journeys/q_003.json",
        ]
        mock_services["store"].read_json.side_effect = [
            {
                "query_id": "q_001",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
            {
                "query_id": "q_002",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
            {
                "query_id": "q_003",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
        ]

        response = client.get("/api/v1/assessments/job-1/query-journeys")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-1"
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["page_size"] == 50
        assert data["total_pages"] == 1
        assert len(data["items"]) == 3
        assert data["items"][0]["query_id"] == "q_001"

    def test_respects_page_and_page_size(self, mock_services):
        """Returns the requested page with correct slicing."""
        mock_services["store"].list_prefix.return_value = [
            f"test_db/job-1/query-journeys/q_00{i}.json" for i in range(1, 6)
        ]
        mock_services["store"].read_json.side_effect = [
            {
                "query_id": "q_003",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
            {
                "query_id": "q_004",
                "source": {},
                "assignment": None,
                "design": None,
                "load_test": None,
                "sdk_code": None,
            },
        ]

        response = client.get("/api/v1/assessments/job-1/query-journeys?page=2&page_size=2")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert data["total_pages"] == 3
        assert len(data["items"]) == 2

    def test_returns_404_when_no_journeys(self, mock_services):
        """Returns 404 when query-journeys folder is empty or missing."""
        mock_services["store"].list_prefix.return_value = []

        response = client.get("/api/v1/assessments/job-1/query-journeys")

        assert response.status_code == 404

    def test_returns_empty_items_for_page_beyond_total(self, mock_services):
        """Returns 200 with empty items when page exceeds total_pages."""
        mock_services["store"].list_prefix.return_value = [
            "test_db/job-1/query-journeys/q_001.json",
        ]

        response = client.get("/api/v1/assessments/job-1/query-journeys?page=99")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["page"] == 99
        assert data["items"] == []

    def test_clamps_page_size_to_max(self, mock_services):
        """page_size > 200 is clamped to 200."""
        mock_services["store"].list_prefix.return_value = [
            "test_db/job-1/query-journeys/q_001.json",
        ]
        mock_services["store"].read_json.return_value = {
            "query_id": "q_001",
            "source": {},
            "assignment": None,
            "design": None,
            "load_test": None,
            "sdk_code": None,
        }

        response = client.get("/api/v1/assessments/job-1/query-journeys?page_size=500")

        assert response.status_code == 200
        data = response.json()
        assert data["page_size"] == 200

    def test_returns_503_when_store_not_configured(self, mock_services):
        """Returns 503 when artifact_store is None."""
        query_journeys.artifact_store = None

        response = client.get("/api/v1/assessments/job-1/query-journeys")

        assert response.status_code == 503


class TestGetQueryJourney:
    """Tests for GET /api/v1/assessments/{job_id}/query-journeys/{query_id}."""

    def test_returns_200_with_journey_data(self, mock_services):
        """Returns 200 with journey data when found."""
        mock_services["store"].read_json.return_value = {
            "query_id": "q-001",
            "original_sql": "SELECT * FROM orders",
            "modernized_sql": "SELECT * FROM orders",
            "steps": [],
        }

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-001")

        assert response.status_code == 200
        data = response.json()
        assert data["query_id"] == "q-001"
        assert "original_sql" in data
        mock_services["store"].read_json.assert_called_once_with(
            "test_db/job-1/query-journeys/q-001.json"
        )

    def test_returns_404_when_journey_file_not_found(self, mock_services):
        """Returns 404 when the journey file does not exist in the artifact store."""
        mock_services["store"].read_json.side_effect = FileNotFoundError("not found")

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-999")

        assert response.status_code == 404
        assert "q-999" in response.json()["detail"]

    def test_returns_404_when_job_not_found(self, mock_services):
        """Returns 404 when the Step Functions execution does not exist."""
        mock_services["sfn"].describe_execution.return_value = None

        response = client.get("/api/v1/assessments/nonexistent-job/query-journeys/q-001")

        assert response.status_code == 404
        assert response.json()["detail"] == "Assessment not found"

    def test_returns_503_when_sfn_service_not_configured(self, mock_services):
        """Returns 503 when sfn_service is None (services not configured)."""
        query_journeys.sfn_service = None

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-001")

        assert response.status_code == 503
        assert response.json()["detail"] == "Services not configured"

    def test_returns_503_when_artifact_store_not_configured(self, mock_services):
        """Returns 503 when artifact_store is None (services not configured)."""
        query_journeys.artifact_store = None

        response = client.get("/api/v1/assessments/job-1/query-journeys/q-001")

        assert response.status_code == 503
        assert response.json()["detail"] == "Services not configured"


@contextmanager
def _graph_with_queries(tmp_path, n: int):
    """A real graph with ``n`` queries (gq0, gq1, ...) and a graph_lease stand-in for it."""
    from src.graph.populators import populate_from_collector
    from src.graph.schema import initialize_schema
    from src.graph.store import GraphStore

    path = str(tmp_path / "journeys.lbug")
    writer = GraphStore(path)
    initialize_schema(writer)
    populate_from_collector(
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": f"gq{i}",
                        "query_text": "SELECT 1",
                        "query_type": "SELECT",
                        "tables_accessed": ["t"],
                        "calls_per_second": 1.0,
                    }
                    for i in range(n)
                ]
            }
        },
        writer,
    )
    writer.close()
    reader = GraphStore(path, read_only=True)

    @contextmanager
    def _lease(job_id):
        yield reader, "test_db"

    try:
        yield _lease
    finally:
        reader.close()


class TestGraphBackedJourneys:
    """When the context graph is available it is the source of journeys; the
    per-query artifacts are the fallback for legacy / json-mode jobs."""

    def test_list_served_from_graph_when_available(self, mock_services, tmp_path):
        from unittest.mock import patch

        from src.api.routes import graph as graph_route

        with _graph_with_queries(tmp_path, 3) as lease:
            with patch.object(graph_route, "graph_lease", lease):
                resp = client.get("/api/v1/assessments/job-1/query-journeys?page=1&page_size=2")
                last = client.get("/api/v1/assessments/job-1/query-journeys?page=2&page_size=2")
                beyond = client.get("/api/v1/assessments/job-1/query-journeys?page=9&page_size=2")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert body["page_size"] == 2
        assert body["total_pages"] == 2
        assert [i["query_id"] for i in body["items"]] == ["gq0", "gq1"]
        assert [i["query_id"] for i in last.json()["items"]] == ["gq2"]
        assert beyond.status_code == 200
        assert beyond.json()["items"] == []
        # The artifact path was not consulted when the graph served the list.
        mock_services["store"].list_prefix.assert_not_called()

    def test_list_reads_only_the_requested_page_from_the_graph(self, mock_services, tmp_path):
        """#998: a page request never materialises every journey of the job."""
        from unittest.mock import patch

        from src.api.routes import graph as graph_route
        from src.graph import queries as graph_queries

        def _everything(*args, **kwargs):
            raise AssertionError("the list endpoint read every journey")

        pages: list[tuple[int, int]] = []
        real_page = graph_queries.query_journeys_page

        def _page(store, skip, limit):
            pages.append((skip, limit))
            return real_page(store, skip, limit)

        with _graph_with_queries(tmp_path, 5) as lease:
            with (
                patch.object(graph_route, "graph_lease", lease),
                patch.object(graph_queries, "query_journeys", _everything),
                patch.object(graph_queries, "query_journeys_page", _page),
            ):
                resp = client.get("/api/v1/assessments/job-1/query-journeys?page=2&page_size=2")

        assert resp.status_code == 200
        assert [i["query_id"] for i in resp.json()["items"]] == ["gq2", "gq3"]
        assert pages == [(2, 2)]

    def test_empty_graph_is_404(self, mock_services, tmp_path):
        from unittest.mock import patch

        from src.api.routes import graph as graph_route

        with _graph_with_queries(tmp_path, 0) as lease:
            with patch.object(graph_route, "graph_lease", lease):
                resp = client.get("/api/v1/assessments/job-1/query-journeys")

        assert resp.status_code == 404
        mock_services["store"].list_prefix.assert_not_called()

    def test_list_falls_back_to_artifacts_when_graph_absent(self, mock_services):
        from unittest.mock import patch

        mock_services["store"].list_prefix.return_value = [
            "test_db/job-1/query-journeys/q_001.json",
        ]
        mock_services["store"].read_json.side_effect = [
            {"query_id": "q_001", "source": {}, "assignment": None, "design": None},
        ]
        with patch.object(query_journeys, "_journeys_page_from_graph", return_value=None):
            resp = client.get("/api/v1/assessments/job-1/query-journeys")

        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        mock_services["store"].list_prefix.assert_called_once()

    def test_detail_served_from_graph_when_available(self, mock_services):
        from contextlib import contextmanager
        from unittest.mock import patch

        from src.api.routes import graph as graph_route
        from src.graph import queries as graph_queries

        journey = {"query_id": "q_001", "source": {"query_type": "SELECT"}, "assignment": None}

        @contextmanager
        def _lease(job_id):
            yield MagicMock(), "test_db"

        with (
            patch.object(graph_route, "graph_lease", _lease),
            patch.object(graph_queries, "query_journey", return_value=journey),
        ):
            resp = client.get("/api/v1/assessments/job-1/query-journeys/q_001")

        assert resp.status_code == 200
        assert resp.json()["query_id"] == "q_001"
        # Artifact read was not used when the graph served the query.
        mock_services["store"].read_json.assert_not_called()


@pytest.mark.parametrize("path", ["/query-journeys", "/query-journeys/q_001"])
def test_graph_timeout_returns_504_not_artifact_fallback(mock_services, path):
    """A graph read that hits the engine timeout is reported, not hidden by the fallback."""
    from contextlib import contextmanager
    from unittest.mock import patch

    from src.api.routes import graph as graph_route
    from src.graph import queries as graph_queries

    @contextmanager
    def _lease(job_id):
        yield MagicMock(), "test_db"

    def _timeout(*args):
        raise RuntimeError("Interrupted.")

    with (
        patch.object(graph_route, "graph_lease", _lease),
        patch.object(graph_queries, "query_count", _timeout),
        patch.object(graph_queries, "query_journeys_page", _timeout),
        patch.object(graph_queries, "query_journey", _timeout),
    ):
        resp = client.get(f"/api/v1/assessments/job-1{path}")

    assert resp.status_code == 504
    assert resp.json()["detail"] == graph_route.TIMEOUT_DETAIL
    mock_services["store"].read_json.assert_not_called()


@pytest.mark.parametrize("path", ["/query-journeys", "/query-journeys/q_001"])
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(
            "Buffer manager exception: Unable to allocate memory! The buffer pool is full"
        ),
        "busy",
    ],
)
def test_graph_memory_or_busy_error_is_a_retryable_503(mock_services, path, error):
    """#998: an out-of-memory or saturated graph read answers 503 with Retry-After,
    and is not hidden by the artifact fallback."""
    from unittest.mock import patch

    from src.api.routes import graph as graph_route
    from src.graph import queries as graph_queries
    from src.graph.store import GraphBusyError

    exc = GraphBusyError("no graph read slot") if error == "busy" else error

    @contextmanager
    def _lease(job_id):
        yield MagicMock(), "test_db"

    def _fail(*args):
        raise exc

    with (
        patch.object(graph_route, "graph_lease", _lease),
        patch.object(graph_queries, "query_count", _fail),
        patch.object(graph_queries, "query_journey", _fail),
    ):
        resp = client.get(f"/api/v1/assessments/job-1{path}")

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "1"
    expected = graph_route.BUSY_DETAIL if error == "busy" else graph_route.MEMORY_DETAIL
    assert resp.json()["detail"] == expected
    mock_services["store"].read_json.assert_not_called()
    mock_services["store"].list_prefix.assert_not_called()


@pytest.mark.parametrize("path", ["/query-journeys", "/query-journeys/q_001"])
@pytest.mark.parametrize("bad_db", ["../outside", "/tmp/outside", "a/b", "..", ""])
def test_refused_graph_path_returns_404_not_artifact_fallback(
    mock_services, tmp_path, monkeypatch, path, bad_db
):
    """#851: when the job's graph path is refused (e.g. invalid stored database_name),
    query-journeys answers 404 without falling through to artifact_store."""
    from src.api.routes import graph as graph_route
    from src.graph import GraphStoreCache

    cache = GraphStoreCache(base_dir=str(tmp_path / "root"))
    monkeypatch.setattr(graph_route, "graph_cache", cache)
    monkeypatch.setattr(graph_route, "artifact_store", mock_services["store"])
    monkeypatch.setattr(graph_route, "graph_persistence", MagicMock())
    monkeypatch.setattr(graph_route, "sfn_service", mock_services["sfn"])
    mock_services["sfn"].describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": bad_db},
    }

    resp = client.get(f"/api/v1/assessments/job-1{path}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Assessment not found"
    mock_services["store"].list_prefix.assert_not_called()
    mock_services["store"].read_json.assert_not_called()


@pytest.mark.parametrize("path", ["/query-journeys", "/query-journeys/q_001"])
def test_refused_job_folder_in_graph_returns_404_without_listing_artifacts(
    mock_services, tmp_path, monkeypatch, path
):
    """#851: when GraphStoreCache.local_path refuses a symlinked/escaped job folder,
    query-journeys answers 404 without falling through to artifact_store."""
    from src.api.routes import graph as graph_route
    from src.graph import GraphStoreCache

    cache = GraphStoreCache(base_dir=str(tmp_path / "root"))
    monkeypatch.setattr(
        cache,
        "local_path",
        MagicMock(side_effect=ValueError("graph path escapes the artifact root")),
    )
    monkeypatch.setattr(graph_route, "graph_cache", cache)
    monkeypatch.setattr(graph_route, "artifact_store", mock_services["store"])
    monkeypatch.setattr(graph_route, "graph_persistence", MagicMock())
    monkeypatch.setattr(graph_route, "sfn_service", mock_services["sfn"])

    resp = client.get(f"/api/v1/assessments/job-1{path}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Assessment not found"
    mock_services["store"].list_prefix.assert_not_called()
    mock_services["store"].read_json.assert_not_called()


@pytest.mark.parametrize("path", ["/query-journeys", "/query-journeys/q_001"])
@pytest.mark.parametrize("bad_db", ["../outside", "/tmp/outside", "a/b", "..", ""])
def test_artifact_fallback_refuses_bad_database_name_with_404(
    mock_services, monkeypatch, path, bad_db
):
    """#851: when the graph layer is absent, the artifact fallback also rejects an
    invalid stored database_name with 404 before touching artifact_store."""
    from src.api.routes import graph as graph_route

    monkeypatch.setattr(graph_route, "graph_cache", None)
    mock_services["sfn"].describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": bad_db},
    }

    resp = client.get(f"/api/v1/assessments/job-1{path}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Assessment not found"
    mock_services["store"].list_prefix.assert_not_called()
    mock_services["store"].read_json.assert_not_called()


def test_artifact_fallback_list_prefix_value_error_returns_404(mock_services, monkeypatch):
    """#851: if artifact_store.list_prefix refuses the prefix with ValueError,
    list_query_journeys answers 404 instead of 500."""
    from src.api.routes import graph as graph_route

    monkeypatch.setattr(graph_route, "graph_cache", None)
    mock_services["store"].list_prefix.side_effect = ValueError(
        "path escapes the artifact root: 'test_db/job-1/query-journeys/'"
    )

    resp = client.get("/api/v1/assessments/job-1/query-journeys")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Assessment not found"
