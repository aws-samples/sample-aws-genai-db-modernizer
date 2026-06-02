"""Unit tests for agent entrypoint dispatcher."""

from unittest.mock import patch

import pytest


class TestEntrypointValidation:
    """Test that the entrypoint validates required env vars."""

    def test_missing_agent_type_exits(self):
        with patch.dict("os.environ", {"AGENT_TYPE": "", "JOB_ID": "j1", "DATABASE_NAME": "db1"}):
            from importlib import reload

            import src.agents.entrypoint as ep

            reload(ep)
            with pytest.raises(SystemExit) as exc:
                ep.main()
            assert exc.value.code == 1

    def test_missing_job_id_exits(self):
        with patch.dict(
            "os.environ", {"AGENT_TYPE": "collector", "JOB_ID": "", "DATABASE_NAME": "db1"}
        ):
            from importlib import reload

            import src.agents.entrypoint as ep

            reload(ep)
            with pytest.raises(SystemExit) as exc:
                ep.main()
            assert exc.value.code == 1

    def test_unknown_agent_type_exits(self):
        with patch.dict(
            "os.environ",
            {"AGENT_TYPE": "unknown-agent", "JOB_ID": "j1", "DATABASE_NAME": "db1"},
        ):
            from importlib import reload

            import src.agents.entrypoint as ep

            reload(ep)
            with pytest.raises(SystemExit) as exc:
                ep.main()
            assert exc.value.code == 1
