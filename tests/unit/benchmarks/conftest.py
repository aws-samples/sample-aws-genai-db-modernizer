import pytest

from tests.graph import conftest as _graph_conftest
from tests.graph.conftest import sample_analysis_output, sample_collector_output  # noqa: F401


@pytest.fixture
def sample_triage_output():
    """Graph's minimal triage fixture (`tests/graph/conftest.py`) plus `selected_agents`.

    The real assignment handler (`run_assignment_resolver`) only loads
    analysis for engines listed in `triage["selected_agents"]`; the shared
    graph fixture omits it since graph populators don't read that field.
    Benchmark cases exercise the real handler, so it must be present.
    """
    base = _graph_conftest.sample_triage_output.__wrapped__()
    return {**base, "selected_agents": [{"agent_type": "dynamodb"}]}
