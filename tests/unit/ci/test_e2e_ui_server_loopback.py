"""The e2e suite's local UI server is reachable from this machine only (#1593)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_the_e2e_ui_server_listens_on_loopback_only() -> None:
    """The e2e suite's `serve` listens on 127.0.0.1, as scripts/start_local_ui.py's
    does: a bare `-l <port>` listens on every interface."""
    conftest = (REPO / "tests" / "e2e" / "conftest.py").read_text()
    assert '"-l", f"tcp://127.0.0.1:{UI_PORT}"' in conftest
    assert '"-l", str(UI_PORT)' not in conftest
