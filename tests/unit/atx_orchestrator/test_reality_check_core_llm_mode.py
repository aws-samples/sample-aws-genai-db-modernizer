"""The transform core's Reality Check LLM mode (ADR-026 Decision 4, #708).

ADR-026 Decision 4 keeps the external LLM mode out of the transform core: the core reads
``REALITY_CHECK_LLM_MODE`` and defaults it to ``bedrock``. The model entry point is patched,
so nothing here calls Bedrock.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.atx_orchestrator.core import run_reality_check_core
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-1"
HANDLER = "src.agents.referee.reality_check_handler.run_reality_check_handler"


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    s = LocalArtifactStore(str(tmp_path))
    prefix = f"{DB}/{JOB}"
    s.write_json(f"{prefix}/collector/output.json", {})
    s.write_json(f"{prefix}/referee-triage/triage.json", {})
    s.write_json(f"{prefix}/assignment/v1/assignment.json", {"version": 1})
    return s


def _llm_mode_passed_to_the_handler(store, monkeypatch, env: str | None) -> str:
    if env is None:
        monkeypatch.delenv("REALITY_CHECK_LLM_MODE", raising=False)
    else:
        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", env)
    with patch(HANDLER) as handler:
        run_reality_check_core(JOB, DB, assignment_version=1, store=store)
    return str(handler.call_args.kwargs["llm_mode"])


class TestRealityCheckCoreLlmMode:
    def test_defaults_to_bedrock_when_the_variable_is_unset(self, store, monkeypatch) -> None:
        assert _llm_mode_passed_to_the_handler(store, monkeypatch, None) == "bedrock"

    def test_the_variable_selects_the_deterministic_mode(self, store, monkeypatch) -> None:
        assert _llm_mode_passed_to_the_handler(store, monkeypatch, "none") == "none"
