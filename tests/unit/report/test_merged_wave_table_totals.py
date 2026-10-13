"""#568 review of #562 (Important 2): the merged DynamoDB/DocumentDB wave's
``table_count`` is the union of both engines' served tables; crediting that
union to every engine in ``engines`` double counted.

On the discourse ``--llm-mode none`` run, wave 3 (merged) served 200 tables
(118 DynamoDB, 82 DocumentDB, no overlap). Before this fix,
``_architecture_engines`` added the wave's whole ``table_count`` (200) to
*each* engine, so the decision report, deck and PDF all showed "DynamoDB 200
source tables" and "DocumentDB 200 source tables", and the "Tables migrate"
tile summed to 400 against "Analyzed 311 source tables" -- a deliverable
total that exceeded the number of tables that exist.

``MigrationWave.table_counts_by_engine``/``owned_counts_by_engine`` (#568
review) now carry each engine's own served and owned counts for a
multi-engine wave, so ``_architecture_engines`` can credit each engine only
its own share.

Fixture: ``discourse_merged_wave_report.json``, the real synthesis report
from ``uv run python scripts/run_assessment.py --all -y --llm-mode none`` on
the bundled discourse sample (post-#568-review code), with ``query_groups``
emptied and ``risk_assessment.risks`` truncated to keep the fixture small --
neither is read by anything this test exercises.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.report import pptx_report, renderers

FIXTURE = Path(__file__).parent / "fixtures" / "discourse_merged_wave_report.json"


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def test_fixture_has_a_merged_wave_with_no_table_overlap(report: dict[str, Any]) -> None:
    merged = next(w for w in report["migration_waves"] if len(w["engines"]) > 1)
    assert merged["engines"] == ["dynamodb", "documentdb"]
    assert merged["table_count"] == 200
    assert merged["table_counts_by_engine"] == {"dynamodb": 118, "documentdb": 82}
    assert merged["owned_counts_by_engine"] == {"dynamodb": 108, "documentdb": 73}


def test_architecture_engines_credits_each_engine_its_own_share(report: dict[str, Any]) -> None:
    rows = {e["engine"]: e for e in renderers._architecture_engines(report)}
    # Scope shows each engine's own SERVED count -- never the union (200/200).
    assert rows["dynamodb"]["scope"] == "118 source tables"
    assert rows["documentdb"]["scope"] == "82 source tables"
    # "Tables migrate" counts only the OWNED (physically moving) subset --
    # a served-but-not-owned table stays on its real owner, kept in sync.
    assert rows["dynamodb"]["migrates"] == 108
    assert rows["documentdb"]["migrates"] == 73


def test_no_deliverable_total_exceeds_the_source_table_count(report: dict[str, Any]) -> None:
    """The explicit #568 review ask: no deliverable's aggregate table total
    may exceed the number of source tables that actually exist."""
    source_total = report["source_table_count"]
    assert source_total == 311

    rows = renderers._architecture_engines(report)
    migrated_total = sum(e["migrates"] for e in rows if e["role"] == "Migration target")
    assert migrated_total <= source_total
    assert migrated_total == 181  # 108 (dynamodb) + 73 (documentdb), the real owned split

    html = renderers.render_decision_report_html(report)
    tile_n = int(re.search(r"<p>Tables migrate</p><p class=val>(\d+)</p>", html).group(1))
    assert tile_n <= source_total
    assert tile_n == migrated_total

    facts = pptx_report.derive(report, {})
    assert facts["migrated"] <= source_total
    assert facts["migrated"] == migrated_total

    md = renderers.render_engineering_report_md(report)
    # The engineering report's own per-engine Scope-equivalent (the roadmap
    # wave card) must also never claim more tables than exist.
    for m in re.finditer(r"(\d+) source tables?:", md):
        assert int(m.group(1)) <= source_total


def test_decision_report_and_deck_agree_on_the_split(report: dict[str, Any]) -> None:
    """The two per-engine Scope cells, read off the rendered decision report
    HTML, must match the deck's own per-engine figures -- the
    "deliverables agree" invariant, specifically for the merged wave."""
    html = renderers.render_decision_report_html(report)
    # #572: the badge now shows the display name, not the raw engine id.
    dynamo_row = re.search(
        r"<span class=badge[^>]*>DynamoDB</span></td><td class=role>Migration target</td>"
        r"<td data-figure=workload>[\d.]+%</td><td data-figure=scope>(\d+) source tables?</td>",
        html,
    )
    doc_row = re.search(
        r"<span class=badge[^>]*>DocumentDB</span></td><td class=role>Migration target</td>"
        r"<td data-figure=workload>[\d.]+%</td><td data-figure=scope>(\d+) source tables?</td>",
        html,
    )
    assert dynamo_row is not None, html
    assert doc_row is not None, html
    assert int(dynamo_row.group(1)) == 118
    assert int(doc_row.group(1)) == 82

    facts = pptx_report.derive(report, {})
    deck_rows = {e["engine"]: e for e in facts["engines"]}
    assert deck_rows["dynamodb"]["scope"] == "118 source tables"
    assert deck_rows["documentdb"]["scope"] == "82 source tables"


def test_deck_card_shows_the_owned_vs_kept_in_sync_split(report: dict[str, Any]) -> None:
    """#568 review round 2 (Important 1): the deck/PDF lost the owned vs.
    kept-in-sync split because the gate text that carries it is clipped to
    90 characters by ``_wave_card_text``. The split must be a structured
    line the card never truncates. Rendered end to end (``slide_sequencing``
    on a real deck, reading actual shape text), not just checked at the
    ``derive()``/field level, because the round-1 claim that the deck
    already showed this split was never actually verified by rendering --
    and it did not.
    """
    facts = pptx_report.derive(report, {})
    merged = next(w for w in facts["waves"] if w["names"] == "DynamoDB + DocumentDB")
    assert merged["sync_summary"] == (
        "DynamoDB moves 108 tables; DocumentDB moves 73 tables. "
        "Kept in sync from Aurora PostgreSQL: 19 tables."
    )

    prs = pptx_report.open_deck()
    slide = pptx_report.slide_sequencing(prs, facts)
    rendered = [
        "".join(run.text for run in para.runs)
        for shape in slide.shapes
        if shape.has_text_frame
        for para in shape.text_frame.paragraphs
    ]
    rendered = [t for t in rendered if t]
    assert any("DynamoDB moves 108 tables" in t for t in rendered), rendered
    assert any("DocumentDB moves 73 tables" in t for t in rendered), rendered
    assert any("Kept in sync from Aurora PostgreSQL: 19 tables" in t for t in rendered), rendered
    # The gate's own clipped prose never has to carry the split alone, but
    # it is still present alongside the new structured line.
    assert any(t.startswith("Gate: DynamoDB:") for t in rendered), rendered
