"""The synthetic SQL Server sample (``trailgear``, #618) reaches the routing
paths only a heterogeneous source can reach.

``scripts/generate_sqlserver_sample.py`` shapes the workload for each path
below; these tests fail if a later change stops the sample from reaching one
of them, so the fixture keeps testing what it was built to test. A routing
change that legitimately moves one of these paths should retune the generator
(and regenerate the routing snapshot), not loosen the assertion.

Queries are found by a fragment of their SQL text, never by digest, so a
reader can see which query each check is about.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, PipelineResult, _env
from tests.e2e.routing_snapshot import measure

pytestmark = [pytest.mark.e2e, pytest.mark.deterministic]

WORKLOAD_FEATURES = [
    "json_usage",
    "merge_statements",
    "multiple_schemas",
    "recursive_ctes",
    "sequences",
]


def _json(path: Path) -> dict:
    data: dict = json.loads(path.read_text())
    return data


def _collector(run: PipelineResult) -> dict:
    return _json(run.job_dir() / "collector" / "output.json")


def _qid(run: PipelineResult, fragment: str) -> str:
    """The one query whose SQL text contains ``fragment``."""
    hits: list[str] = [
        q["query_id"]
        for q in _collector(run)["queries"]["query_patterns"]
        if fragment in q["query_text"]
    ]
    assert len(hits) == 1, f"{fragment!r} matches {len(hits)} queries, want 1"
    return hits[0]


def _assignment(run: PipelineResult, version: int) -> dict:
    return _json(run.job_dir() / "assignment" / f"v{version}" / "assignment.json")


def _final_version(run: PipelineResult) -> int:
    report = _json(run.artifact_root / run.report["report"])
    return int(report["assignment_summary"]["version"])


def _by_id(assignment: dict) -> dict[str, dict]:
    return {qa["query_id"]: qa for qa in assignment["query_assignments"]}


# ---------------------------------------------------------------------------
# Aurora MySQL vs Aurora PostgreSQL: pick exactly one, reasons traced (#381)
# ---------------------------------------------------------------------------


def test_sql_server_source_is_collected_as_sql_server(trailgear_run: PipelineResult) -> None:
    source = _collector(trailgear_run)["metadata"]["source_database"]
    assert source["engine"] == "sqlserver"
    assert source["version"] == "15.0.4322.2"


def test_aurora_choice_is_a_close_call_tipped_by_workload_features(
    trailgear_run: PipelineResult,
) -> None:
    """Aurora MySQL scores the higher total, within the 5% margin, so the
    workload's own features decide -- every one of them, and never Babelfish
    (a last resort only when no workload feature is present)."""
    choice = _assignment(trailgear_run, 1)["aurora_engine_choice"]
    assert choice["source_engine"] == "sqlserver"
    mysql, pg = choice["totals"]["aurora_mysql"], choice["totals"]["aurora_postgresql"]
    assert mysql > pg, "the sample must make the higher-total engine lose on features"
    assert mysql - pg <= 0.05 * mysql, "the totals must be close enough for features to decide"
    assert choice["margin"] == round(pg - mysql, 2)
    assert choice["engine"] == "aurora_postgresql"
    assert choice["deciding_features"] == WORKLOAD_FEATURES
    assert "babelfish_compatible" not in choice["deciding_features"]
    assert choice["reason"].startswith("close on total adjusted score")
    assert "tipped by: JSON usage, MERGE statements, multiple schemas" in choice["reason"]


def test_only_the_chosen_aurora_engine_serves_queries(trailgear_run: PipelineResult) -> None:
    for version in (1, _final_version(trailgear_run)):
        engines = {
            qa["assigned_engine"] for qa in _assignment(trailgear_run, version)["query_assignments"]
        }
        assert "aurora_mysql" not in engines, f"v{version} routes a query to the losing engine"
        assert "aurora_postgresql" in engines


def test_every_deliverable_source_carries_the_same_choice(trailgear_run: PipelineResult) -> None:
    report = _json(trailgear_run.artifact_root / trailgear_run.report["report"])
    assert (
        report["assignment_summary"]["aurora_engine_choice"]
        == _assignment(trailgear_run, 1)["aurora_engine_choice"]
    )
    wave1 = report["migration_waves"][0]
    assert wave1["engines"] == ["aurora_postgresql"]
    assert wave1["homogeneity"] == "cross_engine"
    assert "15.0.4322.2" in wave1["rationale"]
    assert "Microsoft" not in wave1["rationale"]
    assert "has no Aurora engine of its own dialect" in report["summary"]
    sentence = (
        "Aurora MySQL scored {aurora_mysql:.1f} and Aurora PostgreSQL {aurora_postgresql:.1f} "
        "in total adjusted query fit; tipped by JSON usage, MERGE statements, multiple "
        "schemas, recursive CTEs, sequences"
    ).format(**report["assignment_summary"]["aurora_engine_choice"]["totals"])
    decision = trailgear_run.html("decision-report").read_text()
    engineering = next(
        Path(p)
        for p in trailgear_run.report["paths"]
        if "_engineering-report_" in p and p.endswith(".md")
    ).read_text()
    assert sentence in decision
    assert sentence in engineering


# ---------------------------------------------------------------------------
# Ties: a co-dependency group tie, and an individual tie that survives the
# reality check (ADR-031 Decision 2/12)
# ---------------------------------------------------------------------------


def test_category_group_ties_and_no_rule_decides_it(trailgear_run: PipelineResult) -> None:
    """The two self-join aggregates on ProductCategory form one co-dependency
    group whose Aurora PostgreSQL and DocumentDB group scores tie. SQL Server
    has no source-compatible engine, so the tie is reported as unresolved.

    This expectation assumes #656 is answered "no" (the chosen target Aurora
    engine is not source-compatible). If #656 decides otherwise, this tie
    resolves onto Aurora PostgreSQL and the expected outcome here changes --
    that is a test change, not a generator retune."""
    snapshot = measure(trailgear_run.job_dir())["queries"]
    members = [
        _qid(trailgear_run, "COUNT(c.[CategoryID]) AS [Children]"),
        _qid(trailgear_run, "COUNT(*) AS [Subcategories]"),
    ]
    first = [snapshot[q]["f"] for q in members]
    assert {f["c"] for f in first} == {"co_dependency_tie"}
    assert {f["tie"] for f in first} == {"tie_unresolved"}
    group = first[0]["grp"]
    assert first[1]["grp"] == group, "both queries are one group"
    top = max(group["scores"].values())
    assert sorted(e for e, s in group["scores"].items() if s == top) == [
        "aurora_postgresql",
        "documentdb",
    ]
    v1 = _by_id(_assignment(trailgear_run, 1))
    for q in members:
        assert v1[q]["tied_engines"] == ["aurora_postgresql", "documentdb"]
        assert "no routing rule decides between them" in v1[q]["assignment_reason"]


def test_an_unresolved_tie_reaches_the_deliverables(trailgear_run: PipelineResult) -> None:
    """The shipment queries tie Aurora PostgreSQL with DynamoDB. Both engines
    survive the reality check, so the tie is still open in the final assignment
    and every deliverable lists it as pending the customer's choice.

    This expectation assumes #656 is answered "no" (the chosen target Aurora
    engine is not source-compatible). If #656 decides otherwise, the tie
    resolves onto Aurora PostgreSQL and the expected outcome here changes --
    that is a test change, not a generator retune."""
    shipment = [
        _qid(trailgear_run, "FROM [Shipment] WHERE [TrackingNumber] = @TrackingNumber"),
        _qid(trailgear_run, "INSERT INTO [Shipment]"),
        _qid(trailgear_run, "UPDATE [Shipment] SET [DeliveredDate]"),
    ]
    final = _by_id(_assignment(trailgear_run, _final_version(trailgear_run)))
    for q in shipment:
        assert final[q]["tied_engines"] == ["aurora_postgresql", "dynamodb"], q
        assert "pending your choice" in final[q]["assignment_reason"]

    report = _json(trailgear_run.artifact_root / trailgear_run.report["report"])
    assert sorted(t["query_id"] for t in report["tied_queries"]) == sorted(shipment)
    decision = trailgear_run.html("decision-report").read_text()
    engineering = next(
        Path(p)
        for p in trailgear_run.report["paths"]
        if "_engineering-report_" in p and p.endswith(".md")
    ).read_text()
    for q in shipment:
        assert q in decision and q in engineering
    assert "Pending engine choices (3 queries)" in decision
    assert "## Unresolved engine ties (3 queries)" in engineering


def test_the_reality_check_resolves_the_documentdb_ties(trailgear_run: PipelineResult) -> None:
    """DocumentDB does not survive the reality check, so every tie between it
    and Aurora PostgreSQL alone (the category group's included) is resolved
    onto Aurora PostgreSQL, and the reason says why. The shipment ties also
    included DocumentDB; they stay open between the two surviving engines."""
    v1 = _by_id(_assignment(trailgear_run, 1))
    final = _by_id(_assignment(trailgear_run, _final_version(trailgear_run)))
    resolved = [
        q for q, qa in v1.items() if qa.get("tied_engines") == ["aurora_postgresql", "documentdb"]
    ]
    assert _qid(trailgear_run, "COUNT(*) AS [Subcategories]") in resolved
    assert len(resolved) >= 3
    for q in resolved:
        assert final[q]["assigned_engine"] == "aurora_postgresql"
        assert final[q]["tied_engines"] == []
        assert "DocumentDB no longer qualifies" in final[q]["assignment_reason"]


# ---------------------------------------------------------------------------
# Cache overlay, and a customer cache override on top of it (#296)
# ---------------------------------------------------------------------------


def test_hot_reads_are_cached_and_hot_writes_tables_are_not(trailgear_run: PipelineResult) -> None:
    final = _by_id(_assignment(trailgear_run, _final_version(trailgear_run)))
    product = _qid(trailgear_run, "FROM [Product] WITH (NOLOCK) WHERE [ProductID] = @ProductID")
    flags = _qid(trailgear_run, "FROM [FeatureFlag]")
    assert final[product]["cache_engine"] == "elasticache"
    assert final[product]["cache_pattern"] == "point_lookup"
    assert final[flags]["cache_engine"] == "elasticache"
    assert final[flags]["cache_pattern"] == "reference_read"
    # UserSession takes about a quarter of its traffic as writes, under the
    # write-heavy line, so the session lookup is cached too, as a session/token
    # lookup: a PascalCase break counts as a word break, so T-SQL's
    # [SessionToken] matches the session keywords (#663).
    session = _qid(trailgear_run, "[ExpiresAt] FROM [UserSession] WHERE [SessionToken]")
    assert final[session]["cache_engine"] == "elasticache"
    assert final[session]["cache_pattern"] == "session_lookup"
    # A cold read is never cached, whatever its shape.
    supplier = _qid(trailgear_run, "FROM [Supplier] WHERE [SupplierID]")
    assert final[supplier]["cache_engine"] is None


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=600
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def test_customer_cache_overrides_reach_the_deliverables(
    trailgear_run: PipelineResult, tmp_path: Path
) -> None:
    """On a copy of the job: pin a cold read to the cache, remove a hot read
    from it, and name the cache as an owner. The owner never changes, the
    customer's choice survives a re-evaluation, and the re-rendered report
    counts the new overlay."""
    from src.agents.referee.assignment_overrides import (
        LosingAuroraEngineOverride,
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.agents.referee.cache_overlay import CUSTOMER_UNCACHE_REASON, refresh_cache_overlay
    from src.storage.local_store import LocalArtifactStore

    run = trailgear_run
    root = tmp_path / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    store = LocalArtifactStore(base_dir=str(root))

    cold = _qid(run, "FROM [Supplier] WHERE [SupplierID]")
    hot = _qid(run, "FROM [Product] WITH (NOLOCK) WHERE [ProductID] = @ProductID")
    named = _qid(run, "SELECT [WarehouseID], [Code], [Name] FROM [Warehouse]")
    before = _by_id(_assignment(run, _final_version(run)))

    # The #381 losing Aurora engine is not a candidate at all for a SQL Server source.
    with pytest.raises(LosingAuroraEngineOverride):
        apply_assignment_overrides(
            store,
            run.db,
            run.job_id,
            [QueryOverrideInput(query_id=cold, assigned_engine="aurora_mysql")],
        )

    result = apply_assignment_overrides(
        store,
        run.db,
        run.job_id,
        [
            QueryOverrideInput(query_id=cold, cached=True),
            QueryOverrideInput(query_id=hot, cached=False),
            QueryOverrideInput(query_id=named, assigned_engine="elasticache"),
        ],
    )
    after = {qa.query_id: qa for qa in result.assignment.query_assignments}
    for q in (cold, hot, named):
        assert after[q].assigned_engine == before[q]["assigned_engine"], "the cache never owns"
        assert after[q].cache_customer_override is True
    assert after[cold].cache_engine == "elasticache"
    assert any("does not meet the hot-read rule" in w for w in after[cold].warnings)
    assert after[hot].cache_engine is None
    assert after[hot].cache_reason == CUSTOMER_UNCACHE_REASON
    assert after[named].cache_engine == "elasticache"

    # A later re-evaluation (the reality check refreshes the overlay this way)
    # keeps both customer decisions.
    raw = result.assignment.model_dump(mode="json")
    queries = _collector(run)["queries"]["query_patterns"]
    refresh_cache_overlay(raw, queries, ["elasticache", "aurora_postgresql", "dynamodb"])
    refreshed = _by_id(raw)
    assert refreshed[cold]["cache_engine"] == "elasticache"
    assert refreshed[hot]["cache_engine"] is None

    common = ["--job-id", run.job_id, "--db", run.db, "--artifact-root", str(root)]
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    report_step = _script("scripts/run_report.py", *common)
    report = _json(root / report_step["report"])
    assert report["assignment_summary"]["version"] == result.assignment.version
    old = _json(run.artifact_root / run.report["report"])
    # Two cold reads pinned, one hot read removed.
    assert before[cold]["cache_engine"] is None and before[named]["cache_engine"] is None
    assert before[hot]["cache_engine"] == "elasticache"
    assert report["cache_overlay"]["query_count"] == old["cache_overlay"]["query_count"] + 1
    assert report_step["errors"] == []
