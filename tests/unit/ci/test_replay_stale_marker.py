"""``replay_stale`` takes a check off the saved-fixture replay only (#1510).

A check of what synthesis writes holds on every fresh run, deterministic or model, but
not on the replay's saved ``report.json``, which an older synthesis wrote. Only
``ci/replay.sh`` may deselect it: ``ci/e2e-llm.sh`` and ``ci/e2e.sh`` keep running it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _selections(script: str) -> list[str]:
    text = (ROOT / "ci" / script).read_text(encoding="utf-8")
    return re.findall(r'pytest tests/e2e[^\n]*?-m "([^"]+)"', text)


def test_only_the_replay_deselects_replay_stale() -> None:
    replay = _selections("replay.sh")
    assert replay and all("not replay_stale" in m for m in replay), replay
    for script in ("e2e-llm.sh", "e2e.sh"):
        assert not any("replay_stale" in m for m in _selections(script)), script


def test_the_marker_is_registered() -> None:
    assert '"replay_stale: ' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
