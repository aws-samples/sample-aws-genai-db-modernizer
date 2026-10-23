"""A collector output (contract 3.x) is accepted as input, but its derived facts are
never the caller's: the parser reads every statement again (#1182)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.assessment import assess
from src.tools.database.offline_parser import parse_offline_collection, rederive_query_facts
from tests.unit.assessment.conftest import SAMPLES, assessed, sample_collection


def _collector_output(name: str) -> dict[str, Any]:
    return build_offline_collector_output(
        parse_offline_collection(sample_collection(name)),
        job_id="caller",
        database_name=name,
        s3_key="in-memory",
    )


@pytest.mark.parametrize("name", SAMPLES)
def test_rederived_facts_match_the_raw_path(name: str) -> None:
    out = _collector_output(name)
    engine = out["metadata"]["source_database"]["engine"]
    assert rederive_query_facts(out, engine) == out
    assert out["queries"]["query_patterns"]


def _tampered(name: str) -> dict[str, Any]:
    out = _collector_output(name)
    for p in out["queries"]["query_patterns"]:
        p["tables_accessed"] = ["caller.made_up"]
        p["query_type"] = "SELECT"
        p["join_count"] = 9
        p["has_joins"] = True
        p["query_tags"] = ["caller-tag"]
        p["filter_columns"] = ["x"]
    return out


def test_rederive_ignores_caller_facts() -> None:
    out = _tampered("wordpress")
    again = rederive_query_facts(out, "mysql")
    raw_path = _collector_output("wordpress")
    assert again["queries"] == raw_path["queries"]
    assert again["database_schema"] == raw_path["database_schema"]
    # The input is not changed.
    assert out["queries"]["query_patterns"][0]["tables_accessed"] == ["caller.made_up"]


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_a_tampered_collector_output_gives_the_raw_result(run_through: str) -> None:
    raw_result = assessed("trailgear", run_through)
    result = assess(
        {
            "database_name": "trailgear",
            "collection": _tampered("trailgear"),
            "options": {"run_through": run_through},
        }
    )
    assert result.model_dump() == raw_result.model_dump()


def test_rederive_maps_onto_the_collector_outputs_own_table_ids() -> None:
    """A collector output keyed by ``schema.table`` (not ``database.table``) keeps its ids."""
    out = _collector_output("trailgear")
    renamed = copy.deepcopy(out)
    ids = {}
    for t in renamed["database_schema"]["tables"]:
        new = f"{t.get('schema_name') or 'dbo'}.{t['table_name']}"
        ids[t["table_id"]] = new
        t["table_id"] = new
    again = rederive_query_facts(renamed, "sqlserver")
    for before, after in zip(
        out["queries"]["query_patterns"], again["queries"]["query_patterns"], strict=True
    ):
        assert after["tables_accessed"] == [ids.get(t, t) for t in before["tables_accessed"]]
