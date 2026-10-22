"""The engine scope setting through the Assessments API, on wordpress (#1328).

A job is started with ``POST /api/v1/assessments`` and its ``target_databases``; the
collection is collected as the local collector does (``scripts/run_collect.py``),
triage runs (``scripts/run_triage.py``, which reads the recorded scope), and the API
runs analysis, the assignment and the reality check (``POST .../resume``). Synthesis
and the deliverables follow as in the deterministic pipeline.

- ``["dynamodb", "aurora_postgresql"]``: the source-compatible Aurora MySQL is left
  out, so Aurora PostgreSQL is the relational candidate: DynamoDB 40, Aurora
  PostgreSQL 67 (as #1331's library scope), and every deliverable says so.
- No scope: the per-query routing is the routing snapshot's (main's), and nothing is
  recorded or stated.
- ``["dynamodb"]``: 42 queries no in-scope engine can serve (#1329): the run completes,
  they have no engine and are in no wave, and every deliverable lists them as a
  blocking risk; the other 65 go to DynamoDB.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.e2e.pipeline import REPO, _env, unzip_sample

pytestmark = pytest.mark.e2e

DB = "wordpress"
SCOPED = ["dynamodb", "aurora_postgresql"]
STATEMENT = "Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)"


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=900
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stderr[-3000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


class _Api:
    """The FastAPI app wired to one artifact root, as ``src/api/main.py`` wires it."""

    def __init__(self, root: Path) -> None:
        from fastapi.testclient import TestClient

        from src.api.main import app
        from src.api.routes import assessments, phases
        from src.api.services.local_execution import LocalExecutionService
        from src.orchestrator.local_orchestrator import LocalOrchestrator
        from src.storage.local_store import LocalArtifactStore

        self.root = root
        store = LocalArtifactStore(base_dir=str(root))
        sfn = LocalExecutionService(store)
        self._modules: tuple[Any, Any] = (assessments, phases)
        self._saved = (
            assessments.sfn_service,
            assessments.s3_service,
            phases.sfn_service,
            phases.orchestrator,
        )
        assessments.sfn_service = sfn
        assessments.s3_service = MagicMock()
        phases.sfn_service = sfn
        phases.orchestrator = LocalOrchestrator(store=store, llm_mode="none")
        self.client = TestClient(app)

    def close(self) -> None:
        assessments: Any
        phases: Any
        assessments, phases = self._modules
        (
            assessments.sfn_service,
            assessments.s3_service,
            phases.sfn_service,
            phases.orchestrator,
        ) = self._saved

    def start(self, job: str, scope: list[str] | None) -> Any:
        body: dict[str, Any] = {
            "job_id": job,
            "source_database_type": "mysql",
            "database_name": DB,
            "collection_mode": "offline",
            "offline_s3_key": f"{DB}/{job}/uploads/collector-output.json",
        }
        if scope is not None:
            body["target_databases"] = scope
        return self.client.post("/api/v1/assessments", json=body)

    def resume(self, job: str, phase: str) -> Any:
        return self.client.post(f"/api/v1/assessments/{job}/resume", json={"phase": phase})


def _collect_and_triage(base: Path, root: Path, job: str) -> list[str]:
    common = ["--job-id", job, "--db", DB, "--artifact-root", str(root)]
    collection = unzip_sample(DB, base / f"input-{job}")
    _script("scripts/run_collect.py", "--file", str(collection), *common)
    _script("scripts/run_triage.py", *common)
    return common


def _run(base: Path, job: str, scope: list[str] | None) -> dict[str, Any]:
    root = base / "artifacts"
    api = _Api(root)
    try:
        started = api.start(job, scope)
        assert started.status_code == 202, started.text
        common = _collect_and_triage(base, root, job)
        for phase in ("analysis", "assignment", "reality_check"):
            resp = api.resume(job, phase)
            assert resp.status_code == 200, (phase, resp.text)
    finally:
        api.close()
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    report_step = _script("scripts/run_report.py", *common)
    job_dir = root / DB / job
    versions = sorted(
        (json.loads(p.read_text()) for p in (job_dir / "assignment").glob("v*/assignment.json")),
        key=lambda a: a["version"],
    )
    deliverables = job_dir / "deliverables"
    return {
        "job_dir": job_dir,
        "meta": json.loads((job_dir / "_meta.json").read_text()),
        "triage": json.loads((job_dir / "referee-triage" / "triage.json").read_text()),
        "versions": versions,
        "report": json.loads((root / report_step["report"]).read_text()),
        "engineering": (deliverables / "engineering-report.md").read_text(),
        "pdf": deliverables / "executive-report.pdf",
    }


def _distribution(assignment: dict) -> dict[str, int]:
    return dict(Counter(qa["assigned_engine"] for qa in assignment["query_assignments"]))


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return " ".join(" ".join(page.extract_text().split()) for page in PdfReader(path).pages)


@pytest.fixture(scope="module")
def scoped(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run(Path(tmp_path_factory.mktemp("scope-api-wordpress")), "api-scoped", SCOPED)


@pytest.fixture(scope="module")
def unscoped(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run(Path(tmp_path_factory.mktemp("scope-api-wordpress-none")), "api-unscoped", None)


@pytest.mark.deterministic
def test_wordpress_scope_through_the_api_is_recorded_for_the_job(scoped: dict) -> None:
    assert scoped["meta"]["input"]["target_databases"] == sorted(SCOPED)
    decision = json.loads((scoped["job_dir"] / "decisions" / "engine_scope.json").read_text())
    assert decision["engines"] == sorted(SCOPED)
    assert (decision["decided_by"], decision["via"]) == ("customer", "api")
    assert scoped["triage"]["engine_scope"] == sorted(SCOPED)


@pytest.mark.deterministic
def test_wordpress_scope_through_the_api_routes_only_to_it(scoped: dict) -> None:
    first, final = scoped["versions"][0], scoped["versions"][-1]
    assert _distribution(first) == {"dynamodb": 40, "aurora_postgresql": 67}
    assert _distribution(final) == {"dynamodb": 40, "aurora_postgresql": 67}
    for version in scoped["versions"]:
        for qa in version["query_assignments"]:
            assert qa["assigned_engine"] in SCOPED
            assert set(qa.get("tied_engines") or []) <= set(SCOPED)
            assert qa.get("cache_engine") in {*SCOPED, None}
        assert version["aurora_engine_choice"]["basis"] == "engine_scope"


@pytest.mark.deterministic
def test_wordpress_scope_through_the_api_is_stated_in_every_deliverable(scoped: dict) -> None:
    assert scoped["report"]["facts"]["engine_scope"] == ["aurora_postgresql", "dynamodb"]
    assert f"{STATEMENT}." in scoped["engineering"]
    assert STATEMENT in _pdf_text(scoped["pdf"])
    data = json.loads((scoped["job_dir"] / "deliverables" / "assessment-data.json").read_text())
    assert data["facts"]["engine_scope"] == ["aurora_postgresql", "dynamodb"]


@pytest.mark.deterministic
def test_wordpress_with_no_scope_through_the_api_routes_as_main(unscoped: dict) -> None:
    from tests.e2e.routing_snapshot import BASELINE, compare, format_diff, measure

    assert unscoped["meta"]["input"]["target_databases"] is None
    assert not (unscoped["job_dir"] / "decisions" / "engine_scope.json").exists()
    assert unscoped["triage"]["engine_scope"] is None
    baseline = json.loads(BASELINE.read_text())["samples"][DB]
    diff = compare(baseline, measure(unscoped["job_dir"]))
    assert not diff, format_diff(DB, diff)
    assert unscoped["report"]["facts"]["engine_scope"] is None
    assert "Engines in scope" not in unscoped["engineering"]
    assert "Engines in scope" not in _pdf_text(unscoped["pdf"])


@pytest.fixture(scope="module")
def ddb_only(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run(Path(tmp_path_factory.mktemp("scope-api-wordpress-ddb")), "api-ddb", ["dynamodb"])


# wordpress' queries DynamoDB alone cannot serve: the utility statements, the
# aggregates, joins and pattern searches it is gated on, and the write and
# co-dependency groups holding one of them (each unservable as one unit).
DDB_UNSERVABLE = 42


@pytest.mark.deterministic
def test_wordpress_scope_no_engine_can_serve_completes_with_them_unservable(
    ddb_only: dict,
) -> None:
    first = ddb_only["versions"][0]
    ids = {u["query_id"] for u in first["unservable_queries"]}
    assert len(ids) == DDB_UNSERVABLE
    collector = json.loads((ddb_only["job_dir"] / "collector" / "output.json").read_text())
    assert len(ids) + len(first["query_assignments"]) == len(collector["queries"]["query_patterns"])
    for version in ddb_only["versions"]:
        # Carried by every version, never assigned, tied or cached.
        assert version["unservable_queries"] == first["unservable_queries"]
        for qa in version["query_assignments"]:
            assert qa["query_id"] not in ids
            assert qa["assigned_engine"] == "dynamodb"
            assert set(qa.get("tied_engines") or []) <= {"dynamodb"}
        # Each is a Move to unservable, with the engine the scope left out.
        moved = {m["query_id"]: m for m in version.get("moves") or [] if m["query_id"] in ids}
        if version["version"] == 1:
            assert set(moved) == ids
            assert {m["to_engine"] for m in moved.values()} == {"unservable"}
            assert {m["evidence"]["closest_engine"] for m in moved.values()} == {"aurora_mysql"}
        else:
            assert moved == {}


@pytest.mark.deterministic
def test_wordpress_unservable_queries_are_in_no_wave_and_no_engine_count(ddb_only: dict) -> None:
    report = ddb_only["report"]
    ids = {u["query_id"] for u in ddb_only["versions"][0]["unservable_queries"]}
    waves = json.dumps(report["migration_waves"])
    assert not [q for q in ids if q in waves]
    assert all(set(w.get("engines") or []) <= {"dynamodb"} for w in report["migration_waves"])
    facts = report["facts"]
    assert facts["unservable"]["no_engine_in_scope_query_count"] == DDB_UNSERVABLE
    assert facts["in_scope_query_count"] == len(ddb_only["versions"][-1]["query_assignments"])
    assert [e["engine"] for e in facts["engines"]] == ["dynamodb"]
    (risk,) = [
        r
        for r in report["risk_assessment"]["risks"]
        if r.get("rule") == "routing.no_engine_in_scope"
    ]
    assert (risk["severity"], risk["wave"]) == ("HIGH", None)
    assert set(risk["query_ids"]) == ids


@pytest.mark.deterministic
def test_wordpress_unservable_queries_are_in_every_deliverable(ddb_only: dict) -> None:
    from src.report.unservable import UNSERVABLE_INTRO, UNSERVABLE_SCOPE_INTRO

    heading = f"Queries that cannot be served as designed ({DDB_UNSERVABLE} queries)"
    assert f"## {heading}" in ddb_only["engineering"]
    assert "No engine (Aurora MySQL is outside the scope)" in ddb_only["engineering"]
    assert UNSERVABLE_SCOPE_INTRO in ddb_only["engineering"]
    pdf = _pdf_text(ddb_only["pdf"])
    assert heading in pdf
    assert " ".join(UNSERVABLE_SCOPE_INTRO.split()) in pdf
    # Never the assignment review's edit, which that review refuses for these queries.
    serve = "start a new assessment whose scope adds Aurora MySQL, or keep them on the source"
    for text in (ddb_only["engineering"], pdf):
        assert "change a query's engine" not in text
        assert UNSERVABLE_INTRO.split(":")[0] not in text
        assert serve in text
    # The headline totals name them, in both reports.
    no_engine = f"{DDB_UNSERVABLE} have no engine in the customer's engine scope"
    assert (
        f"of 107 queries move to a new engine (a migration target or a search read model); {no_engine}"
        in ddb_only["engineering"]
    )
    assert "of 107 queries move to a new engine" in pdf and f"{no_engine}." in pdf
    excludes = (
        f"Excludes the {DDB_UNSERVABLE} queries with no engine in the customer's engine scope"
    )
    assert excludes in ddb_only["engineering"] and excludes in pdf
    facts = ddb_only["report"]["facts"]
    assert (facts["query_total_count"], facts["no_engine_in_scope_query_count"]) == (
        107,
        DDB_UNSERVABLE,
    )
    risk_id = next(
        r["risk_id"]
        for r in ddb_only["report"]["risk_assessment"]["risks"]
        if r.get("rule") == "routing.no_engine_in_scope"
    )
    assert risk_id in ddb_only["engineering"]
    summary = f"{DDB_UNSERVABLE} of the 107 queries have no engine"
    assert summary in ddb_only["report"]["summary"]
    data = json.loads((ddb_only["job_dir"] / "deliverables" / "assessment-data.json").read_text())
    assert len(data["scope_unservable"]) == DDB_UNSERVABLE
