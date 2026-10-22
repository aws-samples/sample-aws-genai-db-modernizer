"""A customer's engine scope end to end (ADR-031 Decision 2 Amendment 14, #1327).

On wordpress (a MySQL source) and trailgear (SQL Server), triage runs with a scope
of DynamoDB and Aurora PostgreSQL: wordpress' source-compatible engine is left out,
and trailgear keeps one of its two Aurora engines. The rest of the pipeline runs as the
deterministic job does. The discourse case (a PostgreSQL source taking Aurora MySQL) is
covered without a pipeline run, on its collection, in
``tests/unit/agents/referee/test_engine_scope.py::TestTheDiscourseCollection``. No engine outside the scope is assigned in
any version, the scope's Aurora choice is recorded, wave 1 is the cross-engine move to
it, and nothing treats the in-scope Aurora engine as source-compatible.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, _env, unzip_sample

JOB = "scoped"

# The scope per sample: DynamoDB plus the Aurora engine that is not the source's own
# (or, for the SQL Server sample, one of the two). The test ids name the sample, so
# each case runs in that sample's e2e shard (ci/e2e.sh).
SCOPES = {
    "wordpress": ["aurora_postgresql", "dynamodb"],
    "trailgear": ["aurora_postgresql", "dynamodb"],
}


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=900
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stderr[-3000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


@pytest.fixture(scope="module", params=sorted(SCOPES))
def scoped(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    from src.agents.referee.triage_handler import run_triage
    from src.storage.local_store import LocalArtifactStore

    db = str(request.param)
    scope = SCOPES[db]
    base = Path(tmp_path_factory.mktemp(f"scope-{db}"))
    root = base / "artifacts"
    job = root / db / JOB
    common = ["--job-id", JOB, "--db", db, "--artifact-root", str(root)]
    collection = unzip_sample(db, base / "input")
    _script("scripts/run_collect.py", "--file", str(collection), *common)
    store = LocalArtifactStore(base_dir=str(root))
    run_triage(JOB, db, store, engine_scope=scope)
    triage = json.loads((job / "referee-triage" / "triage.json").read_text())
    for selected in triage["selected_agents"]:
        engine = selected["agent_type"]
        _script("scripts/run_analysis.py", *common, "--engine", engine, "--llm-mode", "none")
    _script("scripts/run_assignment.py", *common)
    _script("scripts/run_reality_check.py", *common, "--llm-mode", "none")
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    report_step = _script("scripts/run_report.py", *common)
    versions = sorted(
        (json.loads(p.read_text()) for p in (job / "assignment").glob("v*/assignment.json")),
        key=lambda a: a["version"],
    )
    return {
        "db": db,
        "root": root,
        "scope": scope,
        "triage": triage,
        "versions": versions,
        "report": json.loads((root / report_step["report"]).read_text()),
        "engineering": (job / "deliverables" / "engineering-report.md").read_text(),
    }


@pytest.mark.deterministic
def test_triage_records_the_scope_and_skips_every_engine_outside_it(scoped: dict) -> None:
    triage = scoped["triage"]
    assert triage["engine_scope"] == scoped["scope"]
    assert {a["agent_type"] for a in triage["selected_agents"]} == set(scoped["scope"])
    for skipped in triage["skipped_agents"]:
        assert skipped["agent_type"] not in scoped["scope"]
        assert skipped["reason"] == "Not in the customer's engine scope"


@pytest.mark.deterministic
def test_no_version_assigns_ties_or_caches_an_engine_outside_the_scope(scoped: dict) -> None:
    scope = set(scoped["scope"])
    for version in scoped["versions"]:
        for qa in version["query_assignments"]:
            assert qa["assigned_engine"] in scope, (version["version"], qa["query_id"])
            assert set(qa.get("tied_engines") or []) <= scope, (version["version"], qa["query_id"])
            assert qa.get("cache_engine") in scope | {None}, (version["version"], qa["query_id"])
        overlay = version.get("cache_overlay") or {}
        assert overlay.get("engine") in scope | {None}, version["version"]


@pytest.mark.deterministic
def test_the_scope_choice_is_recorded_on_every_version(scoped: dict) -> None:
    aurora = next(e for e in scoped["scope"] if e.startswith("aurora_"))
    for version in scoped["versions"]:
        choice = version["aurora_engine_choice"]
        assert choice["engine"] == aurora
        assert choice["basis"] == "engine_scope"


@pytest.mark.deterministic
def test_no_move_treats_the_in_scope_aurora_engine_as_source_compatible(scoped: dict) -> None:
    for version in scoped["versions"]:
        for move in version.get("moves") or []:
            assert not (move.get("evidence") or {}).get("source_compatible"), move["query_id"]
            assert move.get("reason_code") not in (
                "tie.source_compatible",
                "consolidate.source_compatible_survives",
            ), move["query_id"]


@pytest.mark.deterministic
def test_wave_one_is_the_cross_engine_move_to_the_scope_choice(scoped: dict) -> None:
    aurora = next(e for e in scoped["scope"] if e.startswith("aurora_"))
    wave1 = scoped["report"]["migration_waves"][0]
    assert wave1["engines"] == [aurora]
    assert wave1["homogeneity"] == "cross_engine"
    assert "customer's engine scope" in wave1["rationale"]
    assert scoped["report"]["facts"]["waves"][0]["heterogeneous_note"]
    assert "customer's engine scope" in scoped["engineering"]
    assert "customer's engine scope" in scoped["report"]["summary_deterministic"]
    assert "competed on the collected workload" not in scoped["report"]["summary_deterministic"]


# #1330: rules the scoped sample's wave 1 must raise (the samples' own features).
SCOPED_GAPS = {
    "wordpress": {
        "mysql-on-duplicate-key-update",
        "mysql-replace-into",
        "mysql-limit-offset-comma",
        "mysql-zero-dates",
        "mysql-case-insensitive-collations",
    },
    "trailgear": {"sqlserver-top-clause"},
}


@pytest.mark.deterministic
def test_wave_one_lists_the_conversions_gaps_in_every_deliverable(scoped: dict) -> None:
    """#1330: the cross-engine wave 1 the scope causes is checked for feature gaps, and
    report.json, the engineering report and the executive PDF list them under wave 1."""
    from pypdf import PdfReader

    from src.report.escaping import md_text

    gaps = scoped["report"]["facts"]["relational_gaps"]
    assert gaps["feature_rules_checked"] > 0
    assert SCOPED_GAPS[scoped["db"]] <= {g["rule_id"] for g in gaps["gaps"]}
    note = scoped["report"]["facts"]["waves"][0]["heterogeneous_note"]
    found = f"The relational gap check found {len(gaps['gaps'])} gaps in this conversion"
    pdf = scoped["root"] / scoped["db"] / JOB / "deliverables" / "executive-report.pdf"
    pdf_text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(str(pdf)).pages
    )
    for text in (note, scoped["engineering"], pdf_text):
        assert found in text
        assert "no feature rules" not in text
    for gap in gaps["gaps"]:
        assert md_text(gap["feature"]) in scoped["engineering"], gap["rule_id"]


def _refused(scoped: dict, override) -> str:
    """Apply one customer override to the scoped job; return the refusal's message."""
    from src.agents.referee.assignment_overrides import (
        OutOfScopeOverride,
        apply_assignment_overrides,
    )
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(scoped["root"]))
    latest = scoped["versions"][-1]
    with pytest.raises(OutOfScopeOverride) as refused:
        apply_assignment_overrides(store, scoped["db"], JOB, [override])
    after = scoped["root"] / scoped["db"] / JOB / "assignment" / f"v{latest['version'] + 1}"
    assert not after.exists()
    return str(refused.value)


def _first_query_on(scoped: dict, engine: str) -> str:
    ids: list[str] = sorted(
        qa["query_id"]
        for qa in scoped["versions"][-1]["query_assignments"]
        if qa["assigned_engine"] == engine
    )
    return ids[0]


@pytest.mark.deterministic
@pytest.mark.parametrize("engine", ["documentdb", "aurora_mysql", "elasticache"])
def test_the_customer_gate_refuses_a_pick_onto_an_engine_outside_the_scope(
    scoped: dict, engine: str
) -> None:
    from src.agents.referee.assignment_overrides import QueryOverrideInput
    from src.shared.engine_names import display_engine

    query_id = _first_query_on(scoped, "dynamodb")
    message = _refused(scoped, QueryOverrideInput(query_id=query_id, assigned_engine=engine))
    assert "Aurora PostgreSQL, DynamoDB" in message
    assert f"Add {display_engine(engine)} to the scope" in message


@pytest.mark.deterministic
def test_the_customer_gate_refuses_a_cache_pin_when_the_scope_leaves_out_elasticache(
    scoped: dict,
) -> None:
    """The review's wordpress repro: a cache pin on a DynamoDB query used to write a
    version with an ElastiCache overlay the scope leaves out."""
    from src.agents.referee.assignment_overrides import QueryOverrideInput

    query_id = _first_query_on(scoped, "dynamodb")
    message = _refused(scoped, QueryOverrideInput(query_id=query_id, cached=True))
    assert "ElastiCache is not in the customer's engine scope" in message
    assert "Add ElastiCache to the scope to cache this query" in message


def _route(db: str, base: Path, scope: list[str] | None) -> list[dict]:
    """Collect, triage (with ``scope``), analysis, assignment and the reality check."""
    from src.agents.referee.triage_handler import run_triage
    from src.storage.local_store import LocalArtifactStore

    root = base / "artifacts"
    job = root / db / JOB
    common = ["--job-id", JOB, "--db", db, "--artifact-root", str(root)]
    _script("scripts/run_collect.py", "--file", str(unzip_sample(db, base / "input")), *common)
    run_triage(JOB, db, LocalArtifactStore(base_dir=str(root)), engine_scope=scope)
    triage = json.loads((job / "referee-triage" / "triage.json").read_text())
    for selected in triage["selected_agents"]:
        engine = selected["agent_type"]
        _script("scripts/run_analysis.py", *common, "--engine", engine, "--llm-mode", "none")
    _script("scripts/run_assignment.py", *common)
    _script("scripts/run_reality_check.py", *common, "--llm-mode", "none")
    return sorted(
        (json.loads(p.read_text()) for p in (job / "assignment").glob("v*/assignment.json")),
        key=lambda a: a["version"],
    )


def _routing(version: dict) -> dict:
    return {
        "engines": [
            (qa["query_id"], qa["assigned_engine"], qa.get("tied_engines") or [])
            for qa in version["query_assignments"]
        ],
        "moves": [
            (m["query_id"], m["from_engine"], m["to_engine"], m["reason_code"], m["score_after"])
            for m in version.get("moves") or []
        ],
        "aurora_engine_choice": version.get("aurora_engine_choice"),
        "cache_overlay": version.get("cache_overlay"),
    }


@pytest.mark.deterministic
def test_a_scope_of_every_engine_routes_wordpress_as_no_scope(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Every engine in scope, both Aurora engines included, gives the same versions as
    no scope, through the reality check: engines, ties, Moves, choice and overlay."""
    from src.agents.referee.triage import ANALYSIS_AGENTS

    base = tmp_path_factory.mktemp("every-wordpress")
    every = _route("wordpress", base / "every", sorted(ANALYSIS_AGENTS))
    plain = _route("wordpress", base / "plain", None)
    assert len(every) == len(plain) >= 2
    for a, b in zip(every, plain, strict=True):
        assert _routing(a) == _routing(b), a["version"]
