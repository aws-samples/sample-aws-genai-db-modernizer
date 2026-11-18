# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2326: the assignments response carries the gate's figures (``gate_facts``), computed by
``src.shared.gate_facts.gate_block_facts`` on read, so the UI gate states the same queries,
source tables, engines and cache candidates as the chat gate and never counts them itself.
A job saved before this field existed gets them too: they come from its artifacts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.api.routes import assignments
from src.shared.gate_facts import gate_block_facts
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import run_pipeline
from tests.shared_runs import cached_run
from tests.unit.api.test_assignment_cache_reads import DB, JOB, _store, _url, client


def test_the_figures_come_from_the_reality_check_and_the_collector(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=False)
    store = assignments.artifact_store
    store.write_json(
        f"{DB}/{JOB}/reality-check/output.json",
        {
            "before_distribution": {"dynamodb": 2, "documentdb": 1},
            "after_distribution": {"dynamodb": 3, "documentdb": 0},
            "eliminated_engines": ["documentdb"],
            "consolidations": [{"from_engine": "documentdb", "to_engine": "dynamodb"}],
        },
    )
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    collector["database_schema"] = {"tables": [{"name": "a"}, {"name": "b"}]}
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)

    facts = client.get(_url()).json()["gate_facts"]
    assert facts["total_after"] == 3
    assert facts["source_table_count"] == 2
    assert facts["surviving"] == ["dynamodb"]
    assert [(r["engine"], r["before"], r["dropped"]) for r in facts["rows"]] == [
        ("dynamodb", 2, False),
        ("documentdb", 1, True),
    ]
    assert facts["cache_candidates"] is None


def test_an_old_job_still_gets_its_figures(tmp_path, monkeypatch) -> None:
    # Saved before eliminated_engines, versioned reality-check records and database_schema:
    # a flat output.json with only the distributions and a collector with a schema block.
    _store(tmp_path, monkeypatch, cached=False)
    store = assignments.artifact_store
    store.write_json(
        f"{DB}/{JOB}/reality-check/output.json",
        {"before_distribution": {"dynamodb": 3}, "after_distribution": {"dynamodb": 3}},
    )
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    del collector["database_schema"]
    collector["schema"] = {"tables": [{"name": "a"}]}
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)

    facts = client.get(_url()).json()["gate_facts"]
    assert facts["total_after"] == 3
    assert facts["source_table_count"] == 1
    assert facts["surviving"] == ["dynamodb"]
    assert not any(r["dropped"] for r in facts["rows"])


def test_without_a_reality_check_the_assignment_is_the_distribution(tmp_path, monkeypatch) -> None:
    # The page renders the gate before the reality check ran from the assignment's own
    # in-scope owners (local_s3's stand-in reality check); the facts count the same.
    _store(tmp_path, monkeypatch, cached=False)
    facts = client.get(_url()).json()["gate_facts"]
    assert facts["total_after"] == 3
    assert [(r["engine"], r["before"], r["after"]) for r in facts["rows"]] == [("dynamodb", 3, 3)]
    assert facts["source_table_count"] == 0


def test_the_cache_candidates_are_the_overlays_stored_candidates(tmp_path, monkeypatch) -> None:
    _store(tmp_path, monkeypatch, cached=True)
    store = assignments.artifact_store
    key = f"{DB}/{JOB}/assignment/v1/assignment.json"
    raw = store.read_json(key)
    # Synthesis stores the candidates next to the designed count; the gate names candidates.
    raw["cache_overlay"] = {
        "engine": "elasticache",
        "query_count": 1,
        "calls_per_second": 30.0,
        "call_share_percent": 30.0,
        "candidate_query_count": 2,
        "candidate_call_share_percent": 40.0,
        "min_calls_per_second": 1.0,
        "max_rows_avg": 10.0,
    }
    store.write_json(key, raw)
    cache = client.get(_url()).json()["gate_facts"]["cache_candidates"]
    assert cache["read_count"] == 2
    assert cache["call_share_percent"] == 40.0
    assert [r["query_id"] for r in cache["reads"]] == ["hot", "warm"]


@pytest.fixture(scope="module")
def run_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    def build(directory: Path) -> None:
        run_pipeline("wordpress", directory / "artifacts", "ps-job")

    return cached_run(tmp_path_factory, "print-summary-wordpress", build) / "artifacts"


def test_the_ui_gate_and_the_chat_gate_state_the_same_figures(
    run_root, tmp_path, monkeypatch
) -> None:
    from scripts import run_assessment

    # A copy: the GET route may write a re-routed version, and the run is shared.
    shutil.copytree(run_root, tmp_path / "artifacts")
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    monkeypatch.setattr(assignments, "artifact_store", store)
    chat = gate_block_facts(run_assessment._reality_check_artifact(store, "ps-job", "wordpress"))
    ui = client.get("/api/v1/assessments/ps-job/assignments?database_name=wordpress").json()
    facts = ui["gate_facts"]
    assert facts["source_table_count"] > 0
    assert {k: v for k, v in facts.items() if k != "source_table_count"} == {
        k: v for k, v in chat.items() if k != "source_table_count"
    }


def _figures(facts: dict) -> dict:
    """What the gate states: the queries, each kept engine's, the removed engines, the
    source tables and the cache candidates."""
    cache = facts["cache_candidates"] or {}
    return {
        "queries": facts["query_total_count"],
        "in_scope": facts["total_after"],
        "engines": {r["engine"]: r["after"] for r in facts["rows"] if r["after"]},
        "removed": sorted(r["engine"] for r in facts["rows"] if r["dropped"]),
        "surviving": facts["surviving"],
        "tables": facts["source_table_count"],
        "cache": cache.get("read_count"),
    }


def _report_figures(report_facts: dict) -> dict:
    cache = report_facts.get("cache_layer") or {}
    return {
        "queries": report_facts["query_total_count"],
        "in_scope": report_facts["in_scope_query_count"],
        "engines": report_facts["engine_query_counts"],
        "removed": sorted(r["engine"] for r in report_facts["removed_engines"]),
        "surviving": report_facts["target_engines"],
        "tables": report_facts["source_table_count"],
        "cache": cache.get("candidate_read_count"),
    }


def test_after_a_customer_edit_the_figures_describe_the_edited_version(
    run_root, tmp_path, monkeypatch
) -> None:
    """The review's repro: on a copy of the wordpress run, mark 5 DynamoDB queries out of
    scope and move 3 to Aurora MySQL (a new version), then run synthesis. The page's
    figures, the chat gate's and the version's report agree, before and after synthesis."""
    from scripts import run_assessment
    from tests.e2e.pipeline import PipelineResult, _run

    shutil.copytree(run_root, tmp_path / "artifacts")
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    monkeypatch.setattr(assignments, "artifact_store", store)
    url = "/api/v1/assessments/ps-job/assignments?database_name=wordpress"
    shown = client.get(url).json()
    record = gate_block_facts(run_assessment._reality_check_artifact(store, "ps-job", "wordpress"))
    dynamodb = [
        qa["query_id"]
        for qa in shown["assignment"]["query_assignments"]
        if qa["assigned_engine"] == "dynamodb" and qa["in_scope"]
    ]
    edit = client.put(
        url,
        json={
            "overrides": [{"query_id": q, "in_scope": False} for q in dynamodb[:5]]
            + [{"query_id": q, "assigned_engine": "aurora_mysql"} for q in dynamodb[5:8]],
            "base_version": shown["assignment"]["version"],
        },
    )
    assert edit.status_code == 200, edit.json()
    version = edit.json()["assignment"]["version"]
    assert version == shown["assignment"]["version"] + 1

    # Waiting for synthesis: counted from the edited version's in-scope queries.
    page = client.get(url).json()
    assert page["assignment"]["version"] == version
    edited = store.read_json(f"wordpress/ps-job/assignment/v{version}/assignment.json")
    in_scope: dict[str, int] = {}
    for qa in edited["query_assignments"]:
        if qa.get("in_scope", True):
            in_scope[qa["assigned_engine"]] = in_scope.get(qa["assigned_engine"], 0) + 1
    figures = _figures(page["gate_facts"])
    assert figures["engines"] == in_scope
    assert figures["queries"] == record["query_total_count"] - 5
    chat = gate_block_facts(run_assessment._reality_check_artifact(store, "ps-job", "wordpress"))
    assert _figures(chat) | {"tables": None} == figures | {"tables": None}

    common = ["--job-id", "ps-job", "--db", "wordpress", "--artifact-root"]
    _run(
        "synthesis",
        ["scripts/run_synthesis.py", *common, str(tmp_path / "artifacts"), "--llm-mode", "none"],
        PipelineResult(db="wordpress", job_id="ps-job", artifact_root=tmp_path / "artifacts"),
    )
    report = store.read_json(f"wordpress/ps-job/synthesis/v{version}/report.json")["facts"]
    page = client.get(url).json()
    assert page["results_version"] == version
    chat = gate_block_facts(run_assessment._reality_check_artifact(store, "ps-job", "wordpress"))
    assert _figures(page["gate_facts"]) == _report_figures(report) == _figures(chat)


def test_an_unreadable_record_leaves_the_page_without_figures(tmp_path, monkeypatch) -> None:
    # A malformed old reality-check record no longer fails the whole GET.
    _store(tmp_path, monkeypatch, cached=False)
    assignments.artifact_store.write_json(
        f"{DB}/{JOB}/reality-check/output.json", {"before_distribution": "not a mapping"}
    )
    response = client.get(_url())
    assert response.status_code == 200
    assert response.json()["gate_facts"] is None
    assert response.json()["assignment"]["version"] == 1
