"""Session fixtures: one deterministic pipeline run per sample input."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.pipeline import PipelineResult, run_pipeline

SAMPLES = ["wordpress", "discourse"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def e2e_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return Path(tmp_path_factory.mktemp("e2e"))


@pytest.fixture(scope="session", params=SAMPLES)
def run(request: pytest.FixtureRequest, e2e_root: Path) -> PipelineResult:
    sample = request.param
    return run_pipeline(sample, e2e_root / "artifacts", job_id=f"e2e-{sample[:4]}")
