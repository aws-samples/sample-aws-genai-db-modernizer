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

    def test_external_is_rejected_with_a_clear_error(self, store, monkeypatch) -> None:
        """ADR-026 Decision 4 (#716): the core cannot take the out-of-band response."""
        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "external")
        with patch(HANDLER) as handler:
            with pytest.raises(ValueError, match="external.*not supported"):
                run_reality_check_core(JOB, DB, assignment_version=1, store=store)
        handler.assert_not_called()

    def test_external_writes_no_awaiting_artifacts(self, store, monkeypatch) -> None:
        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "external")
        with pytest.raises(ValueError):
            run_reality_check_core(JOB, DB, assignment_version=1, store=store)
        prefix = f"{DB}/{JOB}/reality-check"
        assert not store.exists(f"{prefix}/awaiting_llm.json")
        assert not store.exists(f"{prefix}/llm_input.json")

    def test_external_is_rejected_before_the_prerequisite_check(
        self, tmp_path, monkeypatch
    ) -> None:
        """A bad mode is a configuration error whatever the job holds."""
        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "external")
        empty = LocalArtifactStore(str(tmp_path))
        with pytest.raises(ValueError, match="REALITY_CHECK_LLM_MODE"):
            run_reality_check_core(JOB, DB, assignment_version=1, store=empty)

    def test_bedrock_and_none_are_still_accepted(self, store, monkeypatch) -> None:
        assert _llm_mode_passed_to_the_handler(store, monkeypatch, "bedrock") == "bedrock"
        assert _llm_mode_passed_to_the_handler(store, monkeypatch, "none") == "none"

    @pytest.mark.parametrize("value", ["bedrok", "NONE", "", "chat"])
    def test_an_unknown_value_is_rejected_listing_the_valid_ones(
        self, store, monkeypatch, value
    ) -> None:
        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", value)
        with patch(HANDLER) as handler:
            with pytest.raises(ValueError, match="bedrock.*none") as err:
                run_reality_check_core(JOB, DB, assignment_version=1, store=store)
        assert repr(value) in str(err.value)
        handler.assert_not_called()


class TestRealityCheckCoreLlmModeArgument:
    """A caller can pass the mode instead of the variable (the public ``assess()``, #1182)."""

    @pytest.mark.parametrize(("arg", "env"), [("none", "bedrock"), ("bedrock", "none")])
    def test_the_argument_wins_over_the_variable(self, store, monkeypatch, arg, env) -> None:
        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", env)
        with patch(HANDLER) as handler:
            run_reality_check_core(JOB, DB, assignment_version=1, store=store, llm_mode=arg)
        assert handler.call_args.kwargs["llm_mode"] == arg

    @pytest.mark.parametrize("value", ["external", "bedrok", ""])
    def test_the_argument_is_validated_the_same_way(self, store, monkeypatch, value) -> None:
        monkeypatch.delenv("REALITY_CHECK_LLM_MODE", raising=False)
        with patch(HANDLER) as handler:
            with pytest.raises(ValueError, match="bedrock|none"):
                run_reality_check_core(JOB, DB, assignment_version=1, store=store, llm_mode=value)
        handler.assert_not_called()
