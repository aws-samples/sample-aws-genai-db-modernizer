"""The results page's wave plan prints the same wave headline as the reports (#957).

``src/ui/src/utils/migrationWaves.js`` formats each wave's headline (the count
phrase and the table phrase) for the UI results page; the decision and
engineering reports print it from ``src/report/roadmap.py``'s
``_wave_headline_stats``. Both sides read one fixture,
``src/ui/src/utils/__tests__/fixtures/waveHeadlines.json``: this test checks the
Python renderer still prints the fixture's expected headlines, and
``src/ui/src/utils/__tests__/migrationWaves.test.js`` checks the UI prints the
same ones. A wording change on either side fails one of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.report.roadmap import _wave_headline_stats

FIXTURE = (
    Path(__file__).resolve().parents[3] / "src/ui/src/utils/__tests__/fixtures/waveHeadlines.json"
)


def _fixture() -> dict:
    data: dict = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("wave_no", sorted(_fixture()["expected_headlines"]))
def test_python_wave_headline_matches_the_shared_fixture(wave_no: str) -> None:
    data = _fixture()
    waves = {str(w["wave"]): w for w in data["synthesis"]["migration_waves"]}
    rows = {str(r["wave"]): r for r in data["synthesis"]["facts"]["waves"]}
    count_phrase, table_phrase = _wave_headline_stats(waves[wave_no], rows.get(wave_no))
    assert count_phrase + table_phrase == data["expected_headlines"][wave_no]


def test_the_fixture_covers_every_headline_shape() -> None:
    """Cutover (with and without views), calls basis, to move/kept in sync, a plain table
    count, and a wave the facts block does not list."""
    data = _fixture()
    headlines = data["expected_headlines"].values()
    assert any("at cutover" in h and "view" in h.split("and views")[-1] for h in headlines)
    assert any("at cutover" in h and h.endswith("and views") for h in headlines)
    assert any("of calls" in h for h in headlines)
    assert any("to move, " in h and "kept in sync from" in h for h in headlines)
    assert any(h.endswith("source table") for h in headlines)
    listed = {str(r["wave"]) for r in data["synthesis"]["facts"]["waves"]}
    assert set(data["expected_headlines"]) - listed
