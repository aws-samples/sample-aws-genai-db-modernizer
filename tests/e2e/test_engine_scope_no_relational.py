"""A scope with no relational engine end to end (#1369).

On wordpress (a MySQL source), triage runs with a scope of DynamoDB alone and the rest of
the pipeline runs deterministically. Wave 1 says the engine scope has no relational
engine, so the source is not converted to Aurora in this plan, and its gate names the
DynamoDB wave; no deliverable calls the MySQL source heterogeneous or asks to pick an
Aurora engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.pipeline import unzip_sample
from tests.e2e.test_engine_scope import JOB, _script

TITLE = "No move to Aurora: the engine scope has no relational engine"


@pytest.fixture(scope="module", params=["wordpress"])
def dynamodb_only(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    from src.agents.referee.triage_handler import run_triage
    from src.storage.local_store import LocalArtifactStore

    db = str(request.param)
    base = Path(tmp_path_factory.mktemp(f"no-relational-{db}"))
    root = base / "artifacts"
    job = root / db / JOB
    common = ["--job-id", JOB, "--db", db, "--artifact-root", str(root)]
    collection = unzip_sample(db, base / "input")
    _script("scripts/run_collect.py", "--file", str(collection), *common)
    run_triage(JOB, db, LocalArtifactStore(base_dir=str(root)), engine_scope=["dynamodb"])
    _script("scripts/run_analysis.py", *common, "--engine", "dynamodb", "--llm-mode", "none")
    _script("scripts/run_assignment.py", *common)
    _script("scripts/run_reality_check.py", *common, "--llm-mode", "none")
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    report_step = _script("scripts/run_report.py", *common)
    return {
        "report": json.loads((root / report_step["report"]).read_text()),
        "engineering": (job / "deliverables" / "engineering-report.md").read_text(),
    }


@pytest.mark.deterministic
def test_wave_one_says_the_scope_has_no_relational_engine(dynamodb_only: dict) -> None:
    wave1 = dynamodb_only["report"]["migration_waves"][0]
    assert wave1["engines"] == []
    assert wave1["title"] == TITLE
    assert "not converted to Aurora in this plan" in wave1["rationale"]
    assert "DynamoDB" in wave1["gate"] and "instead" in wave1["gate"]


@pytest.mark.deterministic
def test_no_deliverable_calls_the_source_heterogeneous_or_asks_for_a_pick(
    dynamodb_only: dict,
) -> None:
    md = dynamodb_only["engineering"]
    assert f"Wave 1: {TITLE}" in md
    assert "Move off MySQL (heterogeneous)" not in md
    assert "pick and validate" not in md
    wave1 = dynamodb_only["report"]["migration_waves"][0]
    assert wave1["gate"] in md


@pytest.mark.deterministic
def test_the_gate_is_true_for_the_queries_no_engine_in_the_scope_serves(
    dynamodb_only: dict,
) -> None:
    """Only some queries move to DynamoDB: the gate says the rest stay on the source, and
    the facts (not a hard-coded number) show that some do and that the report counts them."""
    facts = dynamodb_only["report"]["facts"]
    unservable = int(facts["unservable"]["no_engine_in_scope_query_count"])
    total = int(facts["query_total_count"])
    assert 0 < unservable < total
    gate = dynamodb_only["report"]["migration_waves"][0]["gate"]
    assert "A query no engine in the scope serves" in gate
    assert "stays on the source database" in gate
    assert "the plan moves the queries to" not in gate
    assert f"{unservable} have no engine in the customer's engine scope" in (
        dynamodb_only["engineering"]
    )
