# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An installed extension a gap names reaches the deliverables, escaped (#1078).

A PostgreSQL source moves to Aurora PostgreSQL in wave 1, so its gaps are stated in the
Risk register (``relational.gaps``, one risk per impact) with each gap's objects in its
details; the engineering report prints those details. An extension's name is collected
text, so it is escaped like any other: a name holding Markdown link syntax stays text.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.report import deliverables as dl
from src.report.roadmap import _evidence_md
from src.shared.migration_risk_rules import relational_gap_risks
from src.shared.report_facts import compute_report_facts
from src.storage.local_store import LocalArtifactStore
from src.tools.analysis.relational_gaps import detect_relational_gaps

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "e2e09_report.json"
DB, JOB = "discourse", "job-1078"
KEY = f"{DB}/{JOB}/synthesis/v1/report.json"
WAVES = [{"wave": 1, "engines": ["aurora_postgresql"], "tables": ["db.t"], "table_count": 1}]
# Collected text, as hostile as the contract lets it be.
HOSTILE = "evil](http://x.example)`*"


def _gaps(version: str = "14.12") -> dict:
    collector = {
        "metadata": {
            "source_database": {"engine": "postgresql", "version": version},
            "extensions": [
                {"name": "acme_geo", "version": "2.1"},
                {"name": HOSTILE, "version": "1.0"},
                {"name": "roaringbitmap", "version": "0.5.4"},
            ],
        },
        "database_schema": {"tables": []},
        "queries": {"query_patterns": []},
    }
    gaps = detect_relational_gaps(collector, "aurora_postgresql")
    assert gaps is not None
    return gaps


def test_the_risk_details_name_each_extension_and_the_line_that_offers_it() -> None:
    details = [d for r in relational_gap_risks(_gaps(), WAVES) for d in r["details"]]
    [blocked] = [d for d in details if d.startswith("Installed extensions Aurora PostgreSQL")]
    # No line offers all three, so each is checked against the source's own line, 14.
    assert blocked.endswith(
        f"Found in extension acme_geo, extension {HOSTILE}, extension roaringbitmap."
    )


def test_a_later_line_is_named_when_every_extension_is_offered_there() -> None:
    collector_gaps = _gaps()
    # acme_geo is on no line, so no line offers all three: each is flagged on line 14.
    assert "postgresql-extension-on-a-later-version" not in {
        g["rule_id"] for g in collector_gaps["gaps"]
    }
    later = {
        "rule_id": "postgresql-extension-on-a-later-version",
        "feature": "Installed extensions offered only from a later Aurora PostgreSQL version",
        "impact": "behaves_differently",
        "reason": "r",
        "docs": "d",
        "objects": [
            {
                "kind": "extension",
                "id": "roaringbitmap",
                "note": "offered from Aurora PostgreSQL 18",
            }
        ],
    }
    [risk] = relational_gap_risks({"target_engine": "aurora_postgresql", "gaps": [later]}, WAVES)
    assert risk["details"][0].endswith(
        "Found in extension roaringbitmap (offered from Aurora PostgreSQL 18)."
    )
    assert _evidence_md(later) == (
        " Found in: extension `roaringbitmap` (offered from Aurora PostgreSQL 18)"
    )


def test_the_engineering_report_prints_the_extensions_escaped(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    report = json.loads(FIXTURE.read_text())
    report["migration_waves"] = WAVES
    report["facts"] = compute_report_facts(report, relational_gaps=_gaps())
    store.write_json(KEY, report)
    out = {d.name: d for d in dl.render_deliverables(store, JOB, DB, KEY).items}

    md = out["engineering-report"].content.decode("utf-8")
    line = next(ln for ln in md.splitlines() if "Found in extension acme" in ln)
    assert line.endswith(
        r"Found in extension acme\_geo, extension evil\]\(http://x.example\)`\*, "
        r"extension roaringbitmap."
    )
    assert "](http://x.example)" not in md
