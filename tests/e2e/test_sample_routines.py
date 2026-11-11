# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The bundled samples carry routines end to end (#1954, ADR-034).

Each MySQL and PostgreSQL sample has a small synthetic routine set next to its zip
(``docs/examples/<sample>/<sample>-routines.json``, the sections the collection script
writes), which the deterministic pipeline adds to the collection
(``tests.e2e.pipeline.add_sample_routines``). Its run then shows every routine outcome:
carried as it is, rewritten (the rewrite recorded, its diff the header only), and one
routine that can't be carried, which is the HIGH "migrate it yourself before wave 1"
to-do that holds no wave. Each outcome is in ``report.json``'s facts, the engineering
report and the results page's data (the page itself: ``tests/e2e/test_ui.py``).
Routing is untouched: the routing snapshot guard runs on the same runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from src.agents.collector.routine_sources import read_routine_sources
from src.shared.routine_facts import ROUTINE_DETAILS_LISTED
from src.storage.local_store import LocalArtifactStore
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
from src.tools.routines.postgresql_checks import PostgresRoutine, check_postgresql_routine
from src.tools.routines.rewrites import rewrite_mysql_routine, rewrite_postgresql_routine
from tests.e2e.deliverable_figures import md_plain
from tests.e2e.pipeline import PipelineResult, sample_routines

pytestmark = [pytest.mark.e2e, pytest.mark.deterministic]

# (kind, routine id) -> the status each synthetic routine gets.
EXPECTED: dict[str, dict[tuple[str, str], str]] = {
    "wordpress": {
        ("function", "wordpress.wp_post_comment_count"): "as_is",
        ("procedure", "wordpress.wp_refresh_comment_count"): "rewritten",
        ("procedure", "wordpress.wp_bulk_import_mode"): "needs_attention",
        ("trigger", "wordpress.wp_comments_count_after_insert"): "as_is",
        ("event", "wordpress.wp_purge_action_logs"): "as_is",
    },
    "discourse": {
        ("function", "public.topic_visible_post_count(integer)"): "as_is",
        ("procedure", "public.prune_read_notifications(integer)"): "as_is",
        ("function", "public.posts_bump_topic()"): "rewritten",
        ("trigger", "public.posts.posts_bump_topic"): "as_is",
        ("procedure", "public.begin_upload_snapshot(text)"): "needs_attention",
    },
}
# The rewrite each sample's rewritten routine gets, and the check that keeps the other
# from being carried.
REWRITE = {
    "wordpress": "mysql-routine-definer-to-target-user",
    "discourse": "postgresql-routine-owner-to-target-user",
}
FAILED = {"wordpress": "mysql-routine-set-global", "discourse": "postgresql-routine-backup-control"}


@pytest.fixture(params=sorted(EXPECTED))
def sample_run(request: pytest.FixtureRequest) -> PipelineResult:
    run: PipelineResult = request.getfixturevalue(f"{request.param}_run")
    return run


def _migration(run: PipelineResult) -> dict[str, Any]:
    [path] = sorted((run.job_dir() / "routines").glob("v*/routine_migration.json"))
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _report(run: PipelineResult) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(run.report_json().read_text())
    return data


def _of(rows: list[dict[str, Any]], kind: str, routine_id: str) -> dict[str, Any]:
    [row] = [r for r in rows if (r["kind"], r["routine_id"]) == (kind, routine_id)]
    return row


def _key(sample: str, status: str) -> tuple[str, str]:
    [key] = [k for k, s in EXPECTED[sample].items() if s == status]
    return key


def test_every_sample_with_a_routine_set_is_checked_here() -> None:
    assert {p for p in EXPECTED if sample_routines(p)} == set(EXPECTED)
    assert sample_routines("trailgear") is None


def test_each_synthetic_routine_gets_its_outcome(sample_run: PipelineResult) -> None:
    sample = sample_run.db
    migration = _migration(sample_run)
    assert migration["routine_sources"] is True
    assert sample_run.steps["routine_migration"]["artifact"]
    for (kind, rid), status in EXPECTED[sample].items():
        row = _of(migration["routines"], kind, rid)
        assert row["status"] == status, (kind, rid, row)
        rewrites = [w["rule_id"] for w in row["rewrites"]]
        if status == "as_is":
            assert rewrites == [] and row["reasons"] == [], (kind, rid, row)
        elif status == "rewritten":
            assert rewrites == [REWRITE[sample]] and row["reasons"] == [], (kind, rid, row)
        else:
            assert row["reasons"] == [FAILED[sample]], (kind, rid, row)
            failed = [c["rule_id"] for c in row["checks"] if c["outcome"] == "fail"]
            assert failed == [FAILED[sample]], (kind, rid, row)


def _rewrite_record(run: PipelineResult, migration: dict[str, Any]) -> Any:
    """The rewritten routine's rewrite, made again from the job's routine sources the way
    the phase makes it: the artifact keeps the rule, never a diff (ADR-034 Decision 4)."""
    kind, rid = _key(run.db, "rewritten")
    store = LocalArtifactStore(base_dir=str(run.artifact_root))
    [source] = [
        s
        for s in read_routine_sources(store, run.db, run.job_id)
        if s.kind == kind and rid.startswith(f"{s.schema_name}.{s.routine_id}")
    ]
    raw = json.loads(sample_routines(run.db).read_text())  # type: ignore[union-attr]
    [row] = [p for p in raw["procedures"] if p["routine_name"] == source.routine_id]
    if run.db == "wordpress":
        routine = MySqlRoutine(
            routine_id=rid, kind=cast(Any, kind), create_sql=source.create_statement,
            definer=row["definer"], sql_mode=row["sql_mode"], security_type="DEFINER",
        )  # fmt: skip
        verdict = check_mysql_routine(
            routine, target_engine=migration["target_engine"],
            target_line=migration["target_line"], target_user=migration["target_user"],
            source_version=migration["source_version"],
        )  # fmt: skip
        return rewrite_mysql_routine(routine, verdict, target_user=migration["target_user"])
    pg = PostgresRoutine(
        routine_id=f"{kind}:{rid}", kind=cast(Any, kind), create_sql=source.create_statement,
        language=row["language"], owner=row["owner"], owner_is_superuser=False,
        security_definer=row["security_definer"],
    )  # fmt: skip
    verdict = check_postgresql_routine(
        pg, target_engine=migration["target_engine"], target_line=migration["target_line"],
        source_version=migration["source_version"],
    )  # fmt: skip
    return rewrite_postgresql_routine(
        pg, verdict, target_owner=migration["target_user"], search_path=("public",)
    )


def test_the_rewrite_is_recorded_with_a_header_only_diff(sample_run: PipelineResult) -> None:
    migration = _migration(sample_run)
    kind, rid = _key(sample_run.db, "rewritten")
    done = _rewrite_record(sample_run, migration)
    assert done.record.status == "rewritten"
    stored = [w["rule_id"] for w in _of(migration["routines"], kind, rid)["rewrites"]]
    assert [w.rule_id for w in done.record.rewrites] == stored == [REWRITE[sample_run.db]]
    [rewrite] = done.record.rewrites
    changed = [
        ln for ln in rewrite.diff.splitlines()
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]  # fmt: skip
    old, new = ("wp_user", "admin") if sample_run.db == "wordpress" else ("discourse", "postgres")
    assert len(changed) == 2, rewrite.diff
    assert changed[0].startswith("-") and old in changed[0], rewrite.diff
    assert changed[1].startswith("+") and new in changed[1] and old not in changed[1]
    # The header only: no line of the body is in the diff, and the text keeps its body.
    body = "UPDATE wp_posts" if sample_run.db == "wordpress" else "UPDATE public.topics"
    assert body not in rewrite.diff and body in done.target_create


def test_the_facts_count_each_outcome(sample_run: PipelineResult) -> None:
    sample = sample_run.db
    migration = _migration(sample_run)
    facts = _report(sample_run)["facts"]["routines"]
    assert facts["routine_count"] == migration["total"]
    assert facts["as_is_count"] == migration["counts"]["as_is"]
    assert facts["rewritten_count"] == migration["counts"]["rewritten"]
    assert facts["as_is_count"] >= 3 and facts["rewritten_count"] >= 1
    assert facts["proof"] == "checked, not load-tested"
    for (kind, rid), status in EXPECTED[sample].items():
        rows = [r for r in facts["routines"] if (r["kind"], r["routine_id"]) == (kind, rid)]
        if status == "as_is" and not rows:
            continue  # carried as it is with every check run: in no risk, so in no row
        [row] = rows
        assert row["status"] == status, (kind, rid)
        if status == "needs_attention":
            assert (row["risk_classes"], row["severity"]) == (["migrate_yourself"], "HIGH")
            assert row["severity_basis"] == [FAILED[sample]] and row["listed"] is True
            assert {"kind": kind, "routine_id": rid} in facts["to_do_routines"]
        elif status == "rewritten":
            assert row["risk_classes"] == ["carried_review"] and row["listed"] is False
        else:  # a check that could not run: one of the carried routines to check
            assert row["risk_classes"] == ["carried_review"] and row["listed"] is False
    [note] = [n for n in facts["check_notes"] if n["note"] == "rewrite"]
    assert note["rule_id"] == REWRITE[sample]
    assert {"kind": _key(sample, "rewritten")[0], "routine_id": _key(sample, "rewritten")[1]} in (
        note["routines"]
    )


def test_the_routine_that_cannot_be_carried_is_a_high_to_do_that_holds_no_wave(
    sample_run: PipelineResult,
) -> None:
    report = _report(sample_run)
    facts = report["facts"]["routines"]
    n = facts["migrate_yourself_count"]
    them = "it" if n == 1 else "them"
    assert facts["wave"] == 1
    assert facts["migrate_yourself_line"] == (
        f"we could not translate {n} routine{'' if n == 1 else 's'} automatically; "
        f"migrate {them} yourself before wave 1"
    )
    risks = {r["rule"]: r for r in report["risk_assessment"]["risks"] if r.get("rule")}
    to_do = risks["routines.migrate_yourself"]
    assert (to_do["severity"], to_do["wave"]) == ("HIGH", 1)
    assert risks["routines.carried"]["severity"] == "LOW"
    kind, rid = _key(sample_run.db, "needs_attention")
    if n <= ROUTINE_DETAILS_LISTED:  # the details name the first ones, then how many more
        assert f"{kind} {rid}: {FAILED[sample_run.db]}" in to_do["details"]
    wave_1 = report["facts"]["waves"][0]
    assert facts["migrate_yourself_line"] in wave_1["to_dos"]
    assert to_do["risk_id"] not in {h.get("risk_id") for h in wave_1["holds"]}
    gate = report["migration_waves"][0]["gate"]
    assert f"({to_do['risk_id']})." in gate and "To do before wave 1:" in gate
    assert f"Close {to_do['risk_id']}" not in gate


def _section(md: str) -> str:
    start = md.index("## Routines")
    nxt = md.find("\n## ", start + 1)
    return md[start : nxt if nxt != -1 else len(md)]


def test_the_engineering_report_states_each_outcome(sample_run: PipelineResult) -> None:
    facts = _report(sample_run)["facts"]["routines"]
    md = sample_run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    section = md_plain(_section(md))
    assert facts["line"] in section
    to_do = facts["migrate_yourself_line"]
    assert f"{to_do[0].upper()}{to_do[1:]}." in section
    kind, rid = _key(sample_run.db, "needs_attention")
    assert f"- {kind} `{rid}`" in section
    [row] = [ln for ln in section.splitlines() if ln.startswith(f"| `{rid}` |")]
    assert f"| {kind} | Not carried: needs repair |" in row
    notes = section[section.index("### Rewrites and checks that could not run") :]
    [note] = [n for n in facts["check_notes"] if n["note"] == "rewrite"]
    assert f"{note['label']} ({note['note_count']}): " in notes
    assert f"`{_key(sample_run.db, 'rewritten')[1]}`" in notes


def test_no_routine_text_reaches_report_json_or_the_deliverables(
    sample_run: PipelineResult,
) -> None:
    """The routine text stays in the routine sources (ADR-034 Decision 4)."""
    texts = [sample_run.report_json().read_text()]
    deliverables = Path(sample_run.report["deliverables_dir"])
    texts += [p.read_text(encoding="utf-8") for p in sorted(deliverables.glob("*.[jm][sd]*"))]
    bodies = (
        "innodb_flush_log_at_trx_commit",
        "COUNT(*) INTO n",
        "pg_backup_start(p_",
        "bumped_at <",
    )
    for body in bodies:
        assert not any(body in t for t in texts), body
