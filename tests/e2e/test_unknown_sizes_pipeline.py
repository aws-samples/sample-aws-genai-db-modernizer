"""The whole pipeline runs on a collection where some tables have no size and no count (#1636).

The bundled samples report a size for every table, so nothing exercised a table with
neither: the DynamoDB cost estimate summed ``t.get("size_mb", 0)`` and crashed on a
``None``, failing ``assess()`` with "analysis failed for dynamodb". This takes the
smallest bundled collection (a MySQL one), leaves 6 tables with no count and no size and
6 more with no count but a size, and runs the deterministic pipeline end to end, then the
analysis of every engine, then ``assess()`` with every engine in scope. Each must finish,
and the deliverables must state both cases.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts.run_analysis import _ALL_ENGINES
from src.assessment import AssessmentRequest, assess
from src.report.facts import report_facts
from src.shared.row_counts import NO_SIZE_ASSUMPTION, UNKNOWN_ROW_COUNT_ASSUMPTION
from src.tools.database.collection_text import extract_json_object
from tests.e2e.deliverable_figures import md_plain
from tests.e2e.pipeline import REPO, PipelineResult, _run, run_pipeline_from_file

pytestmark = pytest.mark.deterministic

NO_SIZE, NO_COUNT = 6, 6


def _collection() -> dict[str, Any]:
    with zipfile.ZipFile(REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data: dict[str, Any] = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    for i, table in enumerate(sorted(data["tables"], key=lambda t: t["table_name"])):
        table["row_count_basis"] = "estimate"  # a collection written since #1636
        if i < NO_SIZE:
            table["row_count"] = None
            table.pop("data_size_mb", None)
            table.pop("index_size_mb", None)
        elif i < NO_SIZE + NO_COUNT:
            table["row_count"] = None
    return data


@pytest.fixture(scope="module")
def collection_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path: Path = tmp_path_factory.mktemp("unknown-sizes") / "collection.json"
    path.write_text(json.dumps(_collection()))
    return path


@pytest.fixture(scope="module")
def unknown_run(collection_file: Path, tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    root = tmp_path_factory.mktemp("unknown-sizes-artifacts")
    return run_pipeline_from_file(collection_file, "shop", root, "unknownsizes")


def test_the_pipeline_finishes_and_states_both_cases(unknown_run: PipelineResult) -> None:
    collector = json.loads((unknown_run.job_dir() / "collector" / "output.json").read_text())
    tables = collector["database_schema"]["tables"]
    assert sum(1 for t in tables if t["row_count"] is None and t["size_mb"] is None) == NO_SIZE
    assert sum(1 for t in tables if t["row_count"] is None) == NO_SIZE + NO_COUNT
    facts = report_facts(json.loads(unknown_run.report_json().read_text()))["row_counts"]
    assert (facts["unknown_count"], facts["no_size_count"]) == (NO_SIZE + NO_COUNT, NO_SIZE)
    md = md_plain(unknown_run.deliverable("engineering-report.md").read_text(encoding="utf-8"))
    assert UNKNOWN_ROW_COUNT_ASSUMPTION in md and NO_SIZE_ASSUMPTION in md
    assert "no collected size, so no storage is priced for them" in md


def test_every_engines_analysis_finishes(unknown_run: PipelineResult) -> None:
    common = [
        "--job-id",
        unknown_run.job_id,
        "--db",
        unknown_run.db,
        "--artifact-root",
        str(unknown_run.artifact_root),
    ]
    for engine in sorted(_ALL_ENGINES):
        step = _run(
            f"analysis-all-{engine}",
            ["scripts/run_analysis.py", *common, "--engine", engine, "--llm-mode", "none"],
            unknown_run,
        )
        assert step, engine
        analysis = json.loads(
            (unknown_run.job_dir() / f"analysis-{engine}" / "analysis.json").read_text()
        )
        assumptions = (analysis.get("cost_estimate") or {}).get("pricing_assumptions") or []
        if assumptions != ["Cost estimation disabled."]:
            assert any("no collected size" in a for a in assumptions), engine


@pytest.mark.parametrize("version", [None, "1.3"])
@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_assess_finishes_with_every_engine_in_scope(
    collection_file: Path, run_through: str, version: str | None
) -> None:
    request = AssessmentRequest.model_validate(
        {
            **({"contract_version": version} if version else {}),
            "database_name": "shop",
            "collection": json.loads(collection_file.read_text()),
            "options": {
                "run_through": run_through,
                "target_engines": sorted(_ALL_ENGINES),
                **({"deterministic": True} if run_through == "reality_check" else {}),
            },
        }
    )
    result = assess(request)
    assert result.statements
    facts = json.loads(result.model_dump_json())["summary"]["table_facts"]
    # A count and a size the source does not have are null (#1632).
    unknown = [f for f in facts if f["size_mb"] is None and f["row_count"] is None]
    assert len(unknown) >= NO_SIZE
