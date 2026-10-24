"""The 14 maintenance writes #1359 found carried onto DynamoDB, on the bundled samples (#1362).

Their write groups keep them on DynamoDB (ADR-031 Decision 1 Amendment 9; the maintainer
kept that routing, #1359 option (c)). The DynamoDB designer is told each one's intent and
the target-native approach, the risk register raises one ``design.carried_writes`` risk
per table, and every deliverable shows the same risks from ``report.json`` (ADR-032).
Deterministic pipeline only; no model call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agents.schema_design import handler
from src.report.escaping import md_text
from src.shared.design_text import split_carried_write_detail
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

RETENTION = "Retention delete"
ORPHAN = "Orphan clean-up"
REPAIR = "Consistency repair sweep"

# query id -> (table, intent name), as the classifier reads them from the parsed facts.
EXPECTED = {
    "discourse": {
        "-805676668594973927": ("discourse.email_tokens", RETENTION),
        "5139002051034385017": ("discourse.scheduler_stats", RETENTION),
        "3022359024859565856": ("discourse.search_logs", RETENTION),
        "-5145007055741204478": ("discourse.skipped_email_logs", RETENTION),
        "-6072012279523579697": ("discourse.stylesheet_cache", RETENTION),
        "-1857846980601328420": ("discourse.topic_search_data", ORPHAN),
        "9113575919552827162": ("discourse.unsubscribe_keys", RETENTION),
        "-2784383380355662666": ("discourse.user_actions", "Duplicate clean-up"),
        "-8208700262275714237": ("discourse.user_actions", REPAIR),
        "3015126576501116248": ("discourse.user_actions", "Delete by any of several references"),
        "2539559289466738950": ("discourse.user_profiles", REPAIR),
        "2150880814507944816": ("discourse.users", REPAIR),
        "8321624443266976557": ("discourse.users", ORPHAN),
    },
    "trailgear": {"0x9D11480F1E01B771": ("trailgear.UserSession", RETENTION)},
}


def _report(r: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(r.report_json().read_text())
    return report


def _carried_risks(r: PipelineResult) -> list[dict[str, Any]]:
    return [
        x
        for x in _report(r)["risk_assessment"]["risks"]
        if x.get("rule") == "design.carried_writes"
    ]


@pytest.fixture(params=["discourse", "trailgear"])
def sample(
    request: pytest.FixtureRequest, discourse_run: PipelineResult, trailgear_run: PipelineResult
) -> PipelineResult:
    runs: dict[str, PipelineResult] = {"discourse": discourse_run, "trailgear": trailgear_run}
    return runs[str(request.param)]


def test_the_risk_register_lists_each_carried_write_with_its_intent(sample):
    """One DynamoDB risk: its description counts the statements by intent, and each
    statement's kind, table, intent and approach is one detail line."""
    expected = EXPECTED[sample.db]
    (risk,) = _carried_risks(sample)
    assert risk["severity"] == "MEDIUM" and risk["description"].startswith("[dynamodb] ")
    assert sorted(risk["query_ids"]) == sorted(expected)
    assert risk["affected_tables"] == sorted({t for t, _ in expected.values()})
    k = len(expected)
    assert f"DynamoDB serves {k} statement{'s' if k > 1 else ''} on " in risk["description"]
    names = [n.lower() for _, n in expected.values()]
    for name in set(names):
        assert f"{name} ({names.count(name)})" in risk["description"], name
    assert len(risk["details"]) == k
    by_line = sorted(
        ((t.rsplit(".", 1)[-1], n.lower()) for t, n in expected.values()),
    )
    found = sorted(
        (line.split(" of ", 1)[1].split(", ", 1)[0], line.split(", ", 1)[1].split(" (score")[0])
        for line in risk["details"]
    )
    assert found == by_line
    assert all(line.startswith(("DELETE of ", "UPDATE of ")) for line in risk["details"])
    retention = [d for d in risk["details"] if "retention delete" in d]
    assert retention and all("Time to Live" in d for d in retention)


def test_the_summary_input_counts_the_details(sample):
    """The executive-summary request gets the risk's detail count, not its long list;
    every other risk keeps its details (#1362)."""
    from src.agents.referee.summary_input_slim import slim_summary_input

    report = _report(sample)
    out = slim_summary_input({"risk_assessment": report["risk_assessment"]})
    for before, after in zip(
        report["risk_assessment"]["risks"], out["risk_assessment"]["risks"], strict=True
    ):
        if before.get("rule") == "design.carried_writes":
            assert after["detail_count"] == len(before["details"]) and "details" not in after
        else:
            assert after.get("details") == before.get("details"), before.get("risk_id")


def test_the_dynamodb_designer_is_given_the_same_statements(sample):
    store = LocalArtifactStore(str(sample.artifact_root))
    analysis = handler._design_analysis_output(store, sample.db, sample.job_id, "dynamodb", 2)
    carried = {c["query_id"]: (c["table_id"], c["intent_name"]) for c in analysis["carried_writes"]}
    assert carried == EXPECTED[sample.db]
    assert all(c["approach"] and c["score"] < c["floor"] for c in analysis["carried_writes"])
    # No other engine serves a carried write on these samples.
    for engine in ("aurora_postgresql", "documentdb"):
        other = handler._design_analysis_output(store, sample.db, sample.job_id, engine, 2)
        assert not other.get("carried_writes"), engine


def test_the_engineering_report_shows_each_statements_approach(sample):
    md = sample.deliverable("engineering-report.md").read_text(encoding="utf-8")
    (risk,) = _carried_risks(sample)
    for line in risk["details"]:
        # Statements that share an approach are listed under it once (#1454).
        lead, approach = split_carried_write_detail(line)
        assert md_text(lead) in md, line
        assert md_text(approach) in md, line


def test_each_sample_carries_exactly_its_statements(run):
    """wordpress carries none; discourse 13 and trailgear 1 (the 14 of #1359)."""
    found = {q for r in _carried_risks(run) for q in r["query_ids"]}
    assert found == set(EXPECTED.get(run.db, {})), run.db
