"""``assess()``'s ``database_name`` only names the run: the result is the same
whatever name the caller picks (#1572), and an ambiguous name is refused."""

from __future__ import annotations

import pytest

from src.assessment import assess
from tests.unit.assessment.conftest import SAMPLES, assessed, sample_collection


@pytest.mark.parametrize("name", SAMPLES)
def test_another_name_gives_the_same_analysis(name: str) -> None:
    result = assess(
        {
            "database_name": "shop",
            "collection": sample_collection(name),
            "options": {"run_through": "analysis"},
        }
    )
    assert result.model_dump() == assessed(name, "analysis").model_dump()


def test_another_name_gives_the_same_placement() -> None:
    result = assess(
        {
            "database_name": "shop",
            "collection": sample_collection("wordpress"),
            "options": {"run_through": "reality_check"},
        }
    )
    expected = assessed("wordpress", "reality_check")
    assert result.model_dump() == expected.model_dump()
    assert {s.engine for s in result.statements} != {"aurora_mysql"}


def test_a_run_named_after_a_sibling_database_is_accepted() -> None:
    """Server-wide statements name a sibling database: not ambiguous, the stated
    name wins and the result is the matching name's (main accepted it too)."""
    raw = sample_collection("trailgear")
    for i, row in enumerate(raw["queries"]):
        row["database_name"] = ("trailgear", "reporting")[i % 2]

    def run(name: str):
        return assess(
            {"database_name": name, "collection": raw, "options": {"run_through": "analysis"}}
        )

    assert run("reporting").model_dump() == run("trailgear").model_dump()


def test_an_ambiguous_name_is_refused() -> None:
    """No stated name, and statements of several databases, none of them the run's."""
    from src.assessment import DatabaseNameMismatchError

    raw = sample_collection("trailgear")
    raw["metadata"].pop("database_name")
    for i, row in enumerate(raw["queries"]):
        row["database_name"] = ("sales", "hr")[i % 2]
    with pytest.raises(DatabaseNameMismatchError, match="'sales'"):
        assess({"database_name": "shop", "collection": raw, "options": {"run_through": "analysis"}})
