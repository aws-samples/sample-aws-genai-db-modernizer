# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The migration risks each sample raises, in every deliverable (#950, ADR-032).

Synthesis raises the risks the migration's shape carries from facts it already
computes (``src.agents.referee.migration_risks``). On the deterministic samples
this checks which rules each one raises, that the tables kept in sync agree
with ``facts.waves``, and that the engineering report, the executive PDF and
``assessment-data.json`` show the same risks as ``report.json``. The designed
wordpress job (schema designs, no model) adds the design rules.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.migration_risks import RULES
from src.report.escaping import md_text
from src.report.executive_report import RISK_DETAILS_POINTER
from src.shared.design_text import split_close_call_description
from src.shared.migration_risk_rules import GATE_IDS_NAMED
from src.shared.report_facts import risk_engine_and_body
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.deliverable_figures import norm, pdf_page_texts
from tests.e2e.designed_run import UNSUPPORTED_CACHE_READ, run_designed_wordpress
from tests.e2e.pipeline import (
    REPO,
    PipelineResult,
    _run,
    run_pipeline_from_file,
    unzip_pre_1955_wordpress,
)
from tests.report_concision import section

pytestmark = pytest.mark.deterministic

# The rules each deterministic sample raises today. A routing change that
# moves one in or out updates this with its reason in the pull request.
EXPECTED_RULES: dict[str, list[str]] = {
    # #1231: wordpress' whole read of the 2-row wp_wc_tax_rate_classes is routed to
    # DynamoDB without the frequent-full-scan penalty a redesign removes: one
    # design.redesign risk for DynamoDB.
    "wordpress": ["sync.wave", "routing.close_calls", "design.redesign"],
    # #1231: discourse's DocumentDB and DynamoDB plans rely on a redesign for some of
    # their queries: one design.redesign risk each (Aurora PostgreSQL scores its
    # redesigned queries as well as written, so it raises none).
    "discourse": [
        "sync.wave",
        "routing.tie_open",
        "routing.close_calls",
        "design.redesign",
        "design.redesign",
        # #1362: ten tables' write groups keep 13 statements DynamoDB serves poorly
        # (scores 11 to 26): one design.carried_writes risk for DynamoDB.
        "design.carried_writes",
    ],
    # #1149: trailgear's SQL Server to Aurora PostgreSQL wave 1 has 6 gaps that need a
    # rewrite and 3 that behave differently, one MEDIUM risk each (wordpress and discourse have one
    # informational gap each, which raises none). #1231: its DynamoDB plan relies on a
    # redesign for 5 queries.
    "trailgear": [
        "sync.wave",
        # #1893: over its own observed window the ProductReview top-10 read scores 76 on
        # Aurora PostgreSQL, DocumentDB and DynamoDB alike; DocumentDB does not survive the
        # reality check, so the Aurora PostgreSQL/DynamoDB tie stays open.
        "routing.tie_open",
        "routing.close_calls",
        "design.redesign",
        # #1362: the UserSession expiry delete DynamoDB serves poorly (22).
        "design.carried_writes",
        "relational.gaps",
        "relational.gaps",
    ],
}
# The open risks each sample lists (analysis risks and migration risks). #1893: wordpress
# gains one HIGH analysis risk: over its own observed window `SELECT meta_id FROM wp_postmeta`
# runs at 23.8 calls/s (14.6 over 24 hours), past Aurora MySQL's simple-cache-read threshold
# (20), so it routes to DynamoDB and Aurora MySQL's finding is flagged on it. #1994: that
# threshold is now Aurora's documented read capacity (21,875 calls/s), so the read stays on
# Aurora MySQL and the HIGH is gone (wordpress 5 -> 4); trailgear loses its HIGH the same
# way (two reads Aurora PostgreSQL's simple-cache-read flagged on DynamoDB, 14 -> 13).
# Discourse gains one MEDIUM (12 -> 13): single-access-pattern-table no longer covers its
# whole-table ordered reads, so the 7 queries it still flags on DocumentDB are under
# BROAD_FLAGGED_QUERIES and get their own risk instead of joining the coverage gap.
# #1954: wordpress and discourse also raise the two routine risks of their routine sets
# (routines.migrate_yourself and routines.carried, tests/e2e/test_sample_routines.py).
EXPECTED_OPEN = {"wordpress": 6, "discourse": 15, "trailgear": 13}

DESIGNED_RULES = [
    "sync.wave",
    "routing.close_calls",
    "cache.unsupported",
    "design.app_layer",
    "design.trade_offs",
    "design.trade_offs",
    "design.trade_offs",
    "design.redesign",  # #1231: the reference read routed on a redesign
]


def _report(r: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(r.report_json().read_text())
    return report


_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _routine(risk: dict[str, Any]) -> bool:
    """A routine risk (ADR-034 Decision 7): it names the collected tables its routines
    name, none when they name none, and no query; tests/e2e/test_sample_routines.py
    checks them (#1954)."""
    return str(risk.get("rule") or "").startswith("routines.")


def _new(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The risks the migration's shape raises (``RULES``), the routine risks left out."""
    return [x for x in report["risk_assessment"]["risks"] if x.get("rule") and not _routine(x)]


def test_each_sample_raises_its_migration_risks(run: PipelineResult) -> None:
    report = _report(run)
    risks = _new(report)
    assert [r["rule"] for r in risks] == EXPECTED_RULES[run.db], run.db
    assert len(report["risk_assessment"]["risks"]) == EXPECTED_OPEN[run.db], run.db
    for r in risks:
        assert r["rule"] in RULES
        assert r["affected_tables"] and r["query_ids"], r["risk_id"]
        assert r["wave"] and f"Wave {r['wave']}" in r["mitigation"], r["risk_id"]
    # #1510: each wave's gate in the roadmap lists the risks to close before it.
    gates = {w["wave"]: w["gate"] for w in report["migration_waves"]}
    per_wave = {w: sum(1 for r in risks if r["wave"] == w) for w in {r["wave"] for r in risks}}
    for r in risks:
        # A wave with more risks than a gate line names is counted (the cards carry
        # the ids, ``GATE_IDS_NAMED``): discourse's Wave 3 (6 risks) since #1362.
        if per_wave[r["wave"]] > GATE_IDS_NAMED:
            assert f"the {per_wave[r['wave']]} Wave {r['wave']} risks" in gates[r["wave"]]
        else:
            assert r["risk_id"] in gates[r["wave"]], r["risk_id"]


def test_the_sync_risk_agrees_with_the_wave_and_source_facts(run: PipelineResult) -> None:
    """Each wave's one sync risk covers every table the wave keeps in sync from
    outside it and every replicated table of ``facts.table_sources``."""
    report = _report(run)
    sync = [r for r in _new(report) if r["rule"] == "sync.wave"]
    synced = {t for r in sync for t in r["affected_tables"]}
    sources = report["facts"]["table_sources"]
    assert {t["table"] for t in sources["replicated_tables"]} <= synced, run.db
    replicas = re.search(
        r"a copy of (\d+) (?:tables?|of them) that CDC", " ".join(r["description"] for r in sync)
    )
    assert replicas and int(replicas.group(1)) == sources["replicated_table_count"], run.db
    for wave in report["facts"]["waves"]:
        if wave["migrating"]:
            kept = {o["table"] for o in wave["kept_in_sync"]}
            assert kept <= synced, (run.db, wave["wave"], kept - synced)


def test_each_sync_risk_states_its_own_evidence_counts(run: PipelineResult) -> None:
    """On every deterministic sample (no schema design, so no ``table_owner_lines`` to
    compare with), a sync risk's stated query and table totals are its own evidence."""
    sync = [r for r in _new(_report(run)) if r["rule"] == "sync.wave"]
    assert sync, run.db
    for r in sync:
        total = re.search(
            r"(\d+) queries read (\d+) tables on an engine that does not own them\.",
            r["description"],
        )
        if total:
            assert int(total.group(1)) == len(r["query_ids"]), (run.db, r["risk_id"])
            assert int(total.group(2)) == len(r["affected_tables"]), (run.db, r["risk_id"])


# The sync risk's sentence for the tables no captured query writes (#2238), as each of
# its two forms ends.
_UNWRITTEN_CDC = (
    "CDC keeps its copy current",
    "CDC keeps their copies current",
)
_UNWRITTEN_STEP = "set up CDC for the table"


def test_an_unwritten_table_a_later_step_reads_is_a_cdc_copy(run: PipelineResult) -> None:
    """#2238: a table no captured query writes that a later step's targets read is a
    replica target fed from a Debezium hub topic in the data-movement plan, as the wave
    plan says ("kept current by CDC"). The sync risk says the same, never "a one-time
    copy", keeps the severity of its CDC pairs (LOW only when it has none), and the
    engineering report, the executive PDF and ``assessment-data.json`` state its words."""
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.plan import build_step_plans, load_inputs

    report = _report(run)
    written = {str(r["table"]) for r in report.get("table_sources") or []}
    sync = [r for r in _new(report) if r["rule"] == "sync.wave"]
    unwritten = {r["risk_id"]: set(r["affected_tables"]) - written for r in sync}
    assert any(unwritten.values()), run.db  # every sample has some since #2166
    inputs = load_inputs(LocalArtifactStore(base_dir=str(run.artifact_root)), run.db, run.job_id)
    plans, hub = build_step_plans(inputs, prices=None)
    assert hub is not None
    topic_of = {t.topic: t.table for t in hub.topics}
    # Every replica target table a Debezium step reads: from a hub topic, or listed as
    # one the hub cannot carry yet (no key, a view), which a written table can be too.
    fed: set[str] = set()
    for plan in plans:
        if plan.cdc.tool != "debezium":
            continue
        topics = {topic_of[t] for t in plan.hub_topics if t in topic_of}
        gaps = {u.table for u in plan.unsupported if u.kind in {"no_key", "view", "unknown_table"}}
        for target in plan.targets:
            if target.role in {"replica", "cache"}:
                assert set(target.tables) <= topics | gaps, (run.db, plan.step)
                fed |= set(target.tables)
    md = run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    pdf = norm(" ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))))
    data = json.loads(run.deliverable("assessment-data.json").read_text())
    stored = {r["risk_id"]: r for r in data["risk_assessment"]["risks"]}
    for r in sync:
        text = f"{r['description']} {r['mitigation']}"
        assert "one-time" not in text and " once" not in text, (run.db, r["risk_id"])
        if not unwritten[r["risk_id"]]:
            continue
        assert unwritten[r["risk_id"]] <= fed, sorted(unwritten[r["risk_id"]] - fed)[:5]
        cdc = next(s for s in _UNWRITTEN_CDC if s in r["description"])
        assert _UNWRITTEN_STEP in r["mitigation"], r["mitigation"]
        pairs = "that CDC keeps current from" in r["description"]
        if "no single source of truth" in r["description"]:
            assert r["severity"] == "HIGH", r["risk_id"]
        else:
            assert r["severity"] == ("MEDIUM" if pairs else "LOW"), r["risk_id"]
        _, body = risk_engine_and_body(r["description"])
        assert md_text(body) in md and md_text(r["mitigation"]) in md, r["risk_id"]
        assert norm(cdc) in pdf and norm(_UNWRITTEN_STEP) in pdf, r["risk_id"]
        assert stored[r["risk_id"]]["description"] == r["description"]
        assert stored[r["risk_id"]]["mitigation"] == r["mitigation"]
    # The same counts on discourse as its wave plan (#2166, #2237): wave 3 reads 134
    # tables nothing writes, all from Aurora PostgreSQL through the hub.
    if run.db == "discourse":
        (risk,) = sync
        assert risk["wave"] == 3 and risk["severity"] == "MEDIUM"
        assert risk["description"].endswith(
            "The other 134 tables are written by no captured query; CDC keeps their copies "
            "current too, and a copy changes only when something outside the captured "
            "workload writes its table."
        ), risk["description"]
        assert _sync_figures(risk["description"])[2] == 134


# One engine pair of a close-call risk: "Aurora MySQL keeps 14 close calls and 5 ties over
# DynamoDB" (#1410).
_PAIR = re.compile(
    r"(?P<engine>.+?) (?:keeps|takes) (?:(?P<close>\d+) close calls?)?(?: and )?"
    r"(?:(?P<ties>\d+) ties?)? over (?P<alternative>.+)"
)


def test_every_deliverable_shows_the_same_risks(run: PipelineResult) -> None:
    report = _report(run)
    risks = _new(report)
    md = run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    pdf = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    data = json.loads(run.deliverable("assessment-data.json").read_text())
    # A 1.41 row carries no tables of its own (``source_tables`` is 1.42): the risk takes
    # what the report lists for those queries (``table_sources``, query groups), a subset.
    backfilled = data["risk_assessment"]["risks"]
    fresh_risks = report["risk_assessment"]["risks"]
    assert [{**r, "affected_tables": None} for r in backfilled] == [
        {**r, "affected_tables": None} for r in fresh_risks
    ]
    for b, f in zip(backfilled, fresh_risks, strict=True):
        assert b["affected_tables"] or _routine(f), b["risk_id"]
        assert set(b["affected_tables"]) <= set(f["affected_tables"])
    for r in risks:
        _, body = risk_engine_and_body(r["description"])
        split = split_close_call_description(body) if r["rule"] == "routing.close_calls" else None
        if split:
            # #1220: the register names the Close calls table, which has each pair's row.
            lead, pairs, tail = split
            pointer = f"{lead}, listed by engine pair under Close calls. {tail}"
            assert f"**{r['risk_id']}**" in md and md_text(pointer) in md, r["risk_id"]
            table = section(md, "Close calls")
            for pair in pairs.split("; "):
                m = _PAIR.fullmatch(pair)
                assert m, pair
                row = f"| {m['engine']} | {m['alternative']} | {m['close'] or 0} |"
                assert row in table, pair
        else:
            assert f"**{r['risk_id']}**" in md and md_text(body) in md, r["risk_id"]
        assert f"`{r['affected_tables'][0]}`" in md, r["risk_id"]
        # Every risk has its card, a low one included (maintainer decision on #950).
        assert norm(r["risk_id"]) in pdf, r["risk_id"]
    # The PDF names no table: the risk cards print their counts, not their tables.
    for r in risks:
        for table in r["affected_tables"]:
            assert norm(table) not in pdf, (r["risk_id"], table)


@pytest.fixture(scope="module")
def designed(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    return run_designed_wordpress(tmp_path_factory.mktemp("risks-designed"), "risks-desi")


@pytest.mark.parametrize(
    "job_name",
    ["designed", pytest.param("designed_pre_1955", marks=pytest.mark.passes_on_main)],
)
def test_the_sync_risk_counts_every_table_read_off_its_owner(
    job_name: str, request: pytest.FixtureRequest
) -> None:
    """Both directions of a migrating engine's copies (the discourse judge read "81"
    plus "37 more" tables against the gate's 70 dual-read and 64 kept-in-sync ones): a
    table a migrating engine owns that another engine reads, or one it reads that another
    engine owns, is in its wave's sync risk, as a CDC pair's copy or an unwritten table's, and the
    risk's own total is the tables it lists. Run on the bundled sample's designed job and
    on the same job on the collection bundled before #1955 (``designed_pre_1955``), whose
    risk has both (#2238: CDC keeps both kinds current)."""
    report = _report(request.getfixturevalue(job_name))
    sync = [r for r in _new(report) if r["rule"] == "sync.wave"]
    synced = {t for r in sync for t in r["affected_tables"]}
    migrating = {e for w in report["facts"]["waves"] if w["migrating"] for e in w["engines"]}
    lines = report["facts"]["table_owner_lines"]["table_lines"]
    read_off_owner = {
        line["table"]
        for line in lines
        if line["also_read_by"] and (migrating & {line["owner"], *line["also_read_by"]})
    }
    assert read_off_owner and read_off_owner <= synced, sorted(read_off_owner - synced)
    # A risk with CDC pairs and unwritten tables states their total, and the total is the
    # tables it lists. Both jobs have both: on the refreshed sample every mapped table's
    # writes are on DynamoDB but one, and #2166 keeps that one, wp_wc_tax_rate_classes,
    # which no query writes, on Aurora MySQL, so DynamoDB reads it as an unwritten copy.
    totals = [
        (
            r,
            re.search(
                r"(\d+) queries read (\d+) tables on an engine that does not own them\.",
                r["description"],
            ),
        )
        for r in sync
    ]
    assert any(m for _, m in totals), [r["description"] for r in sync]
    if job_name != "designed_pre_1955":
        (risk,) = sync
        assert "wordpress.wp_wc_tax_rate_classes" in risk["affected_tables"]
        assert _sync_figures(risk["description"])[2] == 1, risk["description"]
    # The review on #1644: the risk's tables and the facts' count are one computation
    # (``non_owner_read_tables_by_wave``), so the waves' tables add up to it.
    owners = report["facts"]["table_owner_lines"]
    assert sum(len(r["affected_tables"]) for r in sync) == owners["also_read_table_count"]
    for r, m in totals:
        if m:
            # Both counts are the risk's own evidence (the judge read "435 reads" against
            # 531 query ids).
            assert int(m.group(1)) == len(r["query_ids"]), r["risk_id"]
            assert int(m.group(2)) == len(r["affected_tables"]), r["risk_id"]


def test_the_designed_job_adds_the_design_risks(designed: PipelineResult) -> None:
    report = _report(designed)
    risks = _new(report)
    assert [r["rule"] for r in risks] == DESIGNED_RULES
    app = next(r for r in risks if r["rule"] == "design.app_layer")
    assert app["wave"] == 1 and "1 trigger" in app["description"]
    trade_offs = [r for r in risks if r["rule"] == "design.trade_offs"]
    assert all(r["severity"] == "MEDIUM" for r in trade_offs)
    assert "Cache-aside with a short TTL" in [d for r in trade_offs for d in r["details"]]
    # The register is no longer empty on the designed job, which was LOW with no open risk;
    # its HIGH is the routine to-do of the sample's routine set (#1954), a to-do that holds
    # no wave: without it the design risks leave it MEDIUM.
    assert report["risk_assessment"]["overall_risk_level"] == "HIGH"
    assert max(_SEVERITY_RANK[r["severity"]] for r in risks) == _SEVERITY_RANK["MEDIUM"]
    md = designed.deliverable("engineering-report.md").read_text(encoding="utf-8")
    for r in risks:
        assert f"**{r['risk_id']}**" in md, r["risk_id"]
    # A long design text is listed in full in the engineering report and never quoted,
    # clipped or whole, in the executive PDF (the e2e-llm PDF check found "…"). #1410:
    # it is listed once, under the cache design in "Target schemas by engine", which
    # the risk names; the register does not print it again.
    cache = next(r for r in risks if r["rule"] == "cache.unsupported")
    assert any(UNSUPPORTED_CACHE_READ.rstrip(".") in d for d in cache["details"])
    assert "under Target schemas by engine" in cache["description"]
    assert md.count(md_text(UNSUPPORTED_CACHE_READ.rstrip("."))) == 1
    assert md_text(cache["details"][0]) not in md
    pdf = " ".join(pdf_page_texts(str(designed.deliverable("executive-report.pdf"))))
    assert "…" not in pdf and "..." not in pdf
    assert norm(cache["risk_id"]) in pdf and "Lookup by post title" not in pdf


# ---------------------------------------------------------------------------
# Flagged queries no other kept engine takes (#1148)
# ---------------------------------------------------------------------------


def _flagged(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in _new(report) if r["rule"] == "routing.flagged"]


def _flagged_and_held(report: dict[str, Any]) -> int:
    u = report["facts"].get("unservable") or {}
    return int(u.get("flagged_query_count") or 0) + int(u.get("held_query_count") or 0)


def test_the_flagged_risks_agree_with_the_unservable_facts(run: PipelineResult) -> None:
    """On every sample: the ``routing.flagged`` risks cover exactly the flagged and
    held queries ``facts.unservable`` counts (none on the bundled samples)."""
    report = _report(run)
    assert sum(len(r["query_ids"]) for r in _flagged(report)) == _flagged_and_held(report)


FLAG_ENGINE = "dynamodb"


@pytest.fixture(scope="module")
def flagged(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    """The wordpress job with a post-schema router output that keeps one DynamoDB
    query as terminal (no kept engine takes it) and holds another by a pin: the
    two kinds ``flagged_unservable`` lists, which no bundled sample produces."""
    root = tmp_path_factory.mktemp("risks-flagged")
    # #1893: the stored 1.41 report below (OLD_1_41) was written when every call rate
    # assumed a 24-hour window. Its job is rerun the same way, with the statements' first
    # and last seen times and the uptime removed, so the rates, and the routing the two
    # registers rest on, are that job's again (every rate is ``assumed_24h``). #1955
    # refreshed the bundled sample; that job ran on the earlier collection, kept for it in
    # tests/fixtures/wordpress_pre_1955/.
    raw = unzip_pre_1955_wordpress(root / "input")
    text = raw.read_text()
    collection = json.loads(text[text.index("{") :])
    for row in collection["queries"]:
        row.pop("first_seen", None)
        row.pop("last_seen", None)
    (collection.get("global_stats") or {}).pop("uptime", None)
    edited = raw.with_name("wordpress-24h-collection.json")
    edited.write_text(json.dumps(collection))
    result = run_pipeline_from_file(edited, "wordpress", root, "risks-flag")
    store = LocalArtifactStore(base_dir=str(root))
    version = resolve_downstream_assignment_version(store, "wordpress", "risks-flag")
    assignment = store.read_json(f"wordpress/risks-flag/assignment/v{version}/assignment.json")
    ids = sorted(
        str(qa["query_id"])
        for qa in assignment["query_assignments"]
        if qa["assigned_engine"] == FLAG_ENGINE and qa.get("in_scope", True)
    )
    store.write_json(
        "wordpress/risks-flag/post-schema-router/router_output.json",
        {
            "job_id": "risks-flag",
            "terminal": [
                {
                    "query_id": ids[0],
                    "flagged_engine": FLAG_ENGINE,
                    "reason_category": "full_scan",
                    "cause": "no_kept_engine",
                }
            ],
            "held_queries": [ids[1]],
            "held": [
                {
                    "query_id": ids[1],
                    "flagged_engine": FLAG_ENGINE,
                    "reason_category": "client_side_join",
                }
            ],
        },
    )
    common = ["--job-id", "risks-flag", "--db", "wordpress", "--artifact-root", str(root)]
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    _run("report", ["scripts/run_report.py", *common], result)
    return result


def test_a_flagged_and_a_held_query_are_one_high_risk_in_every_deliverable(
    flagged: PipelineResult,
) -> None:
    report = _report(flagged)
    [risk] = _flagged(report)
    assert _flagged_and_held(report) == 2 == len(risk["query_ids"])
    assert risk["severity"] == "HIGH" and report["risk_assessment"]["overall_risk_level"] == "HIGH"
    wave = next(w["wave"] for w in report["migration_waves"] if FLAG_ENGINE in w["engines"])
    assert risk["wave"] == wave and f"Wave {wave} gate" in risk["mitigation"]
    assert risk["affected_tables"]
    assert "change the pin" in risk["mitigation"]
    assert report["facts"]["open_risk_count"] == len(report["risk_assessment"]["risks"])
    md = flagged.deliverable("engineering-report.md").read_text(encoding="utf-8")
    _, body = risk_engine_and_body(risk["description"])
    assert f"**{risk['risk_id']}**" in md and md_text(body) in md
    pdf = " ".join(pdf_page_texts(str(flagged.deliverable("executive-report.pdf"))))
    assert norm(risk["risk_id"]) in pdf
    for table in risk["affected_tables"]:
        assert norm(table) not in pdf, table
    data = json.loads(flagged.deliverable("assessment-data.json").read_text())
    assert data["risk_assessment"] == report["risk_assessment"]


OLD_1_41 = REPO / "tests/unit/report/fixtures/wordpress_1_41_flagged_report.json"
# Rules synthesis raises that the deliverables never backfill into an older report.
NOT_BACKFILLED = frozenset({"design.redesign"})
# #1410 reworded the close-call risk (close calls and settled ties counted apart); the
# 1.41 report keeps the text its synthesis wrote, so that text is compared by its figures.
# #1642 reworded the sync risk (one-time copies counted both ways, with the total first);
# the stored text is compared by its CDC figures, and its one-time copies, counted one
# way only, are at most the new count.
REWORDED = frozenset({"routing.close_calls", "sync.wave"})
# #2166: a fresh synthesis keeps a table nothing writes on the relational step, so its
# sync risk also names the reads other engines make of such tables; the 1.41 report's
# own waves gave them to their main reader. Its reads are compared as a subset.
RULE_CHANGED_READS = frozenset({"sync.wave"})


def _as_stored(risk: dict[str, Any]) -> dict[str, Any]:
    out = {**risk, "affected_tables": None, "risk_id": None}
    if risk.get("rule") in RULE_CHANGED_READS:
        out["query_ids"] = None
    return {**out, "description": None} if risk.get("rule") in REWORDED else out


def _close_call_figures(text: str) -> tuple[int, int]:
    """(queries decided, settled ties) in a close-call risk, worded either way."""
    old = re.search(r"(\d+) queries were decided by less than \d+ points \((\d+) exact", text)
    if old:
        return int(old.group(1)), int(old.group(2))
    new = re.search(r"(\d+) queries were decided by less than \d+ points, and (\d+) more", text)
    assert new, text
    return int(new.group(1)) + int(new.group(2)), int(new.group(2))


def _sync_figures(text: str) -> tuple[int, int, int]:
    """(reads on a CDC copy, CDC-copied tables, unwritten tables) in a sync risk, worded
    any way it has been ("52 reads run on a copy of 13 tables", "52 of them read a copy
    of 13 tables")."""
    cdc = re.search(
        r"(\d+) (?:reads? runs? on|of them reads?) a copy of (\d+) (?:tables?|of them) that CDC",
        text,
    )
    assert cdc, text
    once = re.search(
        r"(\d+) more tables? (?:is|are) read|The other (\d+)(?: tables)? are|The other table",
        text,
    )
    copies = 0 if not once else int(once.group(1) or once.group(2) or 1)
    return int(cdc.group(1)), int(cdc.group(2)), copies


def test_a_report_from_before_the_rule_renders_the_same_risks(
    flagged: PipelineResult, tmp_path: Path
) -> None:
    """A real report main's code wrote before ``routing.flagged`` (contract 1.41, the
    same wordpress job and router output as ``flagged``) renders the register a new
    synthesis writes, and every deliverable states one risk count and level: the
    summary's risk sentence, the risk posture and the executive report's tiles."""
    report = _report(flagged)
    root = tmp_path / "artifacts"
    key = root / "wordpress/probe-flag/synthesis/v2/report.json"
    key.parent.mkdir(parents=True)
    shutil.copyfile(OLD_1_41, key)
    old = json.loads(key.read_text())
    assert not [r for r in old["risk_assessment"]["risks"] if r.get("rule") == "routing.flagged"]
    again = PipelineResult(db="wordpress", job_id="probe-flag", artifact_root=root)
    common = ["--job-id", "probe-flag", "--db", "wordpress", "--artifact-root", str(root)]
    _run("report", ["scripts/run_report.py", *common], again)
    data = json.loads(again.deliverable("assessment-data.json").read_text())
    # A 1.41 row carries no tables of its own (``source_tables`` is 1.42): the risk takes
    # what the report lists for those queries (``table_sources``, query groups), a subset.
    backfilled = data["risk_assessment"]["risks"]
    # A rule only synthesis raises, from an artifact a 1.41 report never read, is not
    # backfilled: #1231's design.redesign (the assignment's access-pattern record). The
    # old register is the fresh one without it, each risk renumbered in order.
    fresh_risks = [
        r for r in report["risk_assessment"]["risks"] if r.get("rule") not in NOT_BACKFILLED
    ]
    # #297: a broad gap's mitigation also names the tables to start with; a stored
    # report's text, written before it, ends at the run-the-design sentence.
    fresh_risks = [
        {**r, "mitigation": str(r["mitigation"]).split(" Start with the tables")[0]}
        for r in fresh_risks
    ]
    # #1510: a fresh mitigation points at its wave's gate; a stored one, written before,
    # still states it. Those two are compared by the wave they name; every other text,
    # the backfilled rule's included, must match as written.
    assert len(backfilled) == len(fresh_risks)
    reworded = {
        i
        for i, (b, f) in enumerate(zip(backfilled, fresh_risks, strict=True))
        if f.get("rule") not in (None, "routing.flagged")
        and "in the roadmap." in str(f["mitigation"])
    }
    for i in reworded:
        b, f = backfilled[i], fresh_risks[i]
        assert f"Wave {f['wave']}" in b["mitigation"] and b["wave"] == f["wave"], b["risk_id"]
    backfilled = [
        {**r, "mitigation": None} if i in reworded else r for i, r in enumerate(backfilled)
    ]
    fresh_risks = [
        {**r, "mitigation": None} if i in reworded else r for i, r in enumerate(fresh_risks)
    ]
    assert [_as_stored(r) for r in backfilled] == [_as_stored(r) for r in fresh_risks]
    for b, f in zip(backfilled, fresh_risks, strict=True):
        if b.get("rule") == "routing.close_calls":
            assert _close_call_figures(b["description"]) == _close_call_figures(f["description"])
        if b.get("rule") == "sync.wave":
            assert set(b["query_ids"]) <= set(f["query_ids"]), b["risk_id"]
            stored, fresh = _sync_figures(b["description"]), _sync_figures(f["description"])
            assert stored[:2] == fresh[:2] and stored[2] <= fresh[2], (stored, fresh)
    assert [r["risk_id"] for r in backfilled] == [
        f"RISK-{i:03d}" for i in range(1, len(backfilled) + 1)
    ]
    for b, f in zip(backfilled, fresh_risks, strict=True):
        assert b["affected_tables"] or _routine(f), b["risk_id"]
        assert set(b["affected_tables"]) <= set(f["affected_tables"])
    assert data["risk_assessment"]["overall_risk_level"] == "HIGH"
    n = len(backfilled)
    sentence = f"{n} open migration risks (overall: HIGH)"
    assert sentence in data["summary"]
    fresh_n = len(report["risk_assessment"]["risks"])
    assert f"{fresh_n} open migration risks (overall: HIGH)" in report["summary_deterministic"]
    # #1403: a new report's executive summary states the same level.
    assert "Risk: overall risk is high." in report["summary"]
    md = again.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert md_text(sentence) in md and "overall: MEDIUM" not in md
    for r in backfilled:
        assert f"**{r['risk_id']}**" in md
    assert "Overall risk: HIGH" in md
    pdf = " ".join(pdf_page_texts(str(again.deliverable("executive-report.pdf"))))
    fresh_pdf = " ".join(pdf_page_texts(str(flagged.deliverable("executive-report.pdf"))))
    for risk in backfilled:
        assert norm(risk["risk_id"]) in pdf
    assert f"{n} Open risks" in _pdf_risk_tiles(pdf)
    assert f"{fresh_n} Open risks" in _pdf_risk_tiles(fresh_pdf)


def _pdf_risk_tiles(pdf: str) -> str:
    """The executive report's risk page head: the overall level and the open tile."""
    m = re.search(r"Overall risk.{0,200}", norm(pdf))
    assert m, "no risk tiles"
    return m.group(0)


# ---------------------------------------------------------------------------
# The relational move's gaps (#1149)
# ---------------------------------------------------------------------------


def test_the_gap_risks_agree_with_the_relational_gap_facts(run: PipelineResult) -> None:
    """On every sample: one ``relational.gaps`` risk per non-informational impact of
    ``facts.relational_gaps``, in Wave 1, listing exactly those gaps, and only when
    the gap check's target is wave 1's engine."""
    report = _report(run)
    gaps = report["facts"].get("relational_gaps") or {}
    wave1 = next(w for w in report["migration_waves"] if w["wave"] == 1)
    raised = [r for r in _new(report) if r["rule"] == "relational.gaps"]
    if gaps.get("target_engine") != wave1["engines"][0]:
        assert raised == [], run.db
        return
    severity = {"blocks_move": "HIGH", "needs_rewrite": "MEDIUM", "behaves_differently": "MEDIUM"}
    want = [
        (severity[impact], n)
        for impact, n in gaps["gaps_by_impact"].items()
        if impact in severity and n
    ]
    assert [(r["severity"], len(r["details"])) for r in raised] == want, run.db
    md = run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    pdf = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    for r in raised:
        assert r["wave"] == 1 and "Wave 1 gate" in r["mitigation"]
        for detail in r["details"]:
            assert md_text(detail) in md, detail[:60]
        # The executive report states the risk and points to the details, never a gap's
        # reason or documentation; the engineering report prints the details instead.
        assert norm(r["risk_id"]) in pdf
        assert norm(RISK_DETAILS_POINTER) in pdf and RISK_DETAILS_POINTER not in md
        for g in gaps["gaps"]:
            if g["impact"] in severity:
                assert norm(g["reason"]) not in pdf, g["rule_id"]


GENERIC_MITIGATION = "to an engine that serves"


def test_no_risk_is_broad_and_generic(run: PipelineResult) -> None:
    """#297: a risk over more than BROAD_FLAGGED_QUERIES queries never carries the
    "route them to an engine that serves them" advice, and every risk names a
    table and a mitigation."""
    from src.agents.referee.synthesis_report import BROAD_FLAGGED_QUERIES

    for r in _report(run)["risk_assessment"]["risks"]:
        assert (r["affected_tables"] or _routine(r)) and r["mitigation"], r["risk_id"]
        if r.get("rule"):
            continue
        broad = len(r.get("query_ids") or []) > BROAD_FLAGGED_QUERIES
        if broad:
            assert GENERIC_MITIGATION not in r["mitigation"] or "Start with" in r["mitigation"], r[
                "risk_id"
            ]
            assert r.get("coverage_gap") or "Flagged by" not in r["description"], r["risk_id"]
