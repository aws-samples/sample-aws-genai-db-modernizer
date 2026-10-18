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
    """The two Aurora totals are within the 5% margin, so the workload's own
    features decide -- every one of them, and never Babelfish (a last resort only
    when no workload feature is present).

    Aurora PostgreSQL leads, 7305 to 7285 (#893). Until #893, Aurora MySQL led
    only because the two analyses' JSON detectors disagreed: MySQL's read
    ``JSON_VALUE(`` and PostgreSQL's did not, worth +7 on four ``Product``
    queries and +3 on three that join it. With one JSON definition both engines
    score ``Product`` the same, and the lead is PostgreSQL's recursive-CTE
    pattern on ``TicketMessage`` (the thread CTE and the ``INSERT``, +3 each; the
    three ``Ticket`` joins, +2 each) plus +1 on eight queries whose tables do not
    resolve (``EXEC``, ``SET``, ``ALTER INDEX``, ``NEXT VALUE FOR``, a scalar
    function), which take the all-tables average. No T-SQL statement favours a
    MySQL-only pattern (``ON DUPLICATE KEY``, ``GROUP_CONCAT``, ``CALL``, MySQL's
    ``UPDATE a JOIN b``), so the higher total and the features now agree; the
    margin is what is pinned.

    #920: 7300 to 7320. The one penalty rule moves both totals by the same 15
    points (the shared Aurora entries: high-frequency-pk-lookup 30 -> 20 on one
    query, simple-cache-read 25 -> 20 on another), so the 20-point lead is
    unchanged.

    #920 catalog PR B: 7396 to 7413. Aurora's own key-value-lookup fit (80, a B-tree
    point read) now scores the key lookups on both engines, which lifts both totals
    about the same; the lead is 17 and the features still decide.

    #920 catalog PR C: 7396 to 7413, an exact tie. The 17 points were PostgreSQL's
    recursive-CTE credit on ``TicketMessage``, which MySQL 8.0 has too: Aurora MySQL's
    analysis now reports ``cte-recursive`` there (fit 90 against PostgreSQL's 95, plus
    the shared +15 bonus), and both engines' ``TicketMessage`` pattern score reaches
    the 100 cap, so PostgreSQL's 5-point edge in the fit does not show in this sample.
    The ``EXEC`` procedure calls are now ``stored-routine`` on both engines (their
    tables do not resolve, so no table score moves). A recursive CTE no longer tips
    the call (ADR-031 Decision 6), and the remaining workload features (JSON, MERGE,
    multiple schemas, sequences) decide it, still for Aurora PostgreSQL.

    #728 (the per-query access-pattern switch): 6989 to 7019. Each query's pattern
    dimension is its own class score, so the Aurora analyses' table pattern scores (and
    their FK, join and shared-feature bonuses) leave both totals alike. The 30-point lead
    is the two leading-wildcard searches (``[Name] LIKE '%' + @Term + '%'`` on Product
    and KnowledgeArticle): the text-search class rates Aurora PostgreSQL 80 (pg_trgm)
    and Aurora MySQL 45 (no index serves a leading wildcard), 14 points each; and the
    ``TicketMessage`` thread CTE, whose recursive-CTE credit (#985: Aurora PostgreSQL 95,
    Aurora MySQL 90) the per-query score keeps, 2 points.

    #494 slice 2 (each query's own cost dimension): 7260 to 7290. Both Aurora engines
    have the same $250 standing cost, could serve the same queries and bill no request,
    so every query's cost on them is the same (97: $2.63 a query) and both totals rise
    by 271 from the tables' cost bands; the 30-point lead is unchanged.

    #982 (CROSS APPLY reads as LATERAL on a SQL Server source): 7262 to 7294. One query,
    ``0xBE098518D277706D`` (``UserSession s CROSS APPLY OPENJSON(s.[CartJson])``), is a
    lateral join, so the lateral-join feature rates it on both engines at each one's
    documented credit (Aurora PostgreSQL 85, Aurora MySQL 80): +4 and +2 on its pattern
    dimension, so the lead grows by 2 and the features still decide."""
    choice = _assignment(trailgear_run, 1)["aurora_engine_choice"]
    assert choice["source_engine"] == "sqlserver"
    mysql, pg = choice["totals"]["aurora_mysql"], choice["totals"]["aurora_postgresql"]
    assert (mysql, pg) == (7262.0, 7294.0)
    assert abs(pg - mysql) <= 0.05 * max(mysql, pg), "close enough for features to decide"
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
    # #920 catalog PR C: recursive CTEs no longer tip the choice (MySQL 8.0 has them).
    sentence = (
        "Aurora MySQL scored {aurora_mysql:.1f} and Aurora PostgreSQL {aurora_postgresql:.1f} "
        "in total adjusted query fit; tipped by JSON usage, MERGE statements, multiple "
        "schemas, sequences"
    ).format(**report["assignment_summary"]["aurora_engine_choice"]["totals"])
    from tests.e2e.deliverable_figures import pdf_page_texts

    pdf = " ".join(pdf_page_texts(str(trailgear_run.deliverable("executive-report.pdf"))))
    engineering = trailgear_run.deliverable("engineering-report.md").read_text()
    assert " ".join(sentence.split()) in pdf
    assert sentence in engineering


# ---------------------------------------------------------------------------
# Ties (ADR-031 Decision 2/12): the individual ties the reality check resolves.
# Since #920 the sample no longer reaches a co-dependency group tie or a tie that
# survives the reality check; the two tests below pin what happens instead.
# ---------------------------------------------------------------------------


def test_category_group_scores_documentdb_below_aurora(trailgear_run: PipelineResult) -> None:
    """The two self-join aggregates on ProductCategory form one co-dependency
    group. Until #920 its Aurora PostgreSQL and DocumentDB group scores tied
    (82 each) and, with no source-compatible engine for SQL Server, the tie was
    reported as unresolved. Since #920 DocumentDB's graph-traversal-hierarchy
    anti-pattern (a self-referential join: ``$graphLookup`` runs only on 8.0
    instance-based clusters) costs its ``bounded_limit`` 10 points instead of 0,
    so the group goes to Aurora PostgreSQL, 82 to 72, with no tie. The sample no
    longer reaches the co-dependency tie path; restoring it is a generator retune,
    recorded on the #920 PR.

    #728: both queries are aggregations by their own class (Aurora PostgreSQL 85,
    DocumentDB 75), not their table's patterns, so the group scores 81 to 61 (DocumentDB
    71 less the same 10).

    #494 slice 2: each query's cost dimension is its own (Aurora PostgreSQL 97,
    DocumentDB 98), not ProductCategory's cost band, so the group scores 82 to 62.5.

    #1002: every engine weights cost the same 10%, so DocumentDB's 48/26/16/10 weights
    replace its 45/25/15/15, and the group scores 82 to 61."""
    snapshot = measure(trailgear_run.job_dir())["queries"]
    members = [
        _qid(trailgear_run, "COUNT(c.[CategoryID]) AS [Children]"),
        _qid(trailgear_run, "COUNT(*) AS [Subcategories]"),
    ]
    first = [snapshot[q]["f"] for q in members]
    assert {f["c"] for f in first} == {"co_dependency_group"}
    assert {f["e"] for f in first} == {"aurora_postgresql"}
    group = first[0]["grp"]
    assert first[1]["grp"] == group, "both queries are one group"
    assert (group["scores"]["aurora_postgresql"], group["scores"]["documentdb"]) == (82.0, 61.0)
    v1 = _by_id(_assignment(trailgear_run, 1))
    for q in members:
        assert v1[q]["tied_engines"] == []


def test_the_shipment_queries_no_longer_tie(trailgear_run: PipelineResult) -> None:
    """The shipment queries tied Aurora PostgreSQL, DocumentDB and DynamoDB at 76
    until #920, and the tie between the two surviving engines reached every
    deliverable as pending the customer's choice. Since #920 DynamoDB scores
    dbo.Shipment 77 (its call-rate fits no longer average its table score down)
    and DocumentDB 74 (its column-shape fits are gone), so all three go to
    DynamoDB with no tie. Catalog PR B raises DynamoDB to 80: dbo.Shipment now also
    has DynamoDB's keyed range-query and co-accessed-tables patterns, and no deliverable
    lists a pending choice. The sample no longer reaches the unresolved-tie-in-the-
    deliverables path; the rendering of a tie stays covered by
    ``tests/unit/report/test_tied_queries_deliverables.py``, and restoring the e2e path
    is a generator retune, recorded on the #920 PR.

    #728: each query now scores its own class, so the three split by what they do. The
    INSERT is a key lookup (DynamoDB 100, Aurora PostgreSQL and DocumentDB 80): DynamoDB
    80, Aurora PostgreSQL 75, DocumentDB 65. The read and the UPDATE by ``TrackingNumber`` (an index's leading column with no
    cardinality statistics, a non-unique key) are key-scoped reads (DocumentDB 85,
    DynamoDB 75, Aurora 70 for the pattern dimension), and Aurora PostgreSQL's other
    dimensions make it 71 against DocumentDB's 67 and DynamoDB's 65. None ties.

    #494 slice 2: each query's cost dimension is its own, not dbo.Shipment's cost band
    (Aurora PostgreSQL 97, DynamoDB 100 for the read and UPDATE, DocumentDB 98), so the
    read and the UPDATE score DynamoDB 72, Aurora PostgreSQL 75, DocumentDB 74, and the
    INSERT DynamoDB 87, Aurora PostgreSQL 79, DocumentDB 72. Same engines, no tie. Other
    trailgear queries now reach a pending choice again (see the next test), so the
    deliverables list those, and none of these three.

    #1002 (every engine weights cost the same 10%): DynamoDB and DocumentDB lose their
    flat cost bonus. The read scores DynamoDB 71, Aurora PostgreSQL 75, DocumentDB 72;
    the UPDATE 71, 75, 73; the INSERT 87, 79, 70. Same engines, no tie.

    #992 (ADR-031 Amendment 9, a table has one writer): the INSERT and the UPDATE are
    dbo.Shipment's write group, placed on its best call-weighted mean, DynamoDB 79.5
    against Aurora PostgreSQL 77.1 and DocumentDB 71.4, so the UPDATE joins the INSERT
    on DynamoDB. The read stays per query on Aurora PostgreSQL, a replica fed by CDC."""
    expected = {
        "FROM [Shipment] WHERE [TrackingNumber] = @TrackingNumber": (
            (71, 75, 72),
            "aurora_postgresql",
        ),
        "INSERT INTO [Shipment]": ((87, 79, 70), "dynamodb"),
        "UPDATE [Shipment] SET [DeliveredDate]": ((71, 75, 73), "dynamodb"),
    }
    snapshot = measure(trailgear_run.job_dir())["queries"]
    final = _by_id(_assignment(trailgear_run, _final_version(trailgear_run)))
    for text, (want, engine) in expected.items():
        q = _qid(trailgear_run, text)
        scores = snapshot[q]["s"]
        assert (scores["dynamodb"], scores["aurora_postgresql"], scores["documentdb"]) == want, q
        assert final[q]["assigned_engine"] == engine, q
        assert final[q]["tied_engines"] == [], q

    report = _json(trailgear_run.artifact_root / trailgear_run.report["report"])
    tied = {t["query_id"] for t in report["tied_queries"]}
    assert not tied & {_qid(trailgear_run, text) for text in expected}


def test_the_reality_check_resolves_the_documentdb_ties(trailgear_run: PipelineResult) -> None:
    """DocumentDB does not survive the reality check, so every tie between it
    and Aurora PostgreSQL alone is resolved onto Aurora PostgreSQL, and the
    reason says why. Since #920 those are a ProductCategory key read and the
    StockLevel ``MERGE`` (catalog PR B gives the Supplier read by key DynamoDB's
    key-value-lookup, 87 against Aurora PostgreSQL's 76, and ties the MERGE at 74); the
    category group no longer ties.

    #728: neither ties any more, each on its own class. The MERGE is an aggregation
    (``SUM`` over a ``GROUP BY``: Aurora PostgreSQL 77, DocumentDB 66) and stays on
    Aurora PostgreSQL; the ProductCategory read by ``ParentCategoryID`` is a key-scoped
    read (DynamoDB 76, Aurora PostgreSQL and DocumentDB 75) and goes to DynamoDB. The
    sample no longer reaches this path; re-resolving a tie whose engine the check
    removes stays covered by
    ``tests/unit/test_reality_check.py::TestReResolvingAnUnresolvedTieInPhaseTwo``.

    #494 slice 2 (each query's own cost dimension): with the tables' cost bands gone
    every engine scores 97-100 on cost here, and six first-assignment scores tie exactly.
    A SQL Server source has no source-compatible engine, so each is a placeholder pick
    (Decision 12). DocumentDB does not survive the reality check, so the four ties with
    one surviving engine clear onto it; the two three-way ties keep the two survivors,
    Aurora PostgreSQL and DynamoDB. The ProductCategory read by ``ParentCategoryID``
    (DocumentDB 77, Aurora PostgreSQL and DynamoDB 76) and a ProductReview read
    (DocumentDB 79, Aurora PostgreSQL and DynamoDB 78) are first-assigned to DocumentDB
    and tie the two survivors when it goes. The MERGE stays on Aurora PostgreSQL.

    #1002 (every engine weights cost the same 10%): DocumentDB and DynamoDB lose their
    flat cost bonus (DocumentDB 1-2 points, DynamoDB 1-2), so the first assignment's
    ties change. The UserSession and OrderStatusHistory ties go to Aurora PostgreSQL
    outright, the StockLevel read to DocumentDB (79 against DynamoDB's 78) and the
    PurchaseOrder reads to Aurora PostgreSQL (73 against DynamoDB's 72 and DocumentDB's
    71). Four DocumentDB leads of one point become ties with Aurora PostgreSQL: three
    ProductReview reads (78 each) and the ProductCategory read by ``ParentCategoryID``
    (76 each). DocumentDB does not survive the reality check, so all four clear onto
    Aurora PostgreSQL (DynamoDB scores them 77, 75 or cannot serve them), and no tie
    reaches the deliverables. The deliverables' tie heading stays covered by discourse's
    33 open ties (``tests/e2e/test_deliverable_figures.py``) and by
    ``tests/unit/report/test_tied_queries_deliverables.py``."""
    v1 = _by_id(_assignment(trailgear_run, 1))
    final = _by_id(_assignment(trailgear_run, _final_version(trailgear_run)))
    pg, doc = "aurora_postgresql", "documentdb"
    first_ties = (
        "SELECT r.[ReviewID], r.[Rating], r.[Title] FROM [ProductReview]",
        "SELECT TOP (10) r.[ReviewID], r.[Rating], r.[Title], r.[CreatedDate]",
        "SELECT AVG(CAST([Rating] AS decimal(4, 2))) AS [AvgRating]",
        "[ParentCategoryID], [Name], [Slug] FROM [ProductCategory]",
    )
    assert {q: qa["tied_engines"] for q, qa in v1.items() if qa.get("tied_engines")} == {
        _qid(trailgear_run, text): [pg, doc] for text in first_ties
    }
    for text in first_ties:
        qa = final[_qid(trailgear_run, text)]
        assert (qa["assigned_engine"], qa["tied_engines"]) == (pg, []), text
        assert "reality check" in qa["assignment_reason"].lower(), text
    assert not [q for q, qa in final.items() if qa.get("tied_engines")]
    assert doc not in {qa["assigned_engine"] for qa in final.values()}
    merge = _qid(trailgear_run, "MERGE [StockLevel] AS tgt")
    assert final[merge]["assigned_engine"] == pg
    assert final[merge]["tied_engines"] == []
    report = _json(trailgear_run.artifact_root / trailgear_run.report["report"])
    assert report["tied_queries"] == []
    # #987: with no open tie, neither the engineering report nor the executive report states one.
    from tests.e2e.deliverable_figures import pdf_page_texts

    md = trailgear_run.deliverable("engineering-report.md").read_text()
    pdf = " ".join(pdf_page_texts(str(trailgear_run.deliverable("executive-report.pdf"))))
    for heading in ("Unresolved engine ties", "Pending engine choices"):
        assert heading not in md
        assert heading not in pdf


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
